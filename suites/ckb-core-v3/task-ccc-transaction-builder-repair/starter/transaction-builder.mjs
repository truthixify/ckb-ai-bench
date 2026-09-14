#!/usr/bin/env node
import fs from "node:fs";
import { ccc } from "@ckb-ccc/core";

const [source, destination] = process.argv.slice(2);
const request = JSON.parse(fs.readFileSync(source, "utf8"));
const first = request.available_inputs[0];
const lock = ({ code_hash, hash_type, args }) => ({ codeHash: code_hash, hashType: hash_type, args });
const transaction = ccc.Transaction.from({
  outputs: [{ capacity: BigInt(request.recipient_capacity), lock: lock(request.recipient_lock) }],
  outputsData: ["0x"],
});

fs.writeFileSync(destination, JSON.stringify({
  cell_deps: [],
  header_deps: [],
  inputs: [{ id: first.id }],
  outputs: [{ capacity: request.recipient_capacity, lock: request.recipient_lock, type: null }],
  outputs_data: transaction.outputsData,
  version: "0x0",
  witnesses: [],
}));
