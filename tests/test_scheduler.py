import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from orchestrator import migrations
from orchestrator.cli import main
from orchestrator.config import ExecutorConfig, ModelProfile, SchedulerConfig
from orchestrator.domain import ValidationError
from orchestrator.execution import ACTIVE, ExecutionService
from orchestrator.providers import FakeAdapter, process_identity
from orchestrator.scheduler import Scheduler, choose_project
from orchestrator.services import ControlPlane
from orchestrator.store import SCHEMA, Store
from orchestrator.workspace import git


def add_project(env, name, priority=50, count=1, approved=True):
    repository = env['config'].repository_root / name
    repository.mkdir(mode=0o700)
    subprocess.run(['/usr/bin/git', 'init', str(repository)], check=True, capture_output=True)
    (repository / '.git').chmod(0o700)
    (repository / 'README.md').write_text('Fixture\n')
    git(repository, 'add', 'README.md')
    git(repository, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Fixture')
    core, store = env['core'], env['store']
    project = store.add_project(name, str(repository), priority)
    objective = store.add_objective(project, f'Objective {name}')
    core.activate_objective(objective)
    plan = core.create_plan_revision(objective, f'Plan {name}')
    tasks, revisions = [], []
    for index in range(count):
        revision = core.create_task(objective, f'Task {name} {index}',
                                    {'description': 'Make a change', 'acceptance_criteria': ['Inspect the result']})
        tasks.append(core.inspect('task-revision', revision)['task_id'])
        revisions.append(revision)
        core.attach(plan, revision)
    authorization = None
    if approved:
        core.propose(plan)
        authorization = core.approve(plan, 'operator', 'Reviewed')
    return {'project': project, 'objective': objective, 'plan': plan, 'tasks': tasks,
            'revisions': revisions, 'authorization': authorization, 'repository': repository}


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError('Timed out waiting for durable process state')


@pytest.fixture
def env(tmp_path):
    repositories = tmp_path / 'repositories'
    repositories.mkdir(mode=0o700)
    store = Store(tmp_path / 'state' / 'control.db')
    store.initialize()
    config = ExecutorConfig(repositories, tmp_path / 'worktrees', tmp_path / 'artifacts',
                            {'sol': ModelProfile('fake', 'fake-sol', True),
                             'luna': ModelProfile('fake', 'fake-luna', True),
                             'astra': ModelProfile('codex', 'placeholder', False)},
                            timeout_seconds=15, cancel_grace_seconds=0.1)
    result = {'store': store, 'core': ControlPlane(store), 'config': config, 'root': tmp_path,
              'scheduling': SchedulerConfig(interval_seconds=0.05, lease_seconds=5, claim_seconds=10)}
    yield result
    executor = ExecutionService(store, config)
    active = [record for record in executor.list() if record['status'] in ACTIVE]
    for record in active:
        executor.cancel(record['id'], 'test-cleanup')
    for record in active:
        if record['supervisor_pid'] != os.getpid():
            wait_until(lambda record=record: executor.show(record['id'])['status'] not in ACTIVE)


def scheduler(env, config=None):
    return Scheduler(env['store'], env['config'], config or env['scheduling'])


@pytest.mark.sandbox
def test_at02_multiproject_dispatch_and_unapproved_work(env):
    a = add_project(env, 'a')
    b = add_project(env, 'b', priority=90)
    c = add_project(env, 'c', approved=False)
    service = scheduler(env)
    result = service.tick()
    assert len(result['dispatched']) == 2
    assert {row['task_id'] for row in result['executions']} == {a['tasks'][0], b['tasks'][0]}
    assert all(row['status'] == 'succeeded' for row in result['executions'])
    assert service.status()['slots_used'] == 0
    assert len(service.status()['holds']) == 2
    assert env['core'].inspect('task', c['tasks'][0])['status'] == 'pending'
    assert any(item['task_id'] == c['tasks'][0] and 'NO_APPROVAL' in item['reasons'] for item in service.status()['skipped'])
    assert service.tick()['dispatched'] == []


@pytest.mark.sandbox
def test_at03_dependency_waits_for_administrative_completion(env):
    project = add_project(env, 'dependency', count=2)
    first, second = project['tasks']
    env['core'].add_dependency(second, first)
    service = scheduler(env)
    result = service.tick()
    assert len(result['executions']) == 1
    assert result['executions'][0]['task_id'] == first
    assert result['executions'][0]['status'] == 'succeeded'
    assert service.tick()['dispatched'] == []
    assert env['core'].inspect('task', first)['status'] == 'pending'
    env['core'].complete_manually(project['revisions'][0], 'human', 'Inspected results', {'tests': 'passed'})
    result = service.tick()
    assert result['executions'][0]['task_id'] == second


def test_at04_known_weighted_sequence():
    credits = {}
    sequence = [choose_project({'a': 1, 'b': 3}, credits) for _ in range(8)]
    assert sequence == ['b', 'a', 'b', 'b', 'b', 'a', 'b', 'b']
    assert credits == {'a': 0, 'b': 0}


@pytest.mark.sandbox
def test_at04_fairness_survives_restart_and_holds_do_not_replay(env):
    a = add_project(env, 'a', priority=10, count=4)
    b = add_project(env, 'b', priority=90, count=8)
    config = replace(env['scheduling'], global_concurrency=1)
    actual = []
    expected_credits = {}
    expected = [choose_project({a['project']: 1, b['project']: 3}, expected_credits) for _ in range(8)]
    for _ in range(8):
        service = scheduler(env, config)
        result = service.tick()
        assert len(result['executions']) == 1
        task_id = result['executions'][0]['task_id']
        actual.append(a['project'] if task_id in a['tasks'] else b['project'])
    assert actual == expected
    assert set(actual) == {a['project'], b['project']}


def test_at05_capacity_counts_manual_reservations_and_shared_locks(env):
    a = add_project(env, 'a', count=3)
    b = add_project(env, 'b')
    config = replace(env['scheduling'], global_concurrency=2, per_project_concurrency=1)
    service = scheduler(env, config)
    with service.ownership():
        claim = service.claim_one()
        assert claim is not None
        other = scheduler(env, config)
        with pytest.raises(ValidationError, match='SCHEDULER_BUSY'):
            other.tick()
        same_project = a if claim['project_id'] == a['project'] else b
        with pytest.raises(ValidationError, match='ACTIVE_EXECUTION'):
            service.executor.reserve(claim['task_id'], 'implementer', 'sol', 'human')
        if len(same_project['tasks']) > 1:
            other_task = next(task for task in same_project['tasks'] if task != claim['task_id'])
            with pytest.raises(ValidationError, match='PROJECT_CAPACITY'):
                service.executor.reserve(other_task, 'implementer', 'sol', 'human')
        second = service.claim_one()
        assert second['project_id'] != claim['project_id']
        with pytest.raises(ValidationError, match='GLOBAL_CAPACITY'):
            service.executor.reserve(a['tasks'][-1], 'implementer', 'sol', 'human')
        assert service.status()['slots_used'] == 2
        for item in (claim, second):
            service.executor.transition(item['execution_id'], 'reserved', 'interrupted', outcome_reason='Test release')


@pytest.mark.sandbox
def test_at05_simultaneous_ticks_do_not_duplicate_work(env):
    add_project(env, 'a', count=2)
    barrier = threading.Barrier(2)

    def tick():
        barrier.wait()
        try:
            return scheduler(env).tick()
        except ValidationError as exc:
            assert 'SCHEDULER_BUSY' in str(exc)
            return {'busy': True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: tick(), range(2)))
    assert sum(len(result.get('dispatched', [])) for result in results) == 2
    assert len(ExecutionService(env['store'], env['config']).list()) == 2


