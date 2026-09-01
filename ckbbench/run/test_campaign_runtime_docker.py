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
