# Spike: on-chain Proof + verifier on the devnet sidecar — FINDINGS (2026-06-12)

Goal (Tier-1 #2): prove an On-chain Task end to end on the `nervos/ckb --chain dev` sidecar — send
a real transaction, write its id as a Proof, and have a verifier that uses ONLY direct CKB RPC
distinguish a valid Proof from a stale/borrowed/forged one (ADR-0001, ADR-0005).

## Sidecar (rebuilt as committed config this time)

`docker-compose.yml` runs `nervos/ckb:v0.207.0` as `ckb-node` (`run --indexer`) + `ckb-miner`
(`miner`) on an isolated network `ckb-e2e-net`, RPC on host `:18120`. Config in `config/` is the
stock `ckb init --chain dev` output (extracted via `docker cp`), patched in three places:

- `ckb.toml`: added `Indexer` to `rpc.modules`; appended a `[block_assembler]` mining to the funded
  genesis lock (secp256k1, args `0xc8328aab...`).
- `ckb-miner.toml`: `rpc_url` → `http://ckb-node:8114/` (container hostname, not the bind address);
  dummy worker interval 5000 → 1000 ms for a snappy spike.

Brought up clean: tip advanced 0 → 0x10 at ~1 block/s; funded lock balance ~20 000 CKB (growing).

## What was done (live)

- `send-proof.js` (the AGENT side): captures the harness tip (direct RPC), then uses CCC
  (`@ckb-ccc/core`) to build + sign + send a transfer of exactly the nonce amount (12345 CKB) to a
  recipient lock, and writes `proof/tx_id.txt` + `proof/harness_meta.json`. Sent tx
  `0x1c86b2b1...`, committed in block 217 (baseline was 213).
- `verify-proof.js` (the VERIFIER side, direct RPC only): reads the Proof and enforces ADR-0001's
  three stateless checks — EXISTS (committed), FRESH (tx block >= harness tip), STRUCTURE (exactly
  the nonce amount to the expected recipient lock). No uniqueness database.

## The decisive results (one valid + three negatives)

| Case | Setup | Verifier result |
|---|---|---|
| A valid | real tx, baseline 213, nonce 12345 | **PASS** (block 217 >= 213, nonce matches) |
| B stale | same tx, baseline forced to 99999 | **FAIL** STALE (block 217 < 99999) |
| C wrong amount | same tx, expected nonce 99999 | **FAIL** STRUCTURE (no output of that amount) |
| D nonexistent | tx hash `0xdeadbeef...` | **FAIL** not found |

The freshness window and the amount-as-nonce both bite exactly as ADR-0001 designs: a tx that
predates the run (B) or that wasn't produced for this run's nonce (C) cannot pass, with no database.

## CCC + custom devnet (the OffCKB pain, solved cleanly)

CCC's `ClientPublicTestnet` takes a custom `url` AND a custom `scripts` map. The devnet
secp256k1_blake160 sighash script has the same `codeHash` as testnet but its cellDep out-point is
in the **devnet genesis** (genesis `tx[1]` index 0, a `depGroup` — confirmed via
`get_block_by_number(0x0)`). We spread `TESTNET_SCRIPTS` (from `@ckb-ccc/core/advanced`) so every
KnownScript codeHash is present (the signer enumerates several incl. AnyoneCanPay) and override only
secp256k1's cellDep. Unlike OffCKB, CCC IS a clean remote client against a foreign node.

## Symmetry (ADR-0005)

The verifier targets the chain purely by RPC URL (`VERIFY_RPC` env). The same code runs against the
testnet archive node (`192.168.0.73:18114`) — DevNet and TestNet verification differ only by URL.

## Reproduce

```
docker compose up -d
node send-proof.js ./proof 12345          # agent side -> Proof
node verify-proof.js ./proof              # verifier  -> PASS
# negatives are reproduced by editing harness_meta.json (see commit / spike script).
docker compose down -v
```