@pytest.mark.parametrize('mode,hold', [('success', 'awaiting_review'), ('failure', 'requires_manual_retry'), ('malformed', 'requires_manual_retry')])
@pytest.mark.sandbox
def test_at06_terminal_dispositions_require_explicit_retry(env, mode, hold):
    project = add_project(env, 'a')
    service = scheduler(env)
    result = service.tick(fake_mode=mode)
    assert len(result['dispatched']) == 1
    assert service.status()['holds'][0]['reason'] == hold
    assert env['core'].inspect('task', project['tasks'][0])['status'] == 'pending'
    assert service.tick()['dispatched'] == []
    with pytest.raises(ValidationError, match='DISPATCH_HOLD'):
        service.executor.run(project['tasks'][0])
    service.retry(project['tasks'][0], 'human', 'Inspected result and authorized continuation')
    assert len(service.tick()['dispatched']) == 1
    assert len(service.checkpoints(project['tasks'][0])['versions']) == 2


@pytest.mark.parametrize('change', ['revocation', 'supersession', 'decision', 'project_pause', 'scheduler_pause'])
def test_at07_changes_between_claim_and_launch_block_agent(env, change):
    project = add_project(env, 'a')
    service = scheduler(env)
    with service.ownership():
        claim = service.claim_one()
        if change == 'revocation':
            env['core'].revoke(project['authorization'], 'human', 'Stop')
        elif change == 'supersession':
            revision = env['core'].create_plan_revision(project['objective'], 'Replacement')
            env['core'].attach(revision, project['revisions'][0])
            env['core'].propose(revision)
            env['core'].approve(revision, 'human', 'New scope')
        elif change == 'decision':
            env['core'].create_decision(project['objective'], 'review', 'Wait', project['tasks'])
        elif change == 'project_pause':
            with env['store'].transaction() as db:
                db.execute("UPDATE projects SET status='paused' WHERE id=?", (project['project'],))
        else:
            service.pause(None, True, 'human')
        service.launch(claim)
        record = wait_until(lambda: (row if (row := service.executor.show(claim['execution_id']))['status'] not in ACTIVE else None))
        assert record['status'] in ('failed', 'cancelled')
        directory = env['config'].artifact_root / record['id'] / 'stage'
        assert not (directory / 'fake-change.txt').exists()
        service.observe()
    assert len(service.status()['holds']) == 1


