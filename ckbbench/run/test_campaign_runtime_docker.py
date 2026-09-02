from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from ckbbench.run.campaign_runtime import DockerTransactionKeyHolder, PrivateSignerEntry
from ckbbench.run.devnet import mentions_exact_name
from ckbbench.run.suite_release import load_suite_release
from ckbbench.run.testnet_integration import LeasedSignerInput


pytestmark = pytest.mark.skipif(
    os.getenv("CKBBENCH_RUNTIME_DOCKER_TEST") != "1",
    reason="set CKBBENCH_RUNTIME_DOCKER_TEST=1 for the local Docker boundary",
)


_VERIFY_SIGNATURE_SCRIPT = r"""
import {bytesFrom, ClientPublicTestnet, hashCkb, Transaction} from '@ckb-ccc/core';
import {secp256k1} from '@noble/curves/secp256k1';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const client = new ClientPublicTestnet({url: 'http://127.0.0.1:1'});
const script = (value) => value === null ? undefined : ({
  codeHash: value.code_hash, hashType: value.hash_type, args: value.args,
});
const point = (value) => ({txHash: value.tx_hash, index: value.index});
const transaction = payload.transaction;
const cells = new Map(payload.cells.map((row) => [`${row.tx_hash}:${row.index}`, row]));
const tx = Transaction.from({
  version: transaction.version,
  cellDeps: transaction.cell_deps.map((row) => ({
    outPoint: point(row.out_point), depType: row.dep_type === 'dep_group' ? 'depGroup' : row.dep_type,
  })),
  headerDeps: transaction.header_deps,
  inputs: transaction.inputs.map((row) => {
    const cell = cells.get(`${row.previous_output.tx_hash}:${Number(BigInt(row.previous_output.index))}`);
    if (!cell) throw new Error('cell');
    return {
      previousOutput: point(row.previous_output), since: row.since,
      cellOutput: {
        capacity: `0x${BigInt(cell.capacity_shannons).toString(16)}`,
        lock: script(payload.own_lock),
      },
      outputData: '0x',
    };
  }),
  outputs: transaction.outputs.map((row) => ({
    capacity: row.capacity, lock: script(row.lock), type: script(row.type),
  })),
  outputsData: transaction.outputs_data,
  witnesses: transaction.witnesses,
});
const witness = tx.getWitnessArgsAt(0);
if (!witness || !witness.lock) throw new Error('witness');
const signature = bytesFrom(witness.lock);
if (signature.length !== 65) throw new Error('signature');
witness.lock = `0x${'00'.repeat(65)}`;
tx.setWitnessArgsAt(0, witness);
const info = await tx.getSignHashInfo(script(payload.own_lock), client);
if (!info) throw new Error('message');
const publicKey = secp256k1.Signature.fromCompact(signature.slice(0, 64))
  .addRecoveryBit(signature[64])
  .recoverPublicKey(bytesFrom(info.message))
  .toBytes(true);
if (hashCkb(publicKey).slice(0, 42) !== payload.own_lock.args) throw new Error('public-key');
process.stdout.write('valid');
"""


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        **kwargs,
    )


def _absent(kind: str, name: str) -> bool:
    completed = _run(["docker", kind, "inspect", name])
    output = (completed.stdout or "") + (completed.stderr or "")
    return (
        completed.returncode != 0
        and f"no such {kind}" in output.lower()
        and mentions_exact_name(output, name)
    )


def test_frozen_images_preserve_stop_before_grade_and_exact_cleanup(tmp_path: Path):
    release = load_suite_release(Path("suites/ckb-independent-v1"))
    agent_image = release.suite.pins.agent_image_digest
    verifier_image = release.suite.pins.verifier_image_digest
    suffix = uuid.uuid4().hex[:16]
    agent_name = f"ckbbench-runtime-agent-{suffix}"
    verifier_name = f"ckbbench-runtime-verifier-{suffix}"
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o755)
    (workspace / "INSTRUCTIONS.md").write_text("isolated task\n", encoding="ascii")
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    events: list[str] = []

    assert _absent("container", agent_name)
    assert _absent("container", verifier_name)
    try:
        started = _run([
            "docker", "run", "-d", "--name", agent_name,
            "--network", "none", "--user", uid_gid,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{workspace}:/workspace", "-w", "/workspace",
            "--entrypoint", "sh", agent_image, "-c",
            "test -f INSTRUCTIONS.md && : > agent-started && while :; do sleep 1; done",
        ])
        assert started.returncode == 0
        deadline = time.monotonic() + 10
        while not (workspace / "agent-started").is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (workspace / "agent-started").is_file()
        events.append("agent-started")

        stopped = _run(["docker", "container", "rm", "-f", agent_name])
        assert stopped.returncode == 0
        assert _absent("container", agent_name)
        events.append("agent-stopped")

        graded = _run([
            "docker", "run", "--rm", "--name", verifier_name,
            "--network", "none", "--user", uid_gid,
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{workspace}:/workspace:ro", "-w", "/workspace",
            "--entrypoint", "sh", verifier_image, "-c",
            "test -f INSTRUCTIONS.md && test -f agent-started",
        ])
        assert graded.returncode == 0
        assert _absent("container", verifier_name)
        events.append("verifier-finished")
    finally:
        for name in (agent_name, verifier_name):
            if not _absent("container", name):
                _run(["docker", "container", "rm", "-f", name])

    assert events == ["agent-started", "agent-stopped", "verifier-finished"]


