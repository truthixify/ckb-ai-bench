// Spike (NOT production): the HARNESS side of an On-chain Task pre-step (ADR-0009).
//
// This runs BEFORE the agent wakes. It owns the integrity inputs the agent must
// never control:
//   - harness_tip: the chain tip at run-start (the freshness baseline),
//   - nonce_amount_ckb: a per-run random nonce amount (the amount-as-nonce),
//   - recipient_args: the run's recipient lock args.
//
// It writes TWO files into separate trust zones:
//   - <agentDir>/task.json     -> PROMPT-INJECTED params (recipient + the exact
//                                 amount the agent is told to send). Agent-readable.
//   - <verifierDir>/secret.json -> VERIFIER-PRIVATE params (harness_tip + the same
//                                 nonce + recipient). NEVER in the agent's view.
//
// The agent writes only tx_id.txt. The verifier reads secret.json (its own), never
// anything the agent could forge. This is the trust boundary the first cut elided.

import { mkdirSync, writeFileSync } from "node:fs";
import { DEVNET_RPC } from "./devnet-config.js";

const AGENT_DIR = process.argv[2] ?? "./proof";
const VERIFIER_DIR = process.argv[3] ?? "./verifier-private";
// Deterministic per-run nonce passed in by the harness (here via arg/env so the
// script stays pure and re-runnable). A random integer amount in CKB.
const NONCE_AMOUNT_CKB = process.env.NONCE_CKB ?? process.argv[4] ?? "12345";
const RECIPIENT_ARGS = "0x470dcdc5e44064909650113a274b3b36aecb6dc7";

async function rawRpc(method, params = []) {
  const r = await fetch(DEVNET_RPC, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ id: 1, jsonrpc: "2.0", method, params }),
  });
  return (await r.json()).result;
}

mkdirSync(AGENT_DIR, { recursive: true });
mkdirSync(VERIFIER_DIR, { recursive: true });

const harnessTip = parseInt(await rawRpc("get_tip_block_number"), 16);

// Agent-visible: only what the agent legitimately needs to do the Task.
writeFileSync(
  `${AGENT_DIR}/task.json`,
  JSON.stringify(
    { send_amount_ckb: NONCE_AMOUNT_CKB, recipient_args: RECIPIENT_ARGS },
    null,
    2,
  ) + "\n",
);

// Verifier-private: the baseline + the nonce, held harness-side.
writeFileSync(
  `${VERIFIER_DIR}/secret.json`,
  JSON.stringify(
    {
      harness_tip: harnessTip,
      nonce_amount_ckb: String(NONCE_AMOUNT_CKB),
      recipient_args: RECIPIENT_ARGS,
    },
    null,
    2,
  ) + "\n",
);

console.log(
  `harness prepared: tip=${harnessTip}, nonce=${NONCE_AMOUNT_CKB} CKB -> ${RECIPIENT_ARGS}`,
);
console.log(`  agent task    -> ${AGENT_DIR}/task.json`);
console.log(`  verifier secret -> ${VERIFIER_DIR}/secret.json`);
