# SPEC-002 — Secure Codex Execution and Workspace Lifecycle

**Project:** Homeserver Orchestrator\
**Status:** APPROVED FOR IMPLEMENTATION — live execution security gate remains closed\
**Target:** v0.2.x\
**Prerequisites:** SPEC-001 implemented and accepted; `AGENTS.md`; architectural decisions D1–D9 (especially D2, D3, D5–D7)\
**Scope:** Controlled, *manually initiated* execution of one approved TaskRevision through Codex CLI, using an isolated Git worktree. **No autonomous scheduling.**

## 1. Purpose

Turn the SPEC-001 control plane into a safe, usable single-task executor. An authorized operator can select an eligible task, invoke Codex CLI with a configured logical model profile and role, observe completion, and inspect a durable record of the process and the changes produced. Runs must not be able to modify the control-plane database, administrative credentials, reference repositories, or unrelated projects.

The initial implementation must be **small and testable**. This SPEC must not implement the Scheduler, Planner, Reviewer, automated merges, or any general-purpose agent framework.

## 2. Baseline and integration constraints

SPEC-001 provides Project, Objective, Plan/PlanRevision, Task/TaskRevision, authorization, decisions, computed eligibility, CLI, transactional SQLite state, and an event log. The bootstrap already contains an `executions` table with a basic set of states. Inspect the actual branch and its migrations before implementation; preserve data and existing CLI behavior. Do not assume that the copy of the bootstrap examined during specification drafting exactly matches the approved SPEC-001 implementation.

Reuse the existing IDs and domain service interfaces where possible. Introduce **sequential, non-destructive SQLite migrations** for new execution fields/tables. Do not create a parallel source of truth for task authorization or eligibility.

## 3. Required end-to-end operator workflow

1. An operator creates and approves a plan with at least one TaskRevision through SPEC-001.
2. The operator explicitly requests execution of an eligible Task (`run <task-id>` or equivalent).
3. The control plane rechecks eligibility, effective approved revision, authorization, and workspace ownership **immediately before launch**.
4. A trusted workspace manager creates or reuses a task-specific Git worktree and branch from a recorded base commit.
5. A trusted supervisor constructs a minimal execution request, chooses a configured role and logical model profile, applies an enforced isolation profile, and starts Codex CLI without shell command interpolation.
6. The supervisor captures exit status, structured provider events where supported, stdout/stderr, timestamps, limits, and resulting Git state. It stores raw artifacts outside SQLite and references them by stable IDs/paths.
7. An `ExecutionRecord` becomes terminal. The task is **not** marked verified, integrated or done. A successful run means only that the process completed successfully; SPEC-004 owns verification and closure.
8. The operator can inspect `execution show`, `execution logs`, and a summary of Git changes without granting the agent direct access to the orchestration database.

The same flow must work with a **fake provider adapter** in tests, without Codex credentials or network access.

## 4. Scope and explicit non-goals

### Included

- One manual invocation at a time per task; no automatic dispatch loop.
- `CodexCliAdapter`, with a narrow provider-neutral interface and a fake adapter.
- Configured logical model profiles (`luna`, `sol`, `astra`) mapped to **operator-supplied, verified** Codex model IDs and supported settings. No hard-coded assumptions about model availability.
- A small role catalog (`implementer`, plus `inspect` if useful); role instructions are versioned inputs rather than privileged policies.
- A durable Execution lifecycle, process supervision, deadlines, safe cancellation and restart reconciliation.
- Git worktree creation, reuse, inspection and conservative cleanup.
- Separation of trust domains and explicit sandbox capability checks.
- Operator-facing CLI and deterministic tests.

### Excluded

Scheduler loop, task priority/fairness, concurrent multi-task dispatch, autonomous corrective tasks, delegation, native conversation continuity, Context Builder with dynamic retrieval, semantic memory, automated verification/Reviewer, automatic Git integration, Git push, deploy, notifications, web API, general secrets broker, unbounded Docker execution, and unattended replanning. SPEC-003 will own dispatch and checkpoints; SPEC-004 will own test-driven verification and integration.

## 5. Execution domain contract

### 5.1 Request

Define a small typed `ExecutionRequest` (dataclass or equivalent) containing:

