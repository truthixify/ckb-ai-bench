"""Orchestrator tests: full cell with fakes, no network/docker/LLM (ADR-0008/0009/0010)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ckbbench.run.arm import resolve_arm
from ckbbench.run.orchestrate import (
    AGENT_DONE_EXIT,
    _compose_for_arm,
    _inject_harness_tip,
    _proxy_env_context,
    run_cell,
    verifier_network_config,
)
from ckbbench.run.preflight import PreflightVersionMismatch
from ckbbench.suite.model import OnchainVerifierSpec, ParamSpec, Task
from ckbbench.suite.registry import load_suite
from ckbbench.suite.runparams import RunParams
from ckbbench.suite.test_registry import build_registry
from ckbbench.verify.onchain import Verdict


HARNESS_TIP = 0x2A


class FakeMcpClient:
    def __init__(self, *, version: str = "1.6.12") -> None:
        self.version = version
        self.initialized = False

    def initialize(self) -> dict:
        self.initialized = True
        return {"serverInfo": {"name": "test", "version": self.version}, "instructions": "deferred loading"}

    def list_tools(self) -> list[dict]:
        return [{"name": "search_tools"}, {"name": "search_resources"}]


class FakeAgent:
    def __init__(
        self,
        *,
        mount_dir: Path,
        exit_status: str = AGENT_DONE_EXIT,
        write_proofs: bool = True,
        messages: list | None = None,
    ) -> None:
        self.mount_dir = mount_dir
        self.exit_status = exit_status
        self.write_proofs = write_proofs
        self.messages = messages or [
            {"extra": {"response": {"usage": {"total_tokens": 50}}}},
        ]

    def run(self, pointer: str) -> dict:
        if self.write_proofs:
            (self.mount_dir / "proof_a.txt").write_text(hex(HARNESS_TIP))
            (self.mount_dir / "proof_b.txt").write_text("0x0")
        return {"exit_status": self.exit_status}


def _rpc(method: str, params: list) -> object:
    if method == "get_tip_block_number":
        return hex(HARNESS_TIP)
    if method == "get_current_epoch":
        return {"number": "0x0"}
    return None


def _make_agent_factory(*, exit_status: str = AGENT_DONE_EXIT, write_proofs: bool = True):
    def factory(**kwargs):
        return FakeAgent(
            mount_dir=kwargs["mount_dir"],
            exit_status=exit_status,
            write_proofs=write_proofs,
        )

    return factory


def _setup(tmp_path: Path):
    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    mount = tmp_path / "mount"
    vpriv = tmp_path / "vpriv"
    results = tmp_path / "results"
    return root, suite, mount, vpriv, results


def test_verifier_network_testnet_gets_proxy_devnet_does_not():
    testnet = verifier_network_config("testnet", proxy_url="http://proxy:8888")
    devnet = verifier_network_config("devnet")
    assert testnet.proxy_env == {
        "HTTP_PROXY": "http://proxy:8888",
        "HTTPS_PROXY": "http://proxy:8888",
    }
    assert devnet.proxy_env == {}


def test_verifier_network_unknown_chain_raises():
    with pytest.raises(ValueError, match="unknown chain"):
        verifier_network_config("mainnet")


def test_proxy_env_context_sets_and_restores(monkeypatch):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    with _proxy_env_context({"HTTP_PROXY": "http://p:1"}):
        assert os.environ["HTTP_PROXY"] == "http://p:1"
    assert "HTTP_PROXY" not in os.environ


def test_proxy_env_context_restores_prior_value(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://old:1")
    with _proxy_env_context({"HTTP_PROXY": "http://new:1"}):
        assert os.environ["HTTP_PROXY"] == "http://new:1"
    assert os.environ["HTTP_PROXY"] == "http://old:1"


def test_proxy_env_context_noop_for_empty():
    with _proxy_env_context({}):
        pass


def test_compose_for_arm_injects_preamble(tmp_path: Path):
    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    composed = _compose_for_arm(suite, resolve_arm("C"))
    assert "prefer mcp_call" in composed


def test_inject_harness_tip_overrides_drawn_value():
    task = Task(
        id="t",
        prompt_fragment="x",
        score=1,
        proof_file="p.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="tip_hex", rpc_method="m"),
        param_schema=(ParamSpec(name="harness_tip", param_class="verifier", generator="harness_tip"),),
    )
    params = RunParams(prompt_injected={}, verifier_private={"harness_tip": 1})
    out = _inject_harness_tip(params, task, 99)
    assert out.verifier_private["harness_tip"] == 99


def test_inject_harness_tip_noop_without_schema():
    task = Task(
        id="t",
        prompt_fragment="x",
        score=1,
        proof_file="p.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="tip_hex", rpc_method="m"),
    )
    params = RunParams(prompt_injected={}, verifier_private={})
    assert _inject_harness_tip(params, task, 99) is params


def test_happy_path_arm_A_passes_and_writes_json(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    captured_verify: list[dict] = []

    def tracking_rpc(method, params):
        captured_verify.append({"method": method})
        return _rpc(method, params)

    result = run_cell(
        suite,
        "devnet",
        "A",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=tracking_rpc,
        agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "pass"
    assert result.total_score == 15
    assert (results / f"{result.run_id}.json").is_file()
    assert captured_verify  # verify ran
    mount_text = "\n".join(p.read_text() for p in mount.rglob("*") if p.is_file())
    assert "harness_tip" not in mount_text
    assert "nonce_amount_shannons" not in mount_text


def test_harness_tip_captured_once_and_reaches_verifier_private(tmp_path: Path, monkeypatch):
    root = build_registry(
        tmp_path / "registry",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "proof_a.txt",
                "score": 10,
                "kind": "onchain",
                "check": "tip_hex",
                "rpc_method": "get_tip_block_number",
                "fragment": "Write tip to proof_a.txt.",
                "param_schema": [
                    {"name": "harness_tip", "class": "verifier", "generator": "harness_tip"},
                ],
            },
        ],
        manifest_overrides={"tasks": ["task-a"]},
    )
    suite = load_suite(root)
    mount = tmp_path / "mount"
    vpriv = tmp_path / "vpriv"
    results = tmp_path / "results"
    tip_calls: list[str] = []
    seen_private: list[int] = []

    def counting_rpc(method, params):
        if method == "get_tip_block_number":
            tip_calls.append(method)
            return hex(HARNESS_TIP)
        return None

    def spy_verify(tasks, mount_arg, verifier_private_by_task, rpc, **kwargs):
        for priv in verifier_private_by_task.values():
            if "harness_tip" in priv:
                seen_private.append(priv["harness_tip"])
        return [
            Verdict(task_id="task-a", passed=True, reason="ok", proof=hex(HARNESS_TIP))
        ]

    monkeypatch.setattr("ckbbench.run.orchestrate.verify_suite", spy_verify)

    run_cell(
        suite,
        "devnet",
        "A",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=counting_rpc,
        agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )

    # Run-start capture plus per-task generate_run_params may both call tip RPC; the value
    # injected into verifier-private must be the single run-start capture (CONTEXT).
    assert tip_calls.count("get_tip_block_number") >= 1
    assert seen_private == [HARNESS_TIP]


def test_preflight_mismatch_arm_C_infra_fail_skips_verify(tmp_path: Path, monkeypatch):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    verify_called = {"n": 0}

    def fail_verify(*args, **kwargs):
        verify_called["n"] += 1
        return []

    monkeypatch.setattr("ckbbench.run.orchestrate.verify_suite", fail_verify)

    def mcp_factory(url: str):
        return FakeMcpClient(version="9.9.9")

    result = run_cell(
        suite,
        "devnet",
        "C",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        mcp_client_factory=mcp_factory,
        rpc=_rpc,
        agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "infra_fail"
    assert verify_called["n"] == 0
    assert result.tasks == ()


def test_agent_stall_agent_fail(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite,
        "devnet",
        "A",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=_rpc,
        agent_factory=_make_agent_factory(exit_status="LimitsExceeded"),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 10.0,
    )
    assert result.outcome == "agent_fail"
    assert result.agent_exit_status == "LimitsExceeded"


def test_failing_task_completes_with_per_task_fail(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite,
        "devnet",
        "A",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=_rpc,
        agent_factory=_make_agent_factory(write_proofs=False),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "agent_fail"
    assert any(not t.passed for t in result.tasks)
    assert (results / f"{result.run_id}.json").is_file()


def test_harness_tip_rpc_failure_infra_fail(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)

    def bad_rpc(method, params):
        raise RuntimeError("rpc down")

    result = run_cell(
        suite,
        "devnet",
        "A",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=bad_rpc,
        agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "infra_fail"


def test_agent_exception_counts_as_agent_fail(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)

    class BrokenAgent:
        def run(self, pointer: str):
            raise RuntimeError("boom")

    def broken_factory(**kwargs):
        return BrokenAgent()

    result = run_cell(
        suite,
        "devnet",
        "A",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=_rpc,
        agent_factory=broken_factory,
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert result.agent_exit_status == "error"
    assert result.outcome == "agent_fail"


def test_testnet_run_sets_proxy_env_during_verify(tmp_path: Path, monkeypatch):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    proxy_seen: list[str | None] = []

    original_verify = __import__("ckbbench.verify.verifier", fromlist=["verify_suite"]).verify_suite

    def proxy_spy(tasks, mount_arg, verifier_private_by_task, rpc, **kwargs):
        proxy_seen.append(os.environ.get("HTTP_PROXY"))
        return [
            Verdict(task_id=t.id, passed=True, reason="ok", proof="0x2a")
            for t in tasks
        ]

    monkeypatch.setattr("ckbbench.run.orchestrate.verify_suite", proxy_spy)

    run_cell(
        suite,
        "testnet",
        "A",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=_rpc,
        agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert proxy_seen == ["http://ckbbench-proxy:8888"]


def test_devnet_run_no_proxy_env_during_verify(tmp_path: Path, monkeypatch):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    proxy_seen: list[str | None] = []

    def proxy_spy(tasks, mount_arg, verifier_private_by_task, rpc, **kwargs):
        proxy_seen.append(os.environ.get("HTTP_PROXY"))
        return [
            Verdict(task_id=t.id, passed=True, reason="ok", proof="0x2a")
            if t.id == "task-a"
            else Verdict(task_id=t.id, passed=True, reason="ok", proof="0x0")
            for t in tasks
        ]

    monkeypatch.setattr("ckbbench.run.orchestrate.verify_suite", proxy_spy)

    run_cell(
        suite,
        "devnet",
        "A",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=_rpc,
        agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert proxy_seen == [None]


def test_missing_agent_factory_raises(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    with pytest.raises(ValueError, match="agent_factory"):
        run_cell(
            suite,
            "devnet",
            "A",
            "test/model",
            1,
            registry_root=root,
            results_dir=results,
            mount_dir=mount,
            verifier_private_root=vpriv,
            rpc=_rpc,
            now_fn=lambda: 1.0,
            monotonic_fn=lambda: 0.0,
        )


def test_compose_for_arm_without_marker_prepends(tmp_path: Path):
    from ckbbench.suite.model import Suite, SuitePins

    suite = Suite(
        suite_semver="0",
        chain_profile="devnet",
        mcp_server_version="1",
        tasks=(),
        pins=SuitePins(),
    )
    text = _compose_for_arm(suite, resolve_arm("A"))
    assert "must NOT use web research" in text


def test_preflight_mismatch_exception_type():
    from ckbbench.run.preflight import preflight_mcp

    with pytest.raises(PreflightVersionMismatch):
        preflight_mcp("u", "1", client=FakeMcpClient(version="0"))


def test_classify_outcome_infra_fail():
    from ckbbench.run.orchestrate import _classify_outcome

    assert _classify_outcome(
        infra_failed=True,
        agent_exit_status=AGENT_DONE_EXIT,
        all_tasks_passed=True,
    ) == "infra_fail"


def test_arm_C_happy_path_with_mcp_factory(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)

    result = run_cell(
        suite,
        "devnet",
        "C",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        mcp_client_factory=lambda url: FakeMcpClient(),
        rpc=_rpc,
        agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "pass"
    assert result.preflight_server_version == "1.6.12"


def test_compose_for_arm_empty_preamble_returns_body(tmp_path: Path):
    from ckbbench.run.arm import ArmConfig

    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    empty_arm = ArmConfig(
        arm="X",
        mcp_enabled=False,
        web_research_allowed=False,
        egress_mode="block",
        prompt_preamble="",
    )
    assert _compose_for_arm(suite, empty_arm) == __import__(
        "ckbbench.suite.compose", fromlist=["compose"]
    ).compose(suite)


def test_run_cell_default_mcp_client_when_factory_absent(tmp_path: Path, monkeypatch):
    import sys
    from types import ModuleType

    root, suite, mount, vpriv, results = _setup(tmp_path)
    fake_mod = ModuleType("ckb_mcp")
    fake_mod.CkbMcpClient = lambda url: FakeMcpClient()
    monkeypatch.setitem(sys.modules, "ckb_mcp", fake_mod)

    result = run_cell(
        suite,
        "devnet",
        "C",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=_rpc,
        agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "pass"


def test_compose_for_arm_prepends_when_marker_missing(tmp_path: Path, monkeypatch):
    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    monkeypatch.setattr(
        "ckbbench.run.orchestrate.compose",
        lambda s: "composed body without the marker phrase",
    )
    text = _compose_for_arm(suite, resolve_arm("A"))
    assert text.startswith("You must NOT use web research")
    assert "composed body" in text


def test_agent_non_dict_return_agent_fail(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)

    class OddAgent:
        def run(self, pointer: str):
            return "not-a-dict"

    result = run_cell(
        suite,
        "devnet",
        "A",
        "test/model",
        1,
        registry_root=root,
        results_dir=results,
        mount_dir=mount,
        verifier_private_root=vpriv,
        rpc=_rpc,
        agent_factory=lambda **kwargs: OddAgent(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert result.agent_exit_status is None
    assert result.outcome == "agent_fail"