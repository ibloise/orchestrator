import json
import sqlite3

import pytest

from orchestrator import migrations
from orchestrator.cli import main
from orchestrator.domain import NotFoundError, ValidationError
from orchestrator.services import ControlPlane
from orchestrator.store import SCHEMA, Store

DEFINITION = {'description': 'Implement change', 'acceptance_criteria': ['Tests pass']}


@pytest.fixture
def core(tmp_path):
    store = Store(tmp_path / 'state.db')
    store.initialize()
    project = store.add_project('example', '/repos/example')
    objective = store.add_objective(project, 'Deliver feature')
    service = ControlPlane(store)
    return service, store, project, objective


def task(service, objective, title='Work'):
    revision = service.create_task(objective, title, DEFINITION)
    return service.inspect('task-revision', revision)['task_id'], revision


def approve(service, objective, revisions):
    plan = service.create_plan_revision(objective, 'Deliver tested feature')
    for revision in revisions:
        service.attach(plan, revision)
    service.propose(plan)
    authorization = service.approve(plan, 'operator', 'Reviewed scope')
    return plan, authorization


def codes(result):
    return {reason['code'] for reason in result['reasons']}


def test_at01_bootstrap_migration_preserves_data(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO projects VALUES ('p','legacy','/repo',50,'active','today')")
        db.execute("INSERT INTO events(kind,subject_id,data_json,created_at) VALUES ('legacy','p','{}','today')")
    store = Store(path)
    store.initialize()
    store.initialize()
    assert store.list_projects()[0][0] == 'p'
    assert store.events()[0][2] == 'legacy'
    with store.transaction() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == len(migrations.MIGRATIONS)
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_migration_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO projects VALUES ('p','legacy','/repo',50,'active','today')")
    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, 'MIGRATIONS', (original[0] + 'INVALID SQL;',))
    with pytest.raises(sqlite3.OperationalError):
        Store(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert 'actor' not in {row[1] for row in db.execute('PRAGMA table_info(authorizations)')}
        assert db.execute("SELECT name FROM sqlite_master WHERE name='manual_completions'").fetchone() is None
        assert db.execute('SELECT name FROM projects').fetchone()[0] == 'legacy'
    monkeypatch.setattr(migrations, 'MIGRATIONS', original)
    Store(path).initialize()


def test_newer_database_is_rejected(core):
    _, store, _, _ = core
    with store.transaction() as db:
        db.execute('PRAGMA user_version=999')
    with pytest.raises(ValueError, match='newer'):
        store.initialize()


def test_at02_lifecycle_and_immutable_membership(core):
    service, _, _, objective = core
    _, revision = task(service, objective)
    plan = service.create_plan_revision(objective, 'Plan')
    with pytest.raises(ValidationError, match='at least one'):
        service.propose(plan)
    with pytest.raises(ValidationError, match='proposed'):
        service.approve(plan, 'operator', 'reviewed')
    service.attach(plan, revision)
    service.attach(plan, revision, remove=True)
    service.attach(plan, revision)
    service.propose(plan)
    with pytest.raises(ValidationError, match='draft'):
        service.attach(plan, revision, remove=True)
    service.approve(plan, 'operator', 'reviewed')
    assert 'OBJECTIVE_INACTIVE' in codes(service.eligibility(revision))
    service.activate_objective(objective)
    assert service.eligibility(revision)['eligible']
    with pytest.raises(ValidationError):
        service.approve(plan, 'operator', 'twice')
    with pytest.raises(ValidationError):
        service.attach(plan, revision, remove=True)


def test_at03_revision_continuity_and_restart(core):
    service, store, _, objective = core
    service.activate_objective(objective)
    _, first = task(service, objective, 'First')
    second_id, second = task(service, objective, 'Second')
    plan1, auth1 = approve(service, objective, [first, second])
    replacement = service.create_task_revision(second_id, {**DEFINITION, 'description': 'New definition'})
    plan2, auth2 = approve(service, objective, [first, replacement])
    reopened = ControlPlane(Store(store.path))
    assert reopened.inspect('plan-revision', plan1)['status'] == 'superseded'
    assert reopened.inspect('authorization', auth1)['status'] == 'active'
    assert reopened.eligibility(first)['authorization_id'] == auth2
    assert reopened.eligibility(replacement)['eligible']
    assert 'TASK_NOT_IN_EFFECTIVE_PLAN' in codes(reopened.eligibility(second))
    assert reopened.inspect('task-revision', second)['definition_json'] == json.dumps(DEFINITION)
    assert reopened.inspect('plan-revision', plan2)['status'] == 'approved'
    assert any(event[2] == 'plan_superseded' for event in store.events(100))
    assert len(reopened.list_for_objective('plans', objective)) == 1


def test_cannot_approve_older_proposal(core):
    service, _, _, objective = core
    _, revision = task(service, objective)
    older = service.create_plan_revision(objective, 'Earlier proposal')
    service.attach(older, revision)
    service.propose(older)
    approve(service, objective, [revision])
    with pytest.raises(ValidationError, match='advance'):
        service.approve(older, 'operator', 'reviewed')


def test_at04_dependency_graph_and_manual_unlocks(core):
    service, _, _, objective = core
    service.activate_objective(objective)
    a, ra = task(service, objective, 'A')
    b, rb = task(service, objective, 'B')
    c, rc = task(service, objective, 'C')
    d, rd = task(service, objective, 'D')
    service.add_dependency(b, a)
    service.add_dependency(c, a)
    service.add_dependency(d, b)
    service.add_dependency(d, c)
    approve(service, objective, [ra, rb, rc, rd])
    with pytest.raises(ValidationError, match='cycle'):
        service.add_dependency(a, d)
    with pytest.raises(ValidationError, match='Self'):
        service.add_dependency(a, a)
    other = service.store.add_objective(service.store.list_projects()[0][0], 'Other')
    x, _ = task(service, other)
    with pytest.raises(ValidationError, match='same objective'):
        service.add_dependency(a, x)
    with pytest.raises(NotFoundError):
        service.add_dependency(a, 'missing')
    assert service.eligibility(ra)['eligible']
    assert 'DEPENDENCY_PENDING' in codes(service.eligibility(rd))
    for revision in (ra, rb, rc):
        service.complete_manually(revision, 'operator', 'Validated manually', {'test': 'passed'})
    assert service.eligibility(rd)['eligible']
    assert service.inspect('task', d)['dependencies'] == sorted([b, c])


def test_out_of_plan_and_cancelled_dependencies_block(core):
    service, _, _, objective = core
    service.activate_objective(objective)
    a, _ = task(service, objective)
    b, rb = task(service, objective)
    service.add_dependency(b, a)
    service.cancel_task(a, 'operator', 'No longer needed')
    approve(service, objective, [rb])
    assert codes(service.eligibility(rb)) == {'DEPENDENCY_PENDING', 'DEPENDENCY_NOT_IN_EFFECTIVE_PLAN'}
    with pytest.raises(ValidationError, match='blocked'):
        service.complete_manually(rb, 'operator', 'Skip dependency', {'claim': 'done'})


def test_at05_revocation_and_active_states(core):
    service, store, project, objective = core
    _, revision = task(service, objective)
    assert 'NO_APPROVAL' in codes(service.eligibility(revision))
    service.activate_objective(objective)
    _, authorization = approve(service, objective, [revision])
    with store.transaction() as db:
        db.execute("UPDATE projects SET status='paused' WHERE id=?", (project,))
    assert 'PROJECT_PAUSED' in codes(service.eligibility(revision))
    with store.transaction() as db:
        db.execute("UPDATE projects SET status='active' WHERE id=?", (project,))
    service.revoke(authorization, 'operator', 'Stop work')
    assert 'REVOKED' in codes(service.eligibility(revision))
    record = service.inspect('authorization', authorization)
    assert record['revoked_by'] == 'operator'
    assert record['revocation_reason'] == 'Stop work'
    with pytest.raises(ValidationError):
        service.revoke(authorization, 'operator', 'Again')


def test_at06_targeted_decisions_and_restart(core):
    service, store, _, objective = core
    service.activate_objective(objective)
    a, ra = task(service, objective)
    _, rb = task(service, objective)
    plan, _ = approve(service, objective, [ra, rb])
    decision = service.create_decision(objective, 'clarification', 'Choose behavior?', [a])
    assert 'OPEN_DECISION' in codes(service.eligibility(ra))
    assert service.eligibility(rb)['eligible']
    reopened = ControlPlane(Store(store.path))
    assert reopened.inspect('decision', decision)['task_ids'] == [a]
    reopened.close_decision(decision, 'human', {'choice': 'A'})
    assert reopened.eligibility(ra)['eligible']
    assert reopened.inspect('decision', decision)['resolved_by'] == 'human'
    with pytest.raises(ValidationError, match='closed'):
        reopened.close_decision(decision, 'human', {'choice': 'B'})
    global_blocker = service.create_decision(objective, 'review', 'Review?', plan_revision_id=plan)
    assert 'OPEN_DECISION' in codes(service.eligibility(rb))
    service.close_decision(global_blocker, 'human', {'reason': 'obsolete'}, 'superseded')
    assert service.eligibility(rb)['eligible']


def test_plan_decision_blocks_operations_but_resolution_does_not_approve(core):
    service, _, _, objective = core
    _, revision = task(service, objective)
    plan = service.create_plan_revision(objective, 'Plan')
    service.attach(plan, revision)
    decision = service.create_decision(objective, 'review', 'Approve?', plan_revision_id=plan)
    with pytest.raises(ValidationError, match='OPEN_DECISION'):
        service.propose(plan)
    service.close_decision(decision, 'human', {'approved': True})
    assert service.inspect('plan-revision', plan)['status'] == 'draft'
    service.propose(plan)
    decision = service.create_decision(objective, 'review', 'More review?', plan_revision_id=plan)
    with pytest.raises(ValidationError, match='OPEN_DECISION'):
        service.approve(plan, 'human', 'reviewed')
    service.close_decision(decision, 'human', {'reason': 'withdrawn'}, 'cancelled')
    service.reject(plan, 'human', 'Not ready')
    assert service.inspect('plan-revision', plan)['status'] == 'rejected'
    with pytest.raises(ValidationError):
        service.attach(plan, revision, remove=True)


def test_at07_manual_completion_requires_authorization_actor_reason_evidence(core):
    service, store, _, objective = core
    a, ra = task(service, objective)
    with pytest.raises(ValidationError, match='blocked'):
        service.complete_manually(ra, 'human', 'Checked', {'tests': True})
    service.activate_objective(objective)
    plan, authorization = approve(service, objective, [ra])
    for actor, reason, evidence in [('', 'Checked', {'x': 1}), ('human', '', {'x': 1}), ('human', 'Checked', {})]:
        with pytest.raises(ValidationError):
            service.complete_manually(ra, actor, reason, evidence)
    decision = service.create_decision(objective, 'review', 'Wait', [a])
    with pytest.raises(ValidationError, match='OPEN_DECISION'):
        service.complete_manually(ra, 'human', 'Checked', {'tests': True})
    service.close_decision(decision, 'human', {'answer': 'go'})
    service.complete_manually(ra, 'human', 'Checked', {'tests': True})
    assert service.inspect('task', a)['status'] == 'done'
    with store.transaction() as db:
        record = db.execute('SELECT actor,reason,authorization_id FROM manual_completions').fetchone()
        assert record == ('human', 'Checked', authorization)
    with pytest.raises(ValidationError):
        service.complete_manually(ra, 'human', 'Again', {'tests': True})
    with pytest.raises(ValidationError):
        service.create_task_revision(a, DEFINITION)
    next_plan = service.create_plan_revision(objective, 'New plan')
    with pytest.raises(ValidationError):
        service.propose(next_plan)
    _, different = task(service, objective)
    service.attach(next_plan, different)
    with pytest.raises(ValidationError, match='completed'):
        service.propose(next_plan)
    service.attach(next_plan, ra)
    service.propose(next_plan)
    service.approve(next_plan, 'human', 'Keep completed work')
    assert service.inspect('plan-revision', plan)['status'] == 'superseded'


@pytest.mark.parametrize('operation', ['approval', 'revocation', 'decision', 'completion'])
def test_at08_transaction_rollbacks(core, monkeypatch, operation):
    service, store, _, objective = core
    service.activate_objective(objective)
    _, revision = task(service, objective)
    plan, authorization = approve(service, objective, [revision])
    decision = service.create_decision(objective, 'question', 'What?')
    next_plan = service.create_plan_revision(objective, 'Updated plan')
    service.attach(next_plan, revision)
    service.propose(next_plan)
    before = store.events(1000)

    def fail(*args, **kwargs):
        raise RuntimeError('forced audit failure')

    monkeypatch.setattr(store, 'emit', fail)
    operations = {
        'approval': lambda: service.approve(next_plan, 'human', 'Reviewed'),
        'revocation': lambda: service.revoke(authorization, 'human', 'Stop'),
        'decision': lambda: service.close_decision(decision, 'human', {'answer': 'yes'}),
        'completion': lambda: service.complete_manually(revision, 'human', 'Checked', {'tests': True}),
    }
    with pytest.raises(RuntimeError, match='forced'):
        operations[operation]()
    assert store.events(1000) == before
    assert service.inspect('plan-revision', plan)['status'] == 'approved'
    assert service.inspect('plan-revision', next_plan)['status'] == 'proposed'
    assert service.inspect('authorization', authorization)['status'] == 'active'
    assert service.inspect('decision', decision)['status'] == 'open'
    assert service.eligibility(revision)['eligible']


def test_approval_rolls_back_after_supersession_and_authorization_insert(core, monkeypatch):
    service, store, _, objective = core
    service.activate_objective(objective)
    _, revision = task(service, objective)
    original_plan, _ = approve(service, objective, [revision])
    next_plan = service.create_plan_revision(objective, 'Replacement')
    service.attach(next_plan, revision)
    service.propose(next_plan)
    before = store.event_records(1000)
    emit = store.emit

    def fail_after_event(db, kind, subject, data=None):
        emit(db, kind, subject, data)
        if kind == 'plan_approved':
            raise RuntimeError('failure after final audit insertion')

    monkeypatch.setattr(store, 'emit', fail_after_event)
    with pytest.raises(RuntimeError):
        service.approve(next_plan, 'human', 'Reviewed')
    assert service.inspect('plan-revision', original_plan)['status'] == 'approved'
    assert service.inspect('plan-revision', next_plan)['status'] == 'proposed'
    with store.transaction() as db:
        assert db.execute('SELECT count(*) FROM authorizations').fetchone()[0] == 1
    assert store.event_records(1000) == before


def test_at10_restart_preserves_complete_control_plane(core):
    service, store, _, objective = core
    service.activate_objective(objective)
    a, ra = task(service, objective, 'A')
    b, rb = task(service, objective, 'B')
    service.add_dependency(b, a)
    plan, authorization = approve(service, objective, [ra, rb])
    service.complete_manually(ra, 'human', 'Inspected', {'tests': True})
    decision = service.create_decision(objective, 'review', 'Review B?', [b])
    expected = service.eligibility(rb)
    history = store.event_records(1000)
    reopened = ControlPlane(Store(store.path))
    reopened.store.initialize()
    assert reopened.eligibility(rb) == expected
    assert reopened.inspect('plan-revision', plan) == service.inspect('plan-revision', plan)
    assert reopened.inspect('authorization', authorization) == service.inspect('authorization', authorization)
    assert reopened.inspect('task', a)['manual_completion']['actor'] == 'human'
    assert reopened.inspect('task', b)['dependencies'] == [a]
    assert reopened.inspect('decision', decision)['status'] == 'open'
    assert reopened.store.event_records(1000) == history
    reopened.close_decision(decision, 'human', {'answer': 'continue'})
    assert reopened.eligibility(rb)['eligible']


def test_scope_must_authorize_exact_task_revision(core):
    service, store, _, objective = core
    service.activate_objective(objective)
    _, revision = task(service, objective)
    plan, authorization = approve(service, objective, [revision])
    with store.transaction() as db:
        scope = {'objective_id': objective, 'plan_revision_id': plan, 'task_revision_ids': []}
        db.execute('UPDATE authorizations SET scope_json=? WHERE id=?', (json.dumps(scope), authorization))
    assert 'NO_AUTHORIZATION' in codes(service.eligibility(revision))
    with pytest.raises(ValidationError, match='NO_AUTHORIZATION'):
        service.complete_manually(revision, 'human', 'Attempt', {'tests': True})


def test_missing_legacy_dependency_is_not_silently_fulfilled(core):
    service, store, _, objective = core
    service.activate_objective(objective)
    a, ra = task(service, objective)
    approve(service, objective, [ra])
    # A bootstrap/externally written DB may contain violations from foreign_keys=OFF.
    with sqlite3.connect(store.path) as db:
        db.execute('INSERT INTO task_dependencies VALUES (?,?)', (a, 'missing'))
    assert 'DEPENDENCY_MISSING' in codes(service.eligibility(ra))


def test_done_out_of_plan_dependency_still_blocks(core):
    service, store, _, objective = core
    service.activate_objective(objective)
    a, _ = task(service, objective)
    b, rb = task(service, objective)
    service.add_dependency(b, a)
    approve(service, objective, [rb])
    # Legacy completed tasks need not have a SPEC-001 manual-completion record.
    with store.transaction() as db:
        db.execute("UPDATE tasks SET status='done' WHERE id=?", (a,))
    assert codes(service.eligibility(rb)) == {'DEPENDENCY_NOT_IN_EFFECTIVE_PLAN'}


@pytest.mark.parametrize('definition', [None, [], {}, {'description': ' ', 'acceptance_criteria': ['x']},
                                       {'description': 'x', 'acceptance_criteria': []},
                                       {'description': 'x', 'acceptance_criteria': ['']},
                                       {**DEFINITION, 'context_references': ['path']},
                                       {**DEFINITION, 'suggested_role': 42}])
def test_definition_validation_is_atomic(core, definition):
    service, store, _, objective = core
    before = store.events(1000)
    with pytest.raises(ValidationError):
        service.create_task(objective, 'Invalid', definition)
    assert store.status()['tasks'] == 0
    assert store.events(1000) == before


def test_cross_objective_membership_and_decisions_rejected(core):
    service, store, project, objective = core
    other = store.add_objective(project, 'Other')
    foreign_task, revision = task(service, other)
    plan = service.create_plan_revision(objective, 'Plan')
    with pytest.raises(ValidationError):
        service.attach(plan, revision)
    with pytest.raises(ValidationError):
        service.create_decision(objective, 'review', 'Question', [foreign_task])
    with pytest.raises(ValidationError):
        service.create_decision(other, 'review', 'Question', plan_revision_id=plan)


def test_at09_cli_full_workflow(tmp_path, capsys):
    path = str(tmp_path / 'cli.db')

    def run(*args, parsed=False):
        assert main(['--db', path, *args]) == 0
        output = capsys.readouterr().out.strip()
        return json.loads(output) if parsed else output

    run('init')
    project = run('projects', 'add', 'example', '/repo')
    objective = run('objectives', 'add', project, 'Feature')
    run('objectives', 'activate', objective)
    plan = run('plans', 'create', objective, 'First plan')
    ra = run('tasks', 'create', objective, 'A', '--definition', json.dumps(DEFINITION))
    rb = run('tasks', 'create', objective, 'B', '--definition', json.dumps(DEFINITION))
    a = run('task-revisions', 'show', ra, parsed=True)['task_id']
    b = run('task-revisions', 'show', rb, parsed=True)['task_id']
    run('dependencies', 'add', b, a)
    assert run('dependencies', 'list', b, parsed=True) == [a]
    for revision in (ra, rb):
        run('plan-revisions', 'attach', plan, revision)
    run('plan-revisions', 'propose', plan)
    authorization = run('plan-revisions', 'approve', plan, '--actor', 'human', '--reason', 'Reviewed')
    assert not run('eligibility', rb, parsed=True)['eligible']
    run('tasks', 'complete-manually', ra, '--actor', 'human', '--reason', 'Inspected',
        '--evidence', '{"tests":"passed"}')
    assert run('eligibility', rb, parsed=True)['eligible']
    decision = run('decisions', 'create', objective, 'review', 'Choose?', '--task', b)
    assert not run('eligibility', rb, parsed=True)['eligible']
    run('decisions', 'resolve', decision, '--actor', 'human', '--answer', '{"choice":"yes"}')
    assert run('eligibility', rb, parsed=True)['eligible']
    plan_id = run('plan-revisions', 'show', plan, parsed=True)['plan_id']
    assert run('plan-revisions', 'list', plan_id, parsed=True)[0]['id'] == plan
    assert run('plans', 'show', plan_id, parsed=True)['revisions'][0]['id'] == plan
    assert len(run('tasks', 'list', objective, parsed=True)) == 2
    assert len(run('task-revisions', 'list', a, parsed=True)) == 1
    assert len(run('plans', 'list', objective, parsed=True)) == 1
    assert run('decisions', 'show', decision, parsed=True)['status'] == 'resolved'
    assert len(run('decisions', 'list', objective, parsed=True)) == 1
    run('authorizations', 'revoke', authorization, '--actor', 'human', '--reason', 'Stop')
    assert 'REVOKED' in codes(run('eligibility', rb, parsed=True))
    run('init')
    assert 'REVOKED' in codes(run('eligibility', rb, parsed=True))
    run('status')
    run('events')


@pytest.mark.parametrize('args, message', [
    (['eligibility', 'missing'], 'Not found'),
    (['objectives', 'add', 'missing', 'Objective'], 'Not found'),
    (['tasks', 'create', 'missing', 'Task', '--definition', 'not json'], 'Validation failed'),
    (['events', '--limit', '0'], 'positive'),
])
def test_cli_actionable_errors(tmp_path, capsys, args, message):
    db = str(tmp_path / 'cli.db')
    main(['--db', db, 'init'])
    capsys.readouterr()
    with pytest.raises(SystemExit) as error:
        main(['--db', db, *args])
    assert error.value.code == 2
    assert message in capsys.readouterr().err
