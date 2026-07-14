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
    _make_tip_pinned_rpc,
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
        self.config = type(
            "FakeAgentConfig",
            (),
            {
                "step_limit": 80,
                "cost_limit": 0.0,
                "wall_time_limit_seconds": 900,
            },
        )()
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


def test_tip_pinned_rpc_returns_capture_for_tip_and_passes_through_else():
    calls: list[str] = []

    def underlying(method, params):
        calls.append(method)
        return {"number": "0x0"}

    pinned = _make_tip_pinned_rpc(underlying, 0x2A)
    # the tip method returns the single capture WITHOUT calling the underlying RPC
    assert pinned("get_tip_block_number", []) == hex(0x2A)
    assert calls == []
    # any other method passes through to the underlying client
    assert pinned("get_current_epoch", []) == {"number": "0x0"}
    assert calls == ["get_current_epoch"]


def test_proxy_env_context_restores_even_when_body_raises(monkeypatch):
    # The proxy env must be cleaned up even if verify raises inside the context (codex).
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    with pytest.raises(RuntimeError):
        with _proxy_env_context({"HTTP_PROXY": "http://p:1"}):
            assert os.environ["HTTP_PROXY"] == "http://p:1"
            raise RuntimeError("verify blew up")
    assert "HTTP_PROXY" not in os.environ


def test_no_research_arm_web_touch_is_protocol_violation(tmp_path: Path):
    # A no-research arm (A) that Submitted + passed but whose proxy log shows web egress must be
    # recorded as protocol_violation, NOT pass (RECOMMENDATION 4). The violation_check seam stands
    # in for the production proxy-log reader.
    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(),
        violation_check=lambda arm, mnt: True,  # proxy log shows a non-allowlisted destination
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "protocol_violation"


def test_run_cell_refuses_mount_inside_registry(tmp_path: Path):
    # The agent mount must not live under the registry tree, else the hidden suite is readable via
    # a relative path (grok-build). run_cell must refuse such a mount.
    root, suite, _mount, vpriv, results = _setup(tmp_path)
    bad_mount = root / "inside" / "mount"
    with pytest.raises(ValueError, match="must not be inside the registry tree"):
        run_cell(
            suite, "devnet", "A", "test/model", 1,
            registry_root=root, results_dir=results, mount_dir=bad_mount,
            verifier_private_root=vpriv, rpc=_rpc, agent_factory=_make_agent_factory(),
            now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
        )


def test_run_cell_default_mount_is_out_of_tree(tmp_path: Path, monkeypatch):
    # With no mount_dir, run_cell defaults to an out-of-tree temp dir (not under the registry).
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "tmproot"))
    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    captured: dict = {}

    def factory(**kwargs):
        captured["mount"] = kwargs["mount_dir"]
        return FakeAgent(mount_dir=kwargs["mount_dir"])

    run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=tmp_path / "results",
        rpc=_rpc, agent_factory=factory,
        now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
    )
    mount = captured["mount"].resolve()
    assert root.resolve() not in mount.parents
    assert "ckbbench-runs" in str(mount)
    # Default cleanup removes the harness-owned run dir after the cell.
    assert not mount.exists()


def test_run_cell_keep_retains_owned_host_run_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "tmproot"))
    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    captured: dict = {}

    def factory(**kwargs):
        captured["mount"] = kwargs["mount_dir"]
        return FakeAgent(mount_dir=kwargs["mount_dir"])

    run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=tmp_path / "results",
        rpc=_rpc, agent_factory=factory,
        now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
        keep=True,
    )
    mount = captured["mount"]
    assert mount.is_dir()


