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

MIGRATION_2 = """
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    repository TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    branch TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    git_marker TEXT NOT NULL,
    created_at TEXT NOT NULL
);
ALTER TABLE executions ADD COLUMN task_id TEXT REFERENCES tasks(id);
ALTER TABLE executions ADD COLUMN plan_revision_id TEXT REFERENCES plan_revisions(id);
ALTER TABLE executions ADD COLUMN workspace_id TEXT REFERENCES workspaces(id);
ALTER TABLE executions ADD COLUMN provider TEXT;
ALTER TABLE executions ADD COLUMN model_id TEXT;
ALTER TABLE executions ADD COLUMN actor TEXT;
ALTER TABLE executions ADD COLUMN started_at TEXT;
ALTER TABLE executions ADD COLUMN deadline TEXT;
ALTER TABLE executions ADD COLUMN pid INTEGER;
ALTER TABLE executions ADD COLUMN pid_identity TEXT;
ALTER TABLE executions ADD COLUMN supervisor_pid INTEGER;
ALTER TABLE executions ADD COLUMN supervisor_identity TEXT;
ALTER TABLE executions ADD COLUMN exit_code INTEGER;
ALTER TABLE executions ADD COLUMN outcome_reason TEXT;
ALTER TABLE executions ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE executions ADD COLUMN base_commit TEXT;
ALTER TABLE executions ADD COLUMN head_commit TEXT;
ALTER TABLE executions ADD COLUMN artifacts_json TEXT;
ALTER TABLE executions ADD COLUMN request_sha256 TEXT;
ALTER TABLE executions ADD COLUMN result_json TEXT;
ALTER TABLE executions ADD COLUMN sandbox_profile TEXT;
ALTER TABLE executions ADD COLUMN provider_version TEXT;
CREATE UNIQUE INDEX ux_execution_task_active ON executions(task_id)
    WHERE status IN ('created','reserved','running');
CREATE UNIQUE INDEX ux_execution_workspace_active ON executions(workspace_id)
    WHERE status IN ('created','reserved','running');
UPDATE executions SET task_id=(SELECT task_id FROM task_revisions WHERE id=task_revision_id),
    plan_revision_id=(SELECT plan_revision_id FROM authorizations WHERE id=authorization_id);
"""

MIGRATION_3 = """
CREATE TABLE scheduler_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    enabled INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    global_limit INTEGER NOT NULL DEFAULT 2 CHECK(global_limit>0),
    project_limit INTEGER NOT NULL DEFAULT 2 CHECK(project_limit>0),
    owner_id TEXT, owner_pid INTEGER, owner_identity TEXT,
    heartbeat TEXT, expires_at TEXT, last_tick TEXT
);
INSERT INTO scheduler_state(id) VALUES (1);
CREATE TABLE scheduler_projects (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    paused INTEGER NOT NULL DEFAULT 0,
    credit INTEGER NOT NULL DEFAULT 0,
    backoff_until TEXT
);
CREATE TABLE scheduler_dispatches (
    execution_id TEXT PRIMARY KEY REFERENCES executions(id),
    owner_id TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    worker_pid INTEGER,
    worker_identity TEXT,
    worker_log TEXT,
    acknowledged_at TEXT
);
CREATE TABLE task_checkpoints (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    task_revision_id TEXT NOT NULL REFERENCES task_revisions(id),
    execution_id TEXT NOT NULL UNIQUE REFERENCES executions(id),
    version INTEGER NOT NULL CHECK(version>0),
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id,version)
);
CREATE TABLE task_dispatch_holds (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    task_revision_id TEXT NOT NULL REFERENCES task_revisions(id),
    execution_id TEXT NOT NULL UNIQUE REFERENCES executions(id),
    reason TEXT NOT NULL CHECK(reason IN ('awaiting_review','requires_manual_retry')),
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT, released_by TEXT, release_reason TEXT
);
CREATE TABLE task_retry_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    task_revision_id TEXT NOT NULL REFERENCES task_revisions(id),
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id),
    authorization_id TEXT NOT NULL REFERENCES authorizations(id),
    model_profile TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
    continuity_json TEXT,
    superseded_at TEXT,
    execution_id TEXT REFERENCES executions(id)
);
CREATE UNIQUE INDEX ux_task_retry_pending ON task_retry_requests(task_id) WHERE execution_id IS NULL AND superseded_at IS NULL;
CREATE TABLE scheduler_skips (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    task_revision_id TEXT NOT NULL REFERENCES task_revisions(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    reasons_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
ALTER TABLE executions ADD COLUMN scheduler_owner TEXT;
ALTER TABLE executions ADD COLUMN continuity_json TEXT;
"""

MIGRATIONS = (MIGRATION_1, MIGRATION_2, MIGRATION_3)


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
