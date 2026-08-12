"""Top-level verifier dispatcher tests: kind routing and failure isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ckbbench.suite.model import OnchainVerifierSpec, Task
from ckbbench.verify.codetask import BENCH_PASSWORD_ENV, RunnerInvocation
from ckbbench.verify.onchain import (
    SECP_CODE_HASH,
    SECP_HASH_TYPE,
    VerificationInfrastructureError,
)
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
    v = verify_task(_onchain_task("epoch_number"), mount, {}, lambda m, p: {"number": "0x10"})
    assert not v.passed
    assert "missing" in v.reason


def test_verify_task_onchain_dispatch(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "proof.txt").write_text("0x5\n")
    rpc = lambda m, p: {"number": "0x5"} if m == "get_current_epoch" else None
    v = verify_task(_onchain_task("epoch_number"), mount, {}, rpc)
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
        _onchain_task("epoch_number", "missing.txt"),
        _onchain_task("epoch_number", "epoch.txt"),
        Task(
            id="boom",
            prompt_fragment="x",
            score=1,
            proof_file="x.txt",
            kind="onchain",
            verifier=OnchainVerifierSpec(check="epoch_number", rpc_method="m"),
        ),
    )
    (mount / "x.txt").write_text("0x1")

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
    (mount / "tip.txt").write_text("0x10\n")
    registry = tmp_path / "registry"
    suite = registry / "code-1" / "hidden"
    suite.mkdir(parents=True)

    onchain = Task(
        id="tip",
        prompt_fragment="tip",
        score=1,
        proof_file="tip.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="epoch_number", rpc_method="get_current_epoch"),
    )

    def rpc(method: str, params: list) -> object:
        if method == "get_current_epoch":
            return {"number": "0x10"}
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
    v = verify_task(_onchain_task("epoch_number", "proof.txt"), mount, {}, lambda m, p: {"number": "0x10"})
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
        verifier=OnchainVerifierSpec(check="epoch_number", rpc_method="m"),
    )
    v = verify_task(task, mount, {}, lambda m, p: "0x1")  # type: ignore[arg-type]
    assert not v.passed
    assert "unknown task kind" in v.reason


def test_verify_task_dispatches_script_identity_from_a_real_two_line_proof(tmp_path: Path):
    """Catches a checker that only works when called directly: the proof must survive being read
    from disk and dispatched, and no RPC may be reached."""
    sudt = "0x5e7a36a77e68eecc013dfa2fe6a23f3b6c344b04005808694ae6dd45eea4cfd5"
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "proof_sudt_script.txt").write_text(f"{sudt}\ntype\n")

    task = Task(
        id="task-06-sudt-script",
        prompt_fragment="x",
        score=10,
        proof_file="proof_sudt_script.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(
            check="script_identity", rpc_method="constant", rpc_params=(sudt, "type")
        ),
    )

    def no_rpc(method, params):
        raise AssertionError("script_identity must never call RPC")

    verdict = verify_task(task, mount, {}, no_rpc)
    assert verdict.passed, verdict.reason


def test_verify_task_script_identity_failure_also_avoids_rpc(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "proof_sudt_script.txt").write_text("0xdeadbeef\ndata1\n")
    task = Task(
        id="task-06-sudt-script",
        prompt_fragment="x",
        score=10,
        proof_file="proof_sudt_script.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(
            check="script_identity", rpc_method="constant",
            rpc_params=("0x5e7a36a77e68eecc013dfa2fe6a23f3b6c344b04005808694ae6dd45eea4cfd5", "type"),
        ),
    )

    def no_rpc(method, params):
        raise AssertionError("script_identity must never call RPC")

    assert not verify_task(task, mount, {}, no_rpc).passed


# --- verifier-infrastructure propagation (Card 3) ---

TX_HASH = "0x" + "11" * 32


def _tx_task(task_id: str = "tx") -> Task:
    return Task(
        id=task_id,
        prompt_fragment="send",
        score=25,
        proof_file="tx_id.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="tx_proof", rpc_method="get_transaction"),
    )


def _tx_private(recipient: str = "0x" + "ab" * 20) -> dict:
    return {
        "harness_tip": 100,
        "nonce_amount_shannons": "10000000123",
        "recipient_args": recipient,
    }


def _mount_with_tx(tmp_path: Path, text: str = TX_HASH) -> Path:
    mount = tmp_path / "mount"
    mount.mkdir(parents=True)
    (mount / "tx_id.txt").write_bytes(text.encode("utf-8"))
    return mount


def test_verify_task_propagates_verification_infrastructure_error(tmp_path: Path):
    """An unobservable chain is not a task result, so it must not become a failed Verdict."""
    def boom(m, p):
        raise ConnectionError("node unreachable")

    with pytest.raises(VerificationInfrastructureError):
        verify_task(_tx_task(), _mount_with_tx(tmp_path), _tx_private(), boom)


def test_verify_suite_aborts_and_keeps_no_partial_verdicts(tmp_path: Path):
    """Partial verdicts would be scored as if grading had completed."""
    mount = _mount_with_tx(tmp_path)
    (mount / "tip.txt").write_text("0x10")
    tip_task = Task(
        id="tip", prompt_fragment="tip", score=5, proof_file="tip.txt", kind="onchain",
        verifier=OnchainVerifierSpec(check="epoch_number", rpc_method="get_current_epoch"),
    )

    def rpc(m, p):
        if m == "get_current_epoch":
            return {"number": "0x10"}
        raise TimeoutError("grading read timed out")

    with pytest.raises(VerificationInfrastructureError):
        verify_suite([tip_task, _tx_task()], mount, {"tx": _tx_private()}, rpc)


def test_verify_suite_forwards_time_seams_to_a_pending_tx_proof(tmp_path: Path):
    """The public path must carry the injected clock, or production would really sleep 90s."""
    sleeps: list[float] = []
    now = [500.0]

    def monotonic():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    recipient = "0x" + "ab" * 20
    committed = {
        "transaction": {"outputs": [
            {"capacity": hex(10_000_000_123),
             "lock": {"code_hash": SECP_CODE_HASH, "hash_type": SECP_HASH_TYPE, "args": recipient}}
        ]},
        "tx_status": {"status": "committed", "block_hash": "0xb"},
    }
    seen: list[str] = []

    def rpc(m, p):
        if m == "get_header":
            return {"number": "0x96"}
        seen.append(m)
        return {"transaction": {"outputs": []}, "tx_status": {"status": "pending"}} \
            if len(seen) < 3 else committed

    verdicts = verify_suite(
        [_tx_task()], _mount_with_tx(tmp_path), {"tx": _tx_private(recipient)}, rpc,
        monotonic_fn=monotonic, sleep_fn=sleep,
    )
    assert verdicts[0].passed, verdicts[0].reason
    assert sleeps == [2.0, 2.0]


def test_verify_suite_isolates_ordinary_failures_across_tasks(tmp_path: Path):
    """Normal negative verdicts still must not stop the suite."""
    mount = _mount_with_tx(tmp_path, "not-a-hash")
    (mount / "tip.txt").write_text("0x10")
    tip_task = Task(
        id="tip", prompt_fragment="tip", score=5, proof_file="tip.txt", kind="onchain",
        verifier=OnchainVerifierSpec(check="epoch_number", rpc_method="get_current_epoch"),
    )
    verdicts = verify_suite(
        [tip_task, _tx_task()], mount, {"tx": _tx_private()}, lambda m, p: {"number": "0x10"},
    )
    assert len(verdicts) == 2
    assert verdicts[0].passed
    assert not verdicts[1].passed


def test_verify_task_still_isolates_unrelated_checker_exceptions(monkeypatch, tmp_path: Path):
    """The propagation whitelist is exact: only the dedicated type escapes."""
    import ckbbench.verify.verifier as verifier_mod

    def broken(*a, **kw):
        raise ValueError("checker bug")

    monkeypatch.setattr(verifier_mod, "grade_onchain_task", broken)
    v = verify_task(_tx_task(), _mount_with_tx(tmp_path), _tx_private(), lambda m, p: None)
    assert not v.passed
    assert "verify error" in v.reason
    assert "ValueError" in v.reason


@pytest.mark.parametrize(
    "raw",
    [f"  {TX_HASH}  \n", f"  {TX_HASH}  \r\n", f"{TX_HASH}\r", f"{TX_HASH}"],
    ids=["lf", "crlf", "bare-cr", "no-terminator"],
)
def test_verify_task_preserves_the_agent_raw_proof_bytes(tmp_path: Path, raw):
    """The persisted evidence must be exactly what the agent wrote: no stripping, and no
    universal-newline translation of CRLF or bare CR."""
    recipient = "0x" + "ab" * 20
    committed = {
        "transaction": {"outputs": [
            {"capacity": hex(10_000_000_123),
             "lock": {"code_hash": SECP_CODE_HASH, "hash_type": SECP_HASH_TYPE, "args": recipient}}
        ]},
        "tx_status": {"status": "committed", "block_hash": "0xb"},
    }
    rpc = lambda m, p: {"number": "0x96"} if m == "get_header" else committed  # noqa: E731

    ok_mount = _mount_with_tx(tmp_path, raw)
    ok = verify_task(_tx_task(), ok_mount, _tx_private(recipient), rpc)
    assert ok.passed, ok.reason
    assert ok.proof == raw, "raw proof must survive the production read path verbatim"
    assert ok.proof.encode("utf-8") == (ok_mount / "tx_id.txt").read_bytes()

    # and on the failing path, where the evidence matters most
    bad_mount = _mount_with_tx(tmp_path / "b", raw)
    bad = verify_task(
        _tx_task(), bad_mount,
        {**_tx_private(recipient), "nonce_amount_shannons": "999"}, rpc,
    )
    assert not bad.passed
    assert bad.proof == raw
    assert bad.proof.encode("utf-8") == (bad_mount / "tx_id.txt").read_bytes()


def test_verify_task_rejects_a_non_type_hash_type_through_the_public_path(tmp_path: Path):
    """A different Script identity must not collect Task 04's score."""
    recipient = "0x" + "ab" * 20
    committed = {
        "transaction": {"outputs": [
            {"capacity": hex(10_000_000_123),
             "lock": {"code_hash": SECP_CODE_HASH, "hash_type": "data1", "args": recipient}}
        ]},
        "tx_status": {"status": "committed", "block_hash": "0xb"},
    }
    rpc = lambda m, p: {"number": "0x96"} if m == "get_header" else committed  # noqa: E731
    v = verify_task(_tx_task(), _mount_with_tx(tmp_path), _tx_private(recipient), rpc)
    assert not v.passed
    assert "exactly 1 output" in v.reason


