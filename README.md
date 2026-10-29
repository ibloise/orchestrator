# Homeserver Orchestrator — Domain Core

A deterministic, auditable Python/SQLite control plane for supervised multi-project development.
The approved scope is [SPEC-001](specs/SPEC-001.md); repository rules are in [AGENTS.md](AGENTS.md).

## What works
- Python package with administrative CLI.
- SQLite schema for projects, objectives, versioned plans/tasks, authorizations, executions, decisions, events and notification outbox.
- Project/objective creation with audit events committed in the same transaction.
- Versioned plans and immutable task definitions, proposal, approval, rejection and revocation.
- Dependency cycle checks, explicit decision blockers and computed eligibility with reason codes.
- Audited manual administrative completion for dependency testing.
- Transactional, nondestructive schema upgrades from the bootstrap database.
- Automated tests.

## Not implemented yet
**This application cannot run agents.** The Executor, execution sandbox, model adapter,
Scheduler, automated verification and Git integration are absent. Authorization is checked
by control-plane eligibility and manual completion; OS isolation and execution permissions
remain future work. Do not expose the database or executor privileges to LLM processes.

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

Run `orchestrator init` on an existing bootstrap database to apply migrations. It is safe
to rerun and preserves existing data and audit history. Schema versioning uses SQLite
`user_version`; each upgrade and its version update commit together. Invalid legacy data
that violates new constraints causes an error and rollback, rather than deletion or repair.
Back up operational state before upgrading it. A newer unsupported schema is rejected.

## CLI workflow

This example uses a separate database. All IDs are printed by creation commands. `plans create`
creates the objective's single logical plan if necessary and returns a **plan revision ID**;
subsequent calls (or `plan-revisions create`) add empty draft revisions to that same plan.
`tasks create` creates a stable task and its first definition, returning a **task revision ID**.

```bash
orchestrator --db /tmp/orchestrator-demo.db init
project=$(orchestrator --db /tmp/orchestrator-demo.db projects add demo /repos/demo)
objective=$(orchestrator --db /tmp/orchestrator-demo.db objectives add "$project" 'Deliver feature')
orchestrator --db /tmp/orchestrator-demo.db objectives activate "$objective"
plan=$(orchestrator --db /tmp/orchestrator-demo.db plans create "$objective" 'Implement and test feature')
a=$(orchestrator --db /tmp/orchestrator-demo.db tasks create "$objective" 'Implement' \
  --definition '{"description":"Implement feature","acceptance_criteria":["Tests pass"]}')
b=$(orchestrator --db /tmp/orchestrator-demo.db tasks create "$objective" 'Document' \
  --definition '{"description":"Document feature","acceptance_criteria":["Example works"]}')
orchestrator --db /tmp/orchestrator-demo.db task-revisions show "$a"
orchestrator --db /tmp/orchestrator-demo.db task-revisions show "$b"
# Use task_id from these JSON responses (stable Task IDs, not revision IDs):
orchestrator --db /tmp/orchestrator-demo.db dependencies add TASK_B_ID TASK_A_ID
orchestrator --db /tmp/orchestrator-demo.db plan-revisions attach "$plan" "$a"
orchestrator --db /tmp/orchestrator-demo.db plan-revisions attach "$plan" "$b"
orchestrator --db /tmp/orchestrator-demo.db plan-revisions propose "$plan"
authorization=$(orchestrator --db /tmp/orchestrator-demo.db plan-revisions approve "$plan" \
  --actor operator --reason 'Reviewed task definitions and scope')
orchestrator --db /tmp/orchestrator-demo.db eligibility "$b"
orchestrator --db /tmp/orchestrator-demo.db tasks complete-manually "$a" \
  --actor operator --reason 'Inspected implementation and test output' \
  --evidence '{"test_result":"passed","reference":"manual inspection"}'
orchestrator --db /tmp/orchestrator-demo.db eligibility "$b"
orchestrator --db /tmp/orchestrator-demo.db events --json
```

The second task initially reports `DEPENDENCY_PENDING`, then becomes eligible after the
first task's manual completion. Completion is an explicit **bootstrap administrative
exception**, not agent execution or automated verification. It requires an active project
and objective, the effective approved revision, an active authorization, fulfilled
dependencies, no applicable open decisions, a nonempty actor/reason and structured evidence.
Actor names are operator-supplied audit labels, not authenticated identities; protect CLI
and database access using OS permissions. There are no CLI commands for simulated execution
or verification stages.

