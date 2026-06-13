// Spike (NOT production): the VERIFIER side of an On-chain Task (ADR-0001, ADR-0009).
//
// Grades using ONLY direct CKB RPC (never the MCP server) and ONLY verifier-private
// inputs (secret.json, written by the harness pre-step) plus the agent's Proof
// (tx_id.txt). It does NOT trust any harness_tip / nonce the agent could write.
//
// Checks:
//   1. EXISTS    - tx hash resolves to a committed transaction.
//   2. FRESH     - tx's enclosing block number >= the verifier-private harness tip
//                  (a tx mined before run-start fails; the agent cannot raise the bar).
//   3. STRUCTURE - EXACTLY ONE output carries the nonce amount to the expected
//                  recipient lock, and NO OTHER output goes to that recipient
//                  (binds the tx to this run and rejects unintended extra effects).
//
// Exit 0 = pass, non-zero = fail with a reason.

import { readFileSync } from "node:fs";
import { DEVNET_RPC } from "./devnet-config.js";

const AGENT_DIR = process.argv[2] ?? "./proof";
const VERIFIER_DIR = process.argv[3] ?? "./verifier-private";
const RPC = process.env.VERIFY_RPC ?? DEVNET_RPC;

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

// Agent-supplied Proof: ONLY the tx id.
const txId = readFileSync(`${AGENT_DIR}/tx_id.txt`, "utf8").trim();
// Verifier-private integrity inputs: NOT from the agent.
const secret = JSON.parse(readFileSync(`${VERIFIER_DIR}/secret.json`, "utf8"));
const harnessTip = secret.harness_tip;
const nonceShannons = BigInt(secret.nonce_amount_ckb) * 100_000_000n;
const recipientArgs = secret.recipient_args.toLowerCase();

console.log(`verifying tx ${txId}`);
console.log(`  (verifier-private) harness tip = ${harnessTip}`);
console.log(`  (verifier-private) nonce = ${secret.nonce_amount_ckb} CKB -> ${recipientArgs}`);

// 1. EXISTS + committed
const txw = await rpc("get_transaction", [txId]);
if (!txw || !txw.transaction) fail("transaction not found on chain");
const status = txw.tx_status?.status;
if (status !== "committed") fail(`tx status is '${status}', not committed`);

// 2. FRESH
const blockHash = txw.tx_status.block_hash;
if (!blockHash) fail("committed tx has no block_hash");
const header = await rpc("get_header", [blockHash]);
const blockNumber = parseInt(header.number, 16);
console.log(`  tx committed in block ${blockNumber}`);
if (blockNumber < harnessTip) {
  fail(`STALE: tx block ${blockNumber} < harness tip ${harnessTip} (tx predates the run)`);
}

// 3. STRUCTURE - exactly one nonce-amount output to the recipient, and no other
//    output to that recipient at all (tight: rejects unintended extra effects).
const outputs = txw.transaction.outputs;
const toRecipient = outputs.filter(
  (o) =>
    o.lock.code_hash.toLowerCase() === SECP_CODE_HASH &&
    o.lock.args.toLowerCase() === recipientArgs,
);
if (toRecipient.length !== 1) {
  fail(`STRUCTURE: expected exactly 1 output to ${recipientArgs}, found ${toRecipient.length}`);
}
if (BigInt(toRecipient[0].capacity) !== nonceShannons) {
  fail(
    `STRUCTURE: output to recipient is ${BigInt(toRecipient[0].capacity)} shannons, ` +
      `not the nonce ${nonceShannons}`,
  );
}

console.log("VERIFY PASS: exists + fresh + exactly-one-nonce-output structure");
process.exit(0);