- `execution_id`, `task_id`, immutable `task_revision_id`, `plan_revision_id`, and `authorization_id`;
- `project_id` and approved repository reference;
- `role`, logical `model_profile`, resolved provider/model identifier;
- immutable instruction input and acceptance criteria from the authorized task revision;
- workspace identity, base/head commit references, sandbox profile and deadline;
- identifiers/hash references for any additional input files.

Generate the final instructions from trusted application templates plus task data. Record the exact effective request in a controlled artifact with a checksum (do not store credentials). Never rely on the provider to enforce authorization.

### 5.2 Provider interface

Define one narrow adapter interface, conceptually `run(request, workspace, event_sink, cancellation) -> ProviderResult`. A streaming callback is optional if it complicates the first implementation. The adapter must expose actual subprocess exit status and observed errors separately from model-declared outcomes. Implement:

- `CodexCliAdapter`, invoking a **locally installed** Codex CLI through `subprocess` with argument arrays, not `shell=True`;
- `FakeAdapter`, deterministic and credential-free for tests.

Do not build plugin discovery or a generalized provider registry. An explicit mapping is sufficient.

### 5.3 Result

Separate:

1. **Process outcome:** observed exit code, timeout, cancellation, signal and process lifecycle.
2. **Agent declaration:** summary and structured JSON, when valid and available; potentially untrusted.
3. **Workspace evidence:** base/head commit, tracked/untracked changes, changed paths and artifact references.

An invalid or missing structured answer must not cause silent success. Preserve raw output for diagnosis, redact known secrets where feasible, and mark the declaration unavailable/invalid. Never report a successful implementation solely from provider text.

## 6. Execution lifecycle, persistence and reconciliation

Use the existing Execution states where practical:

`created → reserved → running → succeeded | failed | timed_out | cancelled | interrupted`.

Required rules:

- Creation/reservation is transactional, bound to the **current effective approved** plan/task revision and active authorization. Revalidate before running.
- At most one active modifying execution for a Task/worktree. In SPEC-002 enforce by transactional reservation or a durable unique lock; do not rely on an in-memory boolean.
- Persist timestamps, actual provider/model, role, workspace identifier, process PID/start metadata (if useful), exit code, outcome reason, deadline, base/head commits, and artifact references. Use minimal supporting tables or JSON fields where justified.
- State transitions and audit events commit atomically. Terminal records must not be rewritten except for explicitly modeled reconciliation metadata.
- `run` must refuse paused/cancelled/blocked/out-of-plan tasks or revoked authorizations.
- Interrupting a process never marks the Task `done`. Task stays available for inspected follow-up only if authorization and safety conditions still hold.
- On restart, an active record is reconciled with observable process/workspace state. If completion is uncertain, mark it `interrupted` (or a clearly documented uncertain result) and **do not automatically rerun**. The operator inspects and explicitly retries.
- Cancellation should first attempt graceful termination, then terminate the process group after a bounded grace period; record partial artifacts. Avoid killing unrelated processes.

SPEC-002 is single-task manual execution, not a concurrent scheduler; nevertheless the schema must not preclude SPEC-003.

## 7. Git workspace lifecycle

- Maintain an **operator-configured trusted repository root** and separate managed worktree root.
- Validate that repository paths resolve within approved roots; reject path traversal, symlinks escaping the root, incompatible ownership, unsupported repositories and collisions.
- Establish a task branch, e.g. `agent/<task-id>`, and record the starting commit. Check for pre-existing unmanaged branches/worktrees rather than overwriting them.
- Reuse the task worktree across explicitly invoked runs only after reconciling its state and ownership; never reset or discard user changes automatically.
- The agent may edit its task worktree but must not be able to write to the reference clone, other worktrees, or the orchestrator's state directory.
- Keep `git worktree` operations in a trusted workspace manager, not in agent-issued privileged tooling.
- Record `git status --porcelain` and changed paths before/after. Do not automatically commit, rebase, merge, push, force-push, or delete worktrees.
- Cleanup must be explicit, refuse dirty/unpreserved worktrees, and operate through validated Git worktree commands; automatic cleanup is not required here.

**Important:** Git worktrees provide operational isolation, not an operating-system security boundary. Shared Git metadata must be protected from untrusted agent processes; if the chosen sandbox cannot reliably protect it, stage a separate isolated checkout or equivalent with a trusted import/reconciliation step. Do not assert that a worktree alone prevents access to other branches or Git metadata.

