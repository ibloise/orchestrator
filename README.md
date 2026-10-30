# Homeserver Orchestrator — Multiproject Scheduler MVP

A deterministic, auditable Python/SQLite control plane for supervised multi-project development.
The domain core is defined by [SPEC-001](specs/SPEC-001.md); repository rules are in [AGENTS.md](AGENTS.md).

[SPEC-002 — Secure Codex Execution and Workspace Lifecycle](specs/SPEC-002.md)
is approved for implementation. Its offline execution workflow is implemented; **live Codex
execution remains disabled** pending a provider authentication/network boundary and AT-12.

[SPEC-003 — Multiproject Scheduler and Execution Continuity](specs/SPEC-003.md)
is approved for implementation. Its deterministic Scheduler and checkpoints are implemented
for **FakeAdapter only**, with a read-only dry-run. The live gate remains closed.

## What works
- Python package with administrative CLI.
- SQLite schema for projects, objectives, versioned plans/tasks, authorizations, executions, decisions, events and notification outbox.
- Project/objective creation with audit events committed in the same transaction.
- Versioned plans and immutable task definitions, proposal, approval, rejection and revocation.
- Dependency cycle checks, explicit decision blockers and computed eligibility with reason codes.
- Audited manual administrative completion for dependency testing.
- Transactional, nondestructive schema upgrades from the bootstrap database.
- Manual, authorized FakeAdapter execution in a real Linux Landlock/seccomp sandbox.
- Trusted task worktrees, isolated staging, bounded process supervision, cancellation and restart reconciliation.
- Durable execution records, checksummed requests, logs and Git evidence outside SQLite.
- Persistent weighted project fairness, atomic capacity claims, dispatch holds and versioned continuity checkpoints.
- Foreground Scheduler service, single-cycle diagnostics, scoped pause/resume and explicit cross-model retry.
- Automated tests.

## Not implemented yet
**The live Codex adapter is gated off.** Its command assembly and local capability diagnostics
are available, but `run` refuses every Codex profile. There is no supported switch to bypass
this gate. Automated verification and Git integration remain future work.
Do not expose the database or executor privileges to LLM processes.

## Development and distribution

The public GitHub repository is the canonical source. A development checkout and its virtual
environment are for development and testing; a future production installation must use a
separately installed, versioned release with isolated operational resources. See
[ADR-001](docs/ADR-001-development-production-isolation.md). Deployment is not implemented.

GitHub Actions runs tests, Ruff and a package build on pushes to main and pull requests
against main. The project is distributed under the [MIT License](LICENSE).

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
the Scheduler; no external API is implemented.

## SPEC-002 configuration and manual execution

The offline executor requires Linux x86-64, a non-root operator, Landlock ABI >= 6,
seccomp filters, Python 3.12+ and `/usr/bin/git`. It fails closed on other platforms or
if the actual sandbox probe fails. No additional Python runtime dependencies are used.

Copy [config.example.toml](config.example.toml) to
`~/.config/orchestrator/config.toml` and replace its absolute directory placeholders.
Use `--config PATH` before the subcommand to select another operator-owned TOML file.
The file must not be group/world writable (use `chmod 600`). Configuration never loads
credentials. Repository, worktree and artifact roots must be canonical, disjoint paths
outside system runtime paths, and must not contain the state database. Existing roots,
reference clones and their `.git` directories must be operator-owned and not group/world
writable (use mode `700` for private directories). The manager creates private worktree
and artifact roots, but never repairs permissions on an existing repository automatically.

The example maps `sol` to the deterministic fake provider. `luna` and `astra` contain
Codex model **placeholders**, not supported model IDs. Profiles can be remapped explicitly
by the operator; no model availability is inferred from these logical names. Use actual
operator-verified IDs only after local discovery. Arbitrary provider flags or commands in
repository files are never executed as configuration.

