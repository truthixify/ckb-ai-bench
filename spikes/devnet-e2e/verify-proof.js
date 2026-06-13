// Spike (NOT production): the VERIFIER side of an On-chain Task (ADR-0001).
//
// Reads the agent's Proof (tx_id.txt) and the harness baseline (harness_meta.json),
// then grades it using ONLY direct CKB RPC (never the MCP server). It enforces the
// three stateless integrity checks, with no uniqueness database:
//
//   1. EXISTS    — the tx hash resolves to a committed transaction on chain.
//   2. FRESH     — the tx's enclosing block number is >= the harness tip captured
//                  at run-start (a borrowed/stale tx from before the run fails).
//   3. STRUCTURE — exactly one output carries the nonce amount to the expected
//                  recipient lock (the amount-as-nonce binds the tx to this run).
//
// Exit 0 = pass, non-zero = fail, with a clear reason. This is what the hermetic
// Verifier container runs; here we run it against the live sidecar by RPC.

import { readFileSync } from "node:fs";
import { DEVNET_RPC } from "./devnet-config.js";

const OUT_DIR = process.argv[2] ?? "./proof";
const RPC = process.env.VERIFY_RPC ?? DEVNET_RPC;

// secp256k1_blake160 system script code hash (same on devnet/testnet).
const SECP_CODE_HASH =
  "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8";

async function rpc(method, params = []) {
  const r = await fetch(RPC, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ id: 1, jsonrpc: "2.0", method, params }),
  });
  const j = await r.json();
  if (j.error) throw new Error(`${method}: ${JSON.stringify(j.error)}`);
  return j.result;
}

function fail(reason) {
  console.error(`VERIFY FAIL: ${reason}`);
  process.exit(1);
}

const txId = readFileSync(`${OUT_DIR}/tx_id.txt`, "utf8").trim();
const meta = JSON.parse(readFileSync(`${OUT_DIR}/harness_meta.json`, "utf8"));
const harnessTip = meta.harness_tip;
const nonceShannons = BigInt(meta.nonce_amount_ckb) * 100_000_000n;
const recipientArgs = meta.recipient_args.toLowerCase();

console.log(`verifying tx ${txId}`);
console.log(`  harness tip baseline = ${harnessTip}`);
console.log(`  expected nonce = ${meta.nonce_amount_ckb} CKB to ${recipientArgs}`);

// 1. EXISTS (and is committed)
const txw = await rpc("get_transaction", [txId]);
if (!txw || !txw.transaction) fail("transaction not found on chain");
const status = txw.tx_status?.status;
if (status !== "committed") fail(`tx status is '${status}', not committed`);

// 2. FRESH — find the block number that committed this tx, compare to harness tip.
const blockHash = txw.tx_status.block_hash;
if (!blockHash) fail("committed tx has no block_hash");
const header = await rpc("get_header", [blockHash]);
const blockNumber = parseInt(header.number, 16);
console.log(`  tx committed in block ${blockNumber}`);
if (blockNumber < harnessTip) {
  fail(
    `STALE: tx block ${blockNumber} < harness tip ${harnessTip} (tx predates the run)`,
  );
}

// 3. STRUCTURE — exactly the nonce amount to the recipient lock.
const outputs = txw.transaction.outputs;
const match = outputs.find(
  (o) =>
    o.lock.code_hash.toLowerCase() === SECP_CODE_HASH &&
    o.lock.args.toLowerCase() === recipientArgs &&
    BigInt(o.capacity) === nonceShannons,
);
if (!match) {
  fail(
    `STRUCTURE: no output of exactly ${nonceShannons} shannons to ${recipientArgs}`,
  );
}

console.log("VERIFY PASS: exists + fresh + correct nonce structure");
process.exit(0);
