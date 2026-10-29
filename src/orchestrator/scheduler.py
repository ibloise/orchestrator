"""Single-host deterministic scheduling through the existing trusted Executor."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import ExecutorConfig, ModelProfile, SchedulerConfig, trusted_directory
from .domain import ValidationError, nonempty
from .execution import ACTIVE, ROLES, ExecutionService
from .providers import FakeAdapter, process_identity
from .security import LIVE_BLOCKER, clean_environment
from .store import Store, new_id, now


def later(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def choose_project(projects: dict[str, int], credits: dict[str, int]) -> str:
    """Smooth weighted round-robin; equal credits break by stable project ID."""
    for project, weight in projects.items():
        credits[project] = credits.get(project, 0) + weight
    chosen = min(projects, key=lambda project: (-credits[project], project))
    credits[chosen] -= sum(projects.values())
    return chosen


class Scheduler:
    def __init__(self, store: Store, executor_config: ExecutorConfig,
                 config: SchedulerConfig | None = None) -> None:
        self.config = config or SchedulerConfig()
        self.config.validate()
        self.executor = ExecutionService(store, executor_config)
        self.store, self.core = store, self.executor.core
        self.owner = new_id('sch')
        self.workers: dict[str, subprocess.Popen] = {}
        self._stop_event: threading.Event | None = None

    @contextmanager
    def ownership(self) -> Iterator[None]:
        trusted_directory(self.store.path.parent)
        path = self.store.path.with_suffix(self.store.path.suffix + '.scheduler.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValidationError('Scheduler lock must be an operator-owned private regular file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValidationError('SCHEDULER_BUSY: another owner holds this database lock') from exc
            identity = process_identity(os.getpid())
            if identity is None:
                raise ValidationError('Cannot prove Scheduler process identity')
            with self.core.transaction() as db:
                previous_owner = db.execute('SELECT * FROM scheduler_state WHERE id=1').fetchone()
                if (previous_owner['owner_id'] and previous_owner['owner_pid'] and previous_owner['owner_identity']
                        and process_identity(previous_owner['owner_pid']) == previous_owner['owner_identity']):
                    raise ValidationError('SCHEDULER_BUSY: durable owner is still alive')
            # Executor reconciliation proves process identity; lease expiry alone never
            # authorizes a second writer. Live independent supervisors retain their slots.
            with self.core.transaction() as db:
                abandoned = db.execute("SELECT e.id FROM executions e JOIN scheduler_dispatches d ON d.execution_id=e.id "
                                       "WHERE e.status='reserved' AND d.worker_pid IS NULL").fetchall()
                for record in abandoned:
                    self.executor.transition(record['id'], 'reserved', 'interrupted', connection=db,
                                             outcome_reason='Unlaunched claim abandoned by previous lock owner; explicit retry required')
                    self.store.emit(db, 'dispatch_claim_reconciled', record['id'], {'reason': 'unlaunched_claim', 'owner_id': self.owner})
            self.executor.reconcile()
            self.observe()
            with self.core.transaction() as db:
                active = db.execute("SELECT count(*) FROM executions WHERE status IN ('created','reserved','running')").fetchone()[0]
                groups = db.execute("SELECT o.project_id,count(*) AS n FROM executions e JOIN tasks t ON t.id=e.task_id "
                                    "JOIN objectives o ON o.id=t.objective_id WHERE e.status IN ('created','reserved','running') GROUP BY o.project_id")
                if active > self.config.global_concurrency or any(row['n'] > self.config.per_project_concurrency for row in groups):
                    raise ValidationError('Cannot reduce capacity below currently active executions')
                previous = db.execute('SELECT * FROM scheduler_state WHERE id=1').fetchone()
                db.execute('UPDATE scheduler_state SET enabled=1,global_limit=?,project_limit=?,owner_id=?,owner_pid=?,owner_identity=?,heartbeat=?,expires_at=? WHERE id=1',
                           (self.config.global_concurrency, self.config.per_project_concurrency, self.owner, os.getpid(),
                            identity, now(), later(self.config.lease_seconds)))
                self.store.emit(db, 'scheduler_owner_acquired', self.owner,
                                {'previous_owner': previous['owner_id'], 'previous_expiry': previous['expires_at'],
                                 'global_limit': self.config.global_concurrency, 'project_limit': self.config.per_project_concurrency})
            try:
                yield
            finally:
                with self.core.transaction() as db:
                    db.execute('UPDATE scheduler_state SET owner_id=NULL,owner_pid=NULL,owner_identity=NULL,expires_at=NULL WHERE id=1 AND owner_id=?', (self.owner,))
                    self.store.emit(db, 'scheduler_owner_released', self.owner)
        finally:
            os.close(fd)

    def heartbeat(self) -> None:
        with self.core.transaction() as db:
            cursor = db.execute('UPDATE scheduler_state SET heartbeat=?,expires_at=?,last_tick=? WHERE id=1 AND owner_id=?',
                                (now(), later(self.config.lease_seconds), now(), self.owner))
            if cursor.rowcount != 1:
                raise ValidationError('Scheduler ownership lost; refusing further dispatch')

    def _candidates(self, db: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
        tasks = db.execute(
            "SELECT t.id,t.created_at,o.project_id,p.priority,p.repository,coalesce((SELECT tr.id FROM task_revisions tr "
            "JOIN plan_tasks pt ON pt.task_revision_id=tr.id JOIN plan_revisions pr ON pr.id=pt.plan_revision_id "
            "JOIN plans pl ON pl.id=pr.plan_id WHERE tr.task_id=t.id AND pl.objective_id=t.objective_id AND pr.status='approved'),"
            "(SELECT id FROM task_revisions WHERE task_id=t.id ORDER BY revision DESC LIMIT 1)) AS revision_id "
            "FROM tasks t JOIN objectives o ON o.id=t.objective_id JOIN projects p ON p.id=o.project_id "
            "WHERE t.status='pending' ORDER BY t.created_at,t.id").fetchall()
        eligible, skipped = [], []
        for task in tasks:
            if task['revision_id'] is None:
                continue
            definition = json.loads(self.core.require(db, 'task_revisions', task['revision_id'])['definition_json'])
            retry = db.execute('SELECT * FROM task_retry_requests WHERE task_id=? AND execution_id IS NULL AND superseded_at IS NULL', (task['id'],)).fetchone()
            model = retry['model_profile'] if retry else definition.get('model_profile', self.config.model_profile)
            role = definition.get('suggested_role', 'implementer')
            result = self.core._eligibility(db, task['revision_id'])
            reasons = [reason['code'] for reason in result['reasons']]
            reasons.extend(self.executor.reservation_blockers(db, task['id'], model, True))
            if db.execute("SELECT 1 FROM executions e LEFT JOIN task_checkpoints c ON c.execution_id=e.id "
                          "WHERE e.task_id=? AND e.status NOT IN ('created','reserved','running') AND c.id IS NULL", (task['id'],)).fetchone():
                reasons.append('UNOBSERVED_EXECUTION')
            configured = self.executor.config.models.get(model)
            if Path(__file__).resolve().is_relative_to(Path(task['repository']).resolve()):
                reasons.append('PROTECTED_INSTALLATION')
            if configured is None:
                reasons.append('MODEL_NOT_CONFIGURED')
            elif configured.provider != 'fake':
                reasons.append('LIVE_SECURITY_GATE')
            if role not in ROLES:
                reasons.append('ROLE_NOT_CONFIGURED')
            if retry and (retry['task_revision_id'] != task['revision_id'] or retry['authorization_id'] != result['authorization_id']):
                reasons.append('RETRY_AUTHORIZATION_CHANGED')
            candidate = {**dict(task), 'task_id': task['id'], 'task_revision_id': task['revision_id'],
                         'plan_revision_id': result['plan_revision_id'], 'authorization_id': result['authorization_id'],
                         'model_profile': model, 'role': role}
            if reasons:
                skipped.append({**candidate, 'reasons': sorted(set(reasons))})
            else:
                eligible.append(candidate)
        return eligible, skipped

    def _persist_skips(self, db: sqlite3.Connection, skipped: list[dict]) -> None:
        previous = {row['task_id']: row['reasons_json'] for row in db.execute('SELECT * FROM scheduler_skips')}
        db.execute('DELETE FROM scheduler_skips')
        for task in skipped:
            encoded = json.dumps(task['reasons'])
            db.execute('INSERT INTO scheduler_skips VALUES (?,?,?,?,?)',
                       (task['task_id'], task['task_revision_id'], task['project_id'], encoded, now()))
            if previous.get(task['task_id']) != encoded:
                self.store.emit(db, 'scheduler_skipped', task['task_id'],
                                {'project_id': task['project_id'], 'task_revision_id': task['task_revision_id'],
                                 'owner_id': self.owner, 'reasons': task['reasons']})

    def _pick(self, db: sqlite3.Connection, eligible: list[dict], persist: bool) -> dict | None:
        projects = {task['project_id']: self.config.weights[0 if task['priority'] <= 33 else 1 if task['priority'] <= 66 else 2]
                    for task in eligible}
        credits = {row['project_id']: row['credit'] for row in db.execute('SELECT * FROM scheduler_projects')}
        if not projects:
            return None
        project = choose_project(projects, credits)
        if persist:
            for identifier, credit in credits.items():
                db.execute('INSERT INTO scheduler_projects(project_id,credit) VALUES (?,?) '
                           'ON CONFLICT(project_id) DO UPDATE SET credit=excluded.credit', (identifier, credit))
        return next(task for task in eligible if task['project_id'] == project)

    def _continuity(self, db: sqlite3.Connection, candidate: dict) -> dict | None:
        checkpoint = db.execute('SELECT * FROM task_checkpoints WHERE task_id=? AND task_revision_id=? ORDER BY version DESC LIMIT 1',
                                (candidate['task_id'], candidate['task_revision_id'])).fetchone()
        if checkpoint is None:
            return None
        metadata = json.loads(checkpoint['metadata_json'])
        exists = db.execute('SELECT 1 FROM workspaces WHERE task_id=?', (candidate['task_id'],)).fetchone()
        workspace = self.executor.workspace_show(candidate['task_id'], connection=db) if exists else None
        decisions = [dict(row) for row in db.execute(
            'SELECT id,kind,question,status FROM decisions WHERE objective_id=(SELECT objective_id FROM tasks WHERE id=?) ORDER BY created_at DESC LIMIT 8',
            (candidate['task_id'],))]
        for decision in decisions:
            decision['question'] = decision['question'][:128]
        return {'unverified': True, 'checkpoint_id': checkpoint['id'], 'checkpoint_version': checkpoint['version'],
                'source_execution_id': checkpoint['execution_id'], 'task_revision_id': candidate['task_revision_id'],
                'current_plan_revision_id': candidate['plan_revision_id'], 'decisions': decisions,
                'previous_summary': metadata['completed'], 'pending': metadata['pending'], 'blockers': metadata['blockers'],
                'next_action': metadata['next_action'], 'workspace_id': workspace['id'] if workspace else None,
                'base_commit': workspace['base_commit'] if workspace else None,
                'observed_head_commit': workspace['git']['head_commit'] if workspace else None}

    def claim_one(self) -> dict | None:
        with self.core.transaction() as db:
            state = db.execute('SELECT * FROM scheduler_state WHERE id=1').fetchone()
            if state['owner_id'] != self.owner or state['expires_at'] <= now():
                raise ValidationError('Scheduler lease is absent/expired')
            eligible, skipped = self._candidates(db)
            self._persist_skips(db, skipped)
            while True:
                candidate = self._pick(db, eligible, False)
                if candidate is None:
                    return None
                try:
                    continuity = self._continuity(db, candidate)
                except (OSError, ValueError):
                    eligible.remove(candidate)
                    skipped.append({**candidate, 'reasons': ['WORKSPACE_UNAVAILABLE']})
                    self._persist_skips(db, skipped)
                    continue
                self._pick(db, eligible, True)
                break
            identifier = self.executor.reserve(candidate['task_id'], candidate['role'], candidate['model_profile'],
                                               'scheduler', connection=db, expected=candidate,
                                               scheduler_owner=self.owner, continuity=continuity)
            token = new_id('clm')
            db.execute('INSERT INTO scheduler_dispatches(execution_id,owner_id,token,claimed_at,expires_at) VALUES (?,?,?,?,?)',
                       (identifier, self.owner, token, now(), later(self.config.claim_seconds)))
            self.store.emit(db, 'scheduler_claimed', identifier,
                            {'project_id': candidate['project_id'], 'task_id': candidate['task_id'],
                             'task_revision_id': candidate['task_revision_id'], 'owner_id': self.owner})
            return {**candidate, 'execution_id': identifier, 'token': token}

    def launch(self, claim: dict, fake_mode: str = 'success') -> None:
        """Launch only a trusted Executor supervisor; it owns the sole agent launcher."""
        identifier = claim['execution_id']
        root = self.executor.config.artifact_root
        logs = root / '.scheduler'
        log_path = logs / f'{identifier}.log'
        config = asdict(self.executor.config)
        for key in ('repository_root', 'workspace_root', 'artifact_root'):
            config[key] = str(config[key])
        process = None
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            trusted_directory(root)
            logs.mkdir(mode=0o700, exist_ok=True)
            trusted_directory(logs)
            with self.core.transaction() as db:
                state = db.execute('SELECT owner_id,expires_at FROM scheduler_state WHERE id=1').fetchone()
                if state['owner_id'] != self.owner or state['expires_at'] <= now():
                    raise ValidationError('Scheduler lease expired before worker launch')
            with log_path.open('xb') as log:
                os.chmod(log_path, 0o600)
                # The worker waits on stdin. Its PID identity commits before release.
                process = subprocess.Popen([sys.executable, '-I', '-m', 'orchestrator.scheduler', '--worker', str(self.store.path), identifier],
                                           stdin=subprocess.PIPE, stdout=log, stderr=log, env=clean_environment(),
                                           start_new_session=True, close_fds=True)
            identity = process_identity(process.pid)
            if identity is None:
                raise ValidationError('Cannot identify trusted Executor worker')
            with self.core.transaction() as db:
                record = self.core.require(db, 'executions', identifier)
                state = db.execute('SELECT owner_id,expires_at FROM scheduler_state WHERE id=1').fetchone()
                if record['status'] != 'reserved' or state['owner_id'] != self.owner or state['expires_at'] <= now():
                    raise ValidationError('Claim no longer reserved')
                db.execute('UPDATE executions SET supervisor_pid=?,supervisor_identity=? WHERE id=?', (process.pid, identity, identifier))
                db.execute('UPDATE scheduler_dispatches SET worker_pid=?,worker_identity=?,worker_log=? WHERE execution_id=?',
                           (process.pid, identity, str(log_path.relative_to(root)), identifier))
                self.store.emit(db, 'scheduler_worker_started', identifier, {'pid': process.pid, 'owner_id': self.owner})
            payload = json.dumps({'token': claim['token'], 'config': config, 'fake_mode': fake_mode}).encode()
            process.stdin.write(payload)
            process.stdin.close()
            self.workers[identifier] = process
        except BaseException:
            if process:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
            self.executor.reconcile()
            record = self.executor.show(identifier)
            if record['status'] in ACTIVE:
                self.executor.transition(identifier, record['status'], 'failed', outcome_reason='Trusted worker launch failed; explicit retry required')
            with self.core.transaction() as db:
                db.execute('UPDATE scheduler_projects SET backoff_until=? WHERE project_id=?',
                           (later(self.config.backoff_seconds), claim['project_id']))
                self.store.emit(db, 'scheduler_dispatch_failed', identifier, {'project_id': claim['project_id'], 'owner_id': self.owner})
            raise

    def _checkpoint_metadata(self, execution: dict) -> dict:
        value = {'unverified': True, 'completed': '', 'pending': ['SPEC-004 verification and operator review'],
                 'blockers': [], 'next_action': 'awaiting_review' if execution['status'] == 'succeeded' else 'requires_manual_retry',
                 'workspace_id': execution['workspace_id'], 'base_commit': execution['base_commit'],
                 'head_commit': execution['head_commit'], 'process_status': execution['status'],
                 'exit_code': execution['exit_code'], 'outcome_reason': execution['outcome_reason'],
                 'artifacts': {}, 'changed_paths': [], 'changed_paths_truncated': False,
                 'clean': None, 'workspace_observation': 'execution_record', 'collection_error': None}
        try:
            result = json.loads(execution['result_json']) if execution['result_json'] else {}
            declaration = result.get('declaration') or {}
            if not isinstance(declaration, dict):
                raise ValidationError('Invalid untrusted agent declaration')
            value['completed'] = str(declaration.get('summary', 'No agent declaration available'))[:512]
            directory = self.executor.config.artifact_root / execution['id']
            refs = json.loads(execution['artifacts_json']) if execution['artifacts_json'] else {}
            for name, relative in refs.items():
                path = self.executor.config.artifact_root / relative
                if path.resolve() != path or not path.is_relative_to(directory) or not path.exists():
                    raise ValidationError(f'Invalid or missing artifact: {name}')
                value['artifacts'][name] = relative
            if execution['request_sha256']:
                request_path = directory / 'request.json'
                if hashlib.sha256(request_path.read_bytes()).hexdigest() != execution['request_sha256']:
                    raise ValidationError('Request checksum mismatch')
            if (directory / 'after.json').is_file():
                after_path = directory / 'after.json'
                if after_path.resolve() != after_path:
                    raise ValidationError('Symlinked workspace evidence refused')
                after = json.loads(after_path.read_text())
                value['changed_paths'] = after['git']['changed_paths'][:64]
                value['changed_paths_truncated'] = len(after['git']['changed_paths']) > 64
                value['head_commit'] = after['git']['head_commit']
                value['clean'] = not bool(after['git']['status_porcelain'])
                value['workspace_observation'] = 'after_artifact'
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            value['collection_error'] = str(exc)[:1024]
            value['next_action'] = 'requires_manual_retry'
        if execution['workspace_id']:
            try:
                workspace = self.executor.workspace_show(execution['task_id'])
                if value['head_commit'] is None:
                    value.update(head_commit=workspace['git']['head_commit'],
                                 clean=not bool(workspace['git']['status_porcelain']),
                                 changed_paths=workspace['git']['changed_paths'][:64],
                                 changed_paths_truncated=len(workspace['git']['changed_paths']) > 64,
                                 workspace_observation='checkpoint_collection')
            except (OSError, ValueError, LookupError) as exc:
                value['collection_error'] = str(exc)[:1024]
                value['next_action'] = 'requires_manual_retry'
        if execution['status'] != 'succeeded' or value['collection_error']:
            value['blockers'] = [value['collection_error'] or execution['outcome_reason'] or execution['status']]
        return value

    def observe(self) -> list[str]:
        """Append checkpoints/holds exactly once, also for pre-Scheduler manual runs."""
        with self.core.transaction() as db:
            records = [dict(row) for row in db.execute(
                "SELECT e.* FROM executions e LEFT JOIN task_checkpoints c ON c.execution_id=e.id "
                "WHERE e.status NOT IN ('created','reserved','running') AND c.id IS NULL ORDER BY e.created_at,e.id")]
        observed = []
        for execution in records:
            metadata = self._checkpoint_metadata(execution)
            with self.core.transaction() as db:
                if db.execute('SELECT 1 FROM task_checkpoints WHERE execution_id=?', (execution['id'],)).fetchone():
                    continue
                version = db.execute('SELECT coalesce(max(version),0)+1 FROM task_checkpoints WHERE task_id=?', (execution['task_id'],)).fetchone()[0]
                identifier = new_id('ckp')
                db.execute('INSERT INTO task_checkpoints VALUES (?,?,?,?,?,?,?)',
                           (identifier, execution['task_id'], execution['task_revision_id'], execution['id'], version, json.dumps(metadata), now()))
                reason = metadata['next_action']
                db.execute('INSERT INTO task_dispatch_holds(id,task_id,task_revision_id,execution_id,reason,detail,created_at) VALUES (?,?,?,?,?,?,?)',
                           (new_id('hld'), execution['task_id'], execution['task_revision_id'], execution['id'], reason,
                            metadata['collection_error'] or execution['outcome_reason'] or execution['status'], now()))
                db.execute('UPDATE scheduler_dispatches SET acknowledged_at=? WHERE execution_id=?', (now(), execution['id']))
                self.store.emit(db, 'checkpoint_created', identifier, {'task_id': execution['task_id'], 'task_revision_id': execution['task_revision_id'],
                                'execution_id': execution['id'], 'version': version, 'unverified': True, 'collection_error': metadata['collection_error']})
                self.store.emit(db, 'dispatch_hold_created', execution['task_id'], {'execution_id': execution['id'], 'reason': reason})
                observed.append(identifier)
        return observed

    def reconcile(self) -> None:
        self.executor.reconcile()
        # Revoke through Executor cancellation. Pauses/decisions do not kill live work.
        for execution in self.executor.list():
            if execution['status'] not in ACTIVE or execution['cancel_requested']:
                continue
            eligibility = self.core.eligibility(execution['task_revision_id'])
            codes = {reason['code'] for reason in eligibility['reasons']}
            if codes & {'REVOKED', 'NO_AUTHORIZATION', 'NO_APPROVAL', 'TASK_NOT_IN_EFFECTIVE_PLAN'}:
                self._request_cancel(execution['id'], 'scheduler-security-revocation')
            elif 'TASK_NOT_PENDING' in codes and self.core.inspect('task', execution['task_id'])['status'] == 'cancelled':
                self._request_cancel(execution['id'], 'scheduler-task-cancellation')
        self.observe()
        for identifier, process in list(self.workers.items()):
            if process.poll() is not None:
                process.wait()
                del self.workers[identifier]

    def _request_cancel(self, identifier: str, actor: str) -> None:
        try:
            self.executor.cancel(identifier, actor)
        except ValidationError:
            current = self.executor.show(identifier)
            if current['status'] in ACTIVE and not current['cancel_requested']:
                raise

    def _cycle(self, fake_mode: str = 'success') -> list[str]:
        self.heartbeat()
        self.reconcile()
        dispatched = []
        for _ in range(self.config.global_concurrency):
            if getattr(self, '_stop_event', None) is not None and self._stop_event.is_set():
                break
            claim = self.claim_one()
            if claim is None:
                break
            try:
                self.launch(claim, fake_mode)
            except (OSError, ValueError):
                self.observe()
                continue
            dispatched.append(claim['execution_id'])
        self.heartbeat()
        return dispatched

    def tick(self, dry_run: bool = False, fake_mode: str = 'success') -> dict:
        if dry_run:
            with self.core.transaction() as db:
                eligible, skipped = self._candidates(db)
                selected = self._pick(db, eligible, False)
                return {'dry_run': True, 'next_candidate': selected, 'eligible': eligible, 'skipped': skipped,
                        'live_enabled': False, 'live_blocker': LIVE_BLOCKER}
        with self.ownership():
            dispatched = self._cycle(fake_mode)
            while any(self.executor.show(identifier)['status'] in ACTIVE for identifier in dispatched):
                self.heartbeat()
                self.reconcile()
                time.sleep(min(self.config.interval_seconds, 0.1))
            self.reconcile()
            executions = [self.executor.show(identifier) for identifier in dispatched]
        return {'dry_run': False, 'dispatched': dispatched, 'executions': executions, 'status': self.status()}

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self._stop_event = stop
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
        try:
            with self.ownership():
                while not stop.is_set():
                    self._cycle()
                    stop.wait(self.config.interval_seconds)
                # Independent trusted supervisors retain ownership of running work.
                self.reconcile()
        finally:
            self._stop_event = None
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def pause(self, project_id: str | None, paused: bool, actor: str) -> None:
        actor = nonempty(actor, 'actor')
        with self.core.transaction() as db:
            if project_id:
                self.core.require(db, 'projects', project_id)
                db.execute('INSERT INTO scheduler_projects(project_id,paused,credit) VALUES (?,?,0) '
                           'ON CONFLICT(project_id) DO UPDATE SET paused=excluded.paused,credit=0', (project_id, int(paused)))
            else:
                db.execute('UPDATE scheduler_state SET paused=? WHERE id=1', (int(paused),))
                if paused:
                    db.execute('UPDATE scheduler_projects SET credit=0')
            self.store.emit(db, 'scheduler_paused' if paused else 'scheduler_resumed', project_id or 'global', {'actor': actor})

    def retry(self, task_id: str, actor: str, reason: str, model_profile: str | None = None) -> str:
        actor, reason = nonempty(actor, 'actor'), nonempty(reason, 'reason')
        self.executor.reconcile()
        self.observe()
        with self.core.transaction() as db:
            task = self.core.require(db, 'tasks', task_id)
            revision = db.execute("SELECT tr.id FROM task_revisions tr JOIN plan_tasks pt ON pt.task_revision_id=tr.id "
                                  "JOIN plan_revisions pr ON pr.id=pt.plan_revision_id WHERE tr.task_id=? AND pr.status='approved'", (task_id,)).fetchone()
            if revision is None:
                raise ValidationError('Retry requires an effective approved task revision')
            eligibility = self.core._eligibility(db, revision['id'])
            if not eligibility['eligible']:
                raise ValidationError('Retry blocked: ' + ', '.join(r['code'] for r in eligibility['reasons']))
            if db.execute("SELECT 1 FROM executions WHERE task_id=? AND status IN ('created','reserved','running')", (task_id,)).fetchone():
                raise ValidationError('ACTIVE_EXECUTION blocks retry')
            holds = db.execute('SELECT id FROM task_dispatch_holds WHERE task_id=? AND released_at IS NULL', (task_id,)).fetchall()
            pending = db.execute('SELECT * FROM task_retry_requests WHERE task_id=? AND execution_id IS NULL AND superseded_at IS NULL', (task_id,)).fetchone()
            stale = pending and (pending['task_revision_id'] != revision['id'] or pending['authorization_id'] != eligibility['authorization_id'])
            if pending and not stale:
                raise ValidationError('A retry request is already pending')
            if not holds and not stale:
                raise ValidationError('No pending inspection/retry hold exists')
            definition = json.loads(self.core.require(db, 'task_revisions', revision['id'])['definition_json'])
            model = model_profile or definition.get('model_profile', self.config.model_profile)
            profile = self.executor.config.models.get(model)
            if profile is None or profile.provider != 'fake':
                raise ValidationError('Retry model unavailable: ' + LIVE_BLOCKER)
            workspace = db.execute('SELECT id FROM workspaces WHERE task_id=?', (task_id,)).fetchone()
            if workspace:
                self.executor.workspace_show(task['id'], connection=db)
            else:
                objective = self.core.require(db, 'objectives', task['objective_id'])
                project = self.core.require(db, 'projects', objective['project_id'])
                self.executor.manager.repository(project['repository'])
            identifier = new_id('rty')
            continuity = self._continuity(db, {'task_id': task_id, 'task_revision_id': revision['id'],
                                               'plan_revision_id': eligibility['plan_revision_id']}) if workspace else None
            db.execute('UPDATE task_dispatch_holds SET released_at=?,released_by=?,release_reason=? WHERE task_id=? AND released_at IS NULL',
                       (now(), actor, reason, task_id))
            if stale:
                db.execute('UPDATE task_retry_requests SET superseded_at=? WHERE id=?', (now(), pending['id']))
                self.store.emit(db, 'scheduler_retry_superseded', task_id, {'request_id': pending['id'], 'actor': actor, 'reason': reason})
            db.execute('INSERT INTO task_retry_requests VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                       (identifier, task_id, revision['id'], eligibility['plan_revision_id'], eligibility['authorization_id'],
                        model, actor, reason, now(), json.dumps(continuity) if continuity else None))
            self.store.emit(db, 'scheduler_retry_authorized', task_id, {'actor': actor, 'reason': reason,
                            'request_id': identifier, 'model_profile': model, 'task_revision_id': revision['id'],
                            'plan_revision_id': eligibility['plan_revision_id'], 'authorization_id': eligibility['authorization_id']})
            return identifier

    def checkpoints(self, task_id: str) -> dict:
        with self.core.transaction() as db:
            self.core.require(db, 'tasks', task_id)
            versions = [dict(row) for row in db.execute('SELECT * FROM task_checkpoints WHERE task_id=? ORDER BY version', (task_id,))]
        for checkpoint in versions:
            checkpoint['metadata'] = json.loads(checkpoint.pop('metadata_json'))
        return {'task_id': task_id, 'latest': versions[-1] if versions else None, 'versions': versions}

    def status(self) -> dict:
        with self.core.transaction() as db:
            state = dict(db.execute('SELECT * FROM scheduler_state WHERE id=1').fetchone())
            active = [dict(row) for row in db.execute("SELECT e.id,e.task_id,e.status,e.supervisor_pid,e.supervisor_identity,o.project_id "
                                                     "FROM executions e JOIN tasks t ON t.id=e.task_id JOIN objectives o ON o.id=t.objective_id "
                                                     "WHERE e.status IN ('created','reserved','running')")]
            holds = [dict(row) for row in db.execute('SELECT * FROM task_dispatch_holds WHERE released_at IS NULL ORDER BY created_at,id')]
            skipped = [dict(row) for row in db.execute('SELECT * FROM scheduler_skips ORDER BY project_id,task_id')]
            projects = [dict(row) for row in db.execute('SELECT * FROM scheduler_projects ORDER BY project_id')]
            claims = [dict(row) for row in db.execute('SELECT execution_id,owner_id,claimed_at,expires_at,worker_pid,worker_identity,worker_log,acknowledged_at FROM scheduler_dispatches ORDER BY claimed_at,execution_id')]
        for item in skipped:
            item['reasons'] = json.loads(item.pop('reasons_json'))
        return {'owner': state, 'slots_used': len(active), 'active_executions': active,
                'project_slots': {project: sum(item['project_id'] == project for item in active) for project in {item['project_id'] for item in active}},
                'holds': holds, 'skipped': skipped, 'projects': projects, 'claims': claims, 'live_enabled': False}


def worker_main(database: str, identifier: str) -> int:
    """Trusted worker protocol; no human CLI simulation and no second agent runner."""
    payload = json.loads(sys.stdin.buffer.read(65536))
    settings = payload['config']
    for key in ('repository_root', 'workspace_root', 'artifact_root'):
        settings[key] = Path(settings[key])
    settings['models'] = {name: ModelProfile(**model) for name, model in settings['models'].items()}
    executor = ExecutionService(Store(Path(database)), ExecutorConfig(**settings))
    with executor.core.transaction() as db:
        claim = db.execute('SELECT * FROM scheduler_dispatches WHERE execution_id=?', (identifier,)).fetchone()
        if claim is None or claim['token'] != payload['token'] or claim['worker_pid'] != os.getpid() or claim['worker_identity'] != process_identity(os.getpid()):
            raise ValidationError('Worker does not own this dispatch claim')
    result = executor.execute_reserved(identifier, FakeAdapter(payload['fake_mode']))
    print(json.dumps({'execution_id': identifier, 'status': result['status']}), flush=True)
    return 0 if result['status'] == 'succeeded' else 1


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--worker':
        raise SystemExit('Use the orchestrator scheduler CLI')
    raise SystemExit(worker_main(sys.argv[2], sys.argv[3]))