## 8. Security boundary — hard release gate

D7=B requires a real separation between control-plane authority and untrusted agent execution.

### 8.1 Required guarantees

1. The agent cannot read or modify the control-plane SQLite database, administrative configuration, operator SSH keys or unrelated projects.
2. The agent cannot obtain a privileged Docker socket, invoke `sudo`, change host security settings or bypass its assigned filesystem boundaries.
3. Only the operator-approved workspace and explicitly required tool inputs are exposed.
4. The agent cannot grant itself expanded sandbox/network/file permissions through its output or repository files.
5. No direct access to deployment credentials, protected Git refs or remote push credentials is provided by default.
6. The runner fails **closed** if the intended sandbox or operating-system boundary is unavailable or cannot be verified.
7. Sandboxing is enforced by the actual runner/OS, **not** only by `AGENTS.md`, prompt text, file naming or JSON policy declarations.

### 8.2 Implementation guidance

Use an explicit isolated runtime identity, OS sandbox, restricted container, or comparable enforceable mechanism. The choice may be guided by the current Ubuntu/Codex installation, but document the threat boundary and verify it empirically. The Codex CLI sandbox can provide a complementary restriction, not the sole assumed control. Running both trusted control logic and arbitrary agent commands under an unrestricted shared UID is **not sufficient** for this SPEC.

Do not pass `--dangerously-bypass-approvals-and-sandbox`, use `danger-full-access`, or silently fall back to unrestricted execution. For unattended CLI invocation use only options actually supported by the installed Codex release; verify `codex exec --help` (and related help) before building command arguments. Pin/record the CLI version in execution metadata where practical. No fixed model IDs, output-event fields or CLI flags should be invented.

### 8.3 Authentication and network

Codex requires access to its provider, but the implementation must not expose long-lived control-plane credentials in agent-readable environment variables, mounted home directories, or logs. If the installed Codex CLI cannot operate under a boundary that meets this requirement, deliver the adapter behind a **disabled-by-default security gate** and report the blocker; do not relax the security model to pass a demonstration.

Network access should default to the least privilege compatible with provider execution. Document any network/credential trust that remains. The SPEC does not require a full credential proxy or custom networking infrastructure.

### 8.4 Security acceptance probes

Run negative tests using a fake agent/tool command under the **same sandbox profile** as actual executions. Attempt to read/write a sentinel control-plane DB or secret, access another project's workspace, reach a restricted path, invoke an unauthorized host capability and escape through symlinks. Access must be denied. Verify that denied actions produce auditable failures without modifying the protected files. Tests must avoid real secrets.

## 9. CLI and configuration

Expose minimal commands (exact syntax may follow existing argparse conventions):

- `orchestrator agents models` — list configured logical profiles and resolved provider/model, without revealing credentials.
- `orchestrator run <task-id> --role implementer --model sol` — manual authorized execution; **no implicit approval**.
- `orchestrator executions [--task <id>]` and `orchestrator execution show <id>`.
- `orchestrator execution logs <id>` — controlled access to execution artifacts.
- `orchestrator execution cancel <id>` — operator-initiated cancellation.
- `orchestrator workspace show <task-id>`; optionally `workspace cleanup <task-id>` with guards.
- `orchestrator executor doctor` — preflight diagnostics: Codex installed/version, model mapping configured, repository path, sandbox boundary, credential availability (without disclosing secrets), Git readiness.

A manually executed command should fail with a clear, actionable message if prerequisites are missing. Use an application-managed TOML config or current project conventions; never require credentials in repository-committed config. Provide a sanitized `config.example.toml` with **placeholders** for actual Codex model identifiers.

The CLI is an authorized operator interface, not a public API. It may not infer permission from arbitrary input files, prompts or environmental variables.

## 10. Testing and acceptance criteria

All automated acceptance tests must be runnable **offline with FakeAdapter**, temporary Git repos, temporary SQLite DBs and temporary artifact directories. Separate integration probes that require installed Codex credentials; never make the ordinary test suite depend on a live model.