def test_at08_abandoned_claim_interrupted_once_not_replayed(env):
    project = add_project(env, 'a')
    original = scheduler(env)
    with original.ownership():
        claim = original.claim_one()
    # A different lock owner can prove no worker was ever released, even if an
    # earlier scheduler object happened to share this test process's PID.
    reopened = scheduler(env)
    result = reopened.tick()
    assert result['dispatched'] == []
    assert reopened.executor.show(claim['execution_id'])['status'] == 'interrupted'
    assert len(reopened.checkpoints(project['tasks'][0])['versions']) == 1
    assert reopened.tick()['dispatched'] == []
    assert len(reopened.checkpoints(project['tasks'][0])['versions']) == 1
    assert reopened.status()['slots_used'] == 0


@pytest.mark.sandbox
def test_at08_running_worker_survives_owner_restart(env):
    project = add_project(env, 'a')
    original = scheduler(env)
    with original.ownership():
        claim = original.claim_one()
        original.launch(claim, 'sleep')
        wait_until(lambda: original.executor.show(claim['execution_id'])['status'] == 'running')
    reopened = scheduler(env)
    assert reopened.tick()['dispatched'] == []
    assert reopened.status()['slots_used'] == 1
    reopened.executor.cancel(claim['execution_id'], 'human')
    wait_until(lambda: reopened.executor.show(claim['execution_id'])['status'] not in ACTIVE)
    assert reopened.tick()['dispatched'] == []
    assert reopened.checkpoints(project['tasks'][0])['latest']['metadata']['next_action'] == 'requires_manual_retry'
    original.workers[claim['execution_id']].wait(timeout=5)


@pytest.mark.sandbox
def test_at08_terminal_before_acknowledgement_is_recovered(env):
    project = add_project(env, 'a')
    original = scheduler(env)
    with original.ownership():
        claim = original.claim_one()
        original.launch(claim)
        wait_until(lambda: original.executor.show(claim['execution_id'])['status'] not in ACTIVE)
    reopened = scheduler(env)
    assert reopened.tick()['dispatched'] == []
    assert reopened.status()['holds'][0]['reason'] == 'awaiting_review'
    assert reopened.status()['claims'][0]['acknowledged_at']
    assert reopened.checkpoints(project['tasks'][0])['latest']['execution_id'] == claim['execution_id']


