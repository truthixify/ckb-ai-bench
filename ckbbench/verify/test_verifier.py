"""Top-level verifier dispatcher tests: kind routing and failure isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ckbbench.suite.model import OnchainVerifierSpec, Task
from ckbbench.verify.codetask import BENCH_PASSWORD_ENV, RunnerInvocation
from ckbbench.verify.onchain import SECP_CODE_HASH
from ckbbench.verify.verifier import verify_suite, verify_task


def _onchain_task(check: str, proof_file: str = "proof.txt") -> Task:
    return Task(
        id="oc",
        prompt_fragment="x",
        score=1,
        proof_file=proof_file,
        kind="onchain",
        verifier=OnchainVerifierSpec(check=check, rpc_method="m"),
    )


def _code_task() -> Task:
    return Task(
        id="code-1",
        prompt_fragment="Build.",
        score=10,
        proof_file="build/release/hashlock",
        kind="code",
        verifier="hidden",
    )


def test_verify_task_missing_proof(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    v = verify_task(_onchain_task("tip_hex"), mount, {}, lambda m, p: "0x10")
    assert not v.passed
    assert "missing" in v.reason


def test_verify_task_onchain_dispatch(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "proof.txt").write_text("0x5\n")
    rpc = lambda m, p: "0x10" if m == "get_tip_block_number" else None
    v = verify_task(_onchain_task("tip_hex"), mount, {}, rpc)
    assert v.passed


def test_verify_task_code_dispatch(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    registry = tmp_path / "registry"
    suite = registry / "code-1" / "hidden"
    suite.mkdir(parents=True)
    calls: list[RunnerInvocation] = []

    def runner(inv: RunnerInvocation) -> int:
        calls.append(inv)
        if inv.stage == "build":
            p = mount / ".ckbbench-artifact" / "build" / "release" / "hashlock"
            p.parent.mkdir(parents=True)
            p.write_text("elf")
        return 0

    v = verify_task(
        _code_task(),
        mount,
        {BENCH_PASSWORD_ENV: "secret"},
        lambda m, p: None,
        registry_root=registry,
        runner=runner,
    )
    assert v.passed
    assert len(calls) == 2


def test_verify_task_code_missing_runner(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    v = verify_task(_code_task(), mount, {}, lambda m, p: None, registry_root=tmp_path)
    assert not v.passed
    assert "runner" in v.reason


def test_verify_task_code_missing_registry_root(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    v = verify_task(
        _code_task(),
        mount,
        {BENCH_PASSWORD_ENV: "x"},
        lambda m, p: None,
        runner=lambda inv: 0,
    )
    assert not v.passed
    assert "registry_root" in v.reason


def test_verify_task_code_missing_suite_dir(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    registry = tmp_path / "registry"
    registry.mkdir()
    v = verify_task(
        _code_task(),
        mount,
        {BENCH_PASSWORD_ENV: "x"},
        lambda m, p: None,
        registry_root=registry,
        runner=lambda inv: 0,
    )
    assert not v.passed
    assert "hidden suite not found" in v.reason


def test_verify_task_onchain_bad_verifier_type(tmp_path: Path):
    task = Task(
        id="bad",
        prompt_fragment="x",
        score=1,
        proof_file="p.txt",
        kind="onchain",
        verifier="not-a-spec",
    )
    mount = tmp_path / "m"
    mount.mkdir()
    (mount / "p.txt").write_text("0x1")
    v = verify_task(task, mount, {}, lambda m, p: "0x1")
    assert not v.passed
    assert "OnchainVerifierSpec" in v.reason


def test_verify_suite_failure_isolation(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "epoch.txt").write_text("0x1\n")

    tasks = (
        _onchain_task("tip_hex", "missing.txt"),
        _onchain_task("epoch_number", "epoch.txt"),
        Task(
            id="boom",
            prompt_fragment="x",
            score=1,
            proof_file="x.txt",
            kind="onchain",
            verifier=OnchainVerifierSpec(check="tip_hex", rpc_method="m"),
        ),
    )
    (mount / "x.txt").write_text("0x5")

    def rpc(method: str, params: list) -> object:
        if method == "get_current_epoch":
            return {"number": "0x1"}
        if method == "get_tip_block_number":
            if params == ["boom"]:
                raise RuntimeError("should not happen")
            return "0x10"
        return None

    results = verify_suite(tasks, mount, {}, rpc)
    assert len(results) == 3
    assert not results[0].passed
    assert results[1].passed
    assert results[2].passed


def test_verify_suite_mixed_onchain_and_code(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "tip.txt").write_text("0x8\n")
    registry = tmp_path / "registry"
    suite = registry / "code-1" / "hidden"
    suite.mkdir(parents=True)

    onchain = Task(
        id="tip",
        prompt_fragment="tip",
        score=1,
        proof_file="tip.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="tip_hex", rpc_method="get_tip_block_number"),
    )

    def rpc(method: str, params: list) -> object:
        if method == "get_tip_block_number":
            return "0x10"
        return None

    def runner(inv: RunnerInvocation) -> int:
        if inv.stage == "build":
            p = mount / ".ckbbench-artifact" / "build" / "release" / "hashlock"
            p.parent.mkdir(parents=True)
            p.write_text("elf")
        return 0

    results = verify_suite(
        (onchain, _code_task()),
        mount,
        {"code-1": {BENCH_PASSWORD_ENV: "pw"}},
        rpc,
        registry_root=registry,
        runner=runner,
    )
    assert results[0].passed
    assert results[1].passed


def test_verify_task_exception_becomes_fail_verdict(tmp_path: Path, monkeypatch):
    mount = tmp_path / "m2"
    mount.mkdir()
    (mount / "proof.txt").write_text("0x1")

    def boom(*_a, **_k):
        raise OSError("network partition")

    monkeypatch.setattr("ckbbench.verify.verifier.grade_onchain_task", boom)
    v = verify_task(_onchain_task("tip_hex", "proof.txt"), mount, {}, lambda m, p: "0x10")
    assert not v.passed
    assert "network partition" in v.reason


def test_verify_task_unknown_kind(tmp_path: Path):
    from types import SimpleNamespace

    mount = tmp_path / "m"
    mount.mkdir()
    task = SimpleNamespace(
        id="weird",
        kind="other",
        proof_file="p.txt",
        verifier=OnchainVerifierSpec(check="tip_hex", rpc_method="m"),
    )
    v = verify_task(task, mount, {}, lambda m, p: "0x1")  # type: ignore[arg-type]
    assert not v.passed
    assert "unknown task kind" in v.reason