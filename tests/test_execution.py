import hashlib
import json
import signal
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator import migrations
from orchestrator.cli import main
from orchestrator.config import ExecutorConfig, ModelProfile
from orchestrator.domain import ValidationError
from orchestrator.execution import ExecutionService
from orchestrator.providers import CodexCliAdapter, FakeAdapter, process_identity
from orchestrator.security import (
    PROFILE,
    PYTHON,
    SecurityError,
    clean_environment,
    sandbox_command,
)
from orchestrator.services import ControlPlane
from orchestrator.store import Store
from orchestrator.workspace import git


@pytest.fixture
def environment(tmp_path):
    repositories = tmp_path / 'repositories'
    repository = repositories / 'project'
    repositories.mkdir(mode=0o700)
    repository.mkdir(mode=0o700)
    subprocess.run(['/usr/bin/git', 'init', str(repository)], check=True, capture_output=True)
    (repository / '.git').chmod(0o700)
    (repository / 'README.md').write_text('original\n')
    git(repository, 'add', 'README.md')
    git(repository, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Initial fixture')
    store = Store(tmp_path / 'state' / 'control.db')
    store.initialize()
    core = ControlPlane(store)
    project = store.add_project('project', str(repository))
    objective = store.add_objective(project, 'Deliver feature')
    core.activate_objective(objective)
    revision = core.create_task(objective, 'Implement feature',
                                {'description': 'Add one file', 'acceptance_criteria': ['File exists']})
    task_id = core.inspect('task-revision', revision)['task_id']
    plan = core.create_plan_revision(objective, 'Implement the feature')
    core.attach(plan, revision)
    core.propose(plan)
    authorization = core.approve(plan, 'operator', 'Reviewed')
    config = ExecutorConfig(repositories, tmp_path / 'worktrees', tmp_path / 'artifacts',
                            {'sol': ModelProfile('fake', 'deterministic-fake-v1', True)},
                            timeout_seconds=5, cancel_grace_seconds=0.1)
    return {'store': store, 'core': core, 'config': config, 'repository': repository,
            'project': project, 'objective': objective, 'revision': revision, 'task': task_id,
            'plan': plan, 'authorization': authorization, 'root': tmp_path}


def service(env, config=None):
    return ExecutionService(env['store'], config or env['config'])


def test_at02_exact_approved_request_and_artifacts(environment):
    env = environment
    executor = service(env)
    adapter = FakeAdapter()
    original = git(env['repository'], 'rev-parse', 'HEAD')
    result = executor.run(env['task'], actor='human', adapter=adapter)
    assert result['status'] == 'succeeded'
    assert result['exit_code'] == 0
    request = adapter.requests[0]
    assert request.task_revision_id == env['revision']
    assert request.plan_revision_id == env['plan']
    assert request.authorization_id == env['authorization']
    assert request.role_version == 'implementer-v1'
    assert request.provider == 'fake'
    assert request.sandbox_profile == PROFILE
    directory = env['config'].artifact_root / result['id']
    assert result['request_sha256'] == hashlib.sha256((directory / 'request.json').read_bytes()).hexdigest()
    assert json.loads((directory / 'request.json').read_text())['execution_id'] == result['id']
    assert json.loads((directory / 'after.json').read_text())['git']['changed_paths'] == ['fake-change.txt']
    assert executor.logs(result['id'])['stdout.log']
    assert result['provider_version'] == 'fake-v1'
    assert git(env['repository'], 'rev-parse', 'HEAD') == original
    assert git(env['repository'], 'status', '--porcelain') == ''
    assert env['core'].inspect('task', env['task'])['status'] == 'pending'
    assert env['core'].eligibility(env['revision'])['eligible']


@pytest.mark.parametrize('blocker', ['revoked', 'decision', 'paused', 'inactive', 'dependency', 'no_approval', 'out_of_plan', 'cancelled'])
def test_at03_denied_before_subprocess(environment, blocker):
    env = environment
    core = env['core']
    if blocker == 'revoked':
        core.revoke(env['authorization'], 'human', 'Stop')
    elif blocker == 'decision':
        core.create_decision(env['objective'], 'review', 'Wait?', [env['task']])
    elif blocker == 'dependency':
        other = core.create_task(env['objective'], 'Prerequisite', {'description': 'First', 'acceptance_criteria': ['Done']})
        core.add_dependency(env['task'], core.inspect('task-revision', other)['task_id'])
    elif blocker == 'cancelled':
        core.cancel_task(env['task'], 'human', 'Stop')
    elif blocker == 'out_of_plan':
        other = core.create_task(env['objective'], 'Other', {'description': 'Other', 'acceptance_criteria': ['Done']})
        plan = core.create_plan_revision(env['objective'], 'Replacement')
        core.attach(plan, other)
        core.propose(plan)
        core.approve(plan, 'human', 'New scope')
    else:
        with env['store'].transaction() as db:
            if blocker == 'paused':
                db.execute("UPDATE projects SET status='paused' WHERE id=?", (env['project'],))
            elif blocker == 'inactive':
                db.execute("UPDATE objectives SET status='draft' WHERE id=?", (env['objective'],))
            else:
                db.execute("UPDATE plan_revisions SET status='superseded' WHERE id=?", (env['plan'],))
    adapter = FakeAdapter()
    with pytest.raises(ValidationError, match='Execution denied'):
        service(env).run(env['task'], adapter=adapter)
    assert adapter.requests == []
    assert service(env).list() == []
    assert env['store'].event_records()[0]['kind'] == 'execution_denied'


def test_revalidate_before_release_of_untrusted_process(environment):
    env = environment

    class RevokeBeforeStart(FakeAdapter):
        def run(self, *args, **kwargs):
            env['core'].revoke(env['authorization'], 'human', 'Revoked during preparation')
            return super().run(*args, **kwargs)

    with pytest.raises(ValidationError, match='immediately before launch'):
        service(env).run(env['task'], adapter=RevokeBeforeStart())
    record = service(env).list()[0]
    assert record['status'] == 'failed'
    assert not (env['config'].artifact_root / record['id'] / 'stage' / 'fake-change.txt').exists()


def test_at04_reuse_preserves_dirty_work_and_reference(environment):
    env = environment
    executor = service(env)
    first = executor.run(env['task'])
    workspace = executor.workspace_show(env['task'])
    path = Path(workspace['path'])
    (path / 'user-work.txt').write_text('Keep my uncommitted work\n')
    (path / 'README.md').write_text('User edit\n')
    second = executor.run(env['task'])
    assert first['workspace_id'] == second['workspace_id']
    assert first['base_commit'] == second['base_commit']
    assert (path / 'user-work.txt').read_text() == 'Keep my uncommitted work\n'
    assert (path / 'README.md').read_text() == 'User edit\n'
    assert (env['repository'] / 'README.md').read_text() == 'original\n'
    assert not (env['repository'] / 'fake-change.txt').exists()


def test_workspace_collision_and_metadata_tampering_refused(environment):
    env = environment
    path = env['config'].workspace_root / env['task']
    path.parent.mkdir(mode=0o700)
    path.mkdir(mode=0o700)
    (path / 'sentinel').write_text('preserve')
    with pytest.raises(ValidationError, match='collision'):
        service(env).run(env['task'])
    assert (path / 'sentinel').read_text() == 'preserve'


def test_workspace_git_marker_is_protected_from_stage(environment):
    env = environment
    executor = service(env)
    executor.run(env['task'])
    workspace = executor.workspace_show(env['task'])
    marker = Path(workspace['path']) / '.git'
    original = marker.read_text()
    marker.write_text('gitdir: /unmanaged\n')
    with pytest.raises(ValidationError, match='metadata changed'):
        executor.run(env['task'])
    assert marker.read_text() == 'gitdir: /unmanaged\n'
    marker.write_text(original)


@pytest.mark.parametrize('mode,status,declaration', [('success', 'succeeded', 'valid'), ('failure', 'failed', 'valid'),
                                                   ('malformed', 'failed', 'invalid'), ('missing', 'failed', 'unavailable'),
                                                   ('ignore-term', 'timed_out', 'unavailable')])
def test_at06_process_outcomes(environment, mode, status, declaration):
    env = environment
    config = replace(env['config'], timeout_seconds=0.2) if mode == 'ignore-term' else env['config']
    result = service(env, config).run(env['task'], adapter=FakeAdapter(mode))
    assert result['status'] == status
    value = json.loads(result['result_json'])
    assert value['declaration_status'] == declaration
    if mode == 'failure':
        assert value['exit_code'] == 7
    if mode == 'ignore-term':
        assert value['signal'] == signal.SIGKILL
    assert env['core'].inspect('task', env['task'])['status'] == 'pending'


def test_at06_cancellation_via_durable_flag(environment):
    env = environment
    executor = service(env)
    started = threading.Event()

    class WaitingAdapter(FakeAdapter):
        def run(self, request, workspace, artifacts, cancelled, notify, *args):
            def ready(pid, identity):
                notify(pid, identity)
                started.set()
            return super().run(request, workspace, artifacts, cancelled, ready, *args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor.run, env['task'], adapter=WaitingAdapter('sleep'))
        assert started.wait(10), 'Supervisor did not start'
        record = executor.list()[0]
        executor.cancel(record['id'], 'human')
        result = future.result(timeout=10)
    assert result['status'] == 'cancelled'
    assert result['cancel_requested'] == 1
    assert process_identity(result['pid']) is None
    assert env['core'].inspect('task', env['task'])['status'] == 'pending'


def test_at07_durable_reservation_across_connections(environment):
    env = environment
    executor = service(env)
    identifier = executor.reserve(env['task'], 'implementer', 'sol', 'human')
    other = ExecutionService(Store(env['store'].path), env['config'])
    with pytest.raises(ValidationError, match='ACTIVE_EXECUTION'):
        other.reserve(env['task'], 'implementer', 'sol', 'human')
    with pytest.raises(ValidationError, match='ACTIVE_EXECUTION'):
        env['core'].complete_manually(env['revision'], 'human', 'Checked', {'tests': True})
    assert other.show(identifier)['status'] == 'reserved'


def test_at08_restart_reconciliation_never_retries(environment):
    env = environment
    executor = service(env)
    identifier = executor.reserve(env['task'], 'implementer', 'sol', 'human')
    executor._prepare(identifier)
    with env['store'].transaction() as db:
        db.execute('UPDATE executions SET supervisor_identity=? WHERE id=?', ('stale-process-token', identifier))
    directory = env['config'].artifact_root / identifier
    directory.parent.mkdir(mode=0o700)
    directory.mkdir(mode=0o700)
    (directory / 'stdout.log').write_text('partial provider output')
    reopened = ExecutionService(Store(env['store'].path), env['config'])
    assert reopened.reconcile() == [{'id': identifier, 'action': 'interrupted'}]
    assert len(reopened.list()) == 1
    assert reopened.logs(identifier)['stdout.log'] == 'partial provider output'
    assert reopened.workspace_show(env['task'])
    with pytest.raises(ValidationError, match='terminal'):
        reopened.transition(identifier, 'reserved', 'running')
    assert reopened.run(env['task'])['status'] == 'succeeded'
    assert len(reopened.list()) == 2


def test_live_supervisor_is_not_reconciled(environment):
    env = environment
    executor = service(env)
    identifier = executor.reserve(env['task'], 'implementer', 'sol', 'human')
    assert executor.reconcile() == [{'id': identifier, 'action': 'supervisor_alive'}]
    assert executor.show(identifier)['status'] == 'reserved'


def test_at09_real_sandbox_negative_probes(environment):
    env = environment
    root = env['root']
    stage = root / 'probe-stage'
    stage.mkdir()
    secret = root / 'sentinel-secret'
    secret.write_text('not-a-real-secret')
    other_project = root / 'other-project'
    other_project.mkdir()
    (other_project / 'data').write_text('other project')
    (stage / 'escape').symlink_to(secret)
    restricted = root / 'restricted-path-sentinel'
    restricted.write_text('restricted synthetic data')
    docker_sentinel = root / 'forbidden-docker-sentinel'
    docker_sentinel.write_text('synthetic capability sentinel')
    before_db = env['store'].path.read_bytes()
    before_mode = secret.stat().st_mode
    script = '''
import ctypes, errno, json, os, pathlib, socket, sys
protected = [pathlib.Path(value) for value in sys.argv[1:]]
results = {}
for path in protected:
    for operation in ("read", "write", "chmod"):
        try:
            if operation == "read": path.read_bytes()
            elif operation == "write": path.write_text("COMPROMISED")
            else: path.chmod(0o777)
            results[str(path) + ":" + operation] = False
        except OSError as exc:
            results[str(path) + ":" + operation] = exc.errno in (errno.EACCES, errno.EPERM)
try:
    socket.socket()
    results["network"] = False
except OSError as exc:
    results["network"] = exc.errno == errno.EPERM
libc = ctypes.CDLL(None, use_errno=True)
for name, number in (("unshare",272),("ptrace",101),("fork",57),("host_kill",62),("keyctl",250)):
    answer = libc.syscall(number, 0, 0, 0, 0, 0)
    results[name] = answer == -1 and ctypes.get_errno() == errno.EPERM
pathlib.Path("allowed.txt").write_text("allowed workspace edit")
print(json.dumps(results))
'''
    command = [PYTHON, '-I', '-c', script, str(env['store'].path), str(secret),
               str(other_project / 'data'), str(stage / 'escape'), str(docker_sentinel), str(restricted)]
    observed = subprocess.run(sandbox_command(stage, command), input='ORCHESTRATOR_LAUNCH\n',
                              capture_output=True, text=True, timeout=10, env=clean_environment(stage), check=False)
    assert observed.returncode == 0, observed.stderr
    results = json.loads(observed.stdout)
    assert all(results.values()), results
    assert (stage / 'allowed.txt').read_text() == 'allowed workspace edit'
    assert secret.read_text() == 'not-a-real-secret'
    assert secret.stat().st_mode == before_mode
    assert env['store'].path.read_bytes() == before_db
    assert (other_project / 'data').read_text() == 'other project'
    # Store observed denials in the same audit mechanism used for production preflight.
    with env['store'].transaction() as db:
        env['store'].emit(db, 'security_acceptance_probe', 'offline-test', results)
    assert env['store'].event_records()[0]['data'] == results


def test_sandbox_failure_is_closed_and_audited(environment, monkeypatch):
    env = environment
    monkeypatch.setattr('orchestrator.execution.probe', lambda stage: {'available': False, 'diagnostic': 'unavailable'})
    adapter = FakeAdapter()
    with pytest.raises(ValidationError, match='refusing launch'):
        service(env).run(env['task'], adapter=adapter)
    assert adapter.requests == []
    assert service(env).list()[0]['status'] == 'failed'
    assert any(event['kind'] == 'execution_security_probe' for event in env['store'].event_records(100))


def test_codex_model_gate_no_subprocess(environment):
    env = environment
    config = replace(env['config'], models={'sol': ModelProfile('codex', 'operator-supplied-test-id', True)})
    with pytest.raises(ValidationError, match='Live Codex is disabled'):
        service(env, config).run(env['task'])
    assert service(env, config).list()[0]['status'] == 'failed'
    assert not env['config'].workspace_root.exists()


def test_output_limit_and_inspect_role_fail_without_task_closure(environment):
    env = environment
    result = service(env, replace(env['config'], max_log_bytes=1024)).run(env['task'], adapter=FakeAdapter('flood'))
    assert result['status'] == 'failed'
    assert result['outcome_reason'].startswith('output_limit')
    directory = env['config'].artifact_root / result['id']
    assert sum((directory / name).stat().st_size for name in ('stdout.log', 'stderr.log')) <= 1024
    managed = env['config'].workspace_root / env['task'] / 'fake-change.txt'
    managed.write_text('Human work that inspect must not overwrite\n')
    with pytest.raises(ValidationError, match='Inspect role modified'):
        service(env).run(env['task'], role='inspect')
    assert managed.read_text() == 'Human work that inspect must not overwrite\n'
    assert env['core'].inspect('task', env['task'])['status'] == 'pending'


def test_at08_reconcile_actual_orphan_identity_without_killing_other_processes(environment):
    env = environment
    executor = service(env)
    identifier = executor.reserve(env['task'], 'implementer', 'sol', 'human')
    workspace = executor._prepare(identifier)
    stage = env['root'] / 'orphan-stage'
    stage.mkdir(mode=0o700)
    child = subprocess.Popen(sandbox_command(stage, [PYTHON, '-I', '-c', 'import time; time.sleep(60)']),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=clean_environment(stage), start_new_session=True)
    try:
        identity = process_identity(child.pid)
        executor.transition(identifier, 'reserved', 'running', pid=child.pid, pid_identity=identity)
        with env['store'].transaction() as db:
            db.execute('UPDATE executions SET supervisor_identity=? WHERE id=?', ('lost-supervisor', identifier))
        child.stdin.write(b'ORCHESTRATOR_LAUNCH\n')
        child.stdin.close()
        assert executor.reconcile() == [{'id': identifier, 'action': 'interrupted'}]
        assert child.wait(timeout=5) != 0
        assert process_identity(child.pid) is None
        assert Path(workspace['path']).exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


@pytest.mark.parametrize('invalid', ['outside_repository', 'escaping_symlink', 'unmanaged_branch', 'dirty_permissions'])
def test_invalid_repository_and_workspace_boundaries(environment, invalid):
    env = environment
    if invalid == 'outside_repository':
        reference = str(env['root'])
    elif invalid == 'escaping_symlink':
        link = env['config'].repository_root / 'escape'
        link.symlink_to(env['root'])
        reference = str(link)
    elif invalid == 'unmanaged_branch':
        git(env['repository'], 'branch', f'agent/{env["task"]}')
        reference = str(env['repository'])
    else:
        env['repository'].chmod(0o777)
        reference = str(env['repository'])
    with env['store'].transaction() as db:
        db.execute('UPDATE projects SET repository=? WHERE id=?', (reference, env['project']))
    adapter = FakeAdapter()
    with pytest.raises(ValidationError):
        service(env).run(env['task'], adapter=adapter)
    assert not adapter.requests


def test_config_rejects_exposed_database_overlap_and_untrusted_file(environment):
    env = environment
    for config in (replace(env['config'], repository_root=env['root']),
                   replace(env['config'], workspace_root=env['config'].artifact_root),
                   replace(env['config'], timeout_seconds=float('nan'))):
        with pytest.raises(ValidationError):
            service(env, config)
    config_path = write_config(env)
    config_path.chmod(0o666)
    with pytest.raises(ValidationError, match='operator-owned'):
        ExecutorConfig.load(config_path)


def test_import_refuses_concurrent_user_edits_and_symlink_escape(environment):
    env = environment
    executor = service(env)
    executor.run(env['task'])
    workspace = executor.workspace_show(env['task'])
    stage = env['root'] / 'staged-copy'
    before = executor.manager.stage(workspace, stage)
    path = Path(workspace['path'])
    (path / 'README.md').write_text('Concurrent user work\n')
    with pytest.raises(ValidationError, match='changed during execution'):
        executor.manager.import_changes(workspace, stage, before)
    assert (path / 'README.md').read_text() == 'Concurrent user work\n'
    # A new outbound symlink is retained as evidence and is never promoted.
    fresh = env['root'] / 'another-stage'
    before = executor.manager.stage(workspace, fresh)
    (fresh / 'escape').symlink_to(env['store'].path)
    with pytest.raises(ValidationError, match='symlink escapes'):
        executor.manager.import_changes(workspace, fresh, before)
    assert not (path / 'escape').exists()


def test_doctor_negative_probes_and_live_gate(environment):
    env = environment
    config = replace(env['config'], codex_binary='/nonexistent/codex')
    diagnostics = service(env, config).doctor()
    assert diagnostics['sandbox']['available']
    assert diagnostics['sandbox']['sentinel_intact']
    assert all(diagnostics['sandbox']['observations'].values())
    assert diagnostics['live_enabled'] is False
    assert diagnostics['codex']['installed'] is False


def test_at05_codex_command_uses_arguments_and_no_hardcoded_model(environment):
    env = environment
    adapter = FakeAdapter()
    service(env).run(env['task'], adapter=adapter)
    request = replace(adapter.requests[0], model_id='operator-model; $(touch /tmp/never-run)', provider='codex')
    codex = CodexCliAdapter('/operator/codex')
    command = codex.command(request)
    assert command[command.index('--model') + 1] == request.model_id
    assert '--ignore-user-config' in command
    assert 'danger-full-access' not in command
    assert '--dangerously-bypass-approvals-and-sandbox' not in command
    with pytest.raises(SecurityError, match='disabled'):
        codex.run(request, Path('/unused'), Path('/unused'), lambda: False, lambda *_: None, 1, 1, 1024)


def write_config(env):
    path = env['root'] / 'config.toml'
    config = env['config']
    path.write_text(f'''[executor]
repository_root = "{config.repository_root}"
workspace_root = "{config.workspace_root}"
artifact_root = "{config.artifact_root}"
timeout_seconds = 5
cancel_grace_seconds = 0.1
[models.sol]
provider = "fake"
model_id = "deterministic-fake-v1"
verified = true
''')
    path.chmod(0o600)
    return path


def test_at10_cli_end_to_end(environment, capsys):
    env = environment
    config_path = write_config(env)
    prefix = ['--db', str(env['store'].path), '--config', str(config_path)]

    def run(*args):
        assert main(prefix + list(args)) == 0
        return json.loads(capsys.readouterr().out)

    # Use only the CLI for the complete objective/approval/execution lifecycle.
    objective = None
    assert main(prefix + ['objectives', 'add', env['project'], 'CLI objective']) == 0
    objective = capsys.readouterr().out.strip()
    main(prefix + ['objectives', 'activate', objective])
    capsys.readouterr()
    definition = json.dumps({'description': 'CLI edit', 'acceptance_criteria': ['Changed file available']})
    main(prefix + ['tasks', 'create', objective, 'CLI task', '--definition', definition])
    revision = capsys.readouterr().out.strip()
    task_id = run('task-revisions', 'show', revision)['task_id']
    main(prefix + ['plans', 'create', objective, 'CLI plan'])
    plan = capsys.readouterr().out.strip()
    run('plan-revisions', 'attach', plan, revision)
    run('plan-revisions', 'propose', plan)
    main(prefix + ['plan-revisions', 'approve', plan, '--actor', 'operator', '--reason', 'reviewed'])
    capsys.readouterr()
    assert run('agents', 'models')['sol']['provider'] == 'fake'
    result = run('run', task_id, '--role', 'implementer', '--model', 'sol')
    assert result['status'] == 'succeeded'
    assert run('execution', 'show', result['id'])['task_revision_id'] == revision
    assert run('execution', 'logs', result['id'])['stdout.log']
    assert run('executions', '--task', task_id)[0]['id'] == result['id']
    assert run('workspace', 'show', task_id)['git']['changed_paths'] == ['fake-change.txt']
    assert run('tasks', 'show', task_id)['status'] == 'pending'
    assert run('executor', 'reconcile') == []


@pytest.mark.parametrize('transition', ['reserve', 'running', 'succeeded', 'cancel'])
def test_at11_state_events_are_atomic(environment, monkeypatch, transition):
    env = environment
    executor = service(env)
    identifier = None
    if transition != 'reserve':
        identifier = executor.reserve(env['task'], 'implementer', 'sol', 'human')
        if transition == 'succeeded':
            executor.transition(identifier, 'reserved', 'running')
    before = env['store'].event_records(1000)
    emit = env['store'].emit

    def broken(db, kind, subject, data=None):
        emit(db, kind, subject, data)
        raise RuntimeError('forced database event failure')

    monkeypatch.setattr(env['store'], 'emit', broken)
    with pytest.raises(RuntimeError):
        if transition == 'reserve':
            executor.reserve(env['task'], 'implementer', 'sol', 'human')
        elif transition == 'running':
            executor.transition(identifier, 'reserved', 'running')
        elif transition == 'succeeded':
            executor.transition(identifier, 'running', 'succeeded', result_json='{}')
        else:
            executor.cancel(identifier, 'human')
    assert env['store'].event_records(1000) == before
    if transition == 'reserve':
        assert executor.list() == []
    else:
        record = executor.show(identifier)
        assert record['status'] == ('running' if transition == 'succeeded' else 'reserved')
        assert record['cancel_requested'] == 0


def test_at11_artifact_failure_is_not_silent_success(environment, monkeypatch):
    env = environment
    from orchestrator.execution import artifact_json

    def broken(path, value):
        if path.name == 'result.json':
            raise OSError('disk full')
        return artifact_json(path, value)

    monkeypatch.setattr('orchestrator.execution.artifact_json', broken)
    with pytest.raises(ValidationError, match='disk full'):
        service(env).run(env['task'])
    record = service(env).list()[0]
    assert record['status'] == 'failed'
    assert record['exit_code'] == 0
    assert service(env).logs(record['id'])['stdout.log']
    assert env['core'].inspect('task', env['task'])['status'] == 'pending'


def test_migration_from_spec001_retains_history_and_rolls_back(environment, monkeypatch):
    env = environment
    # Create a genuine v1 fixture independent of the active migration version.
    path = env['root'] / 'version1.db'
    from orchestrator.store import SCHEMA
    with sqlite3.connect(path) as db:
        migrations.statements(db, SCHEMA)
        migrations.statements(db, migrations.MIGRATION_1)
        db.execute('PRAGMA user_version=1')
        db.execute("INSERT INTO events(kind,subject_id,data_json,created_at) VALUES ('historical','subject','{}','before')")
    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, 'MIGRATIONS', (original[0], original[1] + 'INVALID SQL;'))
    with pytest.raises(sqlite3.OperationalError):
        Store(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert 'pid' not in {row[1] for row in db.execute('PRAGMA table_info(executions)')}
    monkeypatch.setattr(migrations, 'MIGRATIONS', original)
    Store(path).initialize()
    assert Store(path).events()[0][2] == 'historical'
