# Spike: DevNet sidecar topology — FINDINGS (2026-06-12)

Goal: decide how DevNet runs (in-container OffCKB vs a sidecar node), and whether OffCKB can act as a
remote RPC client against a separately-run node. All tests run live with Docker 29.3.

## What was tested (live)

- Pulled official `nervos/ckb:v0.207.0` (~50MB compressed / 126MB on disk, multi-arch).
- `ckb init --chain dev` into a volume: produced `ckb.toml`, `ckb-miner.toml`, `specs/dev.toml`.
- Confirmed the stock `dev.toml` ships **pre-funded genesis cells with PUBLIC private keys**
  (lock args `0xc8328a...` ⇐ key `0xd00c06...d2bc`, 200M CKB; `0x470dcd...` ⇐ key `0x63d867...3f24d`).
- Patched `ckb.toml`: added `Indexer` to `rpc.modules`, appended a `[block_assembler]` mining to the
  funded lock. RPC **already binds `0.0.0.0:8114`** out of the box (sidecar-reachable, zero change).
- Ran node + a separate `miner` container on a docker network. After fixing the miner's `rpc_url` to the
  node's container hostname (`http://ckb-spike-node:8114/`, not `0.0.0.0`), **mining worked**: tip climbed
  0 → 39+. Indexer reachable (`get_indexer_tip`, `get_cells`).
- Cross-container reach: a separate `curl` container hit `http://ckb-spike-node:8114` fine.
- Raw JSON-RPC + indexer query from a separate Node container returned the official funded lock's balance:
  **20,005,225,674 CKB across 27 cells** (200M issued + mining rewards) — funding & mining both confirmed.

## The decisive OffCKB result

Installed `@offckb/cli@0.4.6` in a separate container (needs `build-essential` + `python3` — it builds a
native `cpu-features`/node-gyp dep; **`node:20-slim` alone fails to install it**). Pointed
`devnet.rpcUrl` at the remote node via `settings.json`.

- `offckb accounts` works (bundled 20-account list; no RPC). BUT these are **OffCKB's own accounts**,
  funded only in **OffCKB's** chainspec — they have NO balance on the official `nervos/ckb` genesis.
- `offckb balance <addr>` against the remote node **FAILS** — not on networking, but because it shells out
  to a local `ckb list-hashes` and `buildCCCDevnetKnownScripts()` to learn the devnet's system-script
  addresses. That needs OffCKB's **own binary + chainspec on local disk** (`ChainSpec: file not found`
  even after supplying the binary). The "OffCKB is a clean RPC client" reading does NOT survive contact.

## Conclusions

1. **OffCKB cannot cleanly act as a remote client against a foreign node.** Its CLI is coupled to a
   locally-initialized OffCKB devnet (binary + chainspec + config that only `offckb node` creates).
2. **A docker-native devnet sidecar works and needs no OffCKB at all.** `nervos/ckb --chain dev` +
   `miner` (+ in-process Indexer) gives a funded, mining, network-reachable devnet. The agent/verifier
   talk to it with raw JSON-RPC or any standard SDK (CCC) — proven live.
3. The 28114-proxy-fallback question (the original ADR-0007 worry) is **moot**: we are not using OffCKB
   as the devnet, so its proxy indirection never enters the picture.

## Implication for topology

Sidecar via `nervos/ckb --chain dev` is viable and clean — it gives the three sidecar wins (proxy-
observable RPC, hermetic verifier, uniform harness-tip on both chains) WITHOUT OffCKB's coupling. The
cost: we lose OffCKB's conveniences (20 accounts, pre-deployed scripts like Omnilock/Spore/xUDT, auto-
mining wiring). Those must be reproduced: fund from the 2 official dev.toml keys (or add issued_cells to
a custom dev.toml), wire block_assembler+miner ourselves (done here), and deploy any standard scripts we
need as a setup step. Open question for the owner: is losing OffCKB's pre-deployed script set acceptable,
or do we still want OffCKB to OWN the devnet (in-container, its node) and accept it can't be a sidecar?
