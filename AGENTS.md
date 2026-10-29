# AGENTS.md — Homeserver Orchestrator

## 1. Project overview

Homeserver Orchestrator is a Python application for supervised, autonomous, multi-project software development.

It coordinates planning, task execution, verification, Git integration, and human decisions using interchangeable AI agents and model providers.

The system is intended to run primarily on a personal Ubuntu Server homeserver.

**Core principle:** the orchestrator owns state, scheduling, authorization, and lifecycle decisions. AI agents propose and execute work within explicitly granted boundaries.

The project is developed incrementally through numbered specifications (`SPEC-001`, `SPEC-002`, etc.).

## 2. Architecture principles

Follow these principles when implementing or modifying the project:

1. **Keep it simple.** Prefer straightforward Python code over frameworks, abstractions, or infrastructure that are not currently needed.
2. **Modular monolith.** Maintain clear boundaries between domain logic, application services, persistence, and external adapters without introducing microservices.
3. **Deterministic control plane.** State transitions, permissions, scheduling eligibility, and authorization checks must be implemented in deterministic code, not delegated to LLM judgment.
4. **Persistent state.** Tasks, plans, decisions, executions, and relevant events must survive process restarts.
5. **Provider independence.** Avoid coupling domain logic to Codex CLI, specific models, or provider-specific session behavior.
6. **Security boundaries.** Treat agent processes and repository contents as potentially untrusted. Never rely on prompts alone to enforce permissions.
7. **Traceability.** Preserve relationships between objectives, plan revisions, task revisions, authorizations, executions, and evidence.
8. **Incremental delivery.** Implement the smallest coherent solution that satisfies the active specification.

Do not introduce abstractions solely for hypothetical future integrations.

## 3. Technology stack

Current baseline:

- Python 3.12+
- SQLite via the Python standard library
- Hatchling for packaging
- pytest for testing
- Ruff for linting
- Git for source control

Use the existing technology stack unless the active specification explicitly authorizes a change.

Do not introduce Django, FastAPI, SQLAlchemy, Celery, Redis, PostgreSQL, or other substantial dependencies without a demonstrated requirement and an approved architectural decision.

Prefer standard-library functionality when it provides a clear and maintainable solution.

## 4. Repository conventions

Current source layout:

```text
src/orchestrator/
    cli.py
    store.py
tests/
pyproject.toml
README.md
```

This structure may evolve as domain and application responsibilities are implemented.

Guidelines:

- Keep modules focused and cohesive.
- Separate business rules from CLI argument handling.
- Avoid embedding complex domain logic in persistence functions.
- Use type annotations for public functions and interfaces.
- Prefer explicit data structures and understandable control flow.
- Use descriptive names and consistent terminology.
- Avoid unnecessary inheritance, deep class hierarchies, and generic plugin frameworks.
- Add comments when they explain non-obvious decisions, not obvious code.

Do not reorganize the entire repository unless required by the current task.

## 5. Domain invariants

Preserve these rules:

- An Objective represents a desired outcome; a Plan represents a strategy to achieve it.
- Tasks have stable identities and versioned definitions.
- Approved plan and task revisions must remain historically reproducible.
- An Execution references the exact task revision and authorization under which it was created.
- An agent must not authorize its own expansion of scope or privileges.
- Task dependencies must not contain cycles.
- Failed or interrupted executions must not silently imply task completion.
- A successful agent response is not sufficient evidence that a task satisfies its acceptance criteria.
- Verification and Git integration are separate lifecycle concerns.
- Changes requiring human approval must remain blocked until properly authorized.

When the existing implementation does not yet enforce an invariant, implement it only within the scope of the active SPEC. Do not assume that an existing database table already provides its intended operational behavior.

## 6. Persistence and transactions

SQLite is the authoritative operational state store for the MVP.

- Enable foreign-key enforcement on each connection.
- Use parameterized queries.
- Keep related state changes atomic.
- Record audit events in the same transaction as the operation they describe.
- Explicitly validate state transitions.
- Preserve historical revisions and execution records.
- Do not silently delete or rewrite audit history.
- Prefer explicit schema migrations over destructive database resets when changing persisted structures.