@pytest.mark.sandbox
def test_at09_cross_model_retry_has_untrusted_checkpoint_context(env):
    project = add_project(env, 'a')
    first = scheduler(env)
    first_result = first.tick()['executions'][0]
    reopened = scheduler(env)
    checkpoint = reopened.checkpoints(project['tasks'][0])['latest']
    assert checkpoint['metadata']['unverified']
    request_id = reopened.retry(project['tasks'][0], 'human', 'Inspected partial work', 'luna')
    assert request_id.startswith('rty-')
    result = reopened.tick()['executions'][0]
    assert result['model_profile'] == 'luna'
    directory = env['config'].artifact_root / result['id']
    request = json.loads((directory / 'request.json').read_text())
    assert request['continuity']['checkpoint_id'] == checkpoint['id']
    assert request['continuity']['source_execution_id'] == first_result['id']
    assert request['continuity']['unverified'] is True
    digest = hashlib.sha256((directory / 'continuity.json').read_bytes()).hexdigest()
    assert request['input_files'] == [['continuity.json', digest]]
    assert len(reopened.checkpoints(project['tasks'][0])['versions']) == 2


@pytest.mark.sandbox
def test_dry_run_is_read_only_even_for_previous_manual_results(env):
    project = add_project(env, 'a')
    executor = ExecutionService(env['store'], env['config'])
    executor.run(project['tasks'][0])
    before = env['store'].path.read_bytes()
    events = env['store'].event_records(1000)
    result = scheduler(env).tick(dry_run=True)
    assert result['next_candidate'] is None
    assert result['skipped'][0]['reasons'] == ['UNOBSERVED_EXECUTION']
    assert env['store'].path.read_bytes() == before
    assert env['store'].event_records(1000) == events
    assert len(executor.list()) == 1


@pytest.mark.sandbox
def test_pause_is_scoped_and_does_not_cancel_running_work(env):
    a = add_project(env, 'a')
    b = add_project(env, 'b')
    service = scheduler(env)
    service.pause(a['project'], True, 'human')
    result = service.tick()
    assert [record['task_id'] for record in result['executions']] == b['tasks']
    service.pause(a['project'], False, 'human')
    with service.ownership():
        claim = service.claim_one()
        service.launch(claim, 'sleep')
        wait_until(lambda: service.executor.show(claim['execution_id'])['status'] == 'running')
        service.pause(None, True, 'human')
        service.reconcile()
        assert service.executor.show(claim['execution_id'])['cancel_requested'] == 0
        service.executor.cancel(claim['execution_id'], 'human')
        wait_until(lambda: service.executor.show(claim['execution_id'])['status'] not in ACTIVE)


@pytest.mark.parametrize('failure', ['claim', pytest.param('checkpoint', marks=pytest.mark.sandbox),
                                     pytest.param('retry', marks=pytest.mark.sandbox)])
def test_at11_database_atomicity(env, monkeypatch, failure):
    project = add_project(env, 'a')
    service = scheduler(env)
    if failure == 'checkpoint':
        service.executor.run(project['tasks'][0])
    elif failure == 'retry':
        service.tick()
    original = env['store'].emit

    def broken(db, kind, subject, data=None):
        original(db, kind, subject, data)
        if kind == {'claim': 'scheduler_claimed', 'checkpoint': 'checkpoint_created', 'retry': 'scheduler_retry_authorized'}[failure]:
            raise RuntimeError('forced transactional failure')

    if failure == 'claim':
        with service.ownership():
            before = env['store'].event_records(1000)
            monkeypatch.setattr(env['store'], 'emit', broken)
            with pytest.raises(RuntimeError):
                service.claim_one()
            assert env['store'].event_records(1000) == before
            assert service.executor.list() == []
    else:
        before = env['store'].event_records(1000)
        monkeypatch.setattr(env['store'], 'emit', broken)
        with pytest.raises(RuntimeError):
            if failure == 'checkpoint':
                service.observe()
            else:
                service.retry(project['tasks'][0], 'human', 'Reviewed')
        assert env['store'].event_records(1000) == before
        if failure == 'checkpoint':
            assert service.checkpoints(project['tasks'][0])['versions'] == []
        else:
            assert len(service.status()['holds']) == 1