def test_code_task_gets_fresh_bench_password_in_verifier_private(tmp_path: Path):
    # A Code Task must receive a per-run BENCH_PASSWORD in verifier-private (never the mount), so
    # grade_code_task can grade the hidden suite (grok-build: it was never synthesized). The agent
    # never sees it.
    from ckbbench.suite.model import Suite, SuitePins, Task

    registry = build_registry(tmp_path / "registry")
    code_task = Task(
        id="code-t", prompt_fragment="author a lock", score=30, proof_file="build/release/x",
        kind="code", verifier="hidden",
    )
    # the code task's hidden verifier_dir must exist on disk for load-time, but here we build the
    # Suite directly and only exercise the run-params secret injection + write boundary.
    (tmp_path / "registry" / "code-t").mkdir(exist_ok=True)
    suite = Suite(
        suite_semver="1.0.0", chain_profile="devnet", mcp_server_version="1.6.12",
        tasks=(code_task,), pins=SuitePins(),
    )
    mount = tmp_path / "mount"
    vpriv = tmp_path / "vpriv"
    seen_private: dict = {}

    def spy_verify(tasks, mount_arg, verifier_private_by_task, rpc, **kwargs):
        seen_private.update(verifier_private_by_task)
        return [Verdict(task_id="code-t", passed=True, reason="ok", proof="x")]

    def fake_runner(inv):
        return 0

    import ckbbench.run.orchestrate as orch
    orig = orch.verify_suite
    orch.verify_suite = spy_verify
    try:
        run_cell(
            suite, "devnet", "A", "test/model", 1,
            registry_root=tmp_path / "registry", results_dir=tmp_path / "results",
            mount_dir=mount, verifier_private_root=vpriv,
            rpc=_rpc, agent_factory=_make_agent_factory(), runner=fake_runner,
            now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
        )
    finally:
        orch.verify_suite = orig

    # BENCH_PASSWORD present in verifier-private, fresh (32 hex chars), and NOT in the mount.
    pw = seen_private["code-t"].get("BENCH_PASSWORD")
    assert pw and len(pw) == 32
    mount_text = "\n".join(p.read_text() for p in mount.rglob("*") if p.is_file())
    assert "BENCH_PASSWORD" not in mount_text and pw not in mount_text


def test_unscored_placeholder_does_not_gate_outcome_or_inflate_total(tmp_path: Path):
    # A failing PLACEHOLDER (scored=false) must NOT flip a pass to agent_fail, NOT count toward
    # total_score, and NOT count toward max_score (grok-build/codex). The real task passing is a pass.
    from ckbbench.suite.model import OnchainVerifierSpec, Suite, SuitePins, Task

    real = Task(id="real", prompt_fragment="real", score=10, proof_file="r.txt", kind="onchain",
                verifier=OnchainVerifierSpec(check="tip_hex", rpc_method="get_tip_block_number"))
    placeholder = Task(id="ph", prompt_fragment="ph", score=99, proof_file="p.txt", kind="onchain",
                       verifier=OnchainVerifierSpec(check="tip_hex", rpc_method="get_tip_block_number"),
                       scored=False)
    suite = Suite(suite_semver="1.0.0", chain_profile="devnet", mcp_server_version="1.6.12",
                  tasks=(real, placeholder), pins=SuitePins())
    # freeze hashes each task dir, so the dirs must exist on disk.
    for tid in ("real", "ph"):
        d = tmp_path / "registry" / tid
        d.mkdir(parents=True)
        (d / "prompt.txt").write_text(tid)
    mount = tmp_path / "mount"

    def spy_verify(tasks, mount_arg, verifier_private_by_task, rpc, **kwargs):
        return [
            Verdict(task_id="real", passed=True, reason="ok", proof=hex(HARNESS_TIP)),
            Verdict(task_id="ph", passed=False, reason="placeholder fails", proof=""),
        ]

    import ckbbench.run.orchestrate as orch
    orig = orch.verify_suite
    orch.verify_suite = spy_verify
    try:
        result = run_cell(
            suite, "devnet", "A", "test/model", 1,
            registry_root=tmp_path / "registry", results_dir=tmp_path / "results",
            mount_dir=mount, verifier_private_root=tmp_path / "vpriv",
            rpc=_rpc, agent_factory=_make_agent_factory(),
            now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
        )
    finally:
        orch.verify_suite = orig

    assert result.outcome == "pass"          # the failing placeholder did not gate it
    assert result.total_score == 10          # placeholder's 99 not awarded
    assert result.max_score == 10            # placeholder's 99 not in the denominator