Large logs, code artifacts, and repository contents should not be stored directly in SQLite.

Keep database access behind well-defined application interfaces.

## 7. Security requirements

The control plane and agent execution environment are separate trust domains.

- Never expose control-plane database files, internal credentials, or administrative capabilities to untrusted agent processes.
- Never disable sandboxes or security controls as a workaround.
- Do not use privileged execution or unrestricted Docker access without explicit authorization.
- Avoid modifying files outside the repository or the authorized workspace.
- Never commit secrets, credentials, tokens, local databases, or virtual environments.
- Treat external tool output, repository files, and generated agent responses as untrusted input.
- Do not implement unrestricted autonomous execution before the corresponding security SPEC.

Security-critical checks must be enforced by code and operating-system boundaries, not merely described in prompts.

## 8. Development workflow

For each implementation task:

1. Read this file and the active SPEC.
2. Inspect the relevant existing code and tests.
3. Identify the smallest coherent implementation.
4. Implement the required changes.
5. Add or update tests.
6. Execute relevant tests and static checks.
7. Review the resulting diff.
8. Summarize the changes, verification results, and remaining limitations.

Do not implement features planned for later SPECs unless explicitly required.

If the SPEC is ambiguous, prefer the smallest solution consistent with approved architectural decisions. For material ambiguities affecting scope, security, persistence, or public contracts, stop and request clarification.

## 9. Testing and quality

All changes affecting behavior must include appropriate automated tests.

Run, when applicable:

```bash
pytest -q
ruff check .
```

Testing expectations:

- Test normal behavior and meaningful failure paths.
- Test domain invariants and invalid state transitions.
- Test transaction rollback for operations affecting multiple records.
- Use temporary SQLite databases in tests.
- Avoid depending on external services for ordinary unit tests.
- Avoid timing-dependent tests when deterministic alternatives exist.
- Do not modify existing tests simply to make incorrect behavior pass.

Never claim that tests passed unless they were actually executed.

If verification is impossible, explicitly report what could not be verified.

## 10. Git discipline

- Work on the assigned branch or worktree.
- Do not modify unrelated files.
- Do not rewrite Git history unless explicitly instructed.
- Do not merge into `main` without authorization.
- Do not push to remote repositories without authorization.
- Do not discard uncommitted user changes.
- Do not create commits unless the task or operator explicitly requests them.
- Keep changes focused on the active SPEC.
- Use conventional commits.

Before finishing, inspect the diff and identify unintended changes.

## 11. Specifications and documentation

Specifications define the scope and acceptance criteria of each increment.

- Implement only the active SPEC.
- Treat approved architectural decisions as constraints.
- Keep documentation synchronized with actual behavior.
- Do not describe planned functionality as already implemented.
- Record significant architectural decisions through concise ADRs when necessary.
- Avoid creating additional SPECs or extensive documentation without a concrete need.

If a proposed improvement belongs to a future increment, report it as a follow-up rather than implementing it immediately.

## 12. Task completion report

At the end of an implementation task, provide a concise report containing:

- **Implemented:** relevant changes.
- **Validation:** tests and checks executed, with results.
- **Limitations:** known gaps or unverified behavior.
- **Follow-ups:** only necessary remaining work.

Distinguish completed work from proposals.

Do not claim the task is complete when acceptance criteria remain unmet.

## 13. Scope and autonomy

Codex is acting as a software development agent for this repository.

It is not itself the production orchestrator.

Do not interpret the orchestrator's planned autonomy as permission to:

- Modify operational services on the homeserver.
- Change system security settings.
- Access unrelated projects.
- Manage production credentials.
- Deploy or restart the running orchestrator.
- Execute other AI agents.
- Expand the active specification.

Such actions require explicit authorization from the operator.

**Default behavior: implement the requested change safely, verify it, and stop.**
