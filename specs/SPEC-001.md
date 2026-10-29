# SPEC-001 — Domain Core and Work Authorization

**Project:** Homeserver Orchestrator  
**Status:** PENDING HUMAN REVIEW  
**Target:** v0.1.x (increment on existing bootstrap)  
**Dependencies:** D1–D9 architectural decisions and repository `AGENTS.md`  
**Scope:** Control plane only; no agent execution

## 1. Purpose

Extend the existing Python/SQLite bootstrap into a usable, deterministic control plane. An operator must be able to register an objective, define a versioned plan and tasks, declare task dependencies, approve the plan, inspect executable eligibility, and manage decisions and revocations. All operations must be transactional, testable, and accessible through the CLI.

The implementation must **extend existing code and data** instead of replacing the bootstrap or creating a new framework.

## 2. Existing baseline

The bootstrap provides `src/orchestrator/store.py`, `src/orchestrator/cli.py`, SQLite schema, a project/objective CLI, event logging for those operations, and pytest tests. Tables already exist for plans, plan revisions, tasks, task revisions, plan-task references, dependencies, authorizations, executions, decisions, events and notification outbox. Most of these tables do **not** yet have domain operations. Inspect the actual repository before editing; this specification takes precedence if local files differ.

Preserve compatibility with the existing `init`, `status`, `projects add/list`, `objectives add/list`, and `events` commands.

## 3. Domain model and rules

### 3.1 Hierarchy

`Project → Objective → Plan → PlanRevision`, while `Task → TaskRevision` belongs to an Objective and is referenced by a PlanRevision. A Task may retain its identity across plan revisions. An execution, in future increments, must bind to an exact TaskRevision and authorization.

For this SPEC, each Objective has **one logical Plan** with multiple revisions; additional plans/alternatives are out of scope. A PlanRevision is a snapshot of an authorized task set (links to exact TaskRevisions). A TaskRevision has a numbered, immutable definition, and its Task has a stable ID.

### 3.2 Minimum definitions

PlanRevision definition: meaningful nonempty summary/description (JSON object or explicit fields) and ordered or unordered references to zero or more TaskRevisions while in draft; **at least one task** is required before proposing or approving it.

TaskRevision definition: nonempty description, list of one or more nonempty acceptance criteria, optional suggested role, optional complexity or model profile, and optional structured context references. Do not build a generic workflow schema or enforce provider-specific fields. Validate JSON structure in application code.

### 3.3 Revision and approval semantics

- Only `draft` plan revisions can have their task membership changed.
- `proposed`, `approved`, `rejected`, and `superseded` PlanRevisions are immutable; approval changes workflow status but not definition or membership.
- TaskRevisions are immutable once referenced by a proposed or approved PlanRevision. Prefer immutable TaskRevision content from creation to keep implementation straightforward; modifications create a new revision.
- One Objective may have at most one **effective approved** PlanRevision at a time. Approving a newer revision supersedes the previous effective revision in the same transaction.
- An authorization references the exact approved PlanRevision. Its minimum scope must identify the authorized objective/revision and permitted task revisions; further security permissions will be added later.
- A superseded plan's authorizations must no longer grant eligibility, even if historical authorization rows remain. A revoked authorization never grants eligibility.
- A proposal must not silently remove already-completed work or change the historical definitions of Tasks.
- Do not implement autonomous delegated replanning in SPEC-001. Store clear data structures allowing it later.

### 3.4 Dependencies

Dependencies are directed between Tasks in the **same Objective**. Reject self-dependency, references to nonexistent Tasks, cross-objective dependencies, and cycles (including indirect cycles). A Task is eligible only when all dependencies have status `done` and the other eligibility conditions hold. Dependencies on a Task not present in the effective plan must not be silently treated as fulfilled: mark the dependent task ineligible and explain why.

### 3.5 Decisions

Decisions have stable IDs, a type, question, related Objective and optionally affected Task(s) or PlanRevision. Start `open`; resolve with an explicit structured answer and a recorded actor. Support `cancelled` and `superseded` as terminal outcomes. Reject resolving a closed decision twice. An open decision blocks only explicitly linked Tasks/plan operations; it must not pause unrelated work.

Keep decision resolution and the event describing it in the same transaction. An approval is a privileged CLI/application operation; answering a free-form Decision must not implicitly grant permissions.

### 3.6 Task status and eligibility

For this increment, support `pending`, `done`, and `cancelled` operationally; retain existing schema statuses for future execution stages. Do not expose commands that impersonate actual agent execution or verification.

The CLI may mark a task `done` **only as an explicitly identified manual administrative completion**, after validating authorization and reason/evidence; record actor, reason and event. This is solely to seed and test dependency unlocks before SPEC-002/004. It is not a substitute for future verification and must not allow bypassing blocked or unauthorized tasks. Do not let `cancelled` tasks fulfill dependencies.

Eligibility is **computed**, not persisted as a second authoritative status. A TaskRevision is eligible if and only if:
1. Its Project and Objective are active (Objective must be active; Project active).
2. It is referenced by the Objective's effective approved PlanRevision.
3. There is a current, non-revoked authorization for that PlanRevision and TaskRevision.
4. Its Task is `pending` (not done/cancelled).
5. Every dependency is done and valid for the effective plan.
6. No open Decision explicitly blocks this Task or PlanRevision.

