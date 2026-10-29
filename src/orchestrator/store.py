"""Transactional SQLite store for the initial control plane.

No agent process should receive access to this database file.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    repository TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objectives (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','completed','cancelled')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL REFERENCES objectives(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_revisions (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    status TEXT NOT NULL CHECK (status IN ('draft','proposed','approved','rejected','superseded')),
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, revision)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL REFERENCES objectives(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','verifying','verified','done','cancelled')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_revisions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, revision)
);
CREATE TABLE IF NOT EXISTS plan_tasks (
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id),
    task_revision_id TEXT NOT NULL REFERENCES task_revisions(id),
    PRIMARY KEY (plan_revision_id, task_revision_id)
);
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);
CREATE TABLE IF NOT EXISTS authorizations (
    id TEXT PRIMARY KEY,
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id),
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','revoked')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    task_revision_id TEXT NOT NULL REFERENCES task_revisions(id),
    authorization_id TEXT NOT NULL REFERENCES authorizations(id),
    status TEXT NOT NULL CHECK (status IN ('created','reserved','running','succeeded','failed','timed_out','cancelled','interrupted')),
    role TEXT NOT NULL,
    model_profile TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    objective_id TEXT REFERENCES objectives(id),
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','resolved','cancelled','superseded')),
    resolution_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_objective ON tasks(objective_id);
CREATE INDEX IF NOT EXISTS ix_events_created ON events(created_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class Store:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=15000")
            yield db

    def initialize(self) -> None:
        with self.transaction() as db:
            db.executescript(SCHEMA)
        self.path.chmod(0o600)

    @staticmethod
    def emit(db: sqlite3.Connection, kind: str, subject: str, data: dict | None = None) -> None:
        db.execute("INSERT INTO events(kind,subject_id,data_json,created_at) VALUES (?,?,?,?)",
                   (kind, subject, json.dumps(data or {}, sort_keys=True), now()))

    def add_project(self, name: str, repository: str, priority: int = 50) -> str:
        if not name.strip() or not repository.strip() or not 0 <= priority <= 100:
            raise ValueError("name, repository and priority (0..100) are required")
        project_id = new_id('prj')
        with self.transaction() as db:
            db.execute("INSERT INTO projects(id,name,repository,priority,created_at) VALUES (?,?,?,?,?)",
                       (project_id, name.strip(), repository.strip(), priority, now()))
            self.emit(db, 'project_created', project_id, {'name': name.strip()})
        return project_id

    def add_objective(self, project_id: str, title: str, description: str = '') -> str:
        if not title.strip():
            raise ValueError('title is required')
        objective_id = new_id('obj')
        with self.transaction() as db:
            db.execute("INSERT INTO objectives(id,project_id,title,description,created_at) VALUES (?,?,?,?,?)",
                       (objective_id, project_id, title.strip(), description, now()))
            self.emit(db, 'objective_created', objective_id, {'project_id': project_id})
        return objective_id

    def list_projects(self) -> list[tuple]:
        with self.transaction() as db:
            return db.execute("SELECT id,name,status,priority,repository FROM projects ORDER BY priority DESC,name").fetchall()

    def list_objectives(self) -> list[tuple]:
        with self.transaction() as db:
            return db.execute("SELECT id,project_id,title,status FROM objectives ORDER BY created_at,id").fetchall()

    def events(self, limit: int = 20) -> list[tuple]:
        with self.transaction() as db:
            return db.execute("SELECT id,created_at,kind,subject_id FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def status(self) -> dict:
        with self.transaction() as db:
            counts = {}
            for table in ('projects', 'objectives', 'tasks', 'executions'):
                counts[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            counts['open_decisions'] = db.execute("SELECT count(*) FROM decisions WHERE status='open'").fetchone()[0]
            return counts
