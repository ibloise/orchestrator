# Agent network operations

This is an operator-managed infrastructure baseline, not a live agent runtime. Run commands from the repository root as the rootless Docker operator. Do not attach production agents until the SPEC-002 security gates are met.

## Prerequisites and setup

Use Linux with a working rootless Docker Engine and Compose plugin, access to the pinned image and the diagnostic images `alpine:3.22` and `curlimages/curl:latest`, and permission to manage this daemon. The latter tags are diagnostic tools, not pinned runtime components. Pre-pull them in a trusted environment if needed. Check the daemon and local routes before deployment:

```bash
docker context show
docker info --format 'Root={{.DockerRootDir}} Security={{json .SecurityOptions}}'
docker compose version
docker network ls
docker network inspect bridge
ip route
```

Confirm the intended rootless context and rootless security option, and check existing Docker networks and host/LAN routes for overlap with `172.30.240.0/24`. Resolve collisions before starting: change the internal subnet, static proxy address and source ACL together, then repeat validation. Docker's daemon HTTP proxy settings are unrelated to the explicit container proxy.

The version-controlled files are `infra/agent-network/compose.yml` and `infra/agent-network/squid.conf`. Compose creates the globally named `orchestrator-agent-internal` and `orchestrator-agent-egress` networks, owned by this Compose project, rather than external networks. Do not reuse these names for another project: Compose project labels and an existing network can cause conflicts; pre-existing networks are not automatically trustworthy. Network names are stable across project names, but the proxy service DNS alias `squid` is available on its Compose networks. An agent must be joined only to `orchestrator-agent-internal`. Only trusted administrators can access the Docker daemon and attach containers.

```bash
docker compose -f infra/agent-network/compose.yml config
docker compose -f infra/agent-network/compose.yml up -d
docker compose -f infra/agent-network/compose.yml ps
docker network inspect orchestrator-agent-internal orchestrator-agent-egress
```

Inspect `Internal`, subnet, attached containers and proxy addresses; confirm that the agent is absent from egress. No port is published. The proxy uses an image digest, a read-only config mount, resource limits and `unless-stopped`; no socket, secret, host networking or privileged mode is configured. The internal listener is `172.30.240.2:3128`; proxy clients can use `http://squid:3128`. The 256 MiB container limit must cover the Squid process, its 32 MB memory cache and other internal structures; `cache_mem` is not a limit on total Squid memory. Squid logs live inside its container at `/var/log/squid/` and are lost when it is recreated unless exported. They can include sensitive URLs; restrict access and arrange retention and rotation before operational use.

To stop or remove this deployment:

```bash
docker compose -f infra/agent-network/compose.yml stop
docker compose -f infra/agent-network/compose.yml down
```

`down` removes the Compose container and its networks when no other containers remain attached; it does not remove the downloaded image. Do not remove networks or containers belonging to another deployment. Preserve needed logs first. To recover, inspect network membership, subnet conflicts and configuration; correct the cause, then run `up -d`. Do not relax the ACL or connect an agent to egress as a workaround.

## Configuration and logs

Validate the exact image without using either network:

```bash
docker run --rm --network none \
  -v "$PWD/infra/agent-network/squid.conf:/etc/squid/squid.conf:ro" \
  ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029 \
  -k parse -f /etc/squid/squid.conf
docker compose -f infra/agent-network/compose.yml exec squid tail -n 100 /var/log/squid/cache.log
docker compose -f infra/agent-network/compose.yml exec squid tail -n 100 /var/log/squid/access.log
```

A successful parse is not proof that the listener starts or that the policy is secure. The image writes logs to files because the tested worker could not write `/dev/stdout` or `/dev/stderr`. `docker compose logs squid` may show startup output, but is not a substitute for these files.

## Acceptance procedure

Run only on a disposable deployment after checking its networks and images. These commands create short-lived test containers, not real agents. Record output and failures; a timeout, DNS failure or connection error is not an HTTP 403. Do not treat any single failure as a complete security proof.

1. Select a non-sensitive IPv4 RFC1918 address under your control as the private test destination. Do not use a real service containing secrets. Set it in the shell before running the following checks. Inspect the route and DNS output separately: there must be no default route, and `squid` must resolve. The curl probe should connect to the proxy and return an HTTP response; it deliberately requests a denied private destination, so Squid HTTP 403 confirms both the listener and rejection. Curl exit 5 indicates proxy DNS resolution failed; exit 7 indicates connection failed; a 403 is an HTTP rejection, while other responses require investigation. A timeout or another curl error is not a successful connectivity check. Direct requests must fail with no route or a connection failure.

```bash
TEST_PRIVATE_IP="${TEST_PRIVATE_IP:?Set a private test destination}"
docker run --rm --network orchestrator-agent-internal alpine:3.22 ip route
docker run --rm --network orchestrator-agent-internal alpine:3.22 nslookup squid
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest -v --connect-timeout 5 --max-time 10 --proxy http://squid:3128 --noproxy '' "http://${TEST_PRIVATE_IP}/"
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest --noproxy '*' --connect-timeout 5 -k https://1.1.1.1/
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest --noproxy '*' --connect-timeout 5 -k "https://${TEST_PRIVATE_IP}/"
```

2. Check proxy behavior. The GitHub request should return a successful HTTPS response with certificate verification. Each forbidden request must return Squid HTTP 403, including the CONNECT tunnel response; use the verbose output to distinguish it from an upstream or network error.

