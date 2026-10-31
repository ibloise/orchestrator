# ADR-002: Agent network isolation

## Status and problem

Accepted direction for a future SPEC-002 live runtime; this infrastructure is not an authorization to enable it. Development agents need ordinary public dependencies, documentation and repositories, while project code and agent commands must be treated as hostile. An unrestricted Docker bridge can reach the Internet and potentially private services. The existing offline FakeAdapter sandbox deliberately forbids network access and child processes.

## Decision and trust boundaries

Use the operator's rootless Docker daemon and two bridges: `orchestrator-agent-internal` is internal and carries only agent containers and Squid; `orchestrator-agent-egress` carries Squid to external networks. An agent joins only the internal bridge. Squid listens only on its internal address and acts as an explicit HTTP proxy, including HTTPS CONNECT on port 443, without TLS interception. Neither container publishes a host port; Squid is not a router and no IP forwarding is enabled. The fixed internal subnet enables source ACLs and an interface-specific listener; conflicts require coordinated changes to Compose and Squid configuration.

Rootless Docker reduces daemon and container privilege relative to rootful Docker, but is not proof of containment. The control plane owns authorization, state and scheduling. The trusted Executor must verify the runtime, networks, mounts, resource limits and actual container membership before launch, fail closed, and supervise work and evidence. The agent receives only its authorized workspace and needed inputs, no host network, Docker socket, administrative credentials, control-plane database, home directory or unrelated workspaces. The proxy is a separate, less trusted service with external reach; its ACLs and isolation need validation, not implicit trust. Docker daemon proxy settings are independent of the agent's explicit proxy settings.

The network boundary does not solve provider authentication. Long-lived provider or administrative credentials must not be readable by arbitrary workspace commands. A suitable credential boundary is unresolved; this proxy is not a credential broker.

## Alternatives

The default bridge provides direct external access and is unsuitable. Keeping the offline Landlock/seccomp profile for live development blocks required child processes and networking; weakening that profile would damage its existing guarantee. Host networking, a published proxy, a generic router and transparent TLS interception unnecessarily broaden access. A destination allowlist would require continual operator intervention for ordinary development dependencies; the selected deny policy permits ordinary public traffic but carries residual risk.

## Residual risk and relation to specifications

An internal bridge and destination ACLs do not prove resistance to every host, DNS, address-encoding, rebinding, IPv6 or container escape. Public endpoints can relay or exfiltrate data; CONNECT hides content. Squid logs can contain sensitive URLs. Proxy compromise or misconfiguration can cross the boundary. Rootless backend behavior and host routes must be tested on the deployment host. IP filtering cannot identify all internal services exposed via public addresses. See the [operations guide](operations/agent-network.md) for concrete probes and limitations.

The [SPEC-002 addendum](../specs/SPEC-002-ADDENDUM.md) defines the live-runtime direction and gates. SPEC-003 inherits the SPEC-002 boundary and must not schedule live work while it is closed. SPEC-004 verification and Git integration remain separate.
