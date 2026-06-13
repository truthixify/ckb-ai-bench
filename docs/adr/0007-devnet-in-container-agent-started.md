# DevNet is a nervos/ckb sidecar; OffCKB is dropped

> **Status: accepted (2026-06-12), supersedes the earlier "DevNet runs in-container via OffCKB" draft.**
> Resolved by live spikes (`spikes/devnet-sidecar/FINDINGS.md`). Replaces the prior in-container/OffCKB
> decision recorded in earlier revisions of this ADR.

## Context

DevNet needs a CKB chain reachable by the agent and the Verifier. We first planned to use OffCKB (the
documented CKB dev CLI) running its node inside the agent container, then reconsidered a sidecar so
DevNet RPC would be observable egress (uniform with TestNet). Live spikes settled both the topology and
whether OffCKB is needed at all.

Spike findings:
- The official **`nervos/ckb:v0.207.0`** image runs `--chain dev` with a **pre-funded genesis whose
  private keys are public** (dev.toml issued-cells), binds RPC on **`0.0.0.0:8114` out of the box**
  (sidecar-reachable, zero config), and has an **in-process indexer**. A `miner` container advancing the
  tip and cross-container RPC/indexer queries all worked. A funded genesis lock showed a real balance
  queried by raw JSON-RPC from a separate container — no OffCKB involved.
- **OffCKB cannot act as a clean remote client.** `offckb balance` against a foreign node fails because
  it shells out to a local `ckb list-hashes` / `buildCCCDevnetKnownScripts` that require OffCKB's *own*
  locally-initialized devnet binary + chainspec (`ChainSpec: file not found`). Its CLI is coupled to a
  devnet OffCKB itself created. The 28114-proxy question is therefore moot.
- Current OffCKB `create` (v0.4.6) scaffolds **JavaScript/CKB-JS-VM** contracts, not Rust. The Rust
  on-chain path is **ckb-script-templates** (cargo-generate + ckb-testtool), independent of OffCKB.

## Decision

**DevNet is a sidecar built on the official `nervos/ckb --chain dev` image** (node + miner +
in-process indexer), on its own docker network, reached by RPC. **OffCKB is dropped from the project
entirely** — we do not want JS/CKB-JS-VM contracts, and the official node + standard SDKs cover the
devnet, funding, and queries OffCKB would have provided. Rust contract scaffolding/testing uses
**ckb-script-templates**, not OffCKB.

The agent does not run `offckb node`; the harness brings the sidecar up. Pre-funding uses the official
dev.toml genesis keys (and/or a custom dev.toml). Standard scripts a Task needs (Omnilock, xUDT, Spore,
ACP) are deployed by a deterministic harness setup step, their out-points recorded into Run params.

## Consequences

The sidecar restores the three wins that motivated it: DevNet RPC is **observable egress** through the
proxy (ADR-0006), the Verifier can move to a **clean hermetic container** (revisit ADR-0005), and the
harness can capture a **Harness tip for both chains**, making the Agent-tip probe uniform. The cost is
that OffCKB's conveniences (pre-deployed scripts, account set, auto-mining wiring) are reproduced by us;
the spike showed that is a bounded, one-time setup (mining wiring was ~4 lines of ckb.toml). ADR-0004's
"OffCKB baked into the image" line is void; the agent image no longer needs OffCKB.