Eligibility output includes `eligible: true/false` and structured reason codes (e.g. `NO_APPROVAL`, `REVOKED`, `DEPENDENCY_PENDING`, `OPEN_DECISION`, `PROJECT_PAUSED`, `OBJECTIVE_INACTIVE`, `TASK_NOT_IN_EFFECTIVE_PLAN`). The Scheduler in SPEC-003 will consume this result.

## 4. Application services and persistence

Implement clear service boundaries without creating one class for each table or a generic repository framework. Domain validation belongs outside CLI handlers. Keep SQL parameterized. Use `PRAGMA foreign_keys=ON` per connection. State changes and corresponding audit events must commit atomically, including approval/supersession and decision resolution. Rejection or partial failure must leave both domain state and events unchanged.

Introduce a **minimal schema migration mechanism** (e.g. `PRAGMA user_version` with sequential migrations) before altering existing tables. `orchestrator init` must be safe to rerun on an existing bootstrap DB. Do not delete data or require users to reset SQLite. Migration code and rollback behavior require tests.

Store durable actor/reason information for authorization, revocation, manual completion and decision resolution. Restrict mutable content where needed with application checks and database constraints where practical. Keep history; do not hard-delete revisions, decisions or audit records.

## 5. CLI — required operations

Exact subcommand syntax can follow the existing argparse design, but support these user workflows:

- Create/list/show Plan and PlanRevisions for an Objective.
- Create/list/show Tasks and TaskRevisions under an Objective.
- Attach TaskRevisions to a **draft** PlanRevision.
- Add/list dependencies with cycle validation.
- Propose, approve and reject PlanRevisions, and revoke authorizations.
- Activate an Objective, and show task eligibility with blocking reasons.
- Create/list/show/resolve Decisions and link explicit blockers.
- Perform audited manual task completion in this bootstrap-only mode.
- Inspect recent events and system status using existing commands.

Use machine-readable JSON output for `eligibility` (or a `--json` option), alongside understandable text diagnostics. Error messages must distinguish validation failures from missing identifiers. No external API or background daemon in this SPEC.

## 6. Non-goals

No Scheduler loop, concurrent dispatch, Codex CLI invocation, worktrees, sandbox execution, provider configuration, autonomous planning/replanning, Reviewer, Git integration, notification delivery, web UI, PostgreSQL, Redis, or Celery. No general-purpose workflow engine or domain-specific project templates.

## 7. Acceptance tests

Use temporary SQLite databases; tests must not touch the developer's live state.

**AT-01 — Bootstrap regression:** existing project/objective commands and existing pytest tests still pass. `init` reruns without losing data.

**AT-02 — Plan lifecycle:** create Objective and draft PlanRevision, add TaskRevisions, propose, approve, and verify eligibility. Cannot approve an empty plan or modify immutable revisions.

**AT-03 — Revision continuity:** create PlanRevision 2, reuse one TaskRevision and replace another with a new revision. Approval supersedes revision 1; only revision 2's authorized task revisions are eligible. Historical revisions and events remain accessible.

**AT-04 — Dependency graph:** linear and branching dependencies behave correctly. Reject cycles, cross-objective links and self-dependency. Missing/out-of-plan prerequisites block with an explicit reason.

**AT-05 — Authorization:** no task is eligible before approval. Revocation removes eligibility without deleting history. Superseded authorization cannot authorize stale work.

**AT-06 — Decisions:** an open decision blocks linked tasks only; resolution unblocks them if all other conditions hold. Repeated resolution is rejected; actor and resolution persist.

**AT-07 — Manual completion:** explicitly authorized manual completion of a prerequisite unlocks dependents. It is audited and requires an actor and reason. Unauthorized completion is rejected.

**AT-08 — Transactions:** forced failures during approval, revocation and decision resolution cannot leave half-applied state or missing audit events.

**AT-09 — CLI integration:** the complete workflow is executable from the CLI without direct database manipulation; invalid input gives actionable errors.

**AT-10 — Restart:** close and reopen Store; revisions, authorizations, dependencies, decisions, history and eligibility are consistent.

## 8. Implementation guidance

Suggested sequence (internal tasks, **not separate SPECs**):
1. Migration and persistence refinements.
2. Plan/Task revision domain operations and validation.
3. Dependencies and eligibility service.
4. Authorization, Decision and audit operations.
5. CLI commands and end-to-end tests.

Prefer minimal modules that arise naturally (for example `domain.py`, `services.py`, `store.py`, `cli.py`). Do not split every entity into a separate file. Keep compatibility with Python 3.12+, pytest and Ruff. Use no new runtime dependency without justification.

## 9. Definition of Done

- All AT-01 through AT-10 pass.
- `pytest -q` and `ruff check .` succeed.
- Existing SQLite DB can be migrated nondestructively.
- A user can demonstrate the approved multi-task objective and its eligibility through CLI only.
- No implementation of later SPECs is introduced.
- Documentation explains the implemented commands, bootstrap manual-completion exception, and known limits.
- Codex provides a concise report of changes, executed checks, and unresolved risks; does not merge or deploy without authorization.

## 10. Human review points

This SPEC is **PENDING HUMAN REVIEW**. In particular, validate: (1) one logical Plan per Objective for the MVP; (2) manual administrative completion for dependency tests; (3) eligibility against only the effective approved revision. Once approved, Codex can implement it as one increment.
