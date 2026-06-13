// Spike (NOT production): the AGENT side of an On-chain Task.
//
// Simulates what an agent does for a "send N CKB to address X" Task:
//   1. read the harness tip (here we just capture it ourselves and persist it as
//      the harness baseline; in the real harness the harness captures it),
//   2. build + sign + send a transfer of EXACTLY the nonce amount to the recipient,
//   3. write the resulting tx hash to a Proof file at a known path.
//
// The amount is the integrity nonce (Verifier-private in the real run). Here it is
// passed in so the verifier can check the structure independently.

import { writeFileSync } from "node:fs";
import { SignerCkbPrivateKey } from "@ckb-ccc/core";
import { devnetClient, GENESIS_PRIVKEY, DEVNET_RPC } from "./devnet-config.js";

const OUT_DIR = process.argv[2] ?? "./proof";
const NONCE_AMOUNT_CKB = BigInt(process.argv[3] ?? "12345"); // the amount-as-nonce
// A fresh recipient lock (the run-params recipient). PUBLIC dev key #2's args.
const RECIPIENT_ARGS = "0x470dcdc5e44064909650113a274b3b36aecb6dc7";

const client = devnetClient();
const signer = new SignerCkbPrivateKey(client, GENESIS_PRIVKEY);

async function rawRpc(method, params = []) {
  const r = await fetch(DEVNET_RPC, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ id: 1, jsonrpc: "2.0", method, params }),
  });
  return (await r.json()).result;
}

// 1. capture the harness tip baseline (direct RPC)
const tipBefore = parseInt(await rawRpc("get_tip_block_number"), 16);
console.log(`harness tip (baseline) = ${tipBefore}`);

// 2. build the transfer. Recipient = secp256k1_blake160 lock with the given args.
const { Script, Transaction, KnownScript } = await import("@ckb-ccc/core");
const recipientLock = await Script.fromKnownScript(
  client,
  KnownScript.Secp256k1Blake160,
  RECIPIENT_ARGS,
);

const tx = Transaction.from({
  outputs: [{ lock: recipientLock, capacity: NONCE_AMOUNT_CKB * 100_000_000n }],
});
await tx.completeInputsByCapacity(signer);
await tx.completeFeeBy(signer, 1000);
const txHash = await signer.sendTransaction(tx);
console.log(`sent tx = ${txHash}`);

// 3. write the Proof + the harness baseline (the harness owns the baseline in prod).
writeFileSync(`${OUT_DIR}/tx_id.txt`, txHash.trim() + "\n");
writeFileSync(
  `${OUT_DIR}/harness_meta.json`,
  JSON.stringify(
    {
      harness_tip: Number(tipBefore),
      nonce_amount_ckb: NONCE_AMOUNT_CKB.toString(),
      recipient_args: RECIPIENT_ARGS,
    },
    null,
    2,
  ) + "\n",
);
console.log(`proof written to ${OUT_DIR}/tx_id.txt`);
