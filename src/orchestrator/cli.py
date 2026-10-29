"""Minimal administrative CLI (no agent execution in this milestone)."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
from .store import Store


def default_db() -> Path:
    state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
    return state_home / 'orchestrator' / 'orchestrator.db'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='orchestrator')
    parser.add_argument('--db', type=Path, default=default_db(), help='Control-plane SQLite database')
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
    events = cmd.add_parser('events')
    events.add_argument('--limit', type=int, default=20)
    args = parser.parse_args(argv)
    store = Store(args.db)
    if args.command == 'init':
        store.initialize()
        print(f'Initialized: {store.path}')
        return 0
    if not store.path.is_file():
        parser.error('Database does not exist: run orchestrator init first')
    try:
        if args.command == 'status':
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
        elif args.command == 'events':
            if args.limit < 1:
                parser.error('--limit must be positive')
            for row in store.events(args.limit):
                print('\t'.join(str(x) for x in row))
    except (ValueError, __import__('sqlite3').IntegrityError) as e:
        parser.error(str(e))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
