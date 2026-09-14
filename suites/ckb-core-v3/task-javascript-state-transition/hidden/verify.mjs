import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const { hexFrom, hashTypeToBytes, Transaction } = require("@ckb-ccc/core");
const {
  DEFAULT_SCRIPT_ALWAYS_SUCCESS,
  DEFAULT_SCRIPT_CKB_JS_VM,
  Resource,
  Verifier,
} = require("ckb-testtool");

const candidate = process.env.TOP + "/build/release/state-transition.bc";
const name = process.argv[2];

function data(value, size = 8) {
  if (size !== 8) return "0x" + "00".repeat(size);
  const bytes = Buffer.alloc(8);
  bytes.writeBigUInt64LE(BigInt(value));
  return "0x" + bytes.toString("hex");
}

function transaction(before, after, { unrelated = false } = {}) {
  const resource = Resource.default();
  const passCell = resource.mockCellAsCellDep(hexFrom(readFileSync(DEFAULT_SCRIPT_ALWAYS_SUCCESS)));
  const pass = resource.createScriptByData(passCell, "0x");
  const candidateCell = resource.mockCellAsCellDep(hexFrom(readFileSync(candidate)));
  const candidateScript = resource.createScriptByData(candidateCell, "0x");
  const vmCell = resource.mockCellAsCellDep(hexFrom(readFileSync(DEFAULT_SCRIPT_CKB_JS_VM)));
  const vmScript = resource.createScriptByData(vmCell, hexFrom(
    "0x0000" + candidateScript.codeHash.slice(2) + hexFrom(hashTypeToBytes(candidateScript.hashType)).slice(2),
  ));
  const inputs = before.map((value) => Resource.createCellInput(resource.mockCell(pass, vmScript, value)));
  const outputs = after.map(() => Resource.createCellOutput(pass, vmScript));
  const outputsData = [...after];
  if (unrelated) {
    inputs.splice(1, 0, Resource.createCellInput(resource.mockCell(pass, undefined, data(99))));
    outputs.splice(1, 0, Resource.createCellOutput(pass));
    outputsData.splice(1, 0, data(3));
  }
  return [resource, Transaction.from({
    cellDeps: [
      Resource.createCellDep(passCell, "code"),
      Resource.createCellDep(candidateCell, "code"),
      Resource.createCellDep(vmCell, "code"),
    ],
    inputs,
    outputs,
    outputsData,
  })];
}

const cases = {
  "increments-one-state": { before: [data(0)], after: [data(1)], pass: true },
  "increments-multiple-group-cells": { before: [data(7), data(1000)], after: [data(8), data(1001)], pass: true },
  "ignores-unrelated-cells": { before: [data(12), data(55)], after: [data(13), data(56)], pass: true, unrelated: true },
  "rejects-unchanged-state": { before: [data(9)], after: [data(9)], pass: false },
  "rejects-skipped-state": { before: [data(9)], after: [data(11)], pass: false },
  "rejects-invalid-later-state": { before: [data(2), data(9)], after: [data(3), data(11)], pass: false },
  "rejects-malformed-input-data": { before: [data(1, 7)], after: [data(2)], pass: false },
  "rejects-malformed-output-data": { before: [data(1)], after: [data(2, 7)], pass: false },
  "rejects-missing-group-output": { before: [data(1), data(8)], after: [data(2)], pass: false },
  "rejects-extra-group-output": { before: [data(1)], after: [data(2), data(3)], pass: false },
  "rejects-overflow": { before: [data(0xffffffffffffffffn)], after: [data(0)], pass: false },
};

const selected = cases[name];
if (!selected) throw new Error("unknown case");
const [resource, tx] = transaction(selected.before, selected.after, selected);
const result = await Verifier.from(resource, tx).verify();
const passed = result.every((row) => row.scriptErrorCode === 0);
if (passed !== selected.pass) process.exit(1);
