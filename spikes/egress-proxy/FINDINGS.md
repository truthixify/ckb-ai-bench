# Spike: egress proxy + arm isolation (ADR-0006) - FINDINGS (2026-06-12)

Goal: prove the no-research arm control at the NETWORK layer, not by trusting the agent.
On arms A/D the agent must be unable to reach the web; only an allowlist (chain RPC, and
on MCP-bearing arms the MCP endpoint) may be reached, and every egress attempt must be
machine-logged (not self-reported). This is the load-bearing control behind the headline
`C - B` claim: if the OFF arm can silently research, the delta is contestable.

## Threat model

A capable model on a no-research arm IGNORES `HTTP_PROXY` and tries to reach the network
directly. Prompt-only enforcement and bash-parsing are both defeatable. The control must
hold even when the agent actively tries to bypass it.

## Topology that enforces it (stronger than env-var trust)

- `net-internal` is an `internal: true` docker network: it has NO NAT to the outside
  world. The agent is attached ONLY to this network, so it has no route off the host
  except through a container that ALSO sits on an egress-capable network.
- `net-egress` is a normal bridge with outbound NAT. ONLY the proxy is on it.
- Therefore the proxy is the SOLE bridge internal -> outside. The agent literally cannot
  reach any external host except through the proxy, which logs and allowlists.

The proxy is `tinyproxy` 1.11.3 (baked into `proxy.Dockerfile`) in allowlist mode
(`FilterDefaultDeny On`, `FilterType ere`): only hosts matching `proxy/allowlist` are
permitted; everything else is refused and logged. It logs to stdout (container-native;
collected via `docker logs`, no host-mount permission coupling).

The agent image (`agent.Dockerfile`) BAKES its tools (curl) in. This matters: an agent
that installed tools at runtime would secretly need the very egress we are proving absent
(the internal-only network blocks apk). The fat agent image (ADR-0004) bakes tools the
same way, so this is faithful, not a spike shortcut.

## The decisive results (each asserted by exit code, no output-grep masking)

| # | Claim | How | Result |
|---|---|---|---|
| 1 | ALLOW + reach | allowlisted chain RPC (`192.168.0.73:18114`) VIA proxy | **PASS** curl exit 0; log: `Established connection to host "192.168.0.73"` |
| 2 | BLOCK | non-allowlisted `example.com` (web research) VIA proxy | **PASS** proxy refuses (curl exit 22 / HTTP 403); log: `Proxying refused on filtered domain "example.com"` |
| 3 | NO-BYPASS | agent reaches blocked host DIRECTLY (no proxy) | **PASS** fails at network layer (no route off internal net) |
| 4 | NO-BYPASS (allowed) | agent reaches the ALLOWLISTED host DIRECTLY (no proxy) | **PASS** also fails: the only path out is the proxy, proving the allow in (1) is the proxy's doing, not a boundary leak |
| 5 | LOG (allow) | proxy log records the allowed chain-RPC request | **PASS** |
| 6 | LOG (deny) | proxy log records the denied web-research attempt | **PASS** |

`run-spike.sh` ends by asserting all six passed. Latest run: **6/6**, zero config warnings.

Claim 4 is the subtle one: it rules out the trivial-pass explanation "the host was just
reachable anyway." Direct egress fails for BOTH the blocked and the allowed host; only the
proxy bridges out. So the allow in claim 1 is genuinely the proxy's allowlist decision.

## Mapping to the real design

- The allowlist is `proxy/allowlist`, one POSIX-ERE per line. In production it is
  per-(chain profile x arm): chain RPC + the MCP endpoint (on MCP arms) + the proxy. Exact
  entries are set at test time (ADR-0006), and the MCP endpoint passes through the proxy
  like any other allowlisted host once its location (LAN vs internet) is known.
- DevNet and TestNet RPC are both observable egress through this proxy (uniform), matching
  the ADR-0007 sidecar consequence: chain RPC is an allowlist entry on every chain, never
  invisible loopback.
- The machine-observed log IS the protocol-violation signal: any destination outside the
  allowlist appearing in the log on an A/D arm is a logged violation.

## Relationship to the Tier-1 Spike 3 gap (now closed in principle)

Spike 3 (the model loop) surfaced that on `LocalEnvironment` the MCP-OFF arm answered a tip
task over host network: no MCP surface, but the network was not gated. This spike is the
control that closes exactly that gap: under this topology an OFF-arm agent cannot reach any
non-allowlisted host even if it ignores every proxy env var. OFF-arm data isolation moves
from "visible" (no MCP tools) to "enforced" (network-level allowlist).

## Residual (tracked, design-level, not spike-blocking)

- This spike enforces isolation via an internal-only docker network (the strongest
  guarantee). The ADR also mentions an in-container iptables transparent redirect so an
  agent that DOES honor proxy env vars gets transparently routed; that is an ergonomic
  add-on, not the enforcement boundary. The internal-network model proven here is the
  actual guarantee and does not depend on the agent honoring anything.
- DNS: on the internal network the agent cannot resolve external names either (no route to
  a resolver), which is consistent. If production wants the agent to resolve allowlisted
  names, the proxy (which CAN resolve) does it on the agent's behalf via the CONNECT/HTTP
  request, as shown (the agent passed a hostname-bearing URL and the proxy resolved it).
- TLS / CONNECT to MCP and TestNet over 443 is allowed by `ConnectPort 443` but was not
  exercised here (the reachable chain RPC is plain HTTP); the allowlist still gates WHICH
  hosts a CONNECT may target. A TLS allowlisted destination should be exercised once the
  pinned MCP instance location is known.

## Reproduce

```
cd spikes/egress-proxy
bash run-spike.sh        # builds images, brings up proxy+agent, asserts 6/6, tears down
```

The script tears down only the containers it started (`docker compose down -v`); it does
not touch unrelated containers.