# --- Task 01 run-bound tip through the public verifier path (Card 4) ---

TIP_HASH = "0x" + "ab" * 32
OTHER_TIP_HASH = "0x" + "cd" * 32
RUN_START = 0x2A
LATE_TIP = RUN_START + 5_000


def _tip_task(task_id: str = "task-01-tip") -> Task:
    return Task(
        id=task_id,
        prompt_fragment="tip",
        score=10,
        proof_file="proof_tip.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="tip_block_identity",
                                     rpc_method="get_tip_block_number"),
    )


def _mount_with_tip(tmp_path: Path, text: str) -> Path:
    mount = tmp_path / "mount"
    mount.mkdir(parents=True)
    (mount / "proof_tip.txt").write_bytes(text.encode("utf-8"))
    return mount


def _tip_rpc(block_hash: str = TIP_HASH, tip: int = LATE_TIP):
    def rpc(method, params):
        if method == "get_tip_block_number":
            return hex(tip)
        if method == "get_block_hash":
            return block_hash
        return None

    return rpc


@pytest.mark.parametrize(
    "raw",
    [f"{hex(RUN_START)}\n{TIP_HASH}",
     f"  {hex(RUN_START)}  \r\n  {TIP_HASH}  \r\n",
     f"{hex(RUN_START)}\n{TIP_HASH}\n"],
    ids=["lf", "crlf-padded", "trailing-lf"],
)
def test_verify_task_dispatches_tip_identity_and_preserves_raw_proof(tmp_path: Path, raw):
    mount = _mount_with_tip(tmp_path, raw)
    v = verify_task(_tip_task(), mount, {"harness_tip": RUN_START}, _tip_rpc())
    assert v.passed, v.reason
    assert v.proof == raw
    assert v.proof.encode("utf-8") == (mount / "proof_tip.txt").read_bytes()


