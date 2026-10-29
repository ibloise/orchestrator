"""Transactional domain operations; no agent execution or provider integration."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .domain import (
    NotFoundError,
    ValidationError,
    json_object,
    nonempty,
    task_definition,
)
from .store import Store, new_id, now


class ControlPlane:
    def __init__(self, store: Store) -> None:
        self.store = store

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.store.transaction() as db:
            db.row_factory = sqlite3.Row
            yield db

    @staticmethod
    def require(db: sqlite3.Connection, table: str, identifier: str) -> sqlite3.Row:
        # Table names are application constants, never CLI input.
        row = db.execute(f'SELECT * FROM {table} WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise NotFoundError(f'{table}: identifier {identifier!r} not found')
        return row

    def activate_objective(self, objective_id: str) -> None:
        with self.transaction() as db:
            objective = self.require(db, 'objectives', objective_id)
            if objective['status'] != 'draft':
                raise ValidationError('Only a draft objective can be activated')
            db.execute("UPDATE objectives SET status='active' WHERE id=?", (objective_id,))
            self.store.emit(db, 'objective_activated', objective_id)

    def create_plan_revision(self, objective_id: str, summary: str) -> str:
        summary = nonempty(summary, 'summary')
        with self.transaction() as db:
            self.require(db, 'objectives', objective_id)
            plan = db.execute('SELECT id FROM plans WHERE objective_id=?', (objective_id,)).fetchone()
            if plan is None:
                plan_id = new_id('pln')
                db.execute('INSERT INTO plans(id,objective_id,created_at) VALUES (?,?,?)',
                           (plan_id, objective_id, now()))
                self.store.emit(db, 'plan_created', plan_id, {'objective_id': objective_id})
            else:
                plan_id = plan['id']
            revision = db.execute('SELECT coalesce(max(revision),0)+1 FROM plan_revisions WHERE plan_id=?',
                                  (plan_id,)).fetchone()[0]
            identifier = new_id('prv')
            db.execute("INSERT INTO plan_revisions VALUES (?,?,?,'draft',?,?)",
                       (identifier, plan_id, revision, json.dumps({'summary': summary}), now()))
            self.store.emit(db, 'plan_revision_created', identifier, {'plan_id': plan_id, 'revision': revision})
            return identifier

    def create_task(self, objective_id: str, title: str, definition: dict) -> str:
        title = nonempty(title, 'title')
        definition = task_definition(definition)
        with self.transaction() as db:
            self.require(db, 'objectives', objective_id)
            task_id = new_id('tsk')
            db.execute('INSERT INTO tasks(id,objective_id,title,created_at) VALUES (?,?,?,?)',
                       (task_id, objective_id, title, now()))
            self.store.emit(db, 'task_created', task_id, {'objective_id': objective_id})
            return self._task_revision(db, task_id, definition)

    def create_task_revision(self, task_id: str, definition: dict) -> str:
        definition = task_definition(definition)
        with self.transaction() as db:
            task = self.require(db, 'tasks', task_id)
            if task['status'] != 'pending':
                raise ValidationError('Only pending tasks can receive new definitions')
            return self._task_revision(db, task_id, definition)

    def _task_revision(self, db: sqlite3.Connection, task_id: str, definition: dict) -> str:
        revision = db.execute('SELECT coalesce(max(revision),0)+1 FROM task_revisions WHERE task_id=?',
                              (task_id,)).fetchone()[0]
        identifier = new_id('trv')
        db.execute('INSERT INTO task_revisions VALUES (?,?,?,?,?)',
                   (identifier, task_id, revision, json.dumps(definition), now()))
        self.store.emit(db, 'task_revision_created', identifier, {'task_id': task_id, 'revision': revision})
        return identifier

    def _plan_objective(self, db: sqlite3.Connection, revision_id: str) -> str:
        revision = self.require(db, 'plan_revisions', revision_id)
        return self.require(db, 'plans', revision['plan_id'])['objective_id']

    def attach(self, plan_revision_id: str, task_revision_id: str, *, remove: bool = False) -> None:
        with self.transaction() as db:
            plan = self.require(db, 'plan_revisions', plan_revision_id)
            if plan['status'] != 'draft':
                raise ValidationError('Task membership can only change on draft plan revisions')
            revision = self.require(db, 'task_revisions', task_revision_id)
            task = self.require(db, 'tasks', revision['task_id'])
            if task['objective_id'] != self._plan_objective(db, plan_revision_id):
                raise ValidationError('Task and plan must belong to the same objective')
            if remove:
                cursor = db.execute('DELETE FROM plan_tasks WHERE plan_revision_id=? AND task_revision_id=?',
                                    (plan_revision_id, task_revision_id))
                if not cursor.rowcount:
                    raise ValidationError('Task revision is not attached')
            else:
                members = self._members(db, plan_revision_id)
                if any(member['task_id'] == task['id'] for member in members):
                    raise ValidationError('A plan may reference only one revision of each task')
                db.execute('INSERT INTO plan_tasks VALUES (?,?)', (plan_revision_id, task_revision_id))
            self.store.emit(db, 'plan_task_removed' if remove else 'plan_task_attached', plan_revision_id,
                            {'task_revision_id': task_revision_id})

    @staticmethod
    def _members(db: sqlite3.Connection, plan_revision_id: str) -> list[sqlite3.Row]:
        return db.execute('SELECT tr.* FROM plan_tasks pt JOIN task_revisions tr '
                          'ON tr.id=pt.task_revision_id WHERE pt.plan_revision_id=? ORDER BY tr.id',
                          (plan_revision_id,)).fetchall()

    def _validate_snapshot(self, db: sqlite3.Connection, revision_id: str) -> None:
        revision = self.require(db, 'plan_revisions', revision_id)
        definition = json_object(json.loads(revision['definition_json']), 'plan definition')
        nonempty(definition.get('summary'), 'plan summary')
        members = self._members(db, revision_id)
        if not members:
            raise ValidationError('A proposed or approved plan must contain at least one task')
        objective_id = self._plan_objective(db, revision_id)
        by_task = {}
        for member in members:
            task = self.require(db, 'tasks', member['task_id'])
            if task['objective_id'] != objective_id or member['task_id'] in by_task:
                raise ValidationError('Invalid plan task membership')
            task_definition(json.loads(member['definition_json']))
            by_task[member['task_id']] = member['id']
        # Completed work remains represented by exactly the definition completed.
        completed = db.execute('SELECT mc.task_id,mc.task_revision_id FROM manual_completions mc '
                               'JOIN tasks t ON t.id=mc.task_id WHERE t.objective_id=?',
                               (objective_id,)).fetchall()
        for task in db.execute("SELECT id FROM tasks WHERE objective_id=? AND status='done'", (objective_id,)):
            if task['id'] not in by_task:
                raise ValidationError('Plan must preserve completed work')
        for item in completed:
            if by_task.get(item['task_id']) != item['task_revision_id']:
                raise ValidationError('Plan must preserve completed task definitions')
        previous = db.execute(
            "SELECT tr.* FROM plan_revisions pr JOIN plan_tasks pt ON pt.plan_revision_id=pr.id "
            "JOIN task_revisions tr ON tr.id=pt.task_revision_id JOIN tasks t ON t.id=tr.task_id "
            "WHERE pr.plan_id=? AND pr.status='approved' AND t.status='done'", (revision['plan_id'],))
        for member in previous:
            if by_task.get(member['task_id']) != member['id']:
                raise ValidationError('Plan must preserve completed task definitions')

    def _plan_blockers(self, db: sqlite3.Connection, revision_id: str) -> list[str]:
        return [row[0] for row in db.execute(
            "SELECT d.id FROM decisions d JOIN decision_plans dp ON dp.decision_id=d.id "
            "WHERE d.status='open' AND dp.plan_revision_id=? ORDER BY d.id", (revision_id,))]

    def propose(self, revision_id: str) -> None:
        with self.transaction() as db:
            revision = self.require(db, 'plan_revisions', revision_id)
            if revision['status'] != 'draft':
                raise ValidationError('Only draft revisions can be proposed')
            self._validate_snapshot(db, revision_id)
            if self._plan_blockers(db, revision_id):
                raise ValidationError('OPEN_DECISION blocks this plan operation')
            db.execute("UPDATE plan_revisions SET status='proposed' WHERE id=?", (revision_id,))
            self.store.emit(db, 'plan_proposed', revision_id)

    def approve(self, revision_id: str, actor: str, reason: str) -> str:
        actor, reason = nonempty(actor, 'actor'), nonempty(reason, 'reason')
        with self.transaction() as db:
            revision = self.require(db, 'plan_revisions', revision_id)
            if revision['status'] != 'proposed':
                raise ValidationError('Only proposed revisions can be approved')
            self._validate_snapshot(db, revision_id)
            if self._plan_blockers(db, revision_id):
                raise ValidationError('OPEN_DECISION blocks approval')
            previous = db.execute("SELECT * FROM plan_revisions WHERE plan_id=? AND status='approved'",
                                  (revision['plan_id'],)).fetchone()
            latest = db.execute("SELECT max(revision) FROM plan_revisions WHERE plan_id=? "
                                "AND status IN ('approved','superseded')", (revision['plan_id'],)).fetchone()[0]
            if latest is not None and revision['revision'] <= latest:
                raise ValidationError('Approval must advance the effective plan revision')
            if previous:
                db.execute("UPDATE plan_revisions SET status='superseded' WHERE id=?", (previous['id'],))
                self.store.emit(db, 'plan_superseded', previous['id'], {'by': revision_id, 'actor': actor})
            db.execute("UPDATE plan_revisions SET status='approved' WHERE id=?", (revision_id,))
            identifier = new_id('auth')
            scope = {'objective_id': self._plan_objective(db, revision_id), 'plan_revision_id': revision_id,
                     'task_revision_ids': [member['id'] for member in self._members(db, revision_id)]}
            db.execute('INSERT INTO authorizations '
                       '(id,plan_revision_id,scope_json,status,created_at,actor,reason) '
                       "VALUES (?,?,?,'active',?,?,?)",
                       (identifier, revision_id, json.dumps(scope), now(), actor, reason))
            self.store.emit(db, 'plan_approved', revision_id, {'actor': actor, 'reason': reason,
                                                            'authorization_id': identifier, 'scope': scope})
            return identifier

    def reject(self, revision_id: str, actor: str, reason: str) -> None:
        actor, reason = nonempty(actor, 'actor'), nonempty(reason, 'reason')
        with self.transaction() as db:
            revision = self.require(db, 'plan_revisions', revision_id)
            if revision['status'] != 'proposed':
                raise ValidationError('Only proposed revisions can be rejected')
            if self._plan_blockers(db, revision_id):
                raise ValidationError('OPEN_DECISION blocks this plan operation')
            db.execute("UPDATE plan_revisions SET status='rejected' WHERE id=?", (revision_id,))
            self.store.emit(db, 'plan_rejected', revision_id, {'actor': actor, 'reason': reason})

    def revoke(self, authorization_id: str, actor: str, reason: str) -> None:
        actor, reason = nonempty(actor, 'actor'), nonempty(reason, 'reason')
        with self.transaction() as db:
            authorization = self.require(db, 'authorizations', authorization_id)
            if authorization['status'] != 'active':
                raise ValidationError('Authorization is already revoked')
            db.execute("UPDATE authorizations SET status='revoked',revoked_by=?,revocation_reason=?,"
                       'revoked_at=? WHERE id=?', (actor, reason, now(), authorization_id))
            self.store.emit(db, 'authorization_revoked', authorization_id, {'actor': actor, 'reason': reason})

    def add_dependency(self, task_id: str, depends_on: str) -> None:
        with self.transaction() as db:
            task = self.require(db, 'tasks', task_id)
            dependency = self.require(db, 'tasks', depends_on)
            if task['objective_id'] != dependency['objective_id']:
                raise ValidationError('Dependencies must belong to the same objective')
            if task['status'] != 'pending':
                raise ValidationError('Dependencies can only be added to pending tasks')
            if task_id == depends_on:
                raise ValidationError('Self-dependency is not allowed')
            cycle = db.execute('WITH RECURSIVE ancestors(id) AS (SELECT ? UNION '
                               'SELECT td.depends_on_task_id FROM task_dependencies td '
                               'JOIN ancestors a ON td.task_id=a.id) SELECT 1 FROM ancestors WHERE id=?',
                               (depends_on, task_id)).fetchone()
            if cycle:
                raise ValidationError('Dependency would introduce a cycle')
            if db.execute('SELECT 1 FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?',
                          (task_id, depends_on)).fetchone():
                raise ValidationError('Dependency already exists')
            db.execute('INSERT INTO task_dependencies VALUES (?,?)', (task_id, depends_on))
            self.store.emit(db, 'dependency_added', task_id, {'depends_on_task_id': depends_on})

    def create_decision(self, objective_id: str, kind: str, question: str,
                        task_ids: list[str] | None = None, plan_revision_id: str | None = None) -> str:
        kind, question = nonempty(kind, 'kind'), nonempty(question, 'question')
        with self.transaction() as db:
            self.require(db, 'objectives', objective_id)
            for task_id in set(task_ids or []):
                if self.require(db, 'tasks', task_id)['objective_id'] != objective_id:
                    raise ValidationError('Decision blockers must belong to its objective')
            if plan_revision_id and self._plan_objective(db, plan_revision_id) != objective_id:
                raise ValidationError('Decision plan must belong to its objective')
            identifier = new_id('dec')
            db.execute('INSERT INTO decisions(id,objective_id,kind,question,status,created_at) '
                       "VALUES (?,?,?,?,'open',?)", (identifier, objective_id, kind, question, now()))
            for task_id in set(task_ids or []):
                db.execute('INSERT INTO decision_tasks VALUES (?,?)', (identifier, task_id))
            if plan_revision_id:
                db.execute('INSERT INTO decision_plans VALUES (?,?)', (identifier, plan_revision_id))
            self.store.emit(db, 'decision_created', identifier,
                            {'objective_id': objective_id, 'task_ids': sorted(set(task_ids or [])),
                             'plan_revision_id': plan_revision_id})
            return identifier

    def close_decision(self, decision_id: str, actor: str, answer: dict, outcome: str = 'resolved') -> None:
        actor = nonempty(actor, 'actor')
        answer = json_object(answer, 'answer')
        if not answer:
            raise ValidationError('An explicit nonempty structured answer is required')
        if outcome not in ('resolved', 'cancelled', 'superseded'):
            raise ValidationError('Invalid decision outcome')
        with self.transaction() as db:
            decision = self.require(db, 'decisions', decision_id)
            if decision['status'] != 'open':
                raise ValidationError('Decision is already closed')
            db.execute('UPDATE decisions SET status=?,resolution_json=?,resolved_by=?,closed_at=? WHERE id=?',
                       (outcome, json.dumps(answer), actor, now(), decision_id))
            self.store.emit(db, f'decision_{outcome}', decision_id, {'actor': actor, 'answer': answer})

    def eligibility(self, task_revision_id: str) -> dict:
        with self.transaction() as db:
            return self._eligibility(db, task_revision_id)

    def _eligibility(self, db: sqlite3.Connection, task_revision_id: str) -> dict:
        revision = self.require(db, 'task_revisions', task_revision_id)
        task = self.require(db, 'tasks', revision['task_id'])
        objective = self.require(db, 'objectives', task['objective_id'])
        project = self.require(db, 'projects', objective['project_id'])
        reasons = []

        def block(code: str, **details: object) -> None:
            reasons.append({'code': code, **details})

        if project['status'] != 'active':
            block('PROJECT_PAUSED' if project['status'] == 'paused' else 'PROJECT_INACTIVE')
        if objective['status'] != 'active':
            block('OBJECTIVE_INACTIVE')
        effective = db.execute("SELECT pr.* FROM plan_revisions pr JOIN plans p ON p.id=pr.plan_id "
                               "WHERE p.objective_id=? AND pr.status='approved'", (objective['id'],)).fetchone()
        members = self._members(db, effective['id']) if effective else []
        authorization_id = None
        if effective is None:
            block('NO_APPROVAL')
        if task_revision_id not in {member['id'] for member in members}:
            block('TASK_NOT_IN_EFFECTIVE_PLAN')
        if effective:
            authorizations = db.execute('SELECT * FROM authorizations WHERE plan_revision_id=? ORDER BY id',
                                        (effective['id'],)).fetchall()
            for authorization in authorizations:
                scope = json.loads(authorization['scope_json'])
                if (authorization['status'] == 'active'
                        and scope.get('objective_id') == objective['id']
                        and scope.get('plan_revision_id') == effective['id']
                        and task_revision_id in scope.get('task_revision_ids', [])):
                    authorization_id = authorization['id']
                    break
            if authorization_id is None:
                block('REVOKED' if authorizations and all(a['status'] == 'revoked' for a in authorizations)
                      else 'NO_AUTHORIZATION')
        if task['status'] != 'pending':
            block('TASK_NOT_PENDING', status=task['status'])
        member_tasks = {member['task_id'] for member in members}
        for dependency in db.execute('SELECT td.depends_on_task_id AS dependency_id,t.* '
                                     'FROM task_dependencies td LEFT JOIN tasks t '
                                     'ON t.id=td.depends_on_task_id WHERE td.task_id=? ORDER BY t.id', (task['id'],)):
            if dependency['id'] is None:
                block('DEPENDENCY_MISSING', task_id=dependency['dependency_id'])
                continue
            if dependency['objective_id'] != objective['id']:
                block('DEPENDENCY_INVALID', task_id=dependency['id'])
            if dependency['id'] not in member_tasks:
                block('DEPENDENCY_NOT_IN_EFFECTIVE_PLAN', task_id=dependency['id'])
            if dependency['status'] != 'done':
                block('DEPENDENCY_PENDING', task_id=dependency['id'], status=dependency['status'])
        blockers = {row[0] for row in db.execute(
            "SELECT d.id FROM decisions d JOIN decision_tasks dt ON dt.decision_id=d.id "
            "WHERE d.status='open' AND dt.task_id=?", (task['id'],))}
        if effective:
            blockers.update(self._plan_blockers(db, effective['id']))
        if blockers:
            block('OPEN_DECISION', decision_ids=sorted(blockers))
        return {'task_id': task['id'], 'task_revision_id': task_revision_id,
                'plan_revision_id': effective['id'] if effective else None,
                'authorization_id': authorization_id, 'eligible': not reasons, 'reasons': reasons}

    def complete_manually(self, task_revision_id: str, actor: str, reason: str, evidence: dict) -> None:
        actor, reason = nonempty(actor, 'actor'), nonempty(reason, 'reason')
        evidence = json_object(evidence, 'evidence')
        if not evidence:
            raise ValidationError('Manual completion requires nonempty evidence')
        with self.transaction() as db:
            result = self._eligibility(db, task_revision_id)
            if not result['eligible']:
                raise ValidationError('Manual completion blocked: ' + ', '.join(r['code'] for r in result['reasons']))
            db.execute("UPDATE tasks SET status='done' WHERE id=?", (result['task_id'],))
            identifier = new_id('manual')
            db.execute('INSERT INTO manual_completions VALUES (?,?,?,?,?,?,?,?)',
                       (identifier, result['task_id'], task_revision_id, result['authorization_id'],
                        actor, reason, json.dumps(evidence), now()))
            self.store.emit(db, 'task_completed_manually', result['task_id'],
                            {'actor': actor, 'reason': reason, 'evidence': evidence,
                             'task_revision_id': task_revision_id, 'authorization_id': result['authorization_id']})

    def cancel_task(self, task_id: str, actor: str, reason: str) -> None:
        actor, reason = nonempty(actor, 'actor'), nonempty(reason, 'reason')
        with self.transaction() as db:
            task = self.require(db, 'tasks', task_id)
            if task['status'] != 'pending':
                raise ValidationError('Only pending tasks can be cancelled')
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
            self.store.emit(db, 'task_cancelled', task_id, {'actor': actor, 'reason': reason})

    def inspect(self, entity: str, identifier: str) -> dict:
        tables = {'plan': 'plans', 'plan-revision': 'plan_revisions', 'task': 'tasks',
                  'task-revision': 'task_revisions', 'decision': 'decisions', 'authorization': 'authorizations'}
        if entity not in tables:
            raise ValidationError('Unknown entity')
        with self.transaction() as db:
            result = dict(self.require(db, tables[entity], identifier))
            if entity == 'plan':
                result['revisions'] = [dict(row) for row in db.execute(
                    'SELECT * FROM plan_revisions WHERE plan_id=? ORDER BY revision', (identifier,))]
            elif entity == 'plan-revision':
                result['task_revisions'] = [dict(row) for row in self._members(db, identifier)]
            elif entity == 'task':
                result['revisions'] = [dict(row) for row in db.execute(
                    'SELECT * FROM task_revisions WHERE task_id=? ORDER BY revision', (identifier,))]
                result['dependencies'] = [row[0] for row in db.execute(
                    'SELECT depends_on_task_id FROM task_dependencies WHERE task_id=? ORDER BY depends_on_task_id',
                    (identifier,))]
                completion = db.execute('SELECT * FROM manual_completions WHERE task_id=?', (identifier,)).fetchone()
                result['manual_completion'] = dict(completion) if completion else None
            elif entity == 'decision':
                result['task_ids'] = [row[0] for row in db.execute(
                    'SELECT task_id FROM decision_tasks WHERE decision_id=? ORDER BY task_id', (identifier,))]
                result['plan_revision_ids'] = [row[0] for row in db.execute(
                    'SELECT plan_revision_id FROM decision_plans WHERE decision_id=? ORDER BY plan_revision_id',
                    (identifier,))]
            return result

    def list_for_objective(self, entity: str, objective_id: str) -> list[dict]:
        tables = {'plans': 'plans', 'tasks': 'tasks', 'decisions': 'decisions'}
        if entity not in tables:
            raise ValidationError('Unknown entity')
        with self.transaction() as db:
            self.require(db, 'objectives', objective_id)
            return [dict(row) for row in db.execute(
                f'SELECT * FROM {tables[entity]} WHERE objective_id=? ORDER BY created_at,id', (objective_id,))]