```bash
# Migrate the existing state database without resetting it.
orchestrator init
orchestrator --config ~/.config/orchestrator/config.toml agents models
orchestrator --config ~/.config/orchestrator/config.toml executor doctor

# Register a real disposable reference repository under repository_root,
# then create/activate an objective and propose/approve its tasks as above.
# Select the stable Task ID, not the TaskRevision ID:
orchestrator --config ~/.config/orchestrator/config.toml run TASK_ID \
  --role implementer --model sol --actor operator
orchestrator --config ~/.config/orchestrator/config.toml executions --task TASK_ID
orchestrator --config ~/.config/orchestrator/config.toml execution show EXECUTION_ID
orchestrator --config ~/.config/orchestrator/config.toml execution logs EXECUTION_ID
orchestrator --config ~/.config/orchestrator/config.toml workspace show TASK_ID
```

`run` is synchronous and manually initiated. It reuses SPEC-001 eligibility, reserves
the exact approved task/plan/authorization transactionally and rechecks immediately before
releasing the subprocess. A launch-token handshake prevents untrusted code from starting
before the final check and durable running event. A unique active-task/workspace index
prevents duplicate reservations across database connections. The task stays `pending`
after **every** process result; SPEC-004 will own verification and closure. SPEC-001 manual
completion is also refused while an execution is active.

The fake provider makes a deterministic edit to `fake-change.txt` and returns a JSON
declaration. `succeeded` means exit code zero **and** a valid declaration, not verified
implementation. Nonzero exit, absent/invalid declaration, timeout, cancellation and output
overflow are distinct recorded outcomes. Structured declarations are untrusted and bounded
to 64 KiB (summary <= 4096 characters); raw logs remain in artifacts. Run exit codes are
0 for `succeeded`, 1 for other terminal process results, 2 for validation/preflight/setup
errors and 130 for an operator interrupt. Errors identify the execution when one was created.

`inspect` is a versioned inspection instruction, not a privileged policy. The service
compares staged contents and refuses promotion if that role modified files. Role templates
and the complete effective request are recorded, with a SHA-256 checksum. Additional context
references remain task data; there is no dynamic input-file retrieval. Effective instructions
are limited to 16384 UTF-8 bytes, including any labeled continuity context.

### Worktrees, evidence and recovery

The trusted manager validates repository roots, ownership, worktree registration, branch
identity and the exact `.git` pointer. It rejects bare/nested worktrees as reference clones,
submodules, external Git filters/includes/extensions, path escapes and unmanaged branch or
directory collisions. Trusted Git commands use argument arrays with hooks and fsmonitor
disabled. A branch `agent/TASK_ID` starts at a recorded reference `HEAD`; the agent never
sees shared Git metadata or the reference clone.

Each run copies the task worktree (including existing dirty and ignored content) to a
private artifact staging directory without `.git`. Only that staging directory is writable
inside the sandbox. After the process ends, the manager compares the original worktree
with its before-run hashes and imports staged file changes only if the original is unchanged.
New Git metadata, unsupported special files/hardlinks and new escaping symlinks cause a
refusal. Concurrent user changes are preserved, and the staged result remains for inspection.
Import is a file-by-file operation, not an atomic tree swap: an I/O failure may leave partial
changes in the managed worktree; the execution is failed and the full stage remains available.
There is no automatic reset, commit, merge, push, branch deletion or workspace cleanup.

`artifact_root/EXECUTION_ID/` contains:

- `request.json`: exact effective request, with checksum stored in SQLite.
- `before.json` / `after.json`: Git status, changed paths, tracked diff and file hashes.
- `stdout.log` / `stderr.log`: trusted-supervisor captures of untrusted process output.
- `result.json`: observed process outcome and separately labeled agent declaration.
- `security.json`: actual negative-probe observations from the execution profile.
- `stage/`: preserved isolated input/output tree, including partial or unpromoted changes.

Artifacts are referenced by stable relative paths. Missing artifacts are never synthesized
as success. Exceptions, including disk errors, leave an auditable failed execution where
the database remains writable. If the database itself fails, an active reservation may
remain; inspect the artifacts and reconcile it before explicitly retrying.