def test_at11_worker_launch_failure_releases_slot_and_backs_off(env, monkeypatch):
    add_project(env, 'a')
    service = scheduler(env)
    with service.ownership():
        claim = service.claim_one()
        monkeypatch.setattr('orchestrator.scheduler.subprocess.Popen', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('launch failed')))
        with pytest.raises(OSError, match='launch failed'):
            service.launch(claim)
        service.observe()
        assert service.status()['slots_used'] == 0
        assert service.status()['holds'][0]['reason'] == 'requires_manual_retry'
        assert service.status()['projects'][0]['backoff_until']
        assert service.executor.show(claim['execution_id'])['status'] == 'failed'


@pytest.mark.sandbox
def test_at11_artifact_collection_failure_creates_inspection_hold(env):
    project = add_project(env, 'a')
    executor = ExecutionService(env['store'], env['config'])
    execution = executor.run(project['tasks'][0])
    (env['config'].artifact_root / execution['id'] / 'after.json').unlink()
    service = scheduler(env)
    assert service.tick()['dispatched'] == []
    checkpoint = service.checkpoints(project['tasks'][0])['latest']
    assert checkpoint['metadata']['collection_error']
    assert checkpoint['metadata']['next_action'] == 'requires_manual_retry'
    assert service.status()['slots_used'] == 0


@pytest.mark.sandbox
def test_at12_live_gate_is_inherited_and_fake_work_continues(env):
    project = add_project(env, 'a')
    blocked = add_project(env, 'b')
    # A new immutable approved definition requests a configured but gated provider.
    revision = env['core'].create_task_revision(blocked['tasks'][0],
                                               {'description': 'Blocked live task', 'acceptance_criteria': ['Inspect'], 'model_profile': 'astra'})
    plan = env['core'].create_plan_revision(blocked['objective'], 'Live plan')
    env['core'].attach(plan, revision)
    env['core'].propose(plan)
    env['core'].approve(plan, 'human', 'Reviewed scope, not live security permission')
    service = scheduler(env)
    result = service.tick()
    assert [row['task_id'] for row in result['executions']] == project['tasks']
    skipped = {row['task_id']: row['reasons'] for row in service.status()['skipped']}
    assert 'LIVE_SECURITY_GATE' in skipped[blocked['tasks'][0]]


