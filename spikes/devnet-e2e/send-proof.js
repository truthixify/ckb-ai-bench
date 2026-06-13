// Spike (NOT production): the AGENT side of an On-chain Task.
//
// The agent reads ONLY the prompt-injected task params (task.json: how much to
// send, to whom) and writes ONLY its Proof (tx_id.txt). It has no access to the
// harness tip or the fact that the amount is a nonce. This is what an agent in the
// real benchmark can see and do, nothing more.

import { readFileSync, writeFileSync } from "node:fs";
import { SignerCkbPrivateKey } from "@ckb-ccc/core";
import { devnetClient, GENESIS_PRIVKEY } from "./devnet-config.js";

const AGENT_DIR = process.argv[2] ?? "./proof";
const task = JSON.parse(readFileSync(`${AGENT_DIR}/task.json`, "utf8"));
const amountCkb = BigInt(task.send_amount_ckb);
const recipientArgs = task.recipient_args;

const client = devnetClient();
const signer = new SignerCkbPrivateKey(client, GENESIS_PRIVKEY);

const { Script, Transaction, KnownScript } = await import("@ckb-ccc/core");
const recipientLock = await Script.fromKnownScript(
  client,
  KnownScript.Secp256k1Blake160,
  recipientArgs,
);

const tx = Transaction.from({
  outputs: [{ lock: recipientLock, capacity: amountCkb * 100_000_000n }],
});
await tx.completeInputsByCapacity(signer);
await tx.completeFeeBy(signer, 1000);
const txHash = await signer.sendTransaction(tx);

writeFileSync(`${AGENT_DIR}/tx_id.txt`, txHash.trim() + "\n");
console.log(`agent sent ${amountCkb} CKB -> ${recipientArgs}`);
console.log(`agent proof: ${AGENT_DIR}/tx_id.txt = ${txHash}`);