- **AT-01 — SPEC-001 regression:** existing CLI, schema migrations, eligibility and all prior tests remain valid.
- **AT-02 — Approved task:** an eligible TaskRevision can be reserved and executed manually; recorded IDs match the effective approved revisions and authorization.
- **AT-03 — Authorization denied:** missing/revoked/superseded authorization, open blocking Decision, paused project or missing prerequisite prevents subprocess launch; an explanatory event/error is recorded where appropriate.
- **AT-04 — Workspace:** clean task worktree created from recorded base, reused safely, no clobbering of existing dirty content, protected root remains unchanged.
- **AT-05 — Provider contract:** FakeAdapter captures request, produces deterministic output and failure; Codex adapter command assembly is validated without network calls or unsafe shell interpolation.
- **AT-06 — Failure modes:** clean exit, nonzero exit, malformed/missing agent result, timeout and cancellation all produce correct terminal outcomes and artifacts without falsely completing a Task.
- **AT-07 — Transactional reservation:** attempts to reserve the same active task/worktree twice cannot produce concurrent modifying executions, including across multiple Store connections.
- **AT-08 — Restart:** simulate abrupt interruption, reopen Store, reconcile as interrupted/uncertain, preserve workspace and logs, and require explicit operator retry.
- **AT-09 — Isolation:** sentinel database/secrets, unrelated workspace and forbidden host operations are inaccessible from the real execution sandbox; no silent downgrade on failure.
- **AT-10 — CLI end-to-end:** register/approve a task using SPEC-001 CLI; invoke FakeAdapter through the same application execution service; inspect execution and Git diff; no task marked done.
- **AT-11 — Durability and audit:** execution transitions and events are atomic; partial database failures leave no misleading successful execution; artifact errors are surfaced.
- **AT-12 — Opt-in live smoke test:** with explicit operator approval and valid credentials, run Codex against a **disposable test repository** to make a trivial non-sensitive edit. Inspect logs, sandbox boundaries, and changed files. Do not require this probe to run during offline CI.

### Definition of Done

- AT-01 through AT-11 pass; AT-12 is documented and must be performed before enabling the live runner for routine work.
- `pytest -q`, `ruff check .`, and relevant migration tests pass.
- There is an enforceable, tested security boundary or the live adapter remains disabled and SPEC-002 is **not accepted as ready for production agent execution**.
- Execution attempts are traceable through CLI, SQLite and artifacts; success never sets Task `done` automatically.
- No Scheduler loop, Reviewer, merge automation, notifications or other future-SPEC work appears.
- README documents setup, config, `executor doctor`, manual run, cancellation, artifacts and known limits.
- Codex supplies a concise report listing files changed, tests observed, security probe results and unresolved blockers; no unsolicited commit/push/merge/deploy.

## 11. Implementation sequence (internal tasks, not separate SPECs)

1. **Persisted execution contract:** migrations, domain services, reservation, state transitions, events and fake adapter.
2. **Git workspace manager:** creation/reconciliation/inspection, base commit tracking and path validation.
3. **Supervisor:** subprocess lifecycle, timeouts, cancellation, artifacts and restart recovery.
4. **Isolation and Codex adapter:** operator-configured model profiles, runtime preflight and negative sandbox tests. Fail closed if security prerequisites cannot be met.
5. **CLI and end-to-end tests:** operator workflow, documentation, opt-in live smoke test.

Keep code structured as a modular monolith. A few cohesive modules (`execution.py`, `providers.py`, `workspace.py`, `security.py`) are reasonable, but do not build a generic service/container framework. Use no additional runtime dependencies without a concrete need.

## 12. Human review points

Before changing status to APPROVED, confirm:

1. **Execution boundary:** preferred concrete isolation strategy for `codexdev` on Ubuntu (separate OS runtime identity, OS sandbox or restricted container), including how provider authentication is kept outside the untrusted workspace.
2. **Worktree ownership:** whether the isolation method can safely share Git metadata; otherwise use isolated checkouts with trusted promotion of changes.
3. **CLI contract:** manual `run` and explicit operator retry are acceptable until SPEC-003.
4. **Model mapping:** actual supported model identifiers and Codex CLI flags must be discovered on the server, not assumed from logical names Luna/Sol/Astra.

**Current status: APPROVED FOR IMPLEMENTATION by the operator. Live Codex execution remains gated until the security acceptance probes and the explicitly authorized live smoke test have passed.**