def test_scheduler_migration_is_nondestructive_and_transactional(env, monkeypatch):
    path = env['root'] / 'version2.db'
    with sqlite3.connect(path) as db:
        migrations.statements(db, SCHEMA)
        migrations.statements(db, migrations.MIGRATION_1)
        migrations.statements(db, migrations.MIGRATION_2)
        db.execute('PRAGMA user_version=2')
        db.execute("INSERT INTO events(kind,subject_id,data_json,created_at) VALUES ('history','subject','{}','before')")
    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, 'MIGRATIONS', (*original[:2], original[2] + 'INVALID SQL;'))
    with pytest.raises(sqlite3.OperationalError):
        Store(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_state'").fetchone() is None
    monkeypatch.setattr(migrations, 'MIGRATIONS', original)
    Store(path).initialize()
    Store(path).initialize()
    assert Store(path).events()[0][2] == 'history'


def write_config(env):
    path = env['root'] / 'scheduler.toml'
    config = env['config']
    path.write_text(f'''[executor]
repository_root = {json.dumps(str(config.repository_root))}
workspace_root = {json.dumps(str(config.workspace_root))}
artifact_root = {json.dumps(str(config.artifact_root))}
timeout_seconds = 15
cancel_grace_seconds = 0.1
[models.sol]
provider = "fake"
model_id = "fake-sol"
verified = true
[models.luna]
provider = "fake"
model_id = "fake-luna"
verified = true
[scheduler]
global_concurrency = 2
per_project_concurrency = 2
interval_seconds = 0.05
lease_seconds = 5
claim_seconds = 10
backoff_seconds = 0.1
''')
    path.chmod(0o600)
    return path


@pytest.mark.sandbox
def test_at10_cli_end_to_end(env, capsys):
    reference = add_project(env, 'reference', approved=False)
    config_path = write_config(env)
    prefix = ['--db', str(env['store'].path), '--config', str(config_path)]

    def run(*args, structured=True):
        assert main([*prefix, *args]) == 0
        output = capsys.readouterr().out.strip()
        return json.loads(output) if structured else output

    project = run('projects', 'add', 'cli', str(reference['repository']), structured=False)
    objective = run('objectives', 'add', project, 'CLI objective', structured=False)
    run('objectives', 'activate', objective, structured=False)
    revision = run('tasks', 'create', objective, 'CLI task', '--definition',
                   '{"description":"CLI work","acceptance_criteria":["Inspect artifact"]}', structured=False)
    task_id = run('task-revisions', 'show', revision)['task_id']
    plan = run('plans', 'create', objective, 'CLI plan', structured=False)
    run('plan-revisions', 'attach', plan, revision)
    run('plan-revisions', 'propose', plan)
    run('plan-revisions', 'approve', plan, '--actor', 'operator', '--reason', 'Reviewed', structured=False)
    assert run('scheduler', 'tick', '--dry-run')['next_candidate']['task_id'] == task_id
    run('scheduler', 'pause', '--project', project)
    assert run('scheduler', 'tick')['dispatched'] == []
    run('scheduler', 'resume', '--project', project)
    result = run('scheduler', 'tick')
    assert result['executions'][0]['status'] == 'succeeded'
    assert run('tasks', 'show', task_id)['status'] == 'pending'
    assert run('scheduler', 'status')['slots_used'] == 0
    assert run('checkpoints', 'show', task_id)['latest']['metadata']['unverified']
    run('scheduler', 'retry', task_id, '--actor', 'human', '--reason', 'Inspected and authorized continuation', '--model', 'luna', structured=False)
    assert run('scheduler', 'tick')['executions'][0]['model_profile'] == 'luna'
    assert len(run('checkpoints', 'show', task_id)['versions']) == 2
    assert any(event['kind'] == 'scheduler_claimed' for event in run('events', '--json', '--limit', '100'))
    run('scheduler', 'pause')
    assert run('scheduler', 'status')['owner']['paused'] == 1
    run('scheduler', 'resume')


@pytest.mark.sandbox
def test_foreground_service_shutdown_and_restart_without_replay(env):
    add_project(env, 'a')
    add_project(env, 'b')
    config = write_config(env)
    command = [sys.executable, '-I', '-m', 'orchestrator.cli', '--db', str(env['store'].path),
               '--config', str(config), 'scheduler', 'run']
    for attempt in range(2):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_until(lambda process=process: scheduler(env).status()['owner']['owner_pid'] == process.pid)
            if attempt == 0:
                wait_until(lambda: len(scheduler(env).status()['holds']) == 2)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=10)
            assert process.returncode == 0, errors
            assert json.loads(output)['stopped']
            assert scheduler(env).status()['owner']['owner_id'] is None
            assert len(scheduler(env).executor.list()) == 2
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


@pytest.mark.sandbox
def test_stop_leaves_independent_executor_owned_work_for_reconciliation(env, monkeypatch):
    project = add_project(env, 'a')
    service = scheduler(env)
    cycle = service._cycle
    monkeypatch.setattr(service, '_cycle', lambda: cycle('sleep'))
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.run, stop)
        execution = wait_until(lambda: next((row for row in service.executor.list() if row['status'] == 'running'), None))
        stop.set()
        future.result(timeout=5)
    assert service.executor.show(execution['id'])['status'] == 'running'
    restarted = scheduler(env)
    assert restarted.tick()['dispatched'] == []
    assert restarted.status()['slots_used'] == 1
    restarted.executor.cancel(execution['id'], 'human')
    wait_until(lambda: restarted.executor.show(execution['id'])['status'] not in ACTIVE)
    restarted.tick()
    assert restarted.checkpoints(project['tasks'][0])['latest']
    service.workers[execution['id']].wait(timeout=5)


