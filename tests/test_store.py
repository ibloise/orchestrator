import sqlite3

import pytest

from orchestrator.cli import main
from orchestrator.store import Store


def test_project_objective_and_audit_are_atomic(tmp_path):
    store = Store(tmp_path / 'state' / 'db.sqlite')
    store.initialize()
    assert (tmp_path / 'state' / 'db.sqlite').exists()
    p = store.add_project('bioenv', '/repos/bioenv', 60)
    o = store.add_objective(p, 'Manage tool databases')
    assert store.status() == {'projects': 1, 'objectives': 1, 'tasks': 0, 'executions': 0, 'open_decisions': 0}
    assert [x[2] for x in store.events()] == ['objective_created', 'project_created']
    assert store.list_objectives()[0][0] == o
    with pytest.raises(sqlite3.IntegrityError):
        store.add_objective('missing', 'orphan')
    assert len(store.events()) == 2


def test_reject_invalid_priority(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    store.initialize()
    with pytest.raises(ValueError):
        store.add_project('bad', '/x', 101)


def test_cli_roundtrip(tmp_path, capsys):
    db = str(tmp_path / 'orchestrator.db')
    assert main(['--db', db, 'init']) == 0
    assert main(['--db', db, 'projects', 'add', 'hansa', '/repos/hansa']) == 0
    assert main(['--db', db, 'status']) == 0
    assert 'projects: 1' in capsys.readouterr().out