def test_verify_task_tip_identity_block_hash_fault_is_infrastructure(tmp_path: Path):
    def rpc(method, params):
        if method == "get_tip_block_number":
            return hex(LATE_TIP)
        raise ConnectionError("block hash read failed")

    with pytest.raises(VerificationInfrastructureError):
        verify_task(
            _tip_task(), _mount_with_tip(tmp_path, f"{hex(RUN_START)}\n{TIP_HASH}"),
            {"harness_tip": RUN_START}, rpc,
        )


def test_verify_suite_aborts_when_task_01_cannot_be_observed(tmp_path: Path):
    mount = _mount_with_tip(tmp_path, f"{hex(RUN_START)}\n{TIP_HASH}")
    (mount / "epoch.txt").write_text("0x1")
    epoch_task = Task(
        id="epoch", prompt_fragment="e", score=5, proof_file="epoch.txt", kind="onchain",
        verifier=OnchainVerifierSpec(check="epoch_number", rpc_method="get_current_epoch"),
    )

    def rpc(method, params):
        if method == "get_current_epoch":
            return {"number": "0x1"}
        raise TimeoutError("tip read timed out")

    with pytest.raises(VerificationInfrastructureError):
        verify_suite([epoch_task, _tip_task()], mount, {"task-01-tip": {"harness_tip": RUN_START}},
                     rpc)


def test_verify_suite_task_01_mismatch_stays_isolated(tmp_path: Path):
    """An ordinary wrong answer must not stop other gradable tasks."""
    mount = _mount_with_tip(tmp_path, f"{hex(RUN_START)}\n{OTHER_TIP_HASH}")
    (mount / "epoch.txt").write_text("0x1")
    epoch_task = Task(
        id="epoch", prompt_fragment="e", score=5, proof_file="epoch.txt", kind="onchain",
        verifier=OnchainVerifierSpec(check="epoch_number", rpc_method="get_current_epoch"),
    )

    def rpc(method, params):
        if method == "get_current_epoch":
            return {"number": "0x1"}
        return _tip_rpc()(method, params)

    verdicts = verify_suite([epoch_task, _tip_task()], mount,
                            {"task-01-tip": {"harness_tip": RUN_START}}, rpc)
    assert len(verdicts) == 2
    assert verdicts[0].passed
    assert not verdicts[1].passed
    assert "block hash mismatch" in verdicts[1].reason


def test_verify_task_tip_identity_missing_private_is_infrastructure(tmp_path: Path):
    """A Task 01 authored without its schema entry is harness misconfiguration."""
    with pytest.raises(VerificationInfrastructureError, match="harness_tip"):
        verify_task(_tip_task(), _mount_with_tip(tmp_path, f"{hex(RUN_START)}\n{TIP_HASH}"),
                    {}, _tip_rpc())
