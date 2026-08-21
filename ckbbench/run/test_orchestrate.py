"""Orchestrator tests: full cell with fakes, no network/docker/LLM (ADR-0008/0009/0010)."""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from ckbbench.run.arm import resolve_arm
from ckbbench.run.mcp_surface import policy_for_arm, profile_for_arm
from ckbbench.run.model_profile import parse_model_profile
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
from ckbbench.run.result import RESULT_SCHEMA_VERSION
from ckbbench.run.runner import PrepareError
from ckbbench.suite.model import OnchainVerifierSpec, ParamSpec, Task
from ckbbench.suite.registry import load_suite
from ckbbench.suite.runparams import RunParams
from ckbbench.suite.test_registry import FIXTURE_CONSTANT, build_registry
from ckbbench.verify.onchain import Verdict


@pytest.fixture(autouse=True)
def _deny_real_subprocess(monkeypatch):
    """No orchestrator test may shell out.

    cleanup_cell() reaches the default runner when a cell names a work volume without keep=True,
    which would run `docker volume rm -f <name>` against the developer's real volume. This gate is
    a safety net, not a convenience: it must fail loudly rather than silently absorb the call.
    """
    def denied(argv):
        raise AssertionError(f"test attempted a real subprocess: {list(argv)}")

    monkeypatch.setattr("ckbbench.run.cleanup._default_run", denied)


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


class _FakeLedger:
    """The read surface run_cell consumes from the production model's usage ledger."""

    def __init__(self, *, turns=1, attempts=1, responses=1, totals=(30, 20, 50),
                 models=("gpt-fake",), complete=True, category=None,
                 correctness_complete=None):
        self.turn_count = turns
        self.attempt_count = attempts
        self.response_count = responses
        self.response_models = set(models)
        self._totals = totals
        self._complete = complete
        # An unanswered attempt must name its cause; an answered one must not claim a failure.
        self.provider_failure_category = category
        self.provider_failure_counts = (
            {category: attempts - responses} if category and category != "multiple" else {}
        )
        self.retry_count = attempts - turns
        self.retry_delay_seconds = 4 * self.retry_count
        self._correctness_complete = (
            turns > 0 and responses == turns and len(self.response_models) == 1
            if correctness_complete is None else correctness_complete
        )

    def totals(self):
        return self._totals

    def is_complete(self):
        return self._complete

    def is_correctness_complete(self):
        return self._correctness_complete


class _FakeModel:
    def __init__(self, ledger):
        self.usage_ledger = ledger


class FakeAgent:
    def __init__(
        self,
        *,
        mount_dir: Path,
        exit_status: str = AGENT_DONE_EXIT,
        write_proofs: bool = True,
        messages: list | None = None,
        surface: object | None = None,
        ledger: object | None = None,
        model_profile: object | None = None,
    ) -> None:
        self.mount_dir = mount_dir
        # A stand-in for the production controller must declare the same provenance it does, or the
        # orchestration tests would prove nothing about what a real result records.
        self.mcp_surface = surface
        self.mcp_surface_profile = None if surface is None else surface.profile
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
        # A stand-in model must carry the ledger the production model carries, or the orchestration
        # tests would prove nothing about the token provenance a real result records.
        self.model = _FakeModel(_FakeLedger() if ledger is None else ledger)
        self.model_profile = model_profile

    def run(self, pointer: str) -> dict:
        if self.write_proofs:
            (self.mount_dir / "proof_a.txt").write_text(FIXTURE_CONSTANT)
            (self.mount_dir / "proof_b.txt").write_text("0x0")
        return {"exit_status": self.exit_status}


def _rpc(method: str, params: list) -> object:
    if method == "get_tip_block_number":
        return hex(HARNESS_TIP)
    if method == "get_current_epoch":
        return {"number": "0x0"}
    return None


def _with_surface(agent, arm_config):
    """Stand-ins must declare the provenance the production controller declares.

    That is the arm's MCP surface (ADR-0013) and the model's usage ledger (ADR-0014); without both
    an orchestration test proves nothing about what a real result records.
    """
    policy = policy_for_arm(arm_config.arm)
    agent.mcp_surface = policy
    agent.mcp_surface_profile = policy.profile
    if getattr(agent, "model", None) is None:
        agent.model = _FakeModel(_FakeLedger())
    if not hasattr(agent, "model_profile"):
        agent.model_profile = None
    return agent


