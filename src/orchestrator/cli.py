"""Administrative control-plane CLI; deliberately cannot execute agents."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from .domain import NotFoundError
from .execution_cli import COMMANDS, add_commands, dispatch
from .services import ControlPlane
from .store import Store


def default_db() -> Path:
    state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
    return state_home / 'orchestrator' / 'orchestrator.db'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='orchestrator')
    parser.add_argument('--db', type=Path, default=default_db(), help='Control-plane SQLite database')
    parser.add_argument('--config', type=Path,
                        default=Path.home() / '.config' / 'orchestrator' / 'config.toml',
                        help='Operator-owned executor TOML config (required only for SPEC-002 commands)')
    cmd = parser.add_subparsers(dest='command', required=True)
    cmd.add_parser('init')
    cmd.add_parser('status')
    projects = cmd.add_parser('projects')
    project_commands = projects.add_subparsers(dest='action', required=True)
    project_commands.add_parser('list')
    add_project = project_commands.add_parser('add')
    add_project.add_argument('name')
    add_project.add_argument('repository')
    add_project.add_argument('--priority', type=int, default=50)
    objectives = cmd.add_parser('objectives')
    objective_commands = objectives.add_subparsers(dest='action', required=True)
    objective_commands.add_parser('list')
    add_objective = objective_commands.add_parser('add')
    add_objective.add_argument('project_id')
    add_objective.add_argument('title')
    add_objective.add_argument('--description', default='')
    activate = objective_commands.add_parser('activate')
    activate.add_argument('objective_id')
    events = cmd.add_parser('events')
    events.add_argument('--limit', type=int, default=20)
    events.add_argument('--json', action='store_true', help='Include full audit payloads as JSON')
    add_domain_commands(cmd)
    add_commands(cmd)
    args = parser.parse_args(argv)
    store = Store(args.db)
    if args.command != 'init' and not store.path.is_file():
        parser.error('Database does not exist: run orchestrator init first')
    try:
        if args.command == 'init':
            store.initialize()
            print(f'Initialized: {store.path}')
        elif args.command == 'status':
            for key, value in store.status().items():
                print(f'{key}: {value}')
        elif args.command == 'projects':
            if args.action == 'list':
                for row in store.list_projects():
                    print('\t'.join(str(x) for x in row))
            elif args.action == 'add':
                print(store.add_project(args.name, args.repository, args.priority))
        elif args.command == 'objectives':
            if args.action == 'list':
                for row in store.list_objectives():
                    print('\t'.join(str(x) for x in row))
            elif args.action == 'add':
                print(store.add_objective(args.project_id, args.title, args.description))
            elif args.action == 'activate':
                ControlPlane(store).activate_objective(args.objective_id)
                print('Objective activated')
        elif args.command == 'events':
            if args.limit < 1:
                parser.error('--limit must be positive')
            if args.json:
                print(json.dumps(store.event_records(args.limit), sort_keys=True))
            else:
                for row in store.events(args.limit):
                    print('\t'.join(str(x) for x in row))
        else:
            result = dispatch(store, args.config, args) if args.command in COMMANDS else domain_command(ControlPlane(store), args)
            if isinstance(result, str):
                print(result)
            else:
                print(json.dumps(result if result is not None else {'ok': True}, sort_keys=True))
            if args.command == 'run' and result['status'] != 'succeeded':
                return 1
    except NotFoundError as e:
        parser.error(f'Not found: {e}')
    except sqlite3.IntegrityError as e:
        if 'FOREIGN KEY constraint failed' in str(e):
            parser.error('Not found: a referenced identifier does not exist')
        parser.error(f'Validation failed: {e}')
    except ValueError as e:
        parser.error(f'Validation failed: {e}')
    except sqlite3.OperationalError as e:
        parser.error(f'Database operation failed (run init to apply migrations): {e}')
    except OSError as e:
        parser.error(f'Filesystem/configuration operation failed: {e}')
    except KeyboardInterrupt:
        parser.exit(130, 'Execution interrupted; inspect executions and logs before retrying\n')
    return 0


def add_domain_commands(commands: argparse._SubParsersAction) -> None:
    def arguments(parser: argparse.ArgumentParser, *names: str) -> None:
        for name in names:
            parser.add_argument(name)

    def actor_reason(parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--actor', required=True)
        parser.add_argument('--reason', required=True)

    plans = commands.add_parser('plans').add_subparsers(dest='action', required=True)
    arguments(plans.add_parser('list'), 'objective_id')
    arguments(plans.add_parser('show'), 'identifier')
    arguments(plans.add_parser('create'), 'objective_id', 'summary')
    revisions = commands.add_parser('plan-revisions').add_subparsers(dest='action', required=True)
    arguments(revisions.add_parser('create'), 'objective_id', 'summary')
    arguments(revisions.add_parser('list'), 'plan_id')
    arguments(revisions.add_parser('show'), 'identifier')
    for action in ('attach', 'detach'):
        arguments(revisions.add_parser(action), 'identifier', 'task_revision_id')
    arguments(revisions.add_parser('propose'), 'identifier')
    for action in ('approve', 'reject'):
        parser = revisions.add_parser(action)
        arguments(parser, 'identifier')
        actor_reason(parser)

    tasks = commands.add_parser('tasks').add_subparsers(dest='action', required=True)
    arguments(tasks.add_parser('list'), 'objective_id')
    arguments(tasks.add_parser('show'), 'identifier')
    create = tasks.add_parser('create')
    arguments(create, 'objective_id', 'title')
    create.add_argument('--definition', required=True, help='JSON object with description and acceptance_criteria')
    complete = tasks.add_parser('complete-manually', help='Administrative bootstrap exception, not verification')
    arguments(complete, 'task_revision_id')
    actor_reason(complete)
    complete.add_argument('--evidence', required=True, help='Nonempty JSON evidence object')
    cancel = tasks.add_parser('cancel')
    arguments(cancel, 'identifier')
    actor_reason(cancel)
    revisions = commands.add_parser('task-revisions').add_subparsers(dest='action', required=True)
    create = revisions.add_parser('create')
    arguments(create, 'task_id')
    create.add_argument('--definition', required=True)
    arguments(revisions.add_parser('list'), 'task_id')
    arguments(revisions.add_parser('show'), 'identifier')

    dependencies = commands.add_parser('dependencies').add_subparsers(dest='action', required=True)
    arguments(dependencies.add_parser('add'), 'task_id', 'depends_on')
    arguments(dependencies.add_parser('list'), 'task_id')
    authorizations = commands.add_parser('authorizations').add_subparsers(dest='action', required=True)
    arguments(authorizations.add_parser('show'), 'identifier')
    revoke = authorizations.add_parser('revoke')
    arguments(revoke, 'identifier')
    actor_reason(revoke)
    eligibility = commands.add_parser('eligibility', help='Computed eligibility as JSON with blocking reason codes')
    arguments(eligibility, 'task_revision_id')

    decisions = commands.add_parser('decisions').add_subparsers(dest='action', required=True)
    arguments(decisions.add_parser('list'), 'objective_id')
    arguments(decisions.add_parser('show'), 'identifier')
    create = decisions.add_parser('create')
    arguments(create, 'objective_id', 'kind', 'question')
    create.add_argument('--task', action='append', default=[], dest='task_ids')
    create.add_argument('--plan-revision')
    for action in ('resolve', 'cancel', 'supersede'):
        close = decisions.add_parser(action)
        arguments(close, 'identifier')
        close.add_argument('--actor', required=True)
        close.add_argument('--answer', required=True, help='Nonempty JSON object')


def domain_command(service: ControlPlane, args: argparse.Namespace) -> object:
    command = args.command
    action = getattr(args, 'action', None)
    if command == 'eligibility':
        return service.eligibility(args.task_revision_id)
    if command in ('plans', 'tasks', 'decisions') and action == 'list':
        return service.list_for_objective(command, args.objective_id)
    entities = {'plans': 'plan', 'plan-revisions': 'plan-revision', 'tasks': 'task',
                'task-revisions': 'task-revision', 'decisions': 'decision', 'authorizations': 'authorization'}
    if action == 'show':
        return service.inspect(entities[command], args.identifier)
    if command == 'plans' or (command == 'plan-revisions' and action == 'create'):
        return service.create_plan_revision(args.objective_id, args.summary)
    if command == 'plan-revisions':
        if action == 'list':
            return service.inspect('plan', args.plan_id)['revisions']
        if action in ('attach', 'detach'):
            return service.attach(args.identifier, args.task_revision_id, remove=action == 'detach')
        if action == 'propose':
            return service.propose(args.identifier)
        if action == 'approve':
            return service.approve(args.identifier, args.actor, args.reason)
        if action == 'reject':
            return service.reject(args.identifier, args.actor, args.reason)
    if command == 'tasks':
        if action == 'create':
            return service.create_task(args.objective_id, args.title, json.loads(args.definition))
        if action == 'complete-manually':
            return service.complete_manually(args.task_revision_id, args.actor, args.reason, json.loads(args.evidence))
        if action == 'cancel':
            return service.cancel_task(args.identifier, args.actor, args.reason)
    if command == 'task-revisions':
        if action == 'list':
            return service.inspect('task', args.task_id)['revisions']
        return service.create_task_revision(args.task_id, json.loads(args.definition))
    if command == 'dependencies':
        if action == 'list':
            return service.inspect('task', args.task_id)['dependencies']
        return service.add_dependency(args.task_id, args.depends_on)
    if command == 'authorizations':
        return service.revoke(args.identifier, args.actor, args.reason)
    if command == 'decisions':
        if action == 'create':
            return service.create_decision(args.objective_id, args.kind, args.question,
                                           args.task_ids, args.plan_revision)
        outcome = {'resolve': 'resolved', 'cancel': 'cancelled', 'supersede': 'superseded'}[action]
        return service.close_decision(args.identifier, args.actor, json.loads(args.answer), outcome)
    raise ValueError('Unknown operation')


if __name__ == '__main__':
    raise SystemExit(main())
