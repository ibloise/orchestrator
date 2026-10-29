# SPEC-003 — Multiproject Scheduler and Execution Continuity

**Project:** Homeserver Orchestrator\
**Status:** APPROVED FOR IMPLEMENTATION — FakeAdapter/dry-run only while SPEC-002 live gate remains closed\
**Target:** v0.3.x\
**Prerequisites:** SPEC-001 accepted; SPEC-002 accepted, including its enforced isolation gate; repository `AGENTS.md`; decisions D1–D9 (especially D2–D7).\
**Scope:** Deterministic, unattended dispatch of previously defined and approved tasks through the existing SPEC-002 Executor; resource limits; persistent scheduling; checkpoints; restart reconciliation. **No autonomous planning or verification.**

## 1. Purpose

Make the orchestrator useful across several projects without an operator manually invoking each agent. The Scheduler must choose eligible TaskRevisions, reserve limited execution capacity, dispatch them through the SPEC-002 execution service, track progress, and continue useful work when another task is blocked. It must survive crashes and restarts without duplicated active work or unsafe retries.

**Milestone:** An operator approves tasks in two separate projects, starts a scheduling service, and observes safe automatic dispatch subject to dependencies, authorizations, project priorities, and concurrency limits.

This SPEC does not authorize the Scheduler to create plans, change task definitions, approve work, mark implementation tasks `done` on provider success, merge code, or bypass the security boundary.

## 2. Integration baseline and non-negotiable constraints

SPEC-001 owns Project/Objective, PlanRevision/TaskRevision, the authoritative authorization rules, dependencies, human Decisions, computed eligibility, transactional SQLite state and the CLI. SPEC-002 owns the trusted Executor, provider adapter, workspace isolation, ExecutionRecord lifecycle, process cancellation, log/artifact capture and manual restart reconciliation.

Before coding, inspect the **actual accepted** SPEC-001/002 schema, API and migrations. Extend those implementations; do not add an alternative eligibility engine or a second process launcher. If SPEC-002 is not accepted or its live-runner isolation probe has not passed, the Scheduler may operate **only with FakeAdapter or in dry-run mode** and must fail closed for live dispatch.

Keep Python 3.12+, stdlib SQLite, pytest and Ruff. A single host, one scheduler owner and a modest number of local workers are assumed. Do not add Celery, Redis, RabbitMQ, PostgreSQL, a distributed consensus service, or an LLM-based scheduling policy.

## 3. Functional boundaries

### Included

- Background scheduling loop plus a single-cycle `tick`/dry-run command for diagnosis.
- Discovery of authorized, eligible TaskRevisions across active projects.
- Deterministic cross-project fairness and task ordering; a configurable global and per-project concurrency limit (defaults: **2 / 2**).
- Transactional dispatch claims and resource reservations; worktree ownership enforced by SPEC-002.
- Completion observation, slot release, error categorization and blocked/uncertain-work handling.
- Lightweight task checkpoints and provider-independent continuity packages.
- Startup reconciliation and safe handling of expired claims, interrupted executions and process termination.
- Administrative start/stop/status/pause/resume controls, observability and tests.

### Excluded

- Planner, autonomous plan revisions, creation of corrective tasks, delegated inter-agent assistance, semantic memory, RAG and provider routing by an LLM.
- Reviewer, automated verification, automatic task completion on successful execution, automated Git merge/push/deploy (SPEC-004).
- Notification channels, web dashboard, general-purpose workflow engine or distributed scheduling (later SPECs).
- Cross-host orchestration and preemption of arbitrary live agent work merely to improve fairness.

## 4. Scheduling model

### 4.1 Units and terminology

- **TaskRevision:** immutable work instructions chosen by the current effective approved PlanRevision.
- **Eligibility:** computed by SPEC-001; must be checked at selection **and again transactionally at reservation/launch**.
- **Dispatch claim:** short-lived exclusive reservation to start one execution. It is not an assertion that the agent has launched.
- **Execution:** one attempt managed by SPEC-002, with its own durable lifecycle and artifacts.
- **Slot:** a concurrency allocation held from successful claim until terminal/reconciled execution. A human-blocked task without a running process holds no slot.
- **Checkpoint:** verified references plus a concise continuity summary, *not* proof of completion.

Scheduler selection is not a new persisted task status. `READY` and `BLOCKED` are computed projections; authoritative Task and Execution states remain in their existing services.

### 4.2 Eligibility and exclusions

Only consider TaskRevisions returned as eligible by SPEC-001 for the effective approved plan. Additionally exclude:

