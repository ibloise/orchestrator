"""Manual operator commands for SPEC-002."""
from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from .config import ExecutorConfig
from .execution import ExecutionService
from .store import Store

COMMANDS = {'agents', 'run', 'executions', 'execution', 'workspace', 'executor'}


def add_commands(commands: argparse._SubParsersAction) -> None:
    agents = commands.add_parser('agents').add_subparsers(dest='action', required=True)
    agents.add_parser('models')
    run = commands.add_parser('run', help='Manually execute an approved task; no implicit approval')
    run.add_argument('task_id')
    run.add_argument('--role', choices=('implementer', 'inspect'), default='implementer')
    run.add_argument('--model', choices=('luna', 'sol', 'astra'), default='sol')
    run.add_argument('--actor', default=getpass.getuser())
    listing = commands.add_parser('executions')
    listing.add_argument('--task', dest='task_id')
    execution = commands.add_parser('execution').add_subparsers(dest='action', required=True)
    for name in ('show', 'logs', 'cancel'):
        action = execution.add_parser(name)
        action.add_argument('identifier')
        if name == 'cancel':
            action.add_argument('--actor', default=getpass.getuser())
    workspace = commands.add_parser('workspace').add_subparsers(dest='action', required=True)
    workspace.add_parser('show').add_argument('task_id')
    executor = commands.add_parser('executor').add_subparsers(dest='action', required=True)
    executor.add_parser('doctor')
    executor.add_parser('reconcile', help='Interrupt abandoned executions; never reruns work')


def dispatch(store: Store, config_path: Path, args: argparse.Namespace) -> object:
    service = ExecutionService(store, ExecutorConfig.load(config_path))
    if args.command == 'agents':
        return service.models()
    if args.command == 'run':
        return service.run(args.task_id, args.role, args.model, args.actor)
    if args.command == 'executions':
        return service.list(args.task_id)
    if args.command == 'workspace':
        return service.workspace_show(args.task_id)
    if args.command == 'executor':
        return service.doctor() if args.action == 'doctor' else service.reconcile()
    if args.action == 'show':
        return service.show(args.identifier)
    if args.action == 'logs':
        return service.logs(args.identifier)
    service.cancel(args.identifier, args.actor)
    return {'cancellation_requested': True}
