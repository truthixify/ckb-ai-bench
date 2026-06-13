# All container egress routed through a logging proxy

## Context

Arms A and D forbid web research. Relying on the agent to self-report web access is worthless (a
cheating agent won't), and parsing bash commands for network calls is leaky. Earlier the plan was
prompt-only enforcement with best-effort logging, which leaves A/D open to silent cheating.

## Decision

Route **all agent-container egress through a single logging proxy** (proxy env vars plus an iptables
redirect so traffic cannot bypass it). The proxy **logs every destination**, giving a machine-observed
(not self-reported) record of web access. This is the protocol-violation signal.

On the no-research arms (A, D) the proxy additionally **blocks** everything except an allowlist,
turning A/D from a compliance experiment into a hard network-level control.

The allowlist is **per-(chain profile × arm)**, not a single fixed list, and its exact entries are
deferred to test time (they will shift during development). The MCP endpoint's location is not yet known
(LAN or internet) and whatever it is, it passes through the proxy like any other allowlisted host. With
DevNet now a sidecar (ADR-0007), **both** DevNet and TestNet RPC are observable egress through the proxy
(uniform), so chain RPC is an allowlist entry on every chain rather than invisible loopback.

## Consequences

This is more infrastructure than prompt-only enforcement, but it removes the "A/D can silently cheat"
criticism entirely and is the network-layer enforcement earlier reviews asked for. Web access is
observed for all arms and enforced on the no-research arms.

**Validated as load-bearing by Spike 3 (2026-06-12, `agent/SPIKE_MODEL_LOOP_FINDINGS.md`).** Running
the model loop on a `LocalEnvironment` with no proxy, the MCP-OFF arm answered an agent-tip task by
reaching CKB over the host network — zero MCP tools used, but the network was not actually gated. That
is precisely the gap this proxy closes: without it, OFF-arm data isolation is only *visible* (no MCP
surface), not *enforced* (open network). The spike makes this concrete rather than speculative.