- tasks or projects paused by an operator;
- tasks with an active/in-doubt modifying execution or conflicting workspace lease;
- tasks whose current revision/authorization changed since candidate collection;
- tasks with a pending manual inspection requirement after an interrupted/uncertain execution;
- tasks which completed an execution and are awaiting SPEC-004 verification or an explicit operator decision about continuation;
- tasks disallowed by runtime security prerequisites, provider configuration or capacity limits.

A project or Decision blocked for one task must not block unrelated eligible work. Persist explanatory reason codes so operators can inspect why a candidate was skipped.

**Important:** because SPEC-001 treats a pending Task as eligible, eligibility alone is insufficient to prevent repeated executions. SPEC-003 must introduce a durable **dispatch disposition** (e.g., `never_dispatched`, `awaiting_review`, `requires_manual_retry`, `ready_for_retry`) or equivalent derived from durable execution/hold records. Successful provider exit transitions to `awaiting_review`, *not* back to the ready queue. A failed or interrupted execution is not automatically requeued without an explicit, narrowly authorized retry policy; the MVP default is operator inspection and retry.

### 4.3 Fairness and ordering

Use a deterministic, documented policy with two stages:

1. Choose a project with available eligible work using **weighted round-robin** (or weighted deficit round-robin) with persisted per-project scheduling credits/cursor. Project priority maps to small configurable positive weights (e.g., low=1, normal=2, high=3); avoid hard-coded priority interpretation before reading SPEC-001.
2. Within that project, select by task priority if the approved task definition supports it, then `ready_since`/stable creation order and task ID as a tie-breaker. If no task-priority field exists, use stable creation order rather than adding elaborate scoring.

Weights bias allocation over time, not a strict guarantee for every two-slot snapshot. Preserve credits across Scheduler restarts; a paused/ineligible project should not accumulate unlimited credit. No eligible project should starve indefinitely when executions finish and capacity is available. Explicit priority changes affect future dispatches, not running jobs.

Document the exact algorithm and tie-breakers; unit-test a known sequence. No random ordering and no LLM calls inside the scheduling tick.

### 4.4 Resource limits

Default `global_concurrency=2` and `per_project_concurrency=2`, configurable at runtime startup. Ensure the scheduler never exceeds these limits under concurrent CLI invocations or after restart. Only dispatch a task if its configured provider/model and security profile are available and its SPEC-002 workspace lock can be acquired.

Do not invent detailed CPU/RAM/token quotas. Provider-specific rate limits may temporarily defer a task with a timestamp/reason, but must not cause unlimited tight-loop retries. Use a short backoff on dispatch failures.

## 5. Scheduler process and lifecycle

Run one scheduler service per database (single-host assumption). Use a **durable owner lease** or OS lock plus a database claim to prevent two scheduling loops from dispatching simultaneously. The lease must include an owner identifier, expiry and heartbeat; an expired lease may be reclaimed only after reconciling active executions. Do not assume a stale PID alone proves the previous agent stopped.

At each tick:

1. Reconcile prior dispatch claims and terminal executions.
2. Determine global/project slot availability from durable state.
3. Query current eligible, not-held TaskRevisions.
4. Choose candidate(s) with the fairness policy.
5. Atomically revalidate the effective approved revision, authorization, dependencies, holds, slot limits and workspace exclusivity; create a dispatch claim tied to immutable IDs.
6. Invoke the existing SPEC-002 Execution Service via its trusted public API (never by simulating human CLI commands). Record dispatch/launch outcome.
7. Observe completion, persist terminal result and release slot/lease; create `awaiting_review` or `requires_manual_retry` hold as applicable.
8. Sleep or wake on state changes and repeat. Polling is sufficient (configurable interval, e.g. 2–5 seconds); no external queue required.

Persist each reservation and event atomically. If process launch fails after reservation, record a failure and release the claim rather than leaving capacity permanently occupied. A task cannot have two active modifying executions, even under simultaneous `tick` and manual `run` calls. Reuse SPEC-002 execution locking where available.

### 5.1 Operator controls

Provide minimal CLI operations following existing argparse conventions:

- `orchestrator scheduler run` — long-running foreground service suitable for systemd user service later;
- `orchestrator scheduler tick [--dry-run]` — one observable scheduling cycle; dry-run must not reserve or launch;
- `orchestrator scheduler status` — owner, last tick, worker slots, active executions, holds and skipped reasons;
- `orchestrator scheduler pause [--project PROJECT_ID]` and `resume` — stop/start future dispatch only; active executions continue unless separately cancelled;
- `orchestrator scheduler retry TASK_ID --actor ... --reason ...` — explicitly release a retry hold after validating current authorization and workspace state;
- `orchestrator checkpoints show TASK_ID` — inspect continuity checkpoint.

