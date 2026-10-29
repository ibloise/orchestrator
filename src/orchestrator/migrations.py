"""Sequential, transactional upgrades, including databases from the bootstrap."""
from __future__ import annotations

import sqlite3

MIGRATION_1 = """
CREATE UNIQUE INDEX ux_plans_objective ON plans(objective_id);
CREATE UNIQUE INDEX ux_effective_plan ON plan_revisions(plan_id) WHERE status='approved';
ALTER TABLE authorizations ADD COLUMN actor TEXT;
ALTER TABLE authorizations ADD COLUMN reason TEXT;
ALTER TABLE authorizations ADD COLUMN revoked_by TEXT;
ALTER TABLE authorizations ADD COLUMN revocation_reason TEXT;
ALTER TABLE authorizations ADD COLUMN revoked_at TEXT;
ALTER TABLE decisions ADD COLUMN resolved_by TEXT;
ALTER TABLE decisions ADD COLUMN closed_at TEXT;
CREATE TABLE decision_tasks (
    decision_id TEXT NOT NULL REFERENCES decisions(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (decision_id, task_id)
);
CREATE TABLE decision_plans (
    decision_id TEXT NOT NULL REFERENCES decisions(id),
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id),
    PRIMARY KEY (decision_id, plan_revision_id)
);
CREATE TABLE manual_completions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    task_revision_id TEXT NOT NULL REFERENCES task_revisions(id),
    authorization_id TEXT NOT NULL REFERENCES authorizations(id),
    actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MIGRATIONS = (MIGRATION_1,)


def statements(db: sqlite3.Connection, script: str) -> None:
    """Avoid executescript's implicit COMMIT so DDL rolls back with its version."""
    for statement in script.split(';'):
        if statement.strip():
            db.execute(statement)


def migrate(db: sqlite3.Connection, bootstrap: str) -> None:
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > len(MIGRATIONS):
        raise ValueError('Database schema is newer than this application')
    statements(db, bootstrap)
    for index in range(version, len(MIGRATIONS)):
        statements(db, MIGRATIONS[index])
        db.execute(f'PRAGMA user_version={index + 1}')
