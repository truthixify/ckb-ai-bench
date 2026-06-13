# Verifier runs in a clean hermetic container, fed by the mounted folder

> **Status: accepted (2026-06-12), supersedes the earlier "Verifier runs inside the agent container"
> draft.** The in-container verifier existed only because an in-container OffCKB devnet was unreachable
> elsewhere; ADR-0007 made DevNet a sidecar reachable over the docker network, so that reason is gone.

## Context

Proofs are written by the agent into a shared area; the Verifier must read them and grade independently,
while the Hidden suite and Verifier-private params stay out of the agent's reach until verify time
(ADR-0002, ADR-0009). With a sidecar DevNet (ADR-0007), the chain is reachable by RPC from any container
on the docker network, not only from inside the agent container.

## Decision

The run's **mounted host folder is the channel** for Proofs (agent writes them during the run). At verify
time the harness runs the Verifier in a **clean, hermetic Verifier container** — a separate pinned image
(its own Rust + Node toolchain, no agent-side pollution) — that:

- reads the agent's Proofs and built artifacts from the mounted folder (read-only),
- reaches the chain by **direct RPC**: the sidecar DevNet or the external TestNet node (only the RPC URL
  differs between chain profiles — now symmetric),
- receives the **Hidden suite and Verifier-private params only at verify time**, after the `done`
  sentinel has ended the agent's run, so the agent never sees them.

For Code Tasks the hermetic container compiles and runs the Rust ckb-testtool suite against the agent's
binary off-chain — this needs the binary plus the toolchain, not the agent's environment.

## Consequences

The Verifier is now genuinely hermetic: it inherits nothing the agent could tamper with (no reliance on
the agent's PATH or installed tools), which is strictly better for trust than the earlier in-container
design. DevNet and TestNet verification are symmetric (RPC by URL). The injection-timing guarantee
(Hidden suite / secrets appear only post-`done`) is unchanged; only the Verifier's location moved out of
the agent container into its own.