@pytest.mark.sandbox
def test_revocation_stops_running_execution_through_executor_policy(env):
    project = add_project(env, 'a')
    service = scheduler(env)
    with service.ownership():
        claim = service.claim_one()
        service.launch(claim, 'sleep')
        wait_until(lambda: service.executor.show(claim['execution_id'])['status'] == 'running')
        env['core'].revoke(project['authorization'], 'human', 'Security revocation')
        service.reconcile()
        wait_until(lambda: service.executor.show(claim['execution_id'])['status'] == 'cancelled')
        service.reconcile()
    assert service.status()['slots_used'] == 0
    assert service.status()['holds'][0]['reason'] == 'requires_manual_retry'


def test_expired_lease_cannot_bypass_live_owner_and_claim_expiry_is_closed(env):
    add_project(env, 'a')
    service = scheduler(env)
    with service.ownership():
        claim = service.claim_one()
        with env['store'].transaction() as db:
            db.execute("UPDATE scheduler_state SET expires_at='2000-01-01' WHERE id=1")
        with pytest.raises(ValidationError, match='SCHEDULER_BUSY'):
            scheduler(env).tick()
        with pytest.raises(ValidationError, match='expired'):
            service.claim_one()
        service.heartbeat()
        with env['store'].transaction() as db:
            db.execute("UPDATE scheduler_dispatches SET expires_at='2000-01-01' WHERE execution_id=?", (claim['execution_id'],))
        service.launch(claim)
        record = wait_until(lambda: (row if (row := service.executor.show(claim['execution_id']))['status'] not in ACTIVE else None))
        assert record['status'] == 'failed'
        assert 'EXPIRED' in record['outcome_reason']


@pytest.mark.sandbox
def test_retry_authorization_drift_requires_new_explicit_permission(env):
    project = add_project(env, 'a')
    service = scheduler(env)
    service.tick()
    first_retry = service.retry(project['tasks'][0], 'human', 'Inspected')
    plan = env['core'].create_plan_revision(project['objective'], 'New scope')
    env['core'].attach(plan, project['revisions'][0])
    env['core'].propose(plan)
    env['core'].approve(plan, 'human', 'Reviewed new approval')
    assert service.tick()['dispatched'] == []
    assert 'RETRY_AUTHORIZATION_CHANGED' in service.status()['skipped'][0]['reasons']
    second_retry = service.retry(project['tasks'][0], 'human', 'Reviewed the new authorization')
    assert second_retry != first_retry
    assert len(service.tick()['dispatched']) == 1


@pytest.mark.sandbox
def test_explicit_manual_retry_uses_same_continuity_contract(env):
    project = add_project(env, 'a')
    service = scheduler(env)
    service.tick()
    service.retry(project['tasks'][0], 'human', 'Reviewed continuation', 'luna')
    result = service.executor.run(project['tasks'][0], model_profile='luna', actor='human')
    request = json.loads((env['config'].artifact_root / result['id'] / 'request.json').read_text())
    assert request['continuity']['unverified'] is True
    assert request['model_profile'] == 'luna'
    assert service.tick()['dispatched'] == []


def test_running_installation_is_not_a_scheduling_target(env, monkeypatch):
    project = add_project(env, 'self')
    monkeypatch.setattr('orchestrator.scheduler.__file__', str(project['repository'] / 'src' / 'orchestrator' / 'scheduler.py'))
    result = scheduler(env).tick()
    assert result['dispatched'] == []
    assert result['status']['skipped'][0]['reasons'] == ['PROTECTED_INSTALLATION']


def test_concurrent_manual_and_scheduled_reservation_share_global_capacity(env):
    project = add_project(env, 'a', count=2)
    service = scheduler(env, replace(env['scheduling'], global_concurrency=1))
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return service.claim_one()

    def manual():
        barrier.wait()
        try:
            return service.executor.reserve(project['tasks'][1], 'implementer', 'sol', 'human')
        except ValidationError as exc:
            assert 'GLOBAL_CAPACITY' in str(exc)
            return None

    with service.ownership(), ThreadPoolExecutor(max_workers=2) as pool:
        selected = pool.submit(claim)
        explicit = pool.submit(manual)
        assert sum(value is not None for value in (selected.result(), explicit.result())) == 1
        assert service.status()['slots_used'] == 1
        record = service.executor.list()[0]
        service.executor.transition(record['id'], 'reserved', 'interrupted', outcome_reason='Test cleanup')


