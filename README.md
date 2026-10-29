# Homeserver Orchestrator — milestone 0 (bootstrap)

A minimal, auditable control-plane foundation for the D1–D9 supervised multi-project orchestrator.

## What works
- Python package with administrative CLI.
- SQLite schema for projects, objectives, versioned plans/tasks, authorizations, executions, decisions, events and notification outbox.
- Project/objective creation with audit events committed in the same transaction.
- Automated tests.

## Not implemented yet
**This scaffold cannot run agents.** The Executor, sandbox boundaries, model adapter, Scheduler, and enforcement of authorizations are intentionally absent. Schema entities do **not** imply operational features. Do not expose the database or executor privileges to LLM processes.

## Setup

```bash
cd orchestrator-mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
orchestrator init
orchestrator projects add bioenv /path/to/repository --priority 60
orchestrator projects list
orchestrator status
orchestrator events
```

Default database: `${XDG_STATE_HOME:-~/.local/state}/orchestrator/orchestrator.db`. Override with `--db` before the subcommand.

## Proposed next increments
1. Domain services: revisions, approvals, tasks, dependency cycle checking, events.
2. Deterministic eligibility and Scheduler with transactional reservations and recovery.
3. Privilege boundary and workspace manager with controlled Git operations.
4. Codex CLI adapter and structured results; initially allow explicit human-triggered smoke tasks only.
5. Verification/integration policies, correction budgets and notification dispatcher.

## Key safeguards before running any agent
- Enforce separate OS-level permissions for the control-plane DB and provider credentials.
- Verify actual sandbox/network behavior of installed Codex CLI.
- Restrict Git operations, Docker socket exposure and workspace mounts.
- Require an authorized plan revision before dispatch.