Command names may be adapted to the existing CLI, but keep their semantics explicit. `scheduler run` must not daemonize itself; rely on an external supervisor when eventually deployed. A stop signal should cease new dispatch, wait briefly for in-progress launch transactions, and leave running executions owned by the Executor for controlled reconciliation.

## 6. Execution continuity and checkpoints

### 6.1 Durable checkpoint

On an execution reaching a terminal or safely suspended state, store a checkpoint attached to the Task and the exact TaskRevision. Minimum fields:

- task/revision/execution identifiers; checkpoint version and creation timestamp;
- workspace identity, base commit, observed head commit, and cleanliness/changed-path references;
- validated artifact/log references;
- concise `completed`, `pending`, `blockers`, `next_action` fields, derived from agent output where present and explicitly marked **unverified** until checked;
- observed process result and any pending manual review requirement.

Store structured metadata in SQLite; keep large logs or full context packages in the ArtifactStore. Checkpoints are **append-only/versioned**, and may be superseded but not silently overwritten. No secrets or full chat transcripts in checkpoints.

### 6.2 Cross-model continuation

An explicit retry or follow-up invocation may run with a different logical model profile (Luna/Sol/Astra) through the same SPEC-002 adapter contract. Build a minimal continuity input from: authorized TaskRevision, current plan/decisions, validated workspace/commit state, and the latest applicable checkpoint. Include previous agent summaries only as untrusted context with provenance.

Do not rely on Codex session-resume features. A complete Context Builder, dynamic retrieval and automated cross-role delegation are deferred to SPEC-005; SPEC-003 needs only enough context to make a deliberate retry coherent.

### 6.3 Uncertain outcomes and restart reconciliation

On scheduler or host restart:

- Determine whether previously launched processes still exist and whether the SPEC-002 supervisor can prove their identity; avoid PID reuse mistakes.
- Recover terminal results and release stale claims only after validating the execution record and workspace state.
- If a process is alive but not safely adoptable by the new supervisor, do not launch a second writer; retain a blocking hold until it stops or is handled explicitly.
- If final outcome is unknown, mark `interrupted`/`uncertain`, preserve artifacts/worktree, create a manual-inspection hold, and **never automatically replay** the action.
- A recorded successful process without verified completion remains `awaiting_review`.

Reconciliation must be idempotent and auditable. Power loss or abrupt termination during a database transaction must not create duplicate active executions or silently permit a second write owner.

## 7. Authorization, security and human boundaries

- Recheck authorization and effective PlanRevision immediately before launch; a queued claim does not grant continuing authority after revocation.
- On pause: stop new dispatch; do not automatically kill running work. On cancellation or security revocation: invoke SPEC-002 policy for controlled stop/suspension and reconcile afterward.
- The Scheduler must not access or expose provider credentials, circumvent sandbox checks, invoke unrestricted shells as agents, or write into worktrees itself. It supplies typed requests to the trusted Executor.
- Checkpoints and logs are untrusted inputs; they cannot modify policy, role privileges, eligible task definitions or approvals.
- Scheduled execution uses the same isolation gate and negative-probe requirements as manually invoked SPEC-002 execution.
- Integration and promotion of code remain outside Scheduler authority (SPEC-004).

## 8. Persistence, migrations and events

Use sequential non-destructive migrations over the accepted SPEC-001/002 schema. Add only the minimum needed tables/fields, likely:

- `scheduler_leases` (single owner/heartbeat), with unique owner scope;
- `dispatch_claims` or a safe extension of existing execution reservation records;
- `task_dispatch_holds` (reason, task_revision_id, related execution, actor, release state);
- `task_checkpoints` (append-only checkpoint metadata and artifact references);
- small persisted fairness state, project pause state, and last-run timestamps (where existing schema does not already provide them).

Prefer reuse of SPEC-002 reservation/Execution state to duplicate tables. IDs must link to immutable TaskRevision/PlanRevision/Authorization records. Keep foreign keys enabled and migrations backward-compatible. Never store a second authoritative copy of eligibility.

Record scheduling claims, dispatch decisions, holds, retries, pauses/resumes, reconciliation and checkpoint creation in the existing EventLog. Mutations and corresponding events must be committed in one transaction. Events should include project, task, execution and correlation identifiers when available. Logs live in ArtifactStore, not event JSON.

## 9. Testing and acceptance scenarios

All mandatory tests run **offline** with FakeAdapter, temporary Git repositories, SQLite databases and artifacts. Run `pytest -q` and `ruff check .`. Do not require live Codex or provider credentials in CI.

