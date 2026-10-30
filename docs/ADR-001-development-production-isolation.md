# ADR-001: Development and production isolation

## Decision

The public GitHub repository is the canonical source of code. Development and production
may share a physical homeserver, but must remain isolated. Production uses a separately
installed, versioned release, not the mutable development checkout. Its state, credentials,
configuration, workspaces and service identity must be isolated appropriately. Agent
executions must not modify the running production installation.

CI builds and verifies artifacts. Deployment is a separate, explicitly authorized operation.
Database migrations require backup and recovery planning. Self-development may create
branches and pull requests, but must not directly update production.

## Rationale

Isolation protects the running service and its operational state from mutable development
work and agent execution. Versioned releases and separate authorization make production
changes reviewable and recoverable.
