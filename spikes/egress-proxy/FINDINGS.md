# Spike: egress proxy + arm isolation (ADR-0006) - FINDINGS (2026-06-12)

Goal: prove the no-research arm control at the NETWORK layer, not by trusting the agent.
On arms A/D the agent must be unable to reach the web; only an allowlist (chain RPC, and
on MCP-bearing arms the MCP endpoint) may be reached, and every PROXY-TRAVERSING egress
attempt must be machine-logged (not self-reported); attempts blocked at L3 never leave the
host and so have nothing to log (see "Scope of the logging claim" below). This is the
load-bearing control behind the headline `C - B` claim: if the OFF arm can silently
research, the delta is contestable.

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
| 4b | NO-BYPASS (raw IP) | agent dials a RAW PUBLIC IP (`1.1.1.1`) DIRECTLY, no DNS | **PASS** curl exit 7 (couldn't connect), asserted SPECIFICALLY (not just nonzero), so it proves an L3 routing block, not a reachable-host HTTP error |
| 4c | ALLOWLIST gates by destination | non-allowlisted raw IP (`1.1.1.1`) VIA proxy | **PASS** proxy refuses (exit 22) AND the proxy log shows `Proxying refused on filtered domain "1.1.1.1"`, proving the refusal is tinyproxy's filter, not an origin error |
| 5 | LOG (allow) | proxy log records the allowed chain-RPC request | **PASS** |
| 6 | LOG (deny) | proxy log records the denied web-research attempt | **PASS** |

`run-spike.sh` ends by asserting all nine passed. Latest run: **9/9**, zero config warnings.

Claim 4 is the subtle one: it rules out the trivial-pass explanation "the host was just
reachable anyway." Direct egress fails for BOTH the blocked and the allowed host; only the
proxy bridges out. So the allow in claim 1 is genuinely the proxy's allowlist decision.

## Scope of the logging claim (sharpened after adversarial review)

"Every egress attempt is machine-logged" means every attempt that traverses the proxy is
logged (allowed connects AND denied filters, to stdout via `docker logs`). An attempt that is
blocked at L3 (the internal-only network has no route off-host) never reaches the proxy and so
leaves no proxy log line, BY DESIGN: it never left the host, so there is nothing to attribute.
The protocol-violation signal is therefore "a non-allowlisted destination appears in the proxy
log," not "the proxy logs packets that never traversed it." On the production transparent-proxy
topology (iptables redirect of all egress to the proxy) every attempted connection is forced
through the proxy and thus logged even when refused; the internal-network model here is the
stricter guarantee (the packet cannot leave at all). Checks 3, 4, 4b prove that stricter block.

## Round-2 hardening (after adversarial review, 2026-06-13)

Round-1 reviewers (grok-build, grok-composer) noted the isolation was exercised only over
curl-to-hostname and that the logging claim, read literally, overstated coverage of L3-blocked
attempts. Closed: added a RAW-IP direct-egress test (check 4b: `1.1.1.1` by IP, no DNS, proves
L3 routing block not DNS failure) and a raw-IP-via-proxy allowlist test (check 4c: the
allowlist refuses a non-allowlisted destination IP even through the proxy), and scoped the
logging claim precisely (above). Still out of scope for this spike (tracked, not blocking):
IPv6 (the internal network is IPv4-only here), ICMP, and an independent packet capture
(tcpdump) proving zero unexpected egress; the internal-only-network boundary makes those
vectors routeless too, but they are not separately asserted.

A round-2 reviewer (codex) then flagged that the raw-IP checks asserted only "nonzero" /
"exit 22", which would not DISTINGUISH a network/proxy denial from a reachable-host HTTP
error. Closed: check 4b now asserts the SPECIFIC L3-failure curl codes (6/7/28), so a
reachable-host HTTP error (22) would FAIL it; check 4c adds a proxy-log assertion confirming
the 403 is tinyproxy's filter refusal of `1.1.1.1`, not an origin error leaked through. Now
9/9. This is why a three-reviewer panel matters: the two grok reviewers passed 4b/4c as
written; codex caught the assertion-precision gap.

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
bash run-spike.sh        # builds images, brings up proxy+agent, asserts 9/9, tears down
```

The script tears down only the containers it started (`docker compose down -v`); it does
not touch unrelated containers.
