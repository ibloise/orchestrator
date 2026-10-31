# SPEC-002 addendum: live agent runtime direction

This addendum records the selected direction; it does not enable live execution or replace SPEC-002's security acceptance criteria. The existing offline FakeAdapter and its Landlock/seccomp profile remain unchanged. A separate future runtime will use rootless Docker for real Codex and development child processes.

Agents join only an internal Docker network; Squid joins that network and an egress network and provides explicit HTTP/HTTPS proxy access. Ordinary public package downloads and public repository access need no per-request approval. Host changes, Docker access, private LAN access, protected pushes and deployment require separately authorized mechanisms or operator action. The [ADR](../docs/ADR-002-agent-network-isolation.md) and [operations guide](../docs/operations/agent-network.md) define the baseline and its limitations.

The trusted Executor must check actual rootless runtime capabilities, isolation, network membership, proxy configuration, mounts, resources and required security controls before launch and fail closed when checks fail. It must never automatically relax a boundary. Credentials, including long-lived provider credentials, must not be accessible to untrusted project commands; network connectivity does not meet this requirement. Live Codex remains disabled until the security acceptance probes and SPEC-002 AT-12, an explicitly approved live smoke test, pass. SPEC-003's independent live dispatch and continuity gates also apply.

## Decisions and validation still required

- Design and test provider authentication isolation against arbitrary child commands and logs; identify supported provider behavior.
- Specify and implement the live container image, user identity, filesystem and Git metadata boundaries, resource limits, process supervision and cleanup.
- Define deterministic runtime capability checks and prove failure is closed, including network membership, Docker socket absence and proxy health/policy.
- Validate host, LAN, DNS, IPv4/IPv6, rebinding and alternate-address escape cases on the actual rootless backend; resolve failures before enabling live work.
- Establish image provenance, digest updates, proxy log retention and operational recovery procedures appropriate to deployment.
- Run SPEC-002 isolation tests and operator-authorized AT-12 on a disposable repository, then satisfy SPEC-003's live scheduling gates separately.