def test_dead_expired_owner_is_recovered_without_replaying_its_claim(env):
    project = add_project(env, 'a')
    original = scheduler(env)
    with original.ownership():
        claim = original.claim_one()
    with env['store'].transaction() as db:
        db.execute("UPDATE scheduler_state SET owner_id=?,owner_pid=?,owner_identity=?,expires_at='2000-01-01' WHERE id=1",
                   (original.owner, os.getpid(), 'obsolete-boot-and-start-time'))
    replacement = scheduler(env)
    result = replacement.tick()
    assert result['dispatched'] == []
    assert replacement.executor.show(claim['execution_id'])['status'] == 'interrupted'
    assert replacement.checkpoints(project['tasks'][0])['latest']['metadata']['next_action'] == 'requires_manual_retry'
    assert replacement.status()['owner']['owner_id'] is None
    assert replacement.status()['slots_used'] == 0


@pytest.mark.sandbox
def test_scheduler_worker_crash_retains_uncertain_checkpoint_and_stops_orphan(env):
    project = add_project(env, 'a')
    service = scheduler(env)
    with service.ownership():
        claim = service.claim_one()
        service.launch(claim, 'sleep')
        running = wait_until(lambda: (row if (row := service.executor.show(claim['execution_id']))['status'] == 'running' else None))
        child = running['pid']
        worker = service.workers[claim['execution_id']]
        worker.kill()
        worker.wait(timeout=5)
        service.reconcile()
        wait_until(lambda: process_identity(child) is None)
    assert service.executor.show(claim['execution_id'])['status'] == 'interrupted'
    checkpoint = service.checkpoints(project['tasks'][0])['latest']['metadata']
    assert checkpoint['unverified']
    assert checkpoint['head_commit']
    assert checkpoint['next_action'] == 'requires_manual_retry'
    assert checkpoint['workspace_observation'] == 'checkpoint_collection'
    assert (env['config'].artifact_root / claim['execution_id'] / 'stage').exists()
    assert scheduler(env).tick()['dispatched'] == []


def test_scheduler_inherits_failed_isolation_probe_without_a_fallback(env, monkeypatch):
    project = add_project(env, 'a')
    service = scheduler(env)
    adapter = FakeAdapter()
    monkeypatch.setattr('orchestrator.execution.probe', lambda _: {'available': False, 'diagnostic': 'isolation unavailable'})
    # Exercise the exact same Executor reservation/launch API in-process so this
    # injected preflight failure is visible; no alternate agent launcher is used.
    monkeypatch.setattr(service, 'launch', lambda claim, mode: service.executor.execute_reserved(claim['execution_id'], adapter))
    assert service.tick()['dispatched'] == []
    assert adapter.requests == []
    assert service.executor.list()[0]['status'] == 'failed'
    assert service.status()['slots_used'] == 0
    assert service.checkpoints(project['tasks'][0])['latest']['metadata']['next_action'] == 'requires_manual_retry'


@pytest.mark.sandbox
def test_scheduled_fake_execution_records_real_negative_security_probes(env):
    project = add_project(env, 'a')
    result = scheduler(env).tick()['executions'][0]
    receipt = json.loads((env['config'].artifact_root / result['id'] / 'security.json').read_text())
    assert receipt['available'] and receipt['sentinel_intact']
    assert all(receipt['observations'].values())
    assert git(project['repository'], 'status', '--porcelain') == ''
    assert env['core'].inspect('task', project['tasks'][0])['status'] == 'pending'


def test_launch_artifact_setup_failure_releases_reserved_capacity(env):
    add_project(env, 'a')
    service = scheduler(env)
    with service.ownership():
        claim = service.claim_one()
        # An operator-side artifact collision appears after configuration validation.
        env['config'].artifact_root.write_text('unexpected file, never overwrite')
        with pytest.raises(OSError):
            service.launch(claim)
        service.observe()
        assert service.status()['slots_used'] == 0
        assert service.executor.show(claim['execution_id'])['status'] == 'failed'
        assert env['config'].artifact_root.read_text() == 'unexpected file, never overwrite'
        env['config'].artifact_root.unlink()  # Remove only this test-created collision.