- **AT-01 — Regression:** SPEC-001/002 tests and migrations continue to pass; manual run still works with its existing guards.
- **AT-02 — Multi-project dispatch:** approved eligible tasks in projects A and B are automatically dispatched; an unapproved project C remains blocked; only configured slots are used.
- **AT-03 — Dependencies:** B depends on A. B is not dispatched while A is pending, running, or merely `succeeded` at Execution level. After A becomes `done` through the authorized SPEC-001 administrative path, B becomes eligible for dispatch. This tests the SPEC-003/004 boundary explicitly.
- **AT-04 — Fairness:** two continuously eligible projects of different weights both receive dispatch opportunities across repeated slot releases; ordering is deterministic after restart.
- **AT-05 — Concurrency:** global/project limits are respected; two simultaneous scheduler ticks and concurrent manual execution cannot reserve the same task/worktree or oversubscribe slots.
- **AT-06 — Holds:** after a successful provider run, the Task remains pending and is held `awaiting_review`; repeated ticks do not re-run it. Failed/interrupted attempts default to explicit retry, not automatic replay.
- **AT-07 — Authorization change:** revocation, plan supersession, decision block and project pause between selection and launch prevent launch. Unrelated work continues.
- **AT-08 — Restart:** crash after claim and before launch; crash while running; and terminal result before scheduler acknowledgement each reconcile conservatively and idempotently with no duplicate launch.
- **AT-09 — Checkpoint continuity:** record a versioned checkpoint, restart, inspect it, then explicitly retry using a different configured model; no native session resume required. Unverified claims are labeled as such.
- **AT-10 — CLI end-to-end:** create/approve work with SPEC-001 commands, start scheduler `tick` with FakeAdapter, inspect events/active slots/holds/checkpoints, pause/resume, and retry safely.
- **AT-11 — Failure/atomicity:** simulate database failure, adapter launch failure, expired lease and artifact collection failure; ensure no misleading successful execution, leaked slot or forgotten audit event.
- **AT-12 — Security inheritance:** a disabled/failed SPEC-002 isolation gate prevents live scheduled dispatch; fake mode still works. Scheduler cannot expose or modify control-plane secrets from the agent runtime.
- **AT-13 — Optional live smoke test:** only after SPEC-002 live isolation has passed and operator explicitly approves: run two independent, trivial tasks in disposable repositories, inspect logs and resource limits, and verify the Scheduler does not complete tasks automatically.

### Definition of Done

- AT-01 through AT-12 pass offline. AT-13 is documented and optional for development, required before using unattended **live** dispatch routinely.
- Scheduler can operate safely on two projects using FakeAdapter and recorded state; can be shut down and restarted without unsafe replay.
- Existing authorization, worktree locking, supervision and cancellation logic are reused rather than reimplemented.
- Every successful execution becomes inspectable and **awaits verification/review**, never implicitly marking the Task `done` or releasing dependent tasks.
- Fairness and limit decisions are deterministic and testable; scheduling reasons are visible via CLI/events.
- README documents startup, lifecycle, `tick`, pause/resume/retry, config, recovery and known limitations.
- No Planner, Reviewer, automatic merge/push, notification bus, web service or general-purpose multi-agent framework is introduced.
- Codex provides a concise implementation/verification report and does not commit, push, merge or deploy without specific operator instruction.

## 10. Recommended implementation sequence (internal tasks, not separate SPECs)

1. **Schema and invariants:** migrations, scheduler lease, holds, checkpoint storage, atomic claims, events.
2. **Deterministic selection:** eligibility adapter and weighted fair scheduling with unit tests.
3. **Execution coupling:** invoke SPEC-002 via its stable service API; capacity accounting and dispatch-state handling.
4. **Continuity and recovery:** restart reconciliation, conservative retries and checkpoint assembly.
5. **CLI and end-to-end:** `tick`, run/status/pause/resume/retry, docs and offline scenarios.

Avoid large module proliferation. A `scheduler.py` service with small supporting persistence/domain helpers is sufficient unless the current codebase provides a better fit.

## 11. Human review points before implementation

1. **SPEC-002 actual contract:** confirm accepted Executor reservation, cancellation, isolation and restart API. Reuse instead of designing a second runner.
2. **Scheduler retry default:** confirm that `succeeded` and `failed` executions both require review/explicit retry until SPEC-004 exists; no endless autonomous attempts.
3. **Fairness policy:** confirm weighted round-robin with initial 2 global/2 per-project slots; task-level priority optional if already supported.
4. **System startup:** CLI foreground service for now; a user `systemd` unit may be documented but should not be installed/enabled automatically.
5. **Self-development:** do not let the Scheduler update its own running installation or protected state; the orchestrator repository may be treated as an ordinary *separate development workspace* only with explicit approval and later SPEC-004 verification.

**Current status: APPROVED FOR IMPLEMENTATION by the operator. Do not enable unattended live Codex dispatch before SPEC-002's security gate and this SPEC's recovery and concurrency tests pass.**
