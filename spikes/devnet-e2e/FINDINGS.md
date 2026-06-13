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

## Trust boundary (rebuilt after adversarial review, 2026-06-12)

The FIRST cut let the AGENT write `harness_meta.json` (the tip + nonce), and the verifier trusted
it. Adversarial reviewers (grok-build, grok-composer) correctly flagged this as fatal: a lying agent
controls its own freshness baseline, so anti-cheat was never actually proven. The spike was
restructured into three trust zones, modelling ADR-0009:

- `harness-prepare.js` (HARNESS pre-step, before the agent wakes): captures the real tip and the
  per-run nonce, and writes them to TWO places — `proof/task.json` (agent-visible: just the amount
  to send + recipient) and `verifier-private/secret.json` (verifier-only: tip + nonce + recipient).
- `send-proof.js` (AGENT): reads ONLY `task.json`, sends the tx, writes ONLY `tx_id.txt`. It has no
  access to the tip or to the fact that the amount is a nonce.
- `verify-proof.js` (VERIFIER, direct RPC only): reads the agent's `tx_id.txt` and its OWN
  `verifier-private/secret.json` (never an agent-written file). The agent cannot influence the
  freshness baseline or the expected nonce.

STRUCTURE was also tightened from "an output exists" (`outputs.find`) to "EXACTLY ONE output to the
recipient, carrying exactly the nonce" — rejecting extra/unintended outputs to that recipient.

## The decisive results (valid + four negatives, incl. a real borrow attack)

| Case | Setup | Verifier result |
|---|---|---|
| A valid | real tx, harness baseline 802, nonce 31337 | **PASS** (block 807 >= 802, exactly-one nonce output) |
| B stale | same tx, baseline forced above its block | **FAIL** STALE |
| C wrong amount | verifier-private nonce != tx amount | **FAIL** STRUCTURE |
| D nonexistent | tx hash `0xdeadbeef...` | **FAIL** not found |
| E **borrow/replay** | run 2 (nonce 42424, baseline 820) fed run 1's committed tx_id | **FAIL** STALE (block 807 < 820); and the nonce 31337 != 42424 would also fail STRUCTURE |

Case E is the real ADR-0001 attack: a cheating agent copies another run's tx_id instead of doing
the work. It fails two independent ways — the harness-owned freshness baseline (the borrowed tx
predates run 2) and the verifier-private amount-nonce (it carries the wrong run's amount). No
uniqueness database, and no agent-writable integrity input.

### Round-2 hardening (after the re-review, 2026-06-12)

Both round-2 reviewers (grok-build, grok-composer) raised the same deepest residual: with a fixed/
predictable nonce, the verifier only checks an *observable effect*, so on a public chain a spectator
or arranged tx whose outputs coincidentally match could be borrowed by id. Closed by making the
amount-as-nonce HIGH-ENTROPY: `harness-prepare.js` now generates a random shannon-precise amount
(100 CKB base + ~1e9 random low-shannon offset) per run. The probability any unrelated tx pays
EXACTLY that many shannons to EXACTLY this recipient is negligible, so a matching committed tx is
bound to this run — entropy, not the freshness window alone, defeats the spectator-tx surface.

The whole flow is now a self-verifying script, `run-spike.sh`, that asserts (by exit code, not by
grepping output) the valid Proof passes and all four negatives reject. Latest run: **5/5**
(`valid passes`, `stale rejected`, `wrong-nonce rejected`, `nonexistent rejected`, `borrowed rejected`).
Stale pre-split `harness_meta.json` artifacts were removed.

Residual (tracked, design-level, not spike-blocking): the verifier proves the observable effect, not
that the agent *constructed* the tx (no binding to the agent's keys/inputs). With a high-entropy
verifier-private nonce this is acceptable for an effect-based On-chain Task; a future task type that
must bind authorship would add an agent-key-signed marker (witness/data) the verifier checks.

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
NONCE_CKB=31337 node harness-prepare.js ./proof ./verifier-private   # harness pre-step
node send-proof.js ./proof                                            # agent -> tx_id.txt only
sleep 4                                                               # let it commit (1s blocks)
node verify-proof.js ./proof ./verifier-private                       # verifier -> PASS
# borrow test: prepare run 2 with a different nonce, feed it run 1's tx_id, verify -> FAIL.
docker compose down -v
```