def test_networkless_key_holder_runs_without_retaining_the_synthetic_key():
    release = load_suite_release(Path("suites/ckb-independent-v1"))
    suffix = uuid.uuid4().hex[:16]
    runtime_namespace = f"ckbbench-key-holder-{suffix}"
    entry = PrivateSignerEntry(
        slot_id="slot-synthetic",
        retry_ordinal=0,
        signer_handle="signer-synthetic",
        public_address="ckt1-synthetic",
        private_key="0x" + "1" * 64,
        own_lock={
            "args": "0x" + "2" * 40,
            "code_hash": "0x" + "3" * 64,
            "hash_type": "type",
        },
        lease_resource_id="lease-synthetic",
        leased_inputs=(LeasedSignerInput(
            tx_hash="0x" + "4" * 64,
            index=0,
            capacity_shannons=30_000_000_000,
        ),),
    )
    holder = DockerTransactionKeyHolder(
        entry,
        image=release.suite.pins.agent_image_digest,
        runtime_namespace=runtime_namespace,
    )

    address, lock = holder.inspect_public_binding()

    assert address.startswith("ckt")
    assert lock["code_hash"].startswith("0x")
    assert len(lock["args"]) == 42
    assert _absent("container", f"{runtime_namespace}-signer")
    assert entry.private_key not in json.dumps({"address": address, "lock": lock})

    signing_entry = PrivateSignerEntry(
        slot_id=entry.slot_id,
        retry_ordinal=entry.retry_ordinal,
        signer_handle=entry.signer_handle,
        public_address=address,
        private_key=entry.private_key,
        own_lock=lock,
        lease_resource_id=entry.lease_resource_id,
        leased_inputs=entry.leased_inputs,
    )
    signer = DockerTransactionKeyHolder(
        signing_entry,
        image=release.suite.pins.agent_image_digest,
        runtime_namespace=runtime_namespace,
    )
    unsigned = {
        "cell_deps": [{
            "dep_type": "dep_group",
            "out_point": {
                "index": "0x0",
                "tx_hash": "0xf8de3bb47d055cdf460d93a2a6e1b05f7432f9777c8c474abf4eec1d4aee5d37",
            },
        }],
        "header_deps": [],
        "inputs": [{
            "previous_output": {"index": "0x0", "tx_hash": "0x" + "4" * 64},
            "since": "0x0",
        }],
        "outputs": [{"capacity": hex(29_900_000_000), "lock": lock, "type": None}],
        "outputs_data": ["0x"],
        "version": "0x0",
        "witnesses": ["0x"],
    }

    signed = signer.sign_transaction(unsigned)

    assert {key: value for key, value in signed.items() if key != "witnesses"} == {
        key: value for key, value in unsigned.items() if key != "witnesses"
    }
    assert signed["witnesses"] != ["0x"]
    assert all(value.startswith("0x") for value in signed["witnesses"])
    assert _absent("container", f"{runtime_namespace}-signer")

    verified = _run(
        [
            "docker", "run", "--rm", "--network", "none",
            "--user", "65532:65532", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "64",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "-i",
            "--entrypoint", "node", release.suite.pins.agent_image_digest,
            "--input-type=module", "-e", _VERIFY_SIGNATURE_SCRIPT,
        ],
        input=json.dumps({
            "cells": [row.to_dict() for row in signing_entry.leased_inputs],
            "own_lock": signing_entry.own_lock,
            "transaction": signed,
        }, sort_keys=True, separators=(",", ":")),
    )
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout == "valid"