Additional operations (use `--help` on any command):

| Command | Purpose |
| --- | --- |
| `plans list OBJECTIVE`, `plans show PLAN` | Inspect the logical plan and its revisions |
| `plan-revisions create OBJECTIVE SUMMARY` | Create the next empty draft snapshot |
| `plan-revisions list PLAN`, `plan-revisions show REVISION` | Inspect exact plan history and task membership |
| `plan-revisions attach/detach PLAN_REV TASK_REV` | Change membership while draft |
| `plan-revisions propose PLAN_REV` | Freeze a nonempty snapshot |
| `plan-revisions approve/reject PLAN_REV --actor NAME --reason TEXT` | Review a proposal |
| `tasks list OBJECTIVE`, `tasks show TASK` | Inspect tasks, dependencies, revisions and manual evidence |
| `task-revisions create TASK --definition JSON` | Create an immutable definition for a pending task |
| `task-revisions list TASK`, `task-revisions show REVISION` | Inspect definition history |
| `dependencies add TASK PREREQUISITE`, `dependencies list TASK` | Manage an acyclic, same-objective graph |
| `tasks cancel TASK --actor NAME --reason TEXT` | Cancel pending work; it cannot fulfill dependencies |
| `decisions create OBJECTIVE TYPE QUESTION [--task TASK] [--plan-revision REV]` | Create a decision with explicit blockers; `--task` is repeatable |
| `decisions list OBJECTIVE`, `decisions show DECISION` | Inspect decisions and their blocker links |
| `decisions resolve/cancel/supersede DECISION --actor NAME --answer JSON` | Close once with a structured answer |
| `authorizations show AUTH`, `authorizations revoke AUTH --actor NAME --reason TEXT` | Inspect or revoke approval scope without deleting history |
| `eligibility TASK_REV` | JSON eligibility and structured blocking reasons |
| `events [--limit N] [--json]`, `status` | Existing system views, with optional complete audit payloads |

Task definitions require `description` and a nonempty list of `acceptance_criteria`.
Optional `suggested_role`, `complexity` and `model_profile` are nonempty text;
`context_references` is a list of JSON objects. JSON arguments must be objects, not filenames.
New inspection commands return JSON; creation commands return IDs. Existing commands
keep their original text output. Invalid identifiers and validation failures have distinct
diagnostics and exit code 2.

Approval requires a proposal. It atomically supersedes the previous approved revision
and creates authorization scoped to exact task revisions. Older revisions cannot replace
newer approvals. A new plan must explicitly reattach reused task definitions and preserve
completed work and its definitions. Only the effective revision grants eligibility;
revocation blocks work without reverting to an old approval. Reapproval of the same revision
is not supported: create and review a newer revision instead.

Dependencies use stable Task IDs and are live objective-level edges, not versioned plan
content. They may be added to pending tasks and immediately affect eligibility. A prerequisite
outside the effective plan blocks even if done; cancellation never fulfills it. Decision
links are declared at creation and cannot be edited. Task links block those tasks; plan links
block proposal/approval/rejection and eligibility for that plan. Unlinked decisions are
informational. Closing a decision never approves a plan or grants authorization.

Eligibility is recomputed from a consistent transaction on every request. Common reasons
include `NO_APPROVAL`, `NO_AUTHORIZATION`, `REVOKED`, `TASK_NOT_IN_EFFECTIVE_PLAN`,
`PROJECT_PAUSED`, `PROJECT_INACTIVE`, `OBJECTIVE_INACTIVE`, `TASK_NOT_PENDING`,
`DEPENDENCY_PENDING`, `DEPENDENCY_NOT_IN_EFFECTIVE_PLAN`, `DEPENDENCY_INVALID`, `DEPENDENCY_MISSING`,
and `OPEN_DECISION`. A result may contain multiple reasons. This interface is intended for
the future Scheduler; no background worker or external API is implemented.

## Proposed next increments
1. Privilege boundary and workspace manager with controlled Git operations.
2. Scheduler with transactional reservations and recovery.
3. Codex CLI adapter and structured results; initially allow explicit human-triggered smoke tasks only.
4. Verification/integration policies, correction budgets and notification dispatcher.

## Key safeguards before running any agent
- Enforce separate OS-level permissions for the control-plane DB and provider credentials.
- Verify actual sandbox/network behavior of installed Codex CLI.
- Restrict Git operations, Docker socket exposure and workspace mounts.
- Require an authorized plan revision before dispatch.