def _make_agent_factory(*, exit_status: str = AGENT_DONE_EXIT, write_proofs: bool = True):
    def factory(**kwargs):
        return FakeAgent(
            mount_dir=kwargs["mount_dir"],
            exit_status=exit_status,
            write_proofs=write_proofs,
            surface=policy_for_arm(kwargs["arm_config"].arm),
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
        return FakeAgent(mount_dir=kwargs["mount_dir"],
                         surface=policy_for_arm(kwargs["arm_config"].arm))

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
        return FakeAgent(mount_dir=kwargs["mount_dir"],
                         surface=policy_for_arm(kwargs["arm_config"].arm))

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
                verifier=OnchainVerifierSpec(check="constant_hex", rpc_method="constant"))
    placeholder = Task(id="ph", prompt_fragment="ph", score=99, proof_file="p.txt", kind="onchain",
                       verifier=OnchainVerifierSpec(check="constant_hex", rpc_method="constant"),
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


def test_supplied_check_is_consulted_on_an_observe_arm(tmp_path: Path):
    # The checker owns the per-arm policy. B is web-enabled but must not reach the product under
    # test, so run_cell can no longer suppress the check from egress mode alone.
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
    assert called["n"] == 1
    assert result.outcome == "protocol_violation"


def test_clean_observe_arm_check_permits_the_determined_result(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite, "devnet", "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(),
        violation_check=lambda arm, mnt: False,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "pass"


def test_compose_for_arm_injects_preamble(tmp_path: Path):
    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    composed = _compose_for_arm(suite, resolve_arm("C"), "devnet")
    assert "mcp_call only for CKB documentation and reference lookup" in composed
    assert "CKB_RPC_URL" in composed


def test_compose_for_arm_states_the_cell_chain_identically_for_every_arm(tmp_path: Path):
    """Plan §8.1: every arm must be told which chain this cell targets, and told it the same way.
    A/B cannot guess an internal service name, and C/D must not receive a different chain fact."""
    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    composed = {arm: _compose_for_arm(suite, resolve_arm(arm), "testnet") for arm in "ABCD"}

    for arm, text in composed.items():
        assert "CKB testnet chain" in text, arm
        assert "CKB_RPC_URL" in text, arm
        assert "CKBBENCH_CHAIN_PROFILE" in text, arm
    # The chain block itself is byte-identical across arms; only the arm policy line differs.
    blocks = {text.split("\n\n")[1] for text in composed.values()}
    assert len(blocks) == 1, blocks


def test_compose_for_arm_uses_the_cell_chain_not_the_suite_default(tmp_path: Path):
    """--chains overrides the suite profile, so composing from suite.chain_profile would tell a
    TestNet cell it is on DevNet."""
    root = build_registry(tmp_path / "registry")
    suite = load_suite(root)
    assert suite.chain_profile == "devnet"
    text = _compose_for_arm(suite, resolve_arm("B"), "testnet")
    assert "CKB testnet chain" in text
    assert "devnet" not in text


@pytest.mark.parametrize("chain", ["devnet", "testnet"])
def test_run_cell_passes_the_concrete_cell_chain_to_the_agent_factory(tmp_path: Path, chain):
    """The factory resolves the agent's RPC endpoint from this value. Passing the suite default
    instead would point a TestNet cell's agent at DevNet (plan §8.1)."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    assert suite.chain_profile == "devnet"  # so a testnet cell proves the override survives
    captured: dict = {}

    def factory(**kwargs):
        captured["chain"] = kwargs["chain"]
        return FakeAgent(mount_dir=kwargs["mount_dir"],
                         surface=policy_for_arm(kwargs["arm_config"].arm))

    run_cell(
        suite, chain, "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=factory,
        now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
    )
    assert captured["chain"] == chain
    assert f"CKB {chain} chain" in (mount / "INSTRUCTIONS.md").read_text()


def test_verifier_rpc_stays_independent_of_agent_visible_chain_values(tmp_path: Path, monkeypatch):
    """Agent-visible chain data is untrusted: a poisoned CKB_RPC_URL in the environment must not
    reach the verifier, which keeps resolving and querying its own endpoint."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    monkeypatch.setenv("CKB_RPC_URL", "http://attacker-controlled:1")
    monkeypatch.setenv("CKBBENCH_CHAIN_PROFILE", "testnet")
    asked: list[str] = []

    def recording_rpc(method: str, params: list):
        asked.append(method)
        return _rpc(method, params)

    result = run_cell(
        suite, "devnet", "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=recording_rpc, agent_factory=_make_agent_factory(),
        now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "pass"
    assert result.chain == "devnet"
    assert "get_tip_block_number" in asked  # graded through the harness client, not the agent's


def test_chain_preparation_runs_before_mcp_preflight_params_and_the_agent(tmp_path: Path):
    """Order is the whole point: a cell that drew params or ran an agent before the reset would be
    observing (and then destroying) the previous cell's chain (plan §9.1)."""
    from ckbbench.run.devnet import DevnetState, LIFECYCLE_POLICY

    root, suite, mount, vpriv, results = _setup(tmp_path)
    events: list[str] = []
    state = DevnetState(LIFECYCLE_POLICY, "ckb_dev", "0x" + "ab" * 32, "d" * 64, 9, "0x" + "cd" * 32)

    def prepare(chain):
        events.append(f"prepared:{chain}")
        return state

    def recording_rpc(method, params):
        events.append(f"rpc:{method}")
        return _rpc(method, params)

    def factory(**kwargs):
        events.append("agent_built")
        return FakeAgent(mount_dir=kwargs["mount_dir"],
                         surface=policy_for_arm(kwargs["arm_config"].arm))

    class RecordingMcp(FakeMcpClient):
        def initialize(self):
            events.append("mcp_preflight")
            return super().initialize()

    result = run_cell(
        suite, "devnet", "C", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=recording_rpc, agent_factory=factory, mcp_client_factory=lambda _url: RecordingMcp(),
        prepare_chain=prepare, now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
    )

    assert events[0] == "prepared:devnet", events
    assert events.index("prepared:devnet") < events.index("mcp_preflight")
    assert events.index("prepared:devnet") < events.index("rpc:get_tip_block_number")
    assert events.index("prepared:devnet") < events.index("agent_built")
    assert result.devnet_state == state


def test_chain_preparation_failure_is_infra_fail_with_no_agent_or_mcp(tmp_path: Path):
    """A failed reset must not be laundered into an agent failure, and must not let the cell touch
    MCP, the chain, or a model."""
    from ckbbench.run.devnet import DevnetLifecycleError

    root, suite, mount, vpriv, results = _setup(tmp_path)
    events: list[str] = []

    def prepare(chain):
        raise DevnetLifecycleError("volume is not benchmark-owned")

    def recording_rpc(method, params):
        events.append(f"rpc:{method}")
        return _rpc(method, params)

    def factory(**kwargs):
        events.append("agent_built")
        return FakeAgent(mount_dir=kwargs["mount_dir"],
                         surface=policy_for_arm(kwargs["arm_config"].arm))

    result = run_cell(
        suite, "devnet", "C", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=recording_rpc, agent_factory=factory,
        mcp_client_factory=lambda _url: pytest.fail("MCP preflight must not run"),
        prepare_chain=prepare, now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "infra_fail"
    assert result.tasks == ()
    assert result.total_score == 0
    assert result.metrics.total_tokens is None
    assert result.devnet_state is None
    assert events == [], f"nothing may run after a failed reset, saw {events}"
    written = json.loads((results / f"{result.run_id}.json").read_text())
    assert written["outcome"] == "infra_fail"
    assert written["devnet_state"] is None


@pytest.mark.parametrize(
    "boom",
    [RuntimeError("malformed RPC transport response"), ValueError("bad tip hex"),
     OSError("docker executable not found")],
)
def test_any_lifecycle_failure_type_is_recorded_as_infra_fail(tmp_path: Path, boom):
    """run_cell converts DevnetLifecycleError; the controller normalises everything else into it.
    If a transport error escaped, the cell would crash with no artifact at all."""
    from ckbbench.run.devnet import DevnetLifecycleError

    root, suite, mount, vpriv, results = _setup(tmp_path)

    def prepare(chain):
        # what production does: the controller wraps unexpected failures before they reach here
        try:
            raise boom
        except Exception as exc:
            raise DevnetLifecycleError(f"{type(exc).__name__}: {exc}") from exc

    result = run_cell(
        suite, "devnet", "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(), prepare_chain=prepare,
        now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "infra_fail"
    assert (results / f"{result.run_id}.json").is_file(), "one early artifact must be persisted"


def test_real_prepare_devnet_client_failure_reaches_the_cell_as_infra_fail(tmp_path, monkeypatch):
    """End-to-end over the seam: the REAL controller, not a stub raising the right type.

    The other lifecycle tests inject a `prepare_chain` that already raises DevnetLifecycleError, so
    they would still pass if the controller stopped normalising. This one lets `prepare_devnet`
    fail for real -- at RPC-client construction, outside the docker work -- and asserts the cell
    persists one infra_fail artifact and crosses no later boundary.
    """
    import ckbbench.ckb_rpc as rpc_mod
    from ckbbench.run.devnet import prepare_devnet

    root, suite, mount, vpriv, results = _setup(tmp_path)
    events: list[str] = []

    def boom(*_a, **_kw):
        raise ValueError("invalid rpc url")

    monkeypatch.setattr(rpc_mod, "make_rpc_client", boom)

    def docker(argv):
        events.append("docker")
        raise AssertionError("docker must not be reached")

    def recording_rpc(method, params):
        events.append(f"rpc:{method}")
        return _rpc(method, params)

    result = run_cell(
        suite, "devnet", "C", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=recording_rpc,
        agent_factory=lambda **kw: pytest.fail("no agent may be built"),
        mcp_client_factory=lambda _url: pytest.fail("MCP preflight must not run"),
        prepare_chain=lambda _chain: prepare_devnet(run=docker, sleep=lambda _s: None),
        now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "infra_fail"
    assert result.tasks == () and result.devnet_state is None
    assert events == [], f"nothing may run after a failed preparation, saw {events}"
    written = json.loads((results / f"{result.run_id}.json").read_text())
    assert written["outcome"] == "infra_fail" and written["devnet_state"] is None


def test_cells_without_a_preparation_seam_are_untouched(tmp_path: Path):
    """TestNet, local mode and unit tests pass no seam: the cell must run and record no
    provenance rather than inventing a managed-DevNet claim."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite, "testnet", "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(),
        now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0,
    )
    assert result.devnet_state is None
    assert result.outcome == "pass"


def test_signer_value_never_reaches_the_mount_private_files_or_the_result(tmp_path: Path):
    """The DevNet signer is public development material, but it must still stay out of every
    artifact a run produces: prompts, task params, verifier-private files, and the result JSON.
    A key that leaks into results would leak an operator's TestNet key by the same path."""
    from ckbbench.config import DEVNET_GENESIS_PRIVKEY

    root, suite, mount, vpriv, results = _setup(tmp_path)
    run_cell(
        suite, "devnet", "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(),
        now_fn=lambda: 1.0, monotonic_fn=lambda: 0.0, keep=True,
    )
    written = [p for p in list(mount.rglob("*")) + list(vpriv.rglob("*")) + list(results.rglob("*"))
               if p.is_file()]
    assert written, "expected the cell to write artifacts"
    for path in written:
        assert DEVNET_GENESIS_PRIVKEY not in path.read_text(errors="ignore"), path
    # the composed instructions may NAME the variable; they must never render it
    assert "CKB_SENDER_PRIVKEY" in (mount / "INSTRUCTIONS.md").read_text()


def test_inject_harness_tip_overrides_drawn_value():
    task = Task(
        id="t",
        prompt_fragment="x",
        score=1,
        proof_file="p.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="constant_hex", rpc_method="constant"),
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
        verifier=OnchainVerifierSpec(check="constant_hex", rpc_method="constant"),
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
                "check": "tip_block_identity",
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


def test_prepare_failure_before_grade_is_infra_fail(tmp_path: Path, monkeypatch):
    """WHY: volume/stop prepare must score infra_fail end-to-end, not agent_fail."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    verify_called = {"n": 0}

    def boom_prepare(_volume, _run=None):
        raise PrepareError("work volume still present")

    def spy_verify(*args, **kwargs):
        verify_called["n"] += 1
        return []

    monkeypatch.setattr("ckbbench.run.orchestrate.prepare_work_volume", boom_prepare)
    monkeypatch.setattr("ckbbench.run.orchestrate.verify_suite", spy_verify)

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
        work_volume="ckbbench-work",
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
        # keep=True: this test's subject is prepare-failure classification, and without it the
        # cleanup path would run `docker volume rm -f` against a real volume.
        keep=True,
    )
    assert result.outcome == "infra_fail"
    assert verify_called["n"] == 0


def test_prepare_error_from_verify_suite_is_infra_fail(tmp_path: Path, monkeypatch):
    root, suite, mount, vpriv, results = _setup(tmp_path)

    def raise_prepare(*args, **kwargs):
        raise PrepareError("chown-free prepare failed in grade")

    monkeypatch.setattr("ckbbench.run.orchestrate.verify_suite", raise_prepare)

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
    assert result.outcome == "infra_fail"


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


def test_agent_exception_is_infrastructure_not_agent_failure(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)

    class BrokenAgent:
        def run(self, pointer: str):
            raise RuntimeError("boom")

    def broken_factory(**kwargs):
        return _with_surface(BrokenAgent(), kwargs["arm_config"])

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
    # The agent loop converts ordinary model behavior into an exit status, so an escaping exception
    # is a harness or provider condition. Grading it as agent failure would charge the model for it.
    assert result.outcome == "infra_fail"
    assert result.tasks == ()


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
    text = _compose_for_arm(suite, resolve_arm("A"), "devnet")
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
    compose_mod = __import__("ckbbench.suite.compose", fromlist=["compose"])
    assert _compose_for_arm(suite, empty_arm, "devnet") == compose_mod.compose(
        suite, chain_context=compose_mod.chain_context_text("devnet")
    )


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
    text = _compose_for_arm(suite, resolve_arm("A"), "devnet")
    base_idx = text.index("BASE PREAMBLE REWORDED")
    chain_idx = text.index("CKB devnet chain")
    arm_idx = text.index("must NOT use web research")
    first_task_idx = text.index("1.")
    assert base_idx < chain_idx < arm_idx < first_task_idx, (
        "chain context and arm preamble must sit between the base preamble and the tasks"
    )


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
        agent_factory=lambda **kwargs: _with_surface(OddAgent(), kwargs["arm_config"]),
        now_fn=lambda: 1_700_000_000.0,
        monotonic_fn=lambda: 0.0,
    )
    assert result.agent_exit_status is None
    assert result.outcome == "agent_fail"



def test_b_violation_outranks_an_already_failed_cell(tmp_path: Path):
    """A detected product connection must not be hidden by an unrelated agent failure."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite, "devnet", "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(exit_status="Submitted", write_proofs=False),
        violation_check=lambda arm, mnt: True,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "protocol_violation"


def test_missing_violation_evidence_persists_an_infra_fail(tmp_path: Path):
    """Absence of evidence is not evidence of compliance."""
    from ckbbench.run.orchestrate import ViolationEvidenceError

    root, suite, mount, vpriv, results = _setup(tmp_path)

    def unreadable(arm, mnt):
        raise ViolationEvidenceError("docker logs ckbbench-proxy exited 1")

    result = run_cell(
        suite, "devnet", "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(), violation_check=unreadable,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "infra_fail"
    written = json.loads((results / f"{result.run_id}.json").read_text())
    assert written["outcome"] == "infra_fail", "the artifact must be persisted, not raised past"


def test_an_unrelated_checker_bug_is_not_laundered_into_infra_fail(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)

    def buggy(arm, mnt):
        raise KeyError("programming error in the checker")

    with pytest.raises(KeyError):
        run_cell(
            suite, "devnet", "B", "test/model", 1,
            registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
            rpc=_rpc, agent_factory=_make_agent_factory(), violation_check=buggy,
            now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        )


def test_no_checker_wired_leaves_the_cell_unchanged(tmp_path: Path):
    """A/D keep their existing behaviour when no reader is supplied (unit context, not production)."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(), violation_check=None,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "pass"


def test_production_checker_with_an_unreadable_reader_persists_infra_fail(tmp_path: Path):
    """End to end across both seams: a reader raising OSError must reach a persisted infra_fail."""
    from ckbbench.run.proxy_log import make_violation_check

    root, suite, mount, vpriv, results = _setup(tmp_path)
    check = make_violation_check(
        arm="B", chain="devnet", mcp_url="https://mcp.example/x",
        log_fetcher=lambda: (_ for _ in ()).throw(OSError("reader unavailable")),
    )
    result = run_cell(
        suite, "devnet", "B", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_make_agent_factory(), violation_check=check,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "infra_fail"
    assert json.loads((results / f"{result.run_id}.json").read_text())["outcome"] == "infra_fail"


# --- grading-observation failure is infra_fail, not agent_fail ------------------------------------

TX_PROOF_HASH = "0x" + "11" * 32
TX_RECIPIENT = "0x470dcdc5e44064909650113a274b3b36aecb6dc7"
TX_PROOF_RAW = f"  {TX_PROOF_HASH}  \r\n"  # CRLF: universal-newline translation would rewrite it


class _TxEnv:
    """Non-container agent env: stop_agent_checked calls cleanup() when there is no container."""

    container_id = None

    def __init__(self, on_cleanup) -> None:
        self._on_cleanup = on_cleanup

    def cleanup(self) -> None:
        self._on_cleanup()


class _TxAgent:
    """Writes one syntactically valid transaction hash, like a compliant agent would."""

    def __init__(self, *, mount_dir: Path, on_stop=lambda: None) -> None:
        self.mount_dir = mount_dir
        self.config = type(
            "C", (), {"step_limit": 80, "cost_limit": 0.0, "wall_time_limit_seconds": 900}
        )()
        self.messages = [{"extra": {"response": {"usage": {"total_tokens": 50}}}}]
        self.env = _TxEnv(on_stop)

    def run(self, pointer: str) -> dict:
        # Deliberately padded: the persisted evidence must be the agent's exact bytes.
        (self.mount_dir / "tx_id.txt").write_bytes(TX_PROOF_RAW.encode("utf-8"))
        return {"exit_status": AGENT_DONE_EXIT}


def _tx_registry(tmp_path: Path):
    return build_registry(
        tmp_path / "registry",
        tasks=[
            {
                "id": "task-tx",
                "proof_file": "tx_id.txt",
                "score": 25,
                "kind": "onchain",
                "check": "tx_proof",
                "rpc_method": "get_transaction",
                "fragment": "Send a transaction and write its hash to tx_id.txt.",
                "param_schema": [
                    {"name": "send_amount_shannons", "class": "prompt",
                     "generator": "high_entropy_nonce_amount_shannons", "share_group": "nonce"},
                    {"name": "recipient_args", "class": "prompt", "generator": "recipient_args",
                     "static_value": TX_RECIPIENT, "share_group": "recipient"},
                    {"name": "harness_tip", "class": "verifier", "generator": "harness_tip"},
                    {"name": "nonce_amount_shannons", "class": "verifier",
                     "generator": "high_entropy_nonce_amount_shannons", "share_group": "nonce"},
                    {"name": "recipient_args", "class": "verifier", "generator": "recipient_args",
                     "static_value": TX_RECIPIENT, "share_group": "recipient"},
                ],
            },
        ],
        manifest_overrides={"tasks": ["task-tx"]},
    )


def test_grading_observation_failure_is_infra_fail_through_the_real_verifier(tmp_path: Path):
    """The whole cell path, not a monkeypatched verify_suite: run-start RPC succeeds and only the
    grading read fails, so the model must not be charged for an unobservable chain."""
    root = _tx_registry(tmp_path)
    suite = load_suite(root)
    mount, vpriv, results = tmp_path / "mount", tmp_path / "vpriv", tmp_path / "results"
    agents: list[_TxAgent] = []
    order: list[str] = []

    def factory(**kwargs):
        agent = _with_surface(
            _TxAgent(mount_dir=kwargs["mount_dir"], on_stop=lambda: order.append("stop")),
            kwargs["arm_config"])
        agents.append(agent)
        return agent

    def rpc(method, params):
        if method == "get_transaction":
            order.append("grade")
            raise ConnectionError("node went away during grading")
        if method == "get_tip_block_number":
            return hex(HARNESS_TIP)
        return None

    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=rpc, agent_factory=factory,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        sleep_fn=lambda s: pytest.fail("grading must not sleep on a transport failure"),
    )

    assert result.outcome == "infra_fail"
    assert result.tasks == ()
    assert result.total_score == 0
    assert result.max_score == sum(t.score for t in suite.tasks if t.scored) == 25
    assert result.agent_exit_status == AGENT_DONE_EXIT
    assert result.metrics.total_tokens == 50, "agent metrics collected before grading are kept"
    assert result.agent_limits["step_limit"] == 80
    assert order[0] == "stop", "grading must run only after the checked agent stop"
    assert "grade" in order

    payload = json.loads((results / f"{result.run_id}.json").read_text())
    assert payload["outcome"] == "infra_fail"
    assert payload["tasks"] == []
    assert payload["total_score"] == 0
    assert payload["max_score"] == 25
    assert payload["schema_version"] == RESULT_SCHEMA_VERSION
    # The artifact must retain what was measured before grading, not only the in-memory object.
    assert payload["agent_exit_status"] == AGENT_DONE_EXIT
    assert payload["metrics"]["total_tokens"] == 50
    assert payload["agent_limits"]["step_limit"] == 80
    # This fixture is a non-Docker devnet cell with no preflight pin, so these are legitimately
    # absent rather than asserted-as-present provenance.
    assert payload["preflight_server_version"] is None
    assert payload["devnet_state"] is None


def test_pending_transaction_polls_with_injected_time_and_scores_normally(tmp_path: Path):
    """run_cell must hand its own clock and sleeper to the verifier; a real 90s wait would
    otherwise appear here."""
    root = _tx_registry(tmp_path)
    suite = load_suite(root)
    mount, vpriv, results = tmp_path / "mount", tmp_path / "vpriv", tmp_path / "results"
    virtual = [0.0]
    sleeps: list[float] = []
    reads = [0]

    def monotonic():
        return virtual[0]

    def sleep(seconds):
        sleeps.append(seconds)
        virtual[0] += seconds

    def rpc(method, params):
        if method == "get_tip_block_number":
            return hex(HARNESS_TIP)
        if method == "get_header":
            return {"number": hex(HARNESS_TIP + 5)}
        reads[0] += 1
        if reads[0] < 3:
            return {"transaction": {"outputs": []}, "tx_status": {"status": "pending"}}
        return {
            "transaction": {"outputs": []},
            "tx_status": {"status": "committed", "block_hash": "0xb"},
        }

    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=rpc,
        agent_factory=lambda **kw: _with_surface(
            _TxAgent(mount_dir=kw["mount_dir"]), kw["arm_config"]),
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=monotonic, sleep_fn=sleep,
    )

    assert sleeps == [2.0, 2.0], "the injected sleeper must be the one that ran"
    assert result.outcome != "infra_fail"
    assert len(result.tasks) == 1
    # No matching output: an ordinary task failure, graded normally after the poll.
    assert not result.tasks[0].passed
    # The agent's exact bytes reach the persisted task row, unstripped.
    payload = json.loads((results / f"{result.run_id}.json").read_text())
    assert payload["tasks"][0]["proof"] == TX_PROOF_RAW, "raw agent bytes must reach the artifact"
    assert payload["tasks"][0]["proof"].encode("utf-8") == (mount / "tx_id.txt").read_bytes()


# --- run-bound tip identity through the real verifier ---------------------------------------------

TIP_BLOCK_HASH = "0x" + "ab" * 32
RUN_START_TIP = 0x2A
VERIFY_TIME_TIP = RUN_START_TIP + 5_000  # far beyond the removed 50-block window


class _TipAgent:
    """Reads the tip early, like a real agent would, and pairs it with that height's hash.

    Carries the non-container env seam so stop_agent_checked() actually calls cleanup(): with
    env=None it returns immediately and a stop-before-grade assertion would be vacuous.
    """

    def __init__(self, *, mount_dir: Path, proof: str, on_stop=lambda: None) -> None:
        self.mount_dir = mount_dir
        self.proof = proof
        self.config = type(
            "C", (), {"step_limit": 80, "cost_limit": 0.0, "wall_time_limit_seconds": 900}
        )()
        self.messages = [{"extra": {"response": {"usage": {"total_tokens": 50}}}}]
        self.env = _TxEnv(on_stop)

    def run(self, pointer: str) -> dict:
        (self.mount_dir / "proof_tip.txt").write_bytes(self.proof.encode("utf-8"))
        return {"exit_status": AGENT_DONE_EXIT}


def _tip_registry(tmp_path: Path):
    return build_registry(
        tmp_path / "registry",
        tasks=[
            {
                "id": "task-01-tip",
                "proof_file": "proof_tip.txt",
                "score": 10,
                "kind": "onchain",
                "check": "tip_block_identity",
                "rpc_method": "get_tip_block_number",
                "fragment": "Write the tip and its block hash to proof_tip.txt.",
                "param_schema": [
                    {"name": "harness_tip", "class": "verifier", "generator": "harness_tip"},
                ],
            },
        ],
        manifest_overrides={"tasks": ["task-01-tip"]},
    )


def _tip_rpc(tip_calls: list[str]):
    def rpc(method, params):
        tip_calls.append(method)
        if method == "get_tip_block_number":
            # First call is the run-start capture; grading sees a much later tip.
            return hex(RUN_START_TIP) if len(tip_calls) == 1 else hex(VERIFY_TIME_TIP)
        if method == "get_block_hash":
            return TIP_BLOCK_HASH
        return None

    return rpc


def test_task_01_early_proof_passes_a_long_cell_and_persists_raw(tmp_path: Path):
    """The whole cell path: checked stop before grading, one run-start capture delivered only to the
    verifier, an early honest proof that survives 5000 later blocks, and exact bytes in the artifact."""
    root = _tip_registry(tmp_path)
    suite = load_suite(root)
    mount, vpriv, results = tmp_path / "mount", tmp_path / "vpriv", tmp_path / "results"
    raw_proof = f"  {hex(RUN_START_TIP)}  \r\n  {TIP_BLOCK_HASH}  \r\n"
    order: list[str] = []

    tip_calls: list[str] = []

    def rpc(method, params):
        if method == "get_tip_block_number":
            # Counted independently of `order`: keying off an empty event list would let any number
            # of accidental pre-agent captures all masquerade as "the first" one.
            tip_calls.append(method)
            if len(tip_calls) == 1:
                return hex(RUN_START_TIP)          # the single run-start capture
            order.append("grade:tip")
            return hex(VERIFY_TIME_TIP)
        if method == "get_block_hash":
            order.append("grade:hash")
            return TIP_BLOCK_HASH
        return None

    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=rpc,
        agent_factory=lambda **kw: _with_surface(_TipAgent(
            mount_dir=kw["mount_dir"], proof=raw_proof, on_stop=lambda: order.append("stop")), kw["arm_config"]),
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "pass", result.tasks
    assert result.total_score == 10
    assert order[0] == "stop", f"grading must follow the checked agent stop: {order}"
    assert "grade:tip" in order and "grade:hash" in order
    assert len(tip_calls) == 2, f"exactly one run-start capture and one grading read: {tip_calls}"
    assert VERIFY_TIME_TIP - RUN_START_TIP > 50, "must exceed the removed freshness window"

    payload = json.loads((results / f"{result.run_id}.json").read_text())
    assert payload["tasks"][0]["passed"] is True
    assert payload["tasks"][0]["proof"] == raw_proof
    assert payload["tasks"][0]["proof"].encode("utf-8") == (mount / "proof_tip.txt").read_bytes()

    # The run-start height is verifier-private: neither its name nor its value may reach the two
    # agent-readable delivery artifacts. The proof file legitimately holds the tip the agent read.
    params_file = mount / "task-01-tip.json"
    assert json.loads(params_file.read_text()) == {}
    instructions = (mount / "INSTRUCTIONS.md").read_text()
    for artifact in (params_file.read_text(), instructions):
        assert "harness_tip" not in artifact
        assert str(RUN_START_TIP) not in artifact
        assert hex(RUN_START_TIP) not in artifact
    stored = "\n".join(p.read_text() for p in vpriv.rglob("*") if p.is_file())
    assert "harness_tip" in stored and str(RUN_START_TIP) in stored


def test_task_01_grading_transport_fault_is_infra_fail(tmp_path: Path):
    root = _tip_registry(tmp_path)
    suite = load_suite(root)
    mount, vpriv, results = tmp_path / "mount", tmp_path / "vpriv", tmp_path / "results"
    seen: list[str] = []

    def rpc(method, params):
        seen.append(method)
        if method == "get_tip_block_number" and len(seen) == 1:
            return hex(RUN_START_TIP)
        raise ConnectionError("node went away during grading")

    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=rpc,
        agent_factory=lambda **kw: _with_surface(_TipAgent(
            mount_dir=kw["mount_dir"], proof=f"{hex(RUN_START_TIP)}\n{TIP_BLOCK_HASH}\n"), kw["arm_config"]),
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "infra_fail"
    assert result.tasks == ()
    assert result.total_score == 0
    payload = json.loads((results / f"{result.run_id}.json").read_text())
    assert payload["outcome"] == "infra_fail"
    assert payload["tasks"] == []


def test_task_01_hardcoded_low_tip_is_an_ordinary_task_failure(tmp_path: Path):
    """`0x1` passed the old window whenever a cell started near genesis."""
    root = _tip_registry(tmp_path)
    suite = load_suite(root)
    mount, vpriv, results = tmp_path / "mount", tmp_path / "vpriv", tmp_path / "results"

    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_tip_rpc([]),
        agent_factory=lambda **kw: _with_surface(_TipAgent(
            mount_dir=kw["mount_dir"], proof=f"0x1\n{TIP_BLOCK_HASH}\n"), kw["arm_config"]),
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "agent_fail"
    assert result.total_score == 0
    assert len(result.tasks) == 1
    assert "predates run start" in result.tasks[0].reason


def test_task_01_stale_proof_result_never_persists_the_private_lower_bound(tmp_path: Path):
    """A failed tip-identity row is persisted without leaking the run-start height."""
    root = _tip_registry(tmp_path)
    suite = load_suite(root)
    mount, vpriv, results = tmp_path / "mount", tmp_path / "vpriv", tmp_path / "results"

    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_tip_rpc([]),
        agent_factory=lambda **kw: _with_surface(_TipAgent(
            mount_dir=kw["mount_dir"], proof=f"0x1\n{TIP_BLOCK_HASH}\n"), kw["arm_config"]),
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "agent_fail"
    row = result.tasks[0]
    assert not row.passed and "predates run start" in row.reason
    artifact = (results / f"{result.run_id}.json").read_text()
    for form in (str(RUN_START_TIP), hex(RUN_START_TIP), "harness_tip"):
        assert form not in json.loads(artifact)["tasks"][0]["reason"], form



from ckbbench.verify.onchain import (  # noqa: E402
    R1_CAPACITY_SHANNONS, SECP_CODE_HASH, SECP_HASH_TYPE, TYPE_ID_CODE_HASH,
    ckb_blake2b, molecule_script, type_id_args,
)

R1_TX_HASH = "0x" + "aa" * 32
R1_INPUT0 = {"since": "0x0", "previous_output": {"tx_hash": "0x" + "11" * 32, "index": "0x0"}}
R1_TYPE_ARGS = type_id_args(R1_INPUT0, 0)
R1_SCRIPT_HASH = "0x" + ckb_blake2b(
    molecule_script(bytes.fromhex(TYPE_ID_CODE_HASH[2:]), "type", R1_TYPE_ARGS)
).hex()
R1_RECIPIENT = "0x470dcdc5e44064909650113a274b3b36aecb6dc7"


class _R1Agent:
    """Reads the prompt-class payload from its own params file and writes a two-line proof."""

    def __init__(self, *, mount_dir: Path, proof: str, on_stop=lambda: None) -> None:
        self.mount_dir = mount_dir
        self.proof = proof
        self.config = type(
            "C", (), {"step_limit": 80, "cost_limit": 0.0, "wall_time_limit_seconds": 900}
        )()
        self.messages = [{"extra": {"response": {"usage": {"total_tokens": 50}}}}]
        self.env = _TxEnv(on_stop)
        self.seen_params: dict = {}

    def run(self, pointer: str) -> dict:
        params = self.mount_dir / "task-08-type-id-data-cell.json"
        self.seen_params = json.loads(params.read_text()) if params.is_file() else {}
        (self.mount_dir / "proof_type_id_data_cell.txt").write_bytes(self.proof.encode("utf-8"))
        return {"exit_status": AGENT_DONE_EXIT}


def _r1_real_suite():
    """Use the data-cell object authored in the frozen suite, not a second schema copy.

    A duplicated fixture stays green while the shipped definition drifts, so the cell proof uses the
    same task that will ship.
    """
    import dataclasses

    real = load_suite(Path("suites/ckb-v1"))
    task = next(t for t in real.tasks if t.id == "task-08-type-id-data-cell")
    return dataclasses.replace(real, tasks=(task,)), task


def _r1_committed(payload_hex: str):
    return {
        "transaction": {
            "inputs": [R1_INPUT0],
            "outputs": [{
                "capacity": hex(R1_CAPACITY_SHANNONS),
                "lock": {"code_hash": SECP_CODE_HASH, "hash_type": SECP_HASH_TYPE,
                         "args": R1_RECIPIENT},
                "type": {"code_hash": TYPE_ID_CODE_HASH, "hash_type": "type",
                         "args": "0x" + R1_TYPE_ARGS.hex()},
            }],
            "outputs_data": [payload_hex],
        },
        "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32},
    }


def test_task_08_cell_shares_one_draw_and_persists_a_25_point_pass(tmp_path: Path):
    """One payload draw reaches the mount and the verifier; only prompt-class values are exposed."""
    suite, real_task = _r1_real_suite()
    root = Path("suites/ckb-v1")
    assert real_task.score == 25 and real_task.proof_file == "proof_type_id_data_cell.txt"
    mount, vpriv, results = tmp_path / "mount", tmp_path / "vpriv", tmp_path / "results"
    agents: list[_R1Agent] = []
    order: list[str] = []
    drawn: dict = {}

    def rpc(method, params):
        if method == "get_tip_block_number":
            return hex(HARNESS_TIP)
        if method == "get_header":
            order.append("grade:header")
            return {"number": hex(HARNESS_TIP + 5)}
        order.append("grade:tx")
        return _r1_committed(drawn["payload"])

    def factory(**kw):
        m = kw["mount_dir"]
        drawn["payload"] = json.loads((m / "task-08-type-id-data-cell.json").read_text())["payload_hex"]
        agent = _with_surface(
            _R1Agent(mount_dir=m, proof=f"{R1_TX_HASH}\n{R1_SCRIPT_HASH}",
                     on_stop=lambda: order.append("stop")),
            kw["arm_config"])
        agents.append(agent)
        return agent

    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=rpc, agent_factory=factory,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "pass", result.tasks
    assert result.total_score == 25
    assert order[0] == "stop", f"grading must follow the checked agent stop: {order}"

    # Exactly the two prompt-class values reach the mount; the verifier copy never does.
    exposed = agents[0].seen_params
    assert set(exposed) == {"payload_hex", "recipient_args"}
    mount_text = "\n".join(p.read_text(errors="ignore") for p in mount.rglob("*") if p.is_file())
    for name in ("expected_payload_hex", "expected_recipient_args", "harness_tip"):
        assert name not in mount_text, name
    stored = "\n".join(p.read_text() for p in vpriv.rglob("*") if p.is_file())
    assert exposed["payload_hex"] in stored, "one shared draw must reach the verifier copy"

    payload = json.loads((results / f"{result.run_id}.json").read_text())
    assert payload["tasks"][0]["passed"] is True
    assert payload["tasks"][0]["score_awarded"] == 25
    assert payload["tasks"][0]["proof"] == f"{R1_TX_HASH}\n{R1_SCRIPT_HASH}"


def test_task_08_grading_transport_fault_is_infra_fail(tmp_path: Path):
    suite, _real_task = _r1_real_suite()
    root = Path("suites/ckb-v1")
    mount, vpriv, results = tmp_path / "mount", tmp_path / "vpriv", tmp_path / "results"
    seen: list[str] = []

    def rpc(method, params):
        seen.append(method)
        if method == "get_tip_block_number" and len(seen) == 1:
            return hex(HARNESS_TIP)
        raise ConnectionError("node went away during grading")

    result = run_cell(
        suite, "devnet", "A", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=rpc,
        agent_factory=lambda **kw: _with_surface(_R1Agent(
            mount_dir=kw["mount_dir"], proof=f"{R1_TX_HASH}\n{R1_SCRIPT_HASH}"), kw["arm_config"]),
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )

    assert result.outcome == "infra_fail"
    assert result.tasks == ()
    assert result.total_score == 0
    artifact = json.loads((results / f"{result.run_id}.json").read_text())
    assert artifact["outcome"] == "infra_fail"
    assert artifact["tasks"] == []
    assert artifact["total_score"] == 0


def test_use_docker_reads_the_production_switch_not_the_test_runner_request(monkeypatch):
    """`scripts/test.sh` captures its layer request and clears this switch for the unit layer.

    The same variable name selects the integration layer AND places production orchestration in
    docker mode. If the unit layer ran with it set, these tests would touch local Docker state.
    """
    from ckbbench.run.defaults import use_docker

    monkeypatch.setenv("CKBBENCH_DOCKER", "0")
    assert use_docker() is False
    monkeypatch.delenv("CKBBENCH_DOCKER", raising=False)
    assert use_docker() is False
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    assert use_docker() is True


def test_test_runner_clears_the_docker_switch_for_the_unit_layer():
    """Static guard: the runner must not hand CKBBENCH_DOCKER=1 to the unit-test process."""
    from pathlib import Path

    script = (Path(__file__).resolve().parents[2] / "scripts" / "test.sh").read_text()
    assert 'CKBBENCH_DOCKER=0 "$PY" -m pytest' in script, (
        "the unit layer must run with the production docker switch explicitly disabled"
    )
    assert 'WANT_DOCKER_LAYER="${CKBBENCH_DOCKER:-0}"' in script, (
        "the integration-layer request must be captured before the unit layer runs"
    )
    assert 'if [ "$WANT_DOCKER_LAYER" = "1" ]; then' in script, (
        "the integration layer must key off the captured request"
    )


# --- MCP surface provenance on the production path (ADR-0013) ------------------------------------

@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
def test_every_persisted_row_records_the_arms_surface_profile(tmp_path: Path, arm):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite, "devnet", arm, "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount,
        verifier_private_root=vpriv,
        mcp_client_factory=(lambda _url: FakeMcpClient()) if arm in ("C", "D") else None,
        rpc=_rpc, agent_factory=_make_agent_factory(),
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    expected = profile_for_arm(arm)
    assert result.mcp_surface_profile == expected
    written = json.loads((results / f"{result.run_id}.json").read_text())
    assert written["mcp_surface_profile"] == expected
    assert written["schema_version"] == RESULT_SCHEMA_VERSION


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
def test_a_pre_agent_infra_fail_still_records_the_profile(tmp_path: Path, arm):
    """The surface is fixed by the arm before any agent exists, unlike the runtime limits."""
    from ckbbench.run.devnet import DevnetLifecycleError

    root, suite, mount, vpriv, results = _setup(tmp_path)

    def boom(_chain):
        raise DevnetLifecycleError("reset failed")

    result = run_cell(
        suite, "devnet", arm, "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount,
        verifier_private_root=vpriv, rpc=_rpc, agent_factory=_make_agent_factory(),
        prepare_chain=boom,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "infra_fail"
    assert result.mcp_surface_profile == profile_for_arm(arm)
    written = json.loads((results / f"{result.run_id}.json").read_text())
    assert written["mcp_surface_profile"] == profile_for_arm(arm)
    # The budgets stay null because no agent was built; the surface does not.
    assert written["agent_limits"] == {
        "step_limit": None, "cost_limit": None, "wall_time_limit_seconds": None,
    }


def test_a_factory_configuring_the_wrong_surface_aborts_the_cell(tmp_path: Path):
    """Provenance must be the controller's actual configuration, never a hopeful label."""
    root, suite, mount, vpriv, results = _setup(tmp_path)

    def wrong_surface_factory(**kwargs):
        agent = _make_agent_factory()(**kwargs)
        agent.mcp_surface_profile = "off"
        return agent

    with pytest.raises(ValueError, match="requires MCP surface"):
        run_cell(
            suite, "devnet", "C", "test/model", 1,
            registry_root=root, results_dir=results, mount_dir=mount,
            verifier_private_root=vpriv,
            mcp_client_factory=lambda _url: FakeMcpClient(),
            rpc=_rpc, agent_factory=wrong_surface_factory,
            now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        )


@pytest.mark.parametrize("second_catalog,label", [
    ([{"name": "search_tools"}], "the required tool disappeared"),
    ([{"name": []}, {"name": "search_resources"}], "the catalog became malformed"),
    (None, "the catalog stopped being a list"),
])
def test_a_catalog_that_drifts_after_preflight_is_a_pre_agent_infra_fail(
    tmp_path: Path, second_catalog, label
):
    """Preflight and construction read the catalog separately, so drift must classify, not crash."""
    from ckbbench.run.mcp_surface import policy_for_arm as _policy

    root, suite, mount, vpriv, results = _setup(tmp_path)
    reads = {"n": 0}

    class DriftingClient(FakeMcpClient):
        def list_tools(self):
            reads["n"] += 1
            return [{"name": "search_resources"}] if reads["n"] == 1 else second_catalog

    client = DriftingClient()
    ran = {"agent": 0}

    def real_surface_factory(**kwargs):
        # Mirrors production: the policy filters the second catalog read at construction time.
        _policy(kwargs["arm_config"].arm).filter_tools(kwargs["mcp_client"].list_tools())
        ran["agent"] += 1
        return _make_agent_factory()(**kwargs)

    result = run_cell(
        suite, "devnet", "C", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        mcp_client_factory=lambda _url: client,
        rpc=_rpc, agent_factory=real_surface_factory,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "infra_fail", label
    assert result.tasks == ()
    assert ran["agent"] == 0
    assert result.mcp_surface_profile == "docs-only-v1"
    written = json.loads((results / f"{result.run_id}.json").read_text())
    assert written["outcome"] == "infra_fail"
    assert written["mcp_surface_profile"] == "docs-only-v1"


@pytest.mark.parametrize("mutate,match", [
    (lambda a: delattr(a, "mcp_surface_profile"), "declares no mcp_surface_profile"),
    (lambda a: setattr(a, "mcp_surface_profile", "off"), "requires MCP surface"),
    (lambda a: setattr(a, "mcp_surface_profile", "full"), "requires MCP surface"),
    (lambda a: setattr(a, "mcp_surface", None), "not the canonical"),
])
def test_absent_or_wrong_controller_provenance_fails_before_the_agent_runs(
    tmp_path: Path, mutate, match
):
    """A missing trusted arm attribute is refused before a result can be written."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    ran = {"run": 0}

    def factory(**kwargs):
        agent = _make_agent_factory()(**kwargs)
        original_run = agent.run

        def counted(pointer):
            ran["run"] += 1
            return original_run(pointer)

        agent.run = counted
        mutate(agent)
        return agent

    with pytest.raises(ValueError, match=match):
        run_cell(
            suite, "devnet", "C", "test/model", 1,
            registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
            mcp_client_factory=lambda _url: FakeMcpClient(),
            rpc=_rpc, agent_factory=factory,
            now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        )
    assert ran["run"] == 0
    assert not list(results.glob("*.json"))


@pytest.mark.parametrize("failure,label", [
    ("initialize", "the agent's own initialize failed"),
    ("list_tools", "the agent's own tools/list failed"),
])
def test_a_failed_agent_handshake_is_a_pre_agent_infra_fail(tmp_path: Path, failure, label):
    """Preflight and the agent use separate client constructions, so the second handshake can fail
    on its own. A raw exception there would abort the matrix instead of writing one row."""
    from ckbbench.run.mcp_surface import McpSurfaceSetupError

    root, suite, mount, vpriv, results = _setup(tmp_path)
    ran = {"agent": 0}

    def failing_factory(**kwargs):
        # Mirrors the production factory: the handshake happens during construction.
        raise McpSurfaceSetupError(f"mcp handshake failed (OSError) at {failure}")

    result = run_cell(
        suite, "devnet", "C", "test/model", 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        mcp_client_factory=lambda _url: FakeMcpClient(),
        rpc=_rpc, agent_factory=failing_factory,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "infra_fail", label
    assert result.tasks == ()
    assert result.total_score == 0
    assert ran["agent"] == 0
    assert result.mcp_surface_profile == "docs-only-v1"
    written = json.loads((results / f"{result.run_id}.json").read_text())
    assert written["outcome"] == "infra_fail"
    assert written["mcp_surface_profile"] == "docs-only-v1"
    assert written["agent_limits"] == {
        "step_limit": None, "cost_limit": None, "wall_time_limit_seconds": None,
    }


# --- model profile and token provenance on the production path (ADR-0014) ------------------------

_T17_PROFILE = parse_model_profile({
    "api_base": "https://proxy.example/v1",
    "api_style": "openai-responses",
    "drop_unsupported_params": True,
    "evidence_utc": "2026-08-15T09:30:00Z",
    "litellm_num_retries": 0,
    "max_agent_query_attempts": 4,
    "model_stability": "moving_alias",
    "probed_response_model": "openai/gpt-5-mini",
    "observation_max_bytes": 32768,
    "profile_id": "phase1-gpt-v10",
    "provider": "openrouter",
    "provider_allow_fallbacks": False,
    "provider_order": ["openai"],
    "provider_require_parameters": True,
    "provider_request_timeout_seconds": 300,
    "provider_retry_backoff_seconds": [4, 8, 16],
    "reasoning_context": "prefix_tail_groups",
    "reasoning_effort": "medium",
    "replay_max_bytes": 131072,
    "replay_policy": "prefix-tail-groups-v1",
    "store": False,
    "requested_model": "openai/gpt-5-mini",
    "retryable_provider_failure_categories": [
        "rate_limit", "timeout", "connection", "server", "protocol", "other_provider",
    ],
    "schema_version": "8",
    "temperature": None,
    "truncation": "disabled",
    "usage_contract": "openai-responses-usage-v1",
}, sha256="d" * 64)


def _profile_factory(ledger=None):
    def factory(**kwargs):
        agent = FakeAgent(
            mount_dir=kwargs["mount_dir"],
            surface=policy_for_arm(kwargs["arm_config"].arm),
            # A cell that answers from any other identity is not this profile's evidence, so the
            # default fake answers as the probed model and drift is stated explicitly per test.
            ledger=_FakeLedger(models=(_T17_PROFILE.probed_response_model,)) if ledger is None
            else ledger,
            model_profile=_T17_PROFILE,
        )
        return agent

    return factory


def test_a_pre_agent_infra_row_records_the_profile_and_not_started_usage(tmp_path: Path):
    from ckbbench.run.devnet import DevnetLifecycleError

    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite, "devnet", "B", _T17_PROFILE.requested_model, 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_profile_factory(), model_profile=_T17_PROFILE,
        prepare_chain=lambda _c: (_ for _ in ()).throw(DevnetLifecycleError("reset failed")),
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "infra_fail"
    assert result.model_profile_id == "phase1-gpt-v10"
    assert result.model_profile_sha256 == "d" * 64
    assert result.model_response_id is None
    assert result.metrics.token_usage_status == "not_started"
    written = json.loads((results / f"{result.run_id}.json").read_text())
    assert written["model_profile_id"] == "phase1-gpt-v10"
    assert written["metrics"]["token_usage_status"] == "not_started"


def test_a_normal_pass_carries_complete_usage_and_the_returned_model(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite, "devnet", "B", _T17_PROFILE.requested_model, 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_profile_factory(), model_profile=_T17_PROFILE,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "pass"
    assert result.metrics.token_usage_status == "complete"
    assert (result.metrics.prompt_tokens, result.metrics.completion_tokens,
            result.metrics.total_tokens) == (30, 20, 50)
    assert result.model_response_id == _T17_PROFILE.probed_response_model


@pytest.mark.parametrize("ledger,label,category,expected_outcome,expected_grades", [
    # These two answer as the probed model so the row is otherwise valid and the assertion below
    # isolates the category invariant rather than tripping the model-identity one.
    (_FakeLedger(turns=2, attempts=2, responses=1, totals=(10, 5, 15), complete=False,
                 models=(_T17_PROFILE.probed_response_model,), category="connection"),
     "a provider attempt failed", "connection", "infra_fail", 0),
    (_FakeLedger(turns=1, attempts=1, responses=1, totals=None, complete=False,
                 models=(_T17_PROFILE.probed_response_model,)),
     "the response carried no usable usage", None, "agent_fail", 1),
    (_FakeLedger(turns=1, attempts=1, responses=1, totals=(10, 5, 15), complete=False,
                 models=("gpt-a", "gpt-b")),
     "the returned model drifted", None, "infra_fail", 0),
])
def test_correctness_and_efficiency_completeness_are_classified_separately(
    tmp_path: Path, ledger, label, category, expected_outcome, expected_grades, monkeypatch
):
    """Missing usage alone excludes efficiency; a missing response or model still blocks grading."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    graded = {"n": 0}

    def counting_verify(*args, **kwargs):
        graded["n"] += 1
        return []

    import ckbbench.run.orchestrate as orch

    original = orch.verify_suite
    orch.verify_suite = counting_verify
    try:
        result = run_cell(
            suite, "devnet", "B", _T17_PROFILE.requested_model, 1,
            registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
            rpc=_rpc, agent_factory=_profile_factory(ledger), model_profile=_T17_PROFILE,
            now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        )
    finally:
        orch.verify_suite = original

    assert result.outcome == expected_outcome, label
    assert result.tasks == ()
    assert result.total_score == 0
    assert graded["n"] == expected_grades, label
    assert result.metrics.token_usage_status == "incomplete"
    written = json.loads((results / f"{result.run_id}.json").read_text())
    assert written["metrics"]["provider_attempts"] >= 1
    # The artifact this orchestration writes must satisfy the boundary that consumes it, so the
    # category is asserted on both sides and the row is put through the real validator.
    import ckbbench.matrix.store as store

    assert result.metrics.provider_failure_category == category, label
    assert written["metrics"]["provider_failure_category"] == category, label
    monkeypatch.setattr(store, "_reviewed_profile", lambda: _T17_PROFILE)
    store.validate_results([written])


def test_a_known_lower_bound_survives_an_incomplete_run(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)
    ledger = _FakeLedger(turns=2, attempts=2, responses=1, totals=(10, 5, 15), complete=False,
                         category="connection")
    result = run_cell(
        suite, "devnet", "B", _T17_PROFILE.requested_model, 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_profile_factory(ledger), model_profile=_T17_PROFILE,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.metrics.token_usage_status == "incomplete"
    assert result.metrics.total_tokens == 15
    assert result.metrics.efficiency_eligible is False


def test_an_agent_that_declares_no_profile_fails_before_it_runs(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)

    def factory(**kwargs):
        agent = _make_agent_factory()(**kwargs)
        del agent.model_profile
        return agent

    with pytest.raises(ValueError, match="declares no model_profile"):
        run_cell(
            suite, "devnet", "B", "m", 1,
            registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
            rpc=_rpc, agent_factory=factory,
            now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        )
    assert not list(results.glob("*.json"))


def test_an_agent_bound_to_another_profile_fails_before_it_runs(tmp_path: Path):
    root, suite, mount, vpriv, results = _setup(tmp_path)

    def factory(**kwargs):
        agent = _make_agent_factory()(**kwargs)
        agent.model_profile = _T17_PROFILE
        return agent

    with pytest.raises(ValueError, match="different model profile"):
        run_cell(
            suite, "devnet", "B", "m", 1,
            registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
            rpc=_rpc, agent_factory=factory,
            now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        )


@pytest.mark.parametrize("ledger,label", [
    (_FakeLedger(turns=0, attempts=0, responses=0, totals=None, complete=False, models=()),
     "the agent never reached a model turn"),
])
def test_a_post_agent_run_with_no_token_evidence_is_infrastructure(tmp_path: Path, ledger, label):
    """An accepted correctness row needs complete usage; nothing measured is not a score."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    graded = {"n": 0}

    import ckbbench.run.orchestrate as orch

    original = orch.verify_suite
    orch.verify_suite = lambda *a, **k: (graded.__setitem__("n", graded["n"] + 1), [])[1]
    try:
        result = run_cell(
            suite, "devnet", "B", _T17_PROFILE.requested_model, 1,
            registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
            rpc=_rpc, agent_factory=_profile_factory(ledger), model_profile=_T17_PROFILE,
            now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        )
    finally:
        orch.verify_suite = original

    assert result.outcome == "infra_fail", label
    assert result.tasks == ()
    assert graded["n"] == 0
    assert result.metrics.token_usage_status == "not_started"


def test_a_pre_agent_failure_still_keeps_an_accepted_not_started_row(tmp_path: Path):
    """The distinction is where the failure happened, not what the status string says."""
    from ckbbench.run.devnet import DevnetLifecycleError

    root, suite, mount, vpriv, results = _setup(tmp_path)
    result = run_cell(
        suite, "devnet", "B", _T17_PROFILE.requested_model, 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=_profile_factory(), model_profile=_T17_PROFILE,
        prepare_chain=lambda _c: (_ for _ in ()).throw(DevnetLifecycleError("reset failed")),
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "infra_fail"
    assert result.metrics.token_usage_status == "not_started"
    assert result.metrics.provider_attempts == 0


def test_a_harness_bug_after_a_valid_response_is_not_charged_to_the_model(tmp_path: Path):
    """A TypeError in this code must skip grading and must not appear as provider health."""
    root, suite, mount, vpriv, results = _setup(tmp_path)
    graded = {"n": 0}

    class _LedgerWithHarnessBug(_FakeLedger):
        internal_errors = 1

    import ckbbench.run.orchestrate as orch

    original = orch.verify_suite
    orch.verify_suite = lambda *a, **k: (graded.__setitem__("n", graded["n"] + 1), [])[1]
    try:
        result = run_cell(
            suite, "devnet", "B", _T17_PROFILE.requested_model, 1,
            registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
            rpc=_rpc, agent_factory=_profile_factory(_LedgerWithHarnessBug()),
            model_profile=_T17_PROFILE,
            now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        )
    finally:
        orch.verify_suite = original

    assert result.outcome == "infra_fail"
    assert graded["n"] == 0
    # The provider answered, so its own health numbers stay honest.
    assert result.metrics.token_usage_status == "complete"
    assert result.metrics.provider_attempts == 1


def test_a_model_generated_format_error_stays_ordinary_agent_behavior(tmp_path: Path):
    """The provider answered and its usage was valid; that is agent behavior, not infrastructure."""
    root, suite, mount, vpriv, results = _setup(tmp_path)

    def factory(**kwargs):
        agent = _profile_factory()(**kwargs)
        agent.exit_status = "LimitsExceeded"
        agent.write_proofs = False
        return agent

    result = run_cell(
        suite, "devnet", "B", _T17_PROFILE.requested_model, 1,
        registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
        rpc=_rpc, agent_factory=factory, model_profile=_T17_PROFILE,
        now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
    )
    assert result.outcome == "agent_fail"
    assert result.metrics.token_usage_status == "complete"


# --- the runtime identity must be the profile's probed model, checked before grading -------------

UNSAFE_MODEL = "gpt-4o sk-live-do-not-log"


def _graded_run(tmp_path: Path, ledger):
    """Run one cell with grading counted, so 'was it scored' is observed rather than inferred."""
    import ckbbench.run.orchestrate as orch

    root, suite, mount, vpriv, results = _setup(tmp_path)
    graded = {"n": 0}
    original = orch.verify_suite
    orch.verify_suite = lambda *a, **k: (graded.__setitem__("n", graded["n"] + 1), [])[1]
    try:
        result = run_cell(
            suite, "devnet", "B", _T17_PROFILE.requested_model, 1,
            registry_root=root, results_dir=results, mount_dir=mount, verifier_private_root=vpriv,
            rpc=_rpc, agent_factory=_profile_factory(ledger), model_profile=_T17_PROFILE,
            now_fn=lambda: 1_700_000_000.0, monotonic_fn=lambda: 0.0,
        )
    finally:
        orch.verify_suite = original
    return result, graded["n"], json.loads((results / f"{result.run_id}.json").read_text())


def test_an_unsafe_runtime_model_is_never_retained_or_graded(tmp_path: Path):
    """A provider-controlled identity that is not a plain identifier reaches no published surface."""
    from ckb_model import UsageLedger

    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_response(types.SimpleNamespace(
        model=UNSAFE_MODEL,
        usage=types.SimpleNamespace(prompt_tokens=30, completion_tokens=20, total_tokens=50),
    ))
    result, graded, written = _graded_run(tmp_path, ledger)

    assert result.outcome == "infra_fail"
    assert graded == 0
    assert result.model_response_id is None
    assert result.metrics.token_usage_status == "incomplete"
    # The provider did answer, so its counts stay honest even though the identity was dropped.
    assert (result.metrics.provider_attempts, result.metrics.provider_responses) == (1, 1)
    for surface in (repr(ledger.attempts), repr(ledger.last_provenance()),
                    json.dumps(written), repr(result)):
        assert "sk-live-do-not-log" not in surface


def test_a_real_failed_attempt_reaches_a_valid_artifact_as_its_category(tmp_path: Path):
    """A partial provider failure is driven through the production ledger rather than a fake.

    One turn answers, the next fails, so the run is `incomplete` with an unanswered attempt that
    must name its cause for the artifact to validate.
    """
    import ckbbench.matrix.store as store
    from ckb_model import UsageLedger

    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_response(types.SimpleNamespace(
        model=_T17_PROFILE.probed_response_model,
        usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    ))
    ledger.record_turn()
    ledger.record_failure(OSError("connection reset by peer sk-live-do-not-log"))

    result, graded, written = _graded_run(tmp_path, ledger)

    assert result.outcome == "infra_fail"
    assert graded == 0, "grading must not run for an unusable token observation"
    assert result.tasks == ()
    assert result.metrics.token_usage_status == "incomplete"
    assert (result.metrics.provider_attempts, result.metrics.provider_responses) == (2, 1)
    assert result.metrics.provider_failure_category == "connection"
    assert written["metrics"]["provider_failure_category"] == "connection"

    original = store._reviewed_profile
    store._reviewed_profile = lambda: _T17_PROFILE
    try:
        store.validate_results([written])
    finally:
        store._reviewed_profile = original

    # Only the fixed token crosses; neither the message nor the exception class does.
    published = json.dumps(written) + repr(result)
    assert "sk-live-do-not-log" not in published
    assert "OSError" not in published


def test_a_recovered_provider_attempt_is_graded_with_incomplete_usage(tmp_path: Path):
    """One failed attempt must not erase correctness after the same turn receives a response."""
    import ckbbench.matrix.store as store
    from ckb_model import UsageLedger

    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_failure(OSError("connection reset by peer sk-live-do-not-log"))
    ledger.record_retry(4)
    ledger.record_response(types.SimpleNamespace(
        model=_T17_PROFILE.probed_response_model,
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
    ))

    result, graded, written = _graded_run(tmp_path, ledger)

    assert graded == 1
    assert result.outcome == "agent_fail"
    assert result.metrics.token_usage_status == "incomplete"
    assert (result.metrics.model_calls, result.metrics.provider_attempts,
            result.metrics.provider_responses) == (1, 2, 1)
    assert result.metrics.provider_failure_category == "connection"

    original = store._reviewed_profile
    store._reviewed_profile = lambda: _T17_PROFILE
    try:
        store.validate_results([written])
    finally:
        store._reviewed_profile = original


def test_a_safe_but_different_runtime_model_skips_grading(tmp_path: Path):
    """A moving alias can resolve elsewhere; that cell is not this profile's correctness evidence."""
    result, graded, written = _graded_run(tmp_path, _FakeLedger(models=("gpt-5.5-2026-06-01",)))

    assert result.outcome == "infra_fail"
    assert graded == 0
    assert result.model_response_id == "gpt-5.5-2026-06-01"
    assert written["total_score"] == 0
    # Provider health and tokens are preserved: the endpoint did its job.
    assert result.metrics.token_usage_status == "complete"
    assert result.metrics.total_tokens == 50


def test_the_validator_diagnostic_echoes_no_provider_value(tmp_path: Path):
    """A tampered result file must not turn the validator's message into a publication channel."""
    import traceback as _tb

    from ckbbench.matrix.store import ResultsValidationError, validate_results
    from ckbbench.matrix.conftest import synthetic_profile

    _result, _graded, written = _graded_run(tmp_path, None)
    written["model_response_id"] = UNSAFE_MODEL
    written["model"] = UNSAFE_MODEL
    import ckbbench.matrix.store as store

    original = store._reviewed_profile
    store._reviewed_profile = lambda: _T17_PROFILE
    try:
        with pytest.raises(ResultsValidationError) as exc:
            validate_results([written])
    finally:
        store._reviewed_profile = original
    rendered = str(exc.value) + "".join(
        _tb.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert "sk-live-do-not-log" not in rendered
    assert synthetic_profile is not None  # the matrix fixture module stays importable here