def test_violation_check_not_consulted_on_research_arm(tmp_path: Path):
    # A research arm (B, egress=observe) cannot violate a no-research rule; the check must not flip it.
    root, suite, mount, vpriv, results = _setup(tmp_path)
    called = {"n": 0}

    def check(arm, mnt):
        called["n"] += 1
        return True

    result = run_cell(
        suite, "devnet", "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(), violation_check=check,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "pass"
    assert called["n"] == 0  # observe arm: the no-research violation check is not consulted


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

    # EXACTLY ONE run-start tip RPC: the tip-pinned rpc means per-task generate_run_params does
    # NOT redraw the tip (codex: enforce single capture, not >=1). The value injected into
    # verifier-private is that single run-start capture (CONTEXT).
    assert tip_calls.count("get_tip_block_number") == 1
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


def test_run_cell_persists_agent_limits_for_audit(tmp_path: Path):
    """Agent budgets affect benchmark validity, so each JSON artifact records the actual limits."""
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
        agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert result.agent_limits == {
        "step_limit": 80,
        "cost_limit": 0.0,
        "wall_time_limit_seconds": 900,
    }
    saved = json.loads((results / f"{result.run_id}.json").read_text())
    assert saved["agent_limits"] == result.agent_limits


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
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    proxy_seen: list[str | None] = []

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
    monkeypatch.delenv("HTTP_PROXY", raising=False)  # do not assume an absent ambient value
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


def test_classify_outcome_truth_table():
    from ckbbench.run.orchestrate import _classify_outcome

    # infra_fail dominates everything, even a clean pass.
    assert _classify_outcome(
        infra_failed=True, protocol_violated=False,
        agent_exit_status=AGENT_DONE_EXIT, all_tasks_passed=True,
    ) == "infra_fail"
    assert _classify_outcome(
        infra_failed=True, protocol_violated=True,
        agent_exit_status="error", all_tasks_passed=False,
    ) == "infra_fail"
    # a violation outranks agent correctness (a no-research arm that touched the web is not a pass).
    assert _classify_outcome(
        infra_failed=False, protocol_violated=True,
        agent_exit_status=AGENT_DONE_EXIT, all_tasks_passed=True,
    ) == "protocol_violation"
    # otherwise: wrong exit or any failed task = agent_fail.
    assert _classify_outcome(
        infra_failed=False, protocol_violated=False,
        agent_exit_status="error", all_tasks_passed=True,
    ) == "agent_fail"
    assert _classify_outcome(
        infra_failed=False, protocol_violated=False,
        agent_exit_status=AGENT_DONE_EXIT, all_tasks_passed=False,
    ) == "agent_fail"
    # only a clean, compliant, all-passed run is a pass.
    assert _classify_outcome(
        infra_failed=False, protocol_violated=False,
        agent_exit_status=AGENT_DONE_EXIT, all_tasks_passed=True,
    ) == "pass"


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


def test_compose_for_arm_places_preamble_after_base_before_tasks(tmp_path: Path, monkeypatch):
    # Structural placement (replaces the old brittle-marker test): the arm preamble must land
    # AFTER the base preamble and BEFORE the first task, regardless of base-preamble wording. We
    # reword PREAMBLE to prove the placement does not depend on a hardcoded marker string.
    from ckbbench.suite import compose as compose_mod

    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    monkeypatch.setattr(compose_mod, "PREAMBLE", "BASE PREAMBLE REWORDED ENTIRELY.")
    text = _compose_for_arm(suite, resolve_arm("A"))
    base_idx = text.index("BASE PREAMBLE REWORDED")
    arm_idx = text.index("must NOT use web research")
    first_task_idx = text.index("1.")
    assert base_idx < arm_idx < first_task_idx, "arm preamble must sit between base preamble and tasks"


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