From another terminal, cancel a run with:

```bash
orchestrator execution cancel EXECUTION_ID --actor operator
orchestrator executor reconcile
```

Cancellation is a durable request polled by the supervisor. It sends SIGTERM first, waits
the configured grace period and then sends SIGKILL to the owned process group if necessary.
Ctrl-C also terminates the owned process. Partial logs and staged edits are retained.
`executor reconcile` compares boot/start-time process identities. A live supervisor is
left alone. An abandoned owned sandbox process is stopped using a PID file descriptor;
the execution becomes `interrupted`, with an explicitly uncertain outcome. PID reuse never
authorizes killing an unrelated process. Reconciliation never imports uncertain staged
changes or reruns a task; an operator must inspect it and explicitly invoke `run` again.
Until reconciliation, the active reservation blocks another run.

### Security boundary and live-run release gate

`landlock-seccomp-offline-v1` is an OS-enforced, inherited restriction, not a prompt policy.
Landlock allows file-content reads/execution from system runtime directories (`/usr`, resolved
`/bin`, `/lib`, `/lib64`, loader cache and minimal devices) and reads/writes only in the
isolated stage. It also scopes host signals and abstract IPC. Seccomp denies sockets,
process creation/escape, tracing/cross-process memory, host IPC, mount/namespace administration,
io_uring and filesystem metadata-changing calls not covered by Landlock. Only benign fcntl
operations are allowed. Linux capabilities are dropped, and `no_new_privs` prevents gaining
privileges from setuid programs. Unknown newer syscalls are refused.
The child receives a minimal environment, pipes and no inherited control-plane descriptors,
home mount, SSH keys, provider credentials or Docker socket. CPU, address-space, file-size,
descriptor, deadline and captured-output limits are enforced. Only synthetic secrets are
used in negative tests; no real credentials are read for diagnostics.

This profile does not hide file names/stat metadata, provide disk quotas or support arbitrary
multi-process build tools. The trusted operator, kernel, system toolchain and reference clone
configuration remain trusted. Its shared UID is restricted by actual irreversible Landlock
and seccomp enforcement; it is never an unrestricted execution identity.

`executor doctor` and each run execute negative probes under this same profile: sentinel
read/write/chmod, symlink escape, networking and forbidden host syscalls. Denials, unchanged
sentinel state and diagnostics are recorded. An unavailable or failed probe refuses launch;
there is no downgrade. The offline acceptance suite additionally checks unrelated project
files, database sentinels, ptrace and recovery/cancellation behavior.

**Live Codex is deliberately unavailable in this release.** The profile blocks both provider
networking and the child commands Codex needs; no safe authentication transport outside the
untrusted domain has been approved. `verified = true` on a model cannot open this gate.
The adapter currently provides command construction and local `--version` / `exec --help`
capability checks only. Command options were verified against installed `codex-cli 0.165.0`
and the [official Codex CLI reference](https://learn.chatgpt.com/docs/cli/reference).
The sandbox design follows the [Linux Landlock documentation](https://docs.kernel.org/userspace-api/landlock.html).

Before live enablement, approve a network/authentication and tool-process boundary, implement
it without relaxing protected paths, repeat all negative probes under that exact live profile,
and explicitly authorize AT-12 on a disposable repository. Record the installed CLI version,
verified model mapping, request checksum, provider logs and changed files; confirm that Task
status remains pending. AT-12 has **not** been run. SPEC-002 is not accepted for production
agent execution while these blockers remain; the ordinary offline tests need no credentials.

## SPEC-003 scheduling and continuity

Run `orchestrator init` to apply migration 3 without resetting data. Configure the optional
`[scheduler]` section in `config.example.toml`; defaults are two global slots, two per project,
a two-second poll, a 15-second owner lease and a 30-second launch claim. Lease duration must
be at least twice the polling interval. Concurrency values are integers from 1 to 32. Limits
can also be supplied to `scheduler run` or `scheduler tick` with `--global-concurrency` and
`--per-project-concurrency`. Startup refuses reductions below current active usage.

```bash
orchestrator scheduler tick --dry-run
orchestrator scheduler tick
orchestrator scheduler status
orchestrator scheduler run
# In a separate terminal:
orchestrator scheduler pause --project PROJECT_ID
orchestrator scheduler resume --project PROJECT_ID
orchestrator scheduler pause
orchestrator scheduler resume
orchestrator checkpoints show TASK_ID
orchestrator scheduler retry TASK_ID --actor operator \
  --reason 'Inspected prior artifacts and authorized one continuation' --model luna
```

As with manual execution, `--db` and `--config` go before the subcommand. All Scheduler
views return JSON. `tick` dispatches at most the available batch capacity, waits for that
batch to finish and records checkpoints; it does not refill slots indefinitely. Dry-run
only computes candidates, skips and the next weighted choice. It does not acquire a lease,
run security probes, reserve work, update fairness or write audit/projection state. It uses
the currently persisted capacity policy. `run` is a foreground polling loop suitable for
an external supervisor later; nothing installs/enables a service or daemonizes itself.

The Scheduler selects exact TaskRevisions through SPEC-001 eligibility, then revalidates
the same revision, plan and authorization in the Executor's reservation transaction and
again at launch. It does not plan, approve, verify or integrate code. The configured model
defaults to `sol`; an approved definition's `model_profile` or a bound explicit retry can
select another configured logical profile. Unknown roles/models and all Codex profiles are
skipped with explanatory codes. Every actual FakeAdapter process still passes SPEC-002's
negative isolation probes. The running installation itself is excluded as
`PROTECTED_INSTALLATION`; this service does not authorize self-development or deployment.

### Deterministic fairness and capacity

Project priorities already range from 0 to 100. Values 0..33, 34..66 and 67..100 map to
the configured low/normal/high `weights` (default `[1, 2, 3]`, each in 1..8). Smooth weighted
round-robin adds each currently selectable project's weight to its persistent credit,
chooses the highest credit and subtracts the sum of participating weights from the winner.
Credit ties break by Project ID. Within a project, stable Task creation time and Task ID
choose the task; no task-priority schema or scoring engine is added. For IDs `a` and `b`
with weights 1 and 3, the known sequence is `b, a, b, b`, repeating while both have work.
Changing the eligible set or available project capacity changes the participating weights.

Credits survive restarts. Ineligible/capacity-blocked projects gain no new credit; existing
credit stays frozen. Explicit Scheduler pause resets the affected credit to zero. Weights
bias dispatch opportunities over released slots, not every individual two-slot snapshot.
There is no random selection, preemption or LLM scheduling call.

Capacity is derived from existing `created`/`reserved`/`running` Executions, including manual
ones. The Executor checks durable global/project limits in the same SQLite transaction
that reserves a task. Scheduler claims bind to that exact reservation, rather than creating
a second execution or maintaining a second authoritative slot counter. The existing active
task/workspace uniqueness constraints still apply. Limits remain persisted after the
Scheduler stops, so a concurrent manual `run` cannot oversubscribe the published policy.

One owner holds an OS file lock beside the database plus a durable owner ID, process-start
identity, expiry and heartbeat. The state directory must be operator-owned and not group/world
writable. A second `run`/`tick` fails with `SCHEDULER_BUSY`, even if the recorded lease expired
while the first owner still holds its lock. A replacement first reconciles claims and
Executions; lease expiry or a stale PID alone never authorizes another writer.

### Holds, checkpoints and deliberate retry

Provider success leaves Task status `pending` and creates an `awaiting_review` hold.
Failures, interruption, uncertain outcomes and incomplete evidence create
`requires_manual_retry`. Terminal executions awaiting observation are also excluded, so a
crash before the Scheduler acknowledges completion cannot cause replay. Repeated ticks do
not rerun held work. Dependencies remain blocked until the prerequisite is actually `done`
through the existing authorized administrative path or future SPEC-004 verification.

At first non-dry Scheduler activation, terminal manual executions are observed too. From
then on, manual runs also respect inspection holds and unobserved terminal history. Use
`scheduler retry` to inspect/reconcile history and authorize one additional attempt; it
requires a nonempty actor/reason, current eligibility, no active writer and valid current
workspace/repository state. It grants no new plan approval or sandbox privileges. The retry
binds to exact revision/authorization/model IDs and is consumed atomically by one reservation.
If approval changes before consumption, it reports `RETRY_AUTHORIZATION_CHANGED`; another
explicit retry against the current approval supersedes that stale request with an audit event.

Checkpoints are append-only, numbered per Task, tied to an exact TaskRevision/Execution,
and unique per Execution. They contain bounded **unverified** agent summaries, pending work,
blockers/next action, process outcome, workspace/commit observations and validated artifact
references. Up to 64 changed paths are shown, with a truncation flag; the full evidence
remains in the referenced artifact. Missing/tampered artifacts produce an inspection hold,
not verified progress. A fallback workspace observation is labeled as checkpoint-collection
state, not the execution's final state. SQLite holds this metadata, not full logs or chats.

An explicit retry may select a different configured model, for example `--model luna` when
that profile maps to FakeAdapter. The request includes the latest checkpoint for the same
TaskRevision, provenance, current plan/decision data and validated workspace/commit state.
Prior summaries stay labeled untrusted and cannot grant permissions or replace instructions.
The continuity JSON is stored as a controlled artifact and checksummed in the effective
request. The same package is used if a bound retry is consumed by an explicit manual `run`
with the selected model. No native Codex session resume, retrieval or autonomous delegation
is implemented.

### Restart, stop and diagnostics

Trusted worker processes call `ExecutionService.execute_reserved` and use the existing
SPEC-002 supervisor and sole agent launcher. Their process identities commit before a
launch-token release. Worker diagnostics live at `artifact_root/.scheduler/EXECUTION_ID.log`;
the claim view exposes that stable reference. The agent never receives this control-plane
environment or database access.

SIGINT/SIGTERM stops future dispatch and releases Scheduler ownership. Already running
independent Executor supervisors continue; inspect or cancel them with `execution cancel`.
On restart, proven live supervisors retain their reservations/slots. Abandoned unlaunched
claims become interrupted rather than being replayed. If a supervisor died, SPEC-002 safely
stops its identity-checked sandbox child and preserves uncertain artifacts/worktree state.
Reconciliation and checkpoint acknowledgement are idempotent. An interrupted or successful
but unverified attempt always requires operator review before continuation.

Scheduler pause applies only to future automatic dispatch; active executions continue.
Project domain status remains a separate SPEC-001 eligibility rule. Security revocation,
plan supersession removing authority, or task cancellation requests controlled cancellation
through the existing Executor policy. Open Decisions block linked work without stopping
unrelated projects. No Scheduler code edits worktree files or integrates changes.

`scheduler status` shows owner/heartbeat/last tick, global and per-project active usage,
claims/worker identities, active holds, persisted fairness/pause/backoff state and latest
skipped reason codes. Skip projections are diagnostic, not a second eligibility source;
changes are audited. Worker launch failure releases capacity conservatively, holds the
task and applies a short project backoff. Other eligible projects can continue. Evidence
collection errors also release terminal slots and remain explicit in checkpoints.

Mandatory tests remain offline with temporary repositories, databases and artifacts.
SPEC-003 AT-13 has **not** been run: unattended live dispatch remains unavailable until
SPEC-002's live boundary and an explicitly authorized disposable-repository smoke test pass.

## Proposed next increments

1. Approved provider authentication/network boundary and opt-in live Codex smoke test.
2. SPEC-004 verification and Git integration.