```bash
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest -v --max-time 15 --proxy http://squid:3128 --noproxy '' -o /dev/null https://github.com/
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest -v --max-time 10 --proxy http://squid:3128 --noproxy '' "https://${TEST_PRIVATE_IP}/"
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest -v --max-time 10 --proxy http://squid:3128 --noproxy '' http://10.0.0.1/
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest -v --max-time 10 --proxy http://squid:3128 --noproxy '' http://172.16.0.1/
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest -v --max-time 10 --proxy http://squid:3128 --noproxy '' 'http://[::1]/'
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest -v --max-time 10 --proxy http://squid:3128 --noproxy '' 'http://[fe80::1]/'
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest -v --max-time 10 --proxy http://squid:3128 --noproxy '' 'http://[fc00::1]/'
docker run --rm --network orchestrator-agent-internal curlimages/curl:latest -v --max-time 10 --proxy http://squid:3128 --noproxy '' https://github.com:8443/
```

3. Inspect the actual proxy and a representative agent container, not just the Compose file. Replace `AGENT_CONTAINER` with a disposable test container's ID. The proxy must have no published ports, no privileged mode, no Docker socket and only the two intended networks; the agent must have only the internal network and no socket or sensitive mounts. The agent cannot alter its own Docker network membership without daemon access. Review `squid.conf` for the internal-only listener, source deny, destination and port denies before allow, and absence of forwarding/router configuration. No host IP forwarding or firewall changes are needed.

```bash
docker compose -f infra/agent-network/compose.yml ps -q squid
docker inspect AGENT_CONTAINER --format 'Networks={{json .NetworkSettings.Networks}} Mounts={{json .Mounts}} Privileged={{.HostConfig.Privileged}}'
docker inspect "$(docker compose -f infra/agent-network/compose.yml ps -q squid)" --format 'Ports={{json .NetworkSettings.Ports}} Networks={{json .NetworkSettings.Networks}} Mounts={{json .Mounts}} Privileged={{.HostConfig.Privileged}}'
```

4. Before trusting live work, also test the actual host and Docker gateway addresses, private-name DNS records and records with mixed public/private answers, changing DNS answers (rebinding), alternate numeric IPv4 and IPv4-mapped IPv6 representations, and public names resolving to private destinations for both HTTP and CONNECT. Use a controlled test DNS zone and non-sensitive endpoints; verify Squid's actual connected destination and response. Test an unauthorized container on egress against both proxy addresses: it must not reach the listener. Check direct DNS to external resolvers and other protocols cannot bypass the internal network. IPv6-disabled environments can yield connection errors instead of exercising IPv6 ACLs: repeat with a controlled IPv6-capable setup and record that distinction. These additional probes are not asserted to have passed.

## Policy and limitations

Squid allows only clients in the fixed internal subnet, HTTP ports 80/443 and CONNECT to 443; it denies manager requests and listed special/private IPv4 and IPv6 destinations before allowing clients. It resolves destination names for `dst` ACLs. DNS caching, multiple answers, rebinding between check and connect, unusual numeric encodings, IPv4-mapped IPv6 and Squid version behavior require targeted tests. The list is not a complete classification of all non-public or special-use addresses and cannot block a private service exposed through a public relay or public IP. Public services can relay traffic or receive exfiltrated data, and HTTPS tunnels hide contents. The proxy is not a general firewall or credential isolation mechanism. Host gateways and backend-specific paths need empirical testing. The listener binding, source ACL and lack of routing must all remain effective; network membership is controlled by trusted Docker administrators.

## Troubleshooting and upgrades

- If a client cannot resolve `squid`, inspect membership and Compose service alias; containers on the default bridge do not use this service discovery.
- If direct requests fail, that is expected; use a proxy-aware client. The tested BusyBox `wget` did not honor HTTPS proxy settings. Explicit curl `--proxy` and `--noproxy ''` avoid environment bypasses.
- For startup failure inspect both Squid logs and bind-mount permissions. The old stdout/stderr paths failed for the `proxy` worker; the built-in `manager` ACL must not be redeclared.
- Check subnet overlaps, daemon context, rootless backend and image availability if network creation or access fails. An isolated TLS failure at one site is not evidence of a general network failure.
- For upgrades, inspect upstream image provenance and release notes, select and review a new immutable digest, validate its config and worker log permissions, then run this entire acceptance procedure in a disposable deployment before an explicitly authorized rollout. Preserve logs and keep the old digest/config for rollback. Do not use `latest` for the deployed proxy.

## Historical validation record — October 31, 2026

A rootless Docker operator tested a separate manual prototype using slirp4netns with host loopback disabled. The default bridge had a default route and ultimately reached Google and GitHub over HTTPS; one initial `example.com` TLS failure and BusyBox HTTP header error did not establish a general fault. An internal bridge had no default route and direct requests to a public IP and an RFC1918 destination failed with network unreachable. Two containers resolved each other and exchanged HTTP over `nc`; Alpine lacked the `httpd` applet.

The manually attached two-network `ubuntu/squid:latest` prototype reported Squid 6.13. After switching worker logs to files and removing the redundant manager ACL, explicit curl CONNECT to GitHub returned 200 with verified TLS; CONNECT to an RFC1918 destination returned Squid 403. BusyBox wget had attempted direct connections instead of using the proxy. These observations concern that prototype, not every destination, bypass, host or this Compose deployment. The version-controlled configuration has separately passed Compose rendering and Squid parse on October 31, 2026; its deployment and connectivity acceptance procedure have not been run.
