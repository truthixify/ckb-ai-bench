"""Agent factory tests: arm isolation, preamble wiring, submit sentinel (ADR-0008).

Encodes WHY each arm must see a different prompt surface and why the factory must pass
mcp_client through unchanged. No network, proxy, or LitellmModel validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dataclasses import replace
from jinja2 import StrictUndefined, Template

from ckbbench.config import DEVNET_GENESIS_PRIVKEY, DEVNET_RPC, TESTNET_RPC
from ckbbench.run import agent_factory as agent_factory_module
from ckbbench.run.agent_factory import (
    CARGO_NET_OFFLINE_ENV,
    DEFAULT_COST_LIMIT,
    DEFAULT_STEP_LIMIT,
    DEFAULT_WALL_TIME_LIMIT_SECONDS,
    SIGNER_ENV_NAMES,
    agent_rpc_url,
    build_system_template,
    chain_env_for,
    local_signer_sanitizer,
    make_agent_factory,
    render_mcp_tool_list,
    signer_env_for,
)
from ckbbench.run.arm import ArmConfig, resolve_arm
from ckbbench.run.model_profile import ModelProfileError, load_run_profile, parse_model_profile
from ckbbench.run.mcp_surface import (
    PROFILE_DOCS_ONLY,
    PROFILE_OFF,
    McpSurfaceError,
    McpSurfaceSetupError,
    policy_for_profile,
    profile_for_arm,
)
from ckbbench.run.treatment_surface import TreatmentSurfaceProfile, TaskMcpSurfacePolicy

# A catalog shaped like the pinned server's: the documentation tool the profile requires, plus
# chain-bound tools the docs-only surface must strip (ADR-0013).
_SAMPLE_TOOLS = [
    {"name": "search_resources", "description": "Search CKB documentation resources"},
    {"name": "search_tools", "description": "Discover deferred live tools"},
    {"name": "rpc_get_tip_block_number", "description": "Current tip height"},
    {"name": "ckb_query_address", "description": "Look up an address"},
]


@pytest.fixture(autouse=True)
def _unit_tests_use_local_agent(request, monkeypatch):
    """Unit tests must not spawn real containers when CKBBENCH_DOCKER=1 is set globally."""
    if "docker_mode" in request.node.name:
        return
    monkeypatch.delenv("CKBBENCH_DOCKER", raising=False)
    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: False)


class _FakeModel:
    def format_observation_messages(self, message, outputs, template_vars):
        return [
            {"role": "user", "content": o.get("output", ""), "extra": o.get("extra", {})}
            for o in outputs
        ]

    def format_message(self, **kwargs):
        return {"role": kwargs.get("role", "assistant"), "content": kwargs.get("content", "")}

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {}


class _FakeMcp:
    def __init__(self, tools: list[dict] | None = None, resources: list[dict] | None = None):
        self.tools = tools or []
        self.resources = resources or []
        self.initialized = False
        self.list_tools_calls = 0

    def initialize(self):
        self.initialized = True
        return {}

    def list_tools(self):
        # Record usage so a test can prove the factory does NOT call list_tools() a second time
        # (the redundant round-trip removed during review) and that it never lists before init.
        assert self.initialized, "list_tools() called before initialize()"
        self.list_tools_calls += 1
        return list(self.tools)

    def call_tool(self, tool, args):
        return {"content": [{"type": "text", "text": "ok"}]}

    def list_resources(self):
        return list(self.resources)


def _render_system(agent) -> str:
    return Template(agent.config.system_template, undefined=StrictUndefined).render(
        **agent.get_template_vars()
    )


def _make_agent(
    *,
    arm: str,
    mcp_client,
    model_builder=_FakeModel,
    chain: str = "devnet",
    signer=None,
    **factory_kwargs,
):
    factory = make_agent_factory(
        model_builder=lambda _m, _b, _k: model_builder(),
        **factory_kwargs,
    )
    return factory(
        mount_dir=Path("/tmp/mount"),
        pointer="do the task",
        arm_config=resolve_arm(arm),
        mcp_client=mcp_client,
        model="grok-test",
        suite=object(),
        chain=chain,
        signer=signer,
    )


@pytest.mark.parametrize("arm", ["C", "D"])
def test_mcp_arm_system_prompt_exposes_only_the_documentation_surface(arm):
    """BOTH MCP arms (C AND D) must offer mcp_call and the documentation tool, and nothing bound to
    a chain this run is not graded on. Parametrized over C and D so a regression on only one arm
    still fails; checking only one MCP arm would not catch a leak in another."""
    mcp = _FakeMcp(_SAMPLE_TOOLS)
    agent = _make_agent(arm=arm, mcp_client=mcp)
    rendered = _render_system(agent)

    assert "mcp_call" in agent.config.system_template
    assert "search_resources" in rendered
    for hidden in ("search_tools", "rpc_get_tip_block_number", "ckb_query_address"):
        assert hidden not in rendered


@pytest.mark.parametrize("arm", ["A", "B"])
def test_off_arm_system_prompt_has_no_mcp_surface(arm):
    """Arm isolation: any MCP leak into A OR B invalidates the C-B headline. Asserted on the RENDERED
    prompt (not just the template source) and over BOTH off arms, so a leak via a template variable
    or on only one arm still fails."""
    agent = _make_agent(arm=arm, mcp_client=None)
    rendered = _render_system(agent)

    assert "mcp_call" not in agent.config.system_template
    assert "mcp_call" not in rendered  # rendered, not only source: a var-injected leak must fail too
    assert "rpc_get_tip_block_number" not in rendered
    assert "ckb_query_address" not in rendered
    assert agent.extra_template_vars["mcp_tool_list"] == "(none)"


def test_arm_preamble_reaches_system_prompt_on_a_and_c_arms():
    """No-web instruction and MCP steering from ArmConfig must reach the model."""
    for arm in ("A", "C"):
        mcp = _FakeMcp(_SAMPLE_TOOLS) if arm == "C" else None
        agent = _make_agent(arm=arm, mcp_client=mcp)
        preamble = resolve_arm(arm).prompt_preamble
        rendered = _render_system(agent)
        assert preamble in rendered
        assert agent.extra_template_vars["arm_preamble"] == preamble


def test_system_prompt_instructs_complete_task_sentinel():
    """Only echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT yields exit_status Submitted."""
    agent = _make_agent(arm="B", mcp_client=None)
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in agent.config.system_template
    assert "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in agent.config.system_template


def test_inner_factory_accepts_run_cell_keyword_args():
    """Signature must match orchestrate.run_cell's keyword call."""
    factory = make_agent_factory(model_builder=lambda _m, _b, _k: _FakeModel())
    agent = factory(
        mount_dir=Path("/tmp/mount"),
        pointer="thin pointer",
        arm_config=resolve_arm("D"),
        mcp_client=_FakeMcp(_SAMPLE_TOOLS),
        model="claude-opus-4-8",
        suite=object(),
        chain="devnet",
    )
    assert hasattr(agent, "run")
    assert hasattr(agent, "messages")
    assert isinstance(agent.messages, list)


def test_mcp_arm_lists_tools_exactly_once_and_after_init():
    """The factory must render the tool list from the agent's own
    handshake, not a second list_tools() call. Exactly one list_tools(), after initialize(). A
    reintroduced redundant round-trip (or a list-before-init ordering) makes this fail."""
    mcp = _FakeMcp(_SAMPLE_TOOLS)
    _make_agent(arm="C", mcp_client=mcp)
    assert mcp.initialized
    assert mcp.list_tools_calls == 1


def test_mcp_client_identity_passthrough_and_off_arm_mcp_is_none():
    """run_cell owns the client lifecycle; the factory must not create a second one."""
    mcp = _FakeMcp(_SAMPLE_TOOLS)
    on_agent = _make_agent(arm="C", mcp_client=mcp)
    assert on_agent.mcp is mcp

    off_agent = _make_agent(arm="A", mcp_client=None)
    assert off_agent.mcp is None


def _agent_for(arm: str, **factory_kwargs):
    mcp = _FakeMcp(_SAMPLE_TOOLS) if arm in ("C", "D") else None
    return _make_agent(arm=arm, mcp_client=mcp, **factory_kwargs)


def test_production_step_default_is_120_and_singular():
    """One production ceiling prevents an MCP/no-MCP pair of defaults from returning."""
    assert DEFAULT_STEP_LIMIT == 120
    assert DEFAULT_COST_LIMIT == 0.0
    assert DEFAULT_WALL_TIME_LIMIT_SECONDS == 1200
    names = dir(agent_factory_module)
    assert not [n for n in names if "STEP_LIMIT" in n.upper() and n != "DEFAULT_STEP_LIMIT"]


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
def test_every_arm_receives_the_same_default_limits(arm):
    """A different budget on either side of C-B would make the headline difference ambiguous."""
    agent = _agent_for(arm)
    assert agent.config.step_limit == DEFAULT_STEP_LIMIT == 120
    assert agent.config.cost_limit == DEFAULT_COST_LIMIT == 0.0
    assert agent.config.wall_time_limit_seconds == DEFAULT_WALL_TIME_LIMIT_SECONDS == 1200


def test_all_four_arms_agree_on_one_budget_tuple():
    """The four-arm comparison itself, not four independent assertions."""
    budgets = {
        arm: (
            _agent_for(arm).config.step_limit,
            _agent_for(arm).config.cost_limit,
            _agent_for(arm).config.wall_time_limit_seconds,
        )
        for arm in ("A", "B", "C", "D")
    }
    assert len(set(budgets.values())) == 1, budgets
    assert budgets["B"] == budgets["C"] == (120, 0.0, 1200)


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
def test_explicit_step_limit_overrides_uniformly_for_all_arms(arm):
    """One programmatic override still applies to every arm, MCP or not."""
    agent = _agent_for(arm, step_limit=25)
    assert agent.config.step_limit == 25


def test_explicit_override_is_identical_across_arms():
    limits = {_agent_for(arm, step_limit=25).config.step_limit for arm in ("A", "B", "C", "D")}
    assert limits == {25}


def test_no_arm_dependent_budget_option_remains():
    """`step_limit_no_mcp` was an arm-asymmetric escape hatch; it must not be accepted again."""
    import inspect

    params = inspect.signature(make_agent_factory).parameters
    assert "step_limit" in params
    assert "step_limit_no_mcp" not in params
    with pytest.raises(TypeError):
        make_agent_factory(step_limit_no_mcp=120)


def test_mcp_availability_does_not_change_the_budget():
    """The only intended B/C treatment difference is the MCP surface, not the ceiling."""
    b = _make_agent(arm="B", mcp_client=None)
    mcp = _FakeMcp(_SAMPLE_TOOLS)
    c = _make_agent(arm="C", mcp_client=mcp)
    assert b.mcp is None
    assert c.mcp is mcp
    assert b.extra_template_vars["mcp_tool_list"] == "(none)"
    assert mcp.list_tools_calls == 1
    assert b.config.step_limit == c.config.step_limit == DEFAULT_STEP_LIMIT


def test_factory_attaches_the_controller_owned_task_sequence():
    marker = object()
    factory = make_agent_factory(model_builder=lambda _m, _b, _k: _FakeModel())
    agent = factory(
        mount_dir=Path("/tmp/mount"),
        pointer="p",
        task_sequence=marker,
        arm_config=resolve_arm("B"),
        mcp_client=None,
        model="grok-test",
        suite=object(),
        chain="devnet",
    )
    assert agent.task_sequence is marker


def test_render_mcp_tool_list_respects_max_tools_cap():
    tools = [{"name": f"tool_{i}", "description": f"desc {i}"} for i in range(5)]
    rendered = render_mcp_tool_list(tools, max_tools=2)
    assert "tool_0" in rendered
    assert "tool_1" in rendered
    assert "tool_2" not in rendered


def test_render_mcp_tool_list_exposes_all_when_max_tools_zero():
    tools = [{"name": "alpha", "description": "a"}, {"name": "beta", "description": "b"}]
    rendered = render_mcp_tool_list(tools, max_tools=0)
    assert "alpha" in rendered
    assert "beta" in rendered


def _capture_docker_env(monkeypatch, **factory_kwargs) -> dict:
    """Run the factory in docker mode and return the DockerEnvironment kwargs it built."""
    captured: dict = {}

    class _FakeDockerEnvironment:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: True)
    monkeypatch.setattr(
        "ckbbench.run.agent_factory.resolve_agent_image",
        lambda **kwargs: "custom-agent:9",
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "minisweagent.environments.docker",
        type("mod", (), {"DockerEnvironment": _FakeDockerEnvironment}),
    )
    _make_agent(arm="C", mcp_client=_FakeMcp(_SAMPLE_TOOLS), **factory_kwargs)
    return captured


def test_the_agent_attaches_to_the_scoped_network_when_one_is_exported(monkeypatch):
    """A validation invocation runs an invocation-scoped internal network.

    Hardcoding the fixed name here attaches the agent to a network that gate never created or
    proved, and leaves it unable to reach the proxy and DevNet service names it did build.
    """
    monkeypatch.setenv("CKBBENCH_DOCKER_NETWORK", "ckbbench-net-internal-scoped-probe")
    run_args = _capture_docker_env(monkeypatch)["run_args"]
    assert run_args[run_args.index("--network") + 1] == "ckbbench-net-internal-scoped-probe"


def test_the_agent_falls_back_to_the_fixed_internal_network_by_default(monkeypatch):
    """Ordinary runs keep the fixed name."""
    monkeypatch.delenv("CKBBENCH_DOCKER_NETWORK", raising=False)
    run_args = _capture_docker_env(monkeypatch)["run_args"]
    assert run_args[run_args.index("--network") + 1] == "ckbbench-net-internal"


def test_parent_supervised_agent_isolates_both_cargo_output_roots(monkeypatch):
    captured = _capture_docker_env(monkeypatch, auto_cleanup=False)
    mount = str(Path("/tmp/mount").resolve())
    assert captured["auto_cleanup"] is False
    assert captured["run_args"] == [
        "--user",
        f"{agent_factory_module.os.getuid()}:{agent_factory_module.os.getgid()}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--network",
        "ckbbench-net-internal",
        "-v",
        f"{mount}:{mount}",
        "--mount",
        f"type=volume,destination={mount}/target,volume-nocopy",
        "--mount",
        f"type=volume,destination={mount}/build,volume-nocopy",
    ]


def test_docker_mode_uses_docker_environment_with_proxy_env(monkeypatch):
    """ADR-0006: production runs must execute the agent inside docker on the internal network."""
    monkeypatch.delenv("CKBBENCH_DOCKER_NETWORK", raising=False)
    mount = Path("/tmp/mount")
    captured: dict = {}

    class _FakeDockerEnvironment:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: True)
    monkeypatch.setattr(
        "ckbbench.run.agent_factory.resolve_agent_image",
        lambda **kwargs: "custom-agent:9",
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "minisweagent.environments.docker",
        type(
            "mod",
            (),
            {"DockerEnvironment": _FakeDockerEnvironment},
        ),
    )

    agent = _make_agent(arm="C", mcp_client=_FakeMcp(_SAMPLE_TOOLS))
    assert captured["image"] == "custom-agent:9"
    assert captured["cwd"] == str(mount.resolve())
    assert captured["run_args"] == [
        "--rm",
        "--user",
        f"{agent_factory_module.os.getuid()}:{agent_factory_module.os.getgid()}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--network",
        "ckbbench-net-internal",
        "-v",
        f"{mount.resolve()}:{mount.resolve()}",
    ]
    assert captured["env"] == {
        "CKBBENCH_CHAIN_PROFILE": "devnet",
        "CKB_RPC_URL": "http://ckbbench-devnet-node:8114",
        "CKB_SENDER_PRIVKEY": DEVNET_GENESIS_PRIVKEY,
        "CKB_SDK_HOME": "/opt/ckbbench-node",
        "CARGO_NET_OFFLINE": "true",
        "HTTP_PROXY": "http://ckbbench-proxy:8888",
        "HTTPS_PROXY": "http://ckbbench-proxy:8888",
    }
    # A DevNet cell forwards no host signer: the TestNet key belongs to TestNet cells only.
    assert captured["forward_env"] == []
    assert agent is not None


def test_local_mode_uses_local_environment(monkeypatch):
    captured: dict = {}

    class _FakeLocalEnvironment:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "minisweagent.environments.local",
        type(
            "mod",
            (),
            {"LocalEnvironment": _FakeLocalEnvironment},
        ),
    )

    _make_agent(arm="B", mcp_client=None)
    assert captured == {
        "cwd": "/tmp/mount",
        "env": {
            "CKBBENCH_CHAIN_PROFILE": "devnet",
            "CKB_RPC_URL": DEVNET_RPC,
            "CKB_SENDER_PRIVKEY": DEVNET_GENESIS_PRIVKEY,
            "CARGO_NET_OFFLINE": "true",
            # blanked so an exported host TestNet key is not readable from a DevNet cell
            "CKBBENCH_TESTNET_SENDER_PRIVKEY": "",
            "BENCH_TESTNET_SENDER_PRIVKEY": "",
        },
        "timeout": 60,
    }


def _fake_docker_env(monkeypatch) -> dict:
    """Patch in a recording DockerEnvironment and force the docker seam on."""
    captured: dict = {}

    class _FakeDockerEnvironment:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: True)
    monkeypatch.setitem(
        __import__("sys").modules,
        "minisweagent.environments.docker",
        type("mod", (), {"DockerEnvironment": _FakeDockerEnvironment}),
    )
    return captured


def _fake_local_env(monkeypatch) -> dict:
    captured: dict = {}

    class _FakeLocalEnvironment:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "minisweagent.environments.local",
        type("mod", (), {"LocalEnvironment": _FakeLocalEnvironment}),
    )
    return captured


def test_docker_mode_devnet_agent_gets_the_sidecar_service_url(monkeypatch):
    """A docker agent sits on ckbbench-net-internal: 127.0.0.1 would reach nothing, so it must be
    handed the sidecar's SERVICE name. Without this an A/B agent has to guess it (plan §8.1)."""
    captured = _fake_docker_env(monkeypatch)
    _make_agent(arm="B", mcp_client=None, chain="devnet")
    assert captured["env"]["CKBBENCH_CHAIN_PROFILE"] == "devnet"
    assert captured["env"]["CKB_RPC_URL"] == "http://ckbbench-devnet-node:8114"


@pytest.mark.parametrize(
    "configured",
    [
        "http://10.0.0.9:18114",
        # The audit's own remedy for curl's uppercase-HTTP_PROXY behaviour is an HTTPS endpoint,
        # so the scheme must survive; a request target that is not the server root must too.
        "https://testnet.ckb.dev/rpc",
        "https://node.example:8443/ckb/rpc?v=2",
    ],
)
def test_docker_mode_testnet_agent_gets_the_whole_configured_url(monkeypatch, configured):
    """TestNet cells must carry the operator's configured endpoint COMPLETE. Reducing it to a
    host (the allowlist's job) would send the agent to port 80 at the server root."""
    monkeypatch.setattr("ckbbench.config.TESTNET_RPC", configured)
    captured = _fake_docker_env(monkeypatch)
    _make_agent(arm="C", mcp_client=_FakeMcp(_SAMPLE_TOOLS), chain="testnet")
    assert captured["env"]["CKBBENCH_CHAIN_PROFILE"] == "testnet"
    assert captured["env"]["CKB_RPC_URL"] == configured


def test_docker_testnet_url_is_not_the_allowlist_host_form(monkeypatch):
    """Regression guard: internal_rpc_for() exists to feed the egress allowlist and deliberately
    drops everything but host and port. Wiring it into CKB_RPC_URL silently downgraded HTTPS."""
    from ckbbench.run.defaults import internal_rpc_for

    monkeypatch.setattr("ckbbench.config.TESTNET_RPC", "https://testnet.ckb.dev/rpc")
    monkeypatch.setattr("ckbbench.run.defaults.TESTNET_RPC", "https://testnet.ckb.dev/rpc")
    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: True)

    assert internal_rpc_for("testnet") == "http://testnet.ckb.dev"  # allowlist form, unchanged
    assert agent_rpc_url("testnet") == "https://testnet.ckb.dev/rpc"  # executable form


@pytest.mark.parametrize(("chain", "expected"), [("devnet", DEVNET_RPC), ("testnet", TESTNET_RPC)])
def test_local_agent_gets_the_host_side_url(monkeypatch, chain, expected):
    """A local agent shares the harness host's namespace, so it gets the same host URL the
    verifier resolves -- not the docker service name."""
    captured = _fake_local_env(monkeypatch)
    _make_agent(arm="A", mcp_client=None, chain=chain)
    assert captured["env"]["CKBBENCH_CHAIN_PROFILE"] == chain
    assert captured["env"]["CKB_RPC_URL"] == expected


@pytest.mark.parametrize("docker_mode", [True, False])
def test_local_hermetic_agent_gets_no_rpc_or_signer(monkeypatch, docker_mode):
    captured = _fake_docker_env(monkeypatch) if docker_mode else _fake_local_env(monkeypatch)
    for name in SIGNER_ENV_NAMES:
        monkeypatch.setenv(name, "stale-value")
    monkeypatch.setenv("CKB_RPC_URL", "http://stale.example")

    _make_agent(arm="B", mcp_client=None, chain="local-hermetic")

    assert captured["env"]["CKBBENCH_CHAIN_PROFILE"] == "local-hermetic"
    assert captured["env"]["CKB_RPC_URL"] == ""
    assert all(captured["env"].get(name, "") == "" for name in SIGNER_ENV_NAMES)


@pytest.mark.parametrize("docker_mode", [True, False])
def test_agent_cargo_resolution_is_offline_before_grading(monkeypatch, docker_mode):
    """The live workspace and offline build stage must not see different crate universes."""
    captured = _fake_docker_env(monkeypatch) if docker_mode else _fake_local_env(monkeypatch)
    _make_agent(arm="B", mcp_client=None)
    assert captured["env"][CARGO_NET_OFFLINE_ENV] == "true"


def test_local_agent_cell_values_beat_stale_host_environment(monkeypatch, tmp_path):
    """A leftover host CKB_RPC_URL must never outrank the cell's chain. Proven by executing a real
    command through LocalEnvironment rather than by reading the config back."""
    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: False)
    monkeypatch.setenv("CKB_RPC_URL", "http://stale-host-value:9999")
    monkeypatch.setenv("CKBBENCH_CHAIN_PROFILE", "testnet")

    factory = make_agent_factory(model_builder=lambda _m, _b, _k: _FakeModel())
    agent = factory(
        mount_dir=tmp_path,
        pointer="p",
        arm_config=resolve_arm("B"),
        mcp_client=None,
        model="grok-test",
        suite=object(),
        chain="devnet",
    )
    out = agent.env.execute({"command": 'printf "%s %s" "$CKBBENCH_CHAIN_PROFILE" "$CKB_RPC_URL"'})
    assert out["output"].strip() == f"devnet {DEVNET_RPC}"


def test_all_four_arms_get_identical_chain_context_for_one_cell(monkeypatch):
    """B and C may differ only by MCP access: a chain difference between arms would confound
    C - B directly."""
    seen = {}
    for arm in ("A", "B", "C", "D"):
        captured = _fake_docker_env(monkeypatch)
        mcp = _FakeMcp(_SAMPLE_TOOLS) if arm in ("C", "D") else None
        _make_agent(arm=arm, mcp_client=mcp, chain="devnet")
        seen[arm] = {
            "CKBBENCH_CHAIN_PROFILE": captured["env"]["CKBBENCH_CHAIN_PROFILE"],
            "CKB_RPC_URL": captured["env"]["CKB_RPC_URL"],
        }
    assert len(set(tuple(sorted(v.items())) for v in seen.values())) == 1, seen


def test_unknown_chain_fails_explicitly_instead_of_defaulting():
    """A typo must abort the cell, not silently benchmark against the wrong chain."""
    with pytest.raises(ValueError, match="unknown chain profile"):
        chain_env_for("mainnet")
    with pytest.raises(ValueError, match="unknown chain profile"):
        agent_rpc_url("")


@pytest.mark.parametrize("arm", ["C", "D"])
def test_mcp_steering_names_no_chain_and_sends_chain_work_to_direct_rpc(arm):
    """The steering line used to say "CKB/testnet", handing C/D a chain fact A/B never saw -- and
    the wrong one on a DevNet cell. It now points chain work at the selected endpoint instead."""
    rendered = _render_system(_make_agent(arm=arm, mcp_client=_FakeMcp(_SAMPLE_TOOLS)))
    assert "mcp_call" in rendered  # the MCP surface itself is untouched
    assert "CKB_RPC_URL" in rendered
    assert "documentation and reference lookup" in rendered
    assert "FALLBACK_RPC" not in rendered
    for absent in ("testnet", "mainnet", "faucet"):
        assert absent not in rendered.lower()


@pytest.mark.parametrize("arm", ["A", "B"])
def test_off_arms_learn_the_chain_without_learning_about_mcp(arm):
    """Chain context must not become a side channel for MCP vocabulary or the endpoint."""
    captured_env = chain_env_for("devnet")
    rendered = _render_system(_make_agent(arm=arm, mcp_client=None, chain="devnet"))
    assert captured_env["CKBBENCH_CHAIN_PROFILE"] == "devnet"
    assert "mcp_call" not in rendered
    assert "mcp.ckbdev.com" not in rendered
    assert "rpc_get_tip_block_number" not in rendered


_STALE_HOST_KEY = "0xstale-host-value-that-must-lose"  # sentinel, never a real key


@pytest.mark.parametrize("mode", ["docker_mode", "local"])
def test_devnet_cell_selects_the_public_development_signer(monkeypatch, mode):
    """DevNet uses the public genesis fixture selected by the cell's chain identity."""
    captured = _fake_docker_env(monkeypatch) if mode == "docker_mode" else _fake_local_env(monkeypatch)
    _make_agent(arm="B", mcp_client=None, chain="devnet")
    assert captured["env"]["CKB_SENDER_PRIVKEY"] == DEVNET_GENESIS_PRIVKEY


def test_testnet_cell_gets_no_injected_signer_and_keeps_the_operator_contract(monkeypatch):
    """TestNet keeps its existing contract: the operator's key is forwarded from the host under its
    existing names, never replaced by a DevNet fixture."""
    captured = _fake_docker_env(monkeypatch)
    _make_agent(arm="B", mcp_client=None, chain="testnet")
    assert "CKB_SENDER_PRIVKEY" not in captured["env"]
    assert captured["forward_env"] == [
        "CKBBENCH_TESTNET_SENDER_PRIVKEY",
        "BENCH_TESTNET_SENDER_PRIVKEY",
    ]


def test_a_broker_bound_testnet_agent_receives_no_raw_signing_material(monkeypatch):
    class Broker:
        def sign_and_submit(self, request):
            raise AssertionError(request)

    broker = Broker()
    captured = _fake_docker_env(monkeypatch)
    agent = _make_agent(
        arm="B",
        mcp_client=None,
        chain="testnet",
        signer=broker,
    )

    assert agent.signer is broker
    assert captured["forward_env"] == []
    assert {name: captured["env"][name] for name in SIGNER_ENV_NAMES} == {
        name: "" for name in SIGNER_ENV_NAMES
    }
    assert "ckb_sign_and_submit" in agent.config.system_template


def test_a_broker_bound_attempt_refuses_the_host_local_agent_environment(monkeypatch):
    class Broker:
        def sign_and_submit(self, request):
            raise AssertionError(request)

    _fake_local_env(monkeypatch)
    with pytest.raises(ValueError, match="isolated agent environment"):
        _make_agent(
            arm="C",
            mcp_client=_FakeMcp(_SAMPLE_TOOLS),
            chain="testnet",
            signer=Broker(),
        )


def test_a_bound_treatment_policy_controls_agent_discovery_and_dispatch():
    tools = [
        {
            "name": "search_resources",
            "description": "Search task resources",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rpc_get_tip_block_number",
            "description": "Controller-only chain read",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    resources = [{"uri": "ckb://docs/reference", "name": "Reference"}]
    profile = TreatmentSurfaceProfile.from_catalogs(
        profile_id="task-resources-v1",
        server_name="ckb-ai-mcp",
        server_version="1.7.0",
        claims_live_chain=False,
        allowed_tools=("search_resources",),
        allowed_resource_prefixes=("ckb://docs/",),
        tools=tools,
        resources=resources,
    )
    policy = TaskMcpSurfacePolicy(profile)
    treated = _make_agent(
        arm="C",
        mcp_client=_FakeMcp(tools, resources),
        treatment_surface=policy,
    )
    control = _make_agent(
        arm="B",
        mcp_client=None,
        treatment_surface=policy,
    )

    assert treated.mcp_surface is policy
    assert [row["name"] for row in treated.mcp_tools] == ["search_resources"]
    assert "search_resources" in _render_system(treated)
    assert control.mcp is None
    assert control.mcp_surface is policy
    assert "mcp_call" not in _render_system(control)


def test_devnet_cell_does_not_forward_the_testnet_signer(monkeypatch):
    """A live-chain key must not ride along into a DevNet cell that has no use for it."""
    captured = _fake_docker_env(monkeypatch)
    _make_agent(arm="B", mcp_client=None, chain="devnet")
    assert captured["forward_env"] == []
    assert not any("TESTNET" in name for name in captured["env"])


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
def test_all_arms_get_the_same_devnet_signing_capability(monkeypatch, arm):
    """Both treatment arms receive identical signing capability."""
    captured = _fake_docker_env(monkeypatch)
    mcp = _FakeMcp(_SAMPLE_TOOLS) if arm in ("C", "D") else None
    _make_agent(arm=arm, mcp_client=mcp, chain="devnet")
    assert captured["env"]["CKB_SENDER_PRIVKEY"] == DEVNET_GENESIS_PRIVKEY
    assert captured["env"]["CKB_SDK_HOME"] == "/opt/ckbbench-node"


def test_devnet_signer_beats_a_stale_host_value(monkeypatch, tmp_path):
    """Proven by execution, not by reading the config back: the host value must lose. The shell
    compares against the sentinel so no key value is ever printed."""
    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: False)
    monkeypatch.setenv("CKB_SENDER_PRIVKEY", _STALE_HOST_KEY)

    factory = make_agent_factory(model_builder=lambda _m, _b, _k: _FakeModel())
    agent = factory(
        mount_dir=tmp_path, pointer="p", arm_config=resolve_arm("B"), mcp_client=None,
        model="grok-test", suite=object(), chain="devnet",
    )
    out = agent.env.execute(
        {"command": f'case "$CKB_SENDER_PRIVKEY" in "{_STALE_HOST_KEY}") echo HOST;; '
                    '"") echo UNSET;; *) echo CELL;; esac'}
    )
    assert out["output"].strip() == "CELL"


def test_unknown_chain_has_no_signer_fallback():
    with pytest.raises(ValueError, match="unknown chain profile"):
        signer_env_for("mainnet")


def _visible_signer_names(agent) -> dict[str, str]:
    """Ask the agent's own shell which signer names carry a value (sentinels only)."""
    probe = "; ".join(
        f'if [ -n "${name}" ]; then echo "{name}=VISIBLE"; else echo "{name}=absent"; fi'
        for name in SIGNER_ENV_NAMES
    )
    out = agent.env.execute({"command": probe})
    return dict(line.split("=", 1) for line in out["output"].strip().splitlines())


def _local_agent(tmp_path, chain: str):
    factory = make_agent_factory(model_builder=lambda _m, _b, _k: _FakeModel())
    return factory(
        mount_dir=tmp_path, pointer="p", arm_config=resolve_arm("B"), mcp_client=None,
        model="grok-test", suite=object(), chain=chain,
    )


def test_local_devnet_cell_cannot_read_a_host_testnet_key(monkeypatch, tmp_path):
    """A local agent executes with os.environ | config.env, so an operator's exported TestNet key
    was readable from every DevNet cell. Sentinels only -- a real key is never used in a test."""
    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: False)
    monkeypatch.setenv("CKBBENCH_TESTNET_SENDER_PRIVKEY", "SENTINEL_TESTNET")
    monkeypatch.setenv("BENCH_TESTNET_SENDER_PRIVKEY", "SENTINEL_LEGACY")

    visible = _visible_signer_names(_local_agent(tmp_path, "devnet"))

    assert visible["CKBBENCH_TESTNET_SENDER_PRIVKEY"] == "absent"
    assert visible["BENCH_TESTNET_SENDER_PRIVKEY"] == "absent"
    assert visible["CKB_SENDER_PRIVKEY"] == "VISIBLE"  # the cell's own DevNet fixture


def test_local_testnet_cell_cannot_read_a_stale_generic_signer(monkeypatch, tmp_path):
    """The reverse direction: a leftover generic key must not become a TestNet cell's signer,
    while the operator's own TestNet variables stay readable as the existing contract requires."""
    monkeypatch.setattr("ckbbench.run.agent_factory.use_docker", lambda: False)
    monkeypatch.setenv("CKB_SENDER_PRIVKEY", "SENTINEL_STALE_GENERIC")
    monkeypatch.setenv("CKBBENCH_TESTNET_SENDER_PRIVKEY", "SENTINEL_TESTNET")
    monkeypatch.setenv("BENCH_TESTNET_SENDER_PRIVKEY", "SENTINEL_LEGACY")

    visible = _visible_signer_names(_local_agent(tmp_path, "testnet"))

    assert visible["CKB_SENDER_PRIVKEY"] == "absent"
    assert visible["CKBBENCH_TESTNET_SENDER_PRIVKEY"] == "VISIBLE"
    assert visible["BENCH_TESTNET_SENDER_PRIVKEY"] == "VISIBLE"


@pytest.mark.parametrize(
    ("chain", "blanked"),
    [
        ("devnet", {"CKBBENCH_TESTNET_SENDER_PRIVKEY", "BENCH_TESTNET_SENDER_PRIVKEY"}),
        ("testnet", {"CKB_SENDER_PRIVKEY"}),
    ],
)
def test_local_sanitizer_blanks_exactly_the_foreign_signer_names(chain, blanked):
    sanitized = local_signer_sanitizer(chain)
    assert set(sanitized) == blanked
    assert all(value == "" for value in sanitized.values())


def test_signer_value_never_reaches_a_rendered_prompt(monkeypatch):
    """A key in the system prompt would land in every model transcript and token count."""
    for arm in ("A", "B", "C", "D"):
        mcp = _FakeMcp(_SAMPLE_TOOLS) if arm in ("C", "D") else None
        rendered = _render_system(_make_agent(arm=arm, mcp_client=mcp, chain="devnet"))
        assert DEVNET_GENESIS_PRIVKEY not in rendered
        assert "CKB_SENDER_PRIVKEY" not in rendered  # the composed instructions name it, not this


def test_default_model_builder_uses_proxy_provider_prefix_and_no_secret(monkeypatch):
    """The default builder must point litellm at the local proxy with the openai/ provider prefix,
    a deterministic temperature, and the no-auth key. WHY: a wrong prefix or a leaked/real api_key
    would either fail to reach the proxy or pull a secret into the run; both must be caught."""
    import minisweagent.models.litellm_model as lm

    from ckbbench.run.agent_factory import _default_model_builder

    captured: dict = {}

    class _RecordingLitellm:
        def __init__(self, *, model_name, model_kwargs, cost_tracking):
            captured["model_name"] = model_name
            captured["model_kwargs"] = model_kwargs
            captured["cost_tracking"] = cost_tracking

    monkeypatch.setattr(lm, "LitellmModel", _RecordingLitellm)

    _default_model_builder("claude-opus-4-8", "http://localhost:18321/v1", "sk-noauth")

    assert captured["model_name"] == "openai/claude-opus-4-8"
    assert captured["model_kwargs"]["api_base"] == "http://localhost:18321/v1"
    assert captured["model_kwargs"]["api_key"] == "sk-noauth"  # passed through; no real secret, ever
    assert captured["model_kwargs"]["temperature"] == 0  # deterministic runs
    assert captured["model_kwargs"]["drop_params"] is True
    assert captured["cost_tracking"] == "ignore_errors"



def test_mcp_prompt_documents_the_reserved_resource_action():
    template = build_system_template(mcp_enabled=True)
    assert "mcp_call <tool_name> <json-args>" in template
    assert 'mcp_call resources/read {"uri": "<resource-uri>"}' in template
    assert "search_resources" in template, "the model needs a discovery path for URIs"
    assert "when the task uses a live chain" in template


def test_task_signer_prompt_is_explicit_and_arm_neutral():
    off = build_system_template(mcp_enabled=False, signer_enabled=True)
    treated = build_system_template(mcp_enabled=True, signer_enabled=True)
    for template in (off, treated):
        assert "ckb_sign_and_submit <json-request>" in template
        assert "SIGNING_POLICY.json" in template
        assert "request_format.unsigned_transaction_template" in template
        assert "canonical 0x hexadecimal" in template
        assert "outputs_data value per output" in template
        assert "own_lock as change" in template
        assert "required_type_id_output" in template
        assert "ends the attempt" in template
        assert "No private key" in template
        assert "owns the private" in template
        assert "task policy" in template
    assert "ckb_sign_and_submit" not in build_system_template(mcp_enabled=True)


@pytest.mark.parametrize(
    "forbidden",
    ["mcp_call", "resources/read", "search_resources", "ckb://", "mcp.ckbdev.com"],
)
def test_no_mcp_prompt_exposes_no_product_vocabulary(forbidden):
    """A/B must not learn the product action or endpoint from the prompt."""
    assert forbidden not in build_system_template(mcp_enabled=False)


@pytest.mark.parametrize("mcp_enabled", [True, False])
def test_no_endpoint_or_answer_is_baked_into_either_prompt(mcp_enabled):
    template = build_system_template(mcp_enabled=mcp_enabled)
    assert "mcp.ckbdev.com" not in template
    assert "5e7a36a7" not in template, "the sUDT oracle must never appear in a prompt"


@pytest.mark.parametrize("mcp_enabled", [True, False])
def test_arm_preamble_and_submit_sentinel_survive(mcp_enabled):
    template = build_system_template(mcp_enabled=mcp_enabled)
    assert "{{arm_preamble}}" in template
    assert "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in template


@pytest.mark.parametrize("mcp_enabled", [True, False])
def test_every_arm_is_told_about_the_offline_cargo_contract(mcp_enabled):
    template = build_system_template(mcp_enabled=mcp_enabled)
    assert "Cargo dependency resolution is offline" in template
    assert "Build and test Rust work in this workspace before submitting it." in template


# --- fixed MCP surface per arm (ADR-0013) --------------------------------------------------------

def test_off_arms_carry_the_off_profile_and_no_mcp_surface():
    for arm in ("A", "B"):
        agent = _make_agent(arm=arm, mcp_client=None)
        assert agent.mcp_surface_profile == PROFILE_OFF
        assert agent.mcp is None
        assert agent.mcp_tools == []
        rendered = _render_system(agent)
        assert "mcp_call" not in rendered
        assert "search_resources" not in rendered


def test_mcp_arms_carry_the_docs_only_profile_and_only_its_tool():
    for arm in ("C", "D"):
        mcp = _FakeMcp(_SAMPLE_TOOLS)
        agent = _make_agent(arm=arm, mcp_client=mcp)
        assert agent.mcp_surface_profile == PROFILE_DOCS_ONLY
        assert [t["name"] for t in agent.mcp_tools] == ["search_resources"]
        assert agent.mcp_surface is policy_for_profile(PROFILE_DOCS_ONLY)


def test_every_arm_gets_the_profile_the_ladder_fixes():
    built = {}
    for arm in ("A", "B", "C", "D"):
        mcp = _FakeMcp(_SAMPLE_TOOLS) if arm in ("C", "D") else None
        built[arm] = _make_agent(arm=arm, mcp_client=mcp).mcp_surface_profile
    assert built == {a: profile_for_arm(a) for a in ("A", "B", "C", "D")}
    assert built == {"A": "off", "B": "off", "C": "docs-only-v1", "D": "docs-only-v1"}


def test_c_and_d_share_one_policy_object():
    """One source of truth, so the visible catalog and the dispatch guard cannot diverge."""
    c = _make_agent(arm="C", mcp_client=_FakeMcp(_SAMPLE_TOOLS))
    d = _make_agent(arm="D", mcp_client=_FakeMcp(_SAMPLE_TOOLS))
    assert c.mcp_surface is d.mcp_surface


@pytest.mark.parametrize("arm", ["C", "D"])
def test_mcp_prompt_carries_no_wrong_chain_or_account_steer(arm):
    rendered = _render_system(_make_agent(arm=arm, mcp_client=_FakeMcp(_SAMPLE_TOOLS)))
    lowered = rendered.lower()
    assert "task-scoped ckb ai surface" in lowered
    assert "ckb_rpc_url" in lowered
    for banned in ("testnet", "mainnet", "faucet", "search_tools", "rpc_get_tip_block_number"):
        assert banned not in lowered


def test_a_missing_documentation_tool_fails_construction_so_no_agent_runs():
    """A server that stopped advertising the surface must not silently run an empty treatment.

    The failure is at construction, so `run()` is never reached and no model turn is spent.
    """
    mcp = _FakeMcp([{"name": "rpc_get_tip_block_number", "description": "tip"}])
    with pytest.raises(McpSurfaceError, match="search_resources"):
        _make_agent(arm="C", mcp_client=mcp)
    assert mcp.list_tools_calls == 1


def test_the_agent_image_still_does_not_carry_the_host_controller():
    """C/D reach MCP through the harness controller, never a copy inside the agent image."""
    dockerfile = (Path(__file__).resolve().parents[2] / "containers" / "agent.Dockerfile").read_text()
    for host_only in ("ckb_mcp", "ckb_agent", "mcp_surface"):
        assert host_only not in dockerfile


def test_building_the_policy_and_prompt_performs_no_external_action(monkeypatch):
    """Surface resolution and prompt construction are pure: no socket, subprocess, or client."""
    import socket
    import subprocess

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("prompt or policy construction performed an external action")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(socket.socket, "connect", explode)

    for arm in ("A", "B", "C", "D"):
        mcp = _FakeMcp(_SAMPLE_TOOLS) if arm in ("C", "D") else None
        agent = _make_agent(arm=arm, mcp_client=mcp)
        assert agent.mcp_surface_profile == profile_for_arm(arm)
        assert _render_system(agent)


# --- the MCP setup boundary ----------------------------------------------------------------------

class _FailingMcp(_FakeMcp):
    """Fails at exactly one handshake step, like an unreachable or drifted server."""

    def __init__(self, *, init_error=None, list_error=None, tools=None):
        super().__init__(tools if tools is not None else _SAMPLE_TOOLS)
        self._init_error = init_error
        self._list_error = list_error

    def initialize(self):
        if self._init_error is not None:
            raise self._init_error
        return super().initialize()

    def list_tools(self):
        if self._list_error is not None:
            raise self._list_error
        return super().list_tools()


class _RecordingEnv:
    def __init__(self):
        self.cleanups = 0
        self.container_id = None

    def cleanup(self):
        self.cleanups += 1


@pytest.mark.parametrize("mcp,label", [
    (_FailingMcp(init_error=OSError("transport")), "initialize transport failure"),
    (_FailingMcp(list_error=OSError("transport")), "tools/list transport failure"),
    (_FailingMcp(list_error=ValueError("protocol")), "tools/list protocol failure"),
    (_FailingMcp(tools=[{"name": "rpc_get_tip_block_number", "description": "tip"}]),
     "the required tool disappeared"),
    (_FailingMcp(tools=[{"name": []}]), "the catalog became malformed"),
])
def test_a_failed_handshake_becomes_a_typed_setup_error(mcp, label):
    """A raw exception here would abort the matrix instead of yielding a pre-agent infra_fail."""
    with pytest.raises(McpSurfaceSetupError):
        _make_agent(arm="C", mcp_client=mcp)


def test_the_setup_error_carries_no_transport_detail():
    mcp = _FailingMcp(init_error=OSError("https://user:tok-abc123@mcp.example raw-body"))
    with pytest.raises(McpSurfaceSetupError) as exc:
        _make_agent(arm="C", mcp_client=mcp)
    for canary in ("tok-abc123", "raw-body", "mcp.example"):
        assert canary not in str(exc.value)
    assert "OSError" in str(exc.value)


def test_an_environment_created_before_a_failed_handshake_is_released_exactly_once(monkeypatch):
    """DockerEnvironment starts its container in __init__, and no agent reaches run_cell's
    cleanup, so the factory must release it rather than leave it to a destructor."""
    envs: list[_RecordingEnv] = []

    def fake_local_env(**_kwargs):
        env = _RecordingEnv()
        envs.append(env)
        return env

    import ckbbench.run.agent_factory as factory_mod

    monkeypatch.setattr(factory_mod, "cleanup_agent", lambda agent: agent.env.cleanup())
    with monkeypatch.context() as ctx:
        import minisweagent.environments.local as local_mod

        ctx.setattr(local_mod, "LocalEnvironment", fake_local_env)
        with pytest.raises(McpSurfaceSetupError):
            _make_agent(arm="C", mcp_client=_FailingMcp(init_error=OSError("transport")))

    assert len(envs) == 1
    assert envs[0].cleanups == 1


def test_a_successful_construction_releases_nothing():
    released = {"n": 0}
    import ckbbench.run.agent_factory as factory_mod

    original = factory_mod.cleanup_agent
    factory_mod.cleanup_agent = lambda agent: released.__setitem__("n", released["n"] + 1)
    try:
        agent = _make_agent(arm="C", mcp_client=_FakeMcp(_SAMPLE_TOOLS))
    finally:
        factory_mod.cleanup_agent = original
    assert released["n"] == 0
    assert agent.mcp_surface_profile == "docs-only-v1"


def test_no_handshake_canary_survives_into_the_formatted_traceback():
    """The direct typed setup boundary must fail closed like preflight, cause included."""
    import traceback

    unsafe = OSError(
        "raw-server-body sk-live-do-not-log https://user:tok-abc123@mcp.example/ckbai"
    )
    with pytest.raises(McpSurfaceSetupError) as exc:
        _make_agent(arm="C", mcp_client=_FailingMcp(init_error=unsafe))
    rendered = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    for canary in ("raw-server-body", "sk-live-do-not-log", "tok-abc123", "mcp.example"):
        assert canary not in str(exc.value)
        assert canary not in rendered
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True
    assert "OSError" in str(exc.value)


def test_the_forks_own_setup_error_also_suppresses_its_cause():
    """Asserted at the fork boundary too: the factory's suppression must not be the only guard."""
    import traceback

    from ckb_agent import CkbMcpAgent, McpSetupError

    unsafe = OSError("raw-server-body https://user:tok-abc123@mcp.example/ckbai")

    class _Env:
        def get_template_vars(self):
            return {}

        def serialize(self):
            return {}

    with pytest.raises(McpSetupError) as exc:
        CkbMcpAgent(
            _FakeModel(), _Env(), mcp=_FailingMcp(init_error=unsafe),
            surface=policy_for_profile(PROFILE_DOCS_ONLY),
            system_template="x", instance_template="x",
        )
    rendered = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    for canary in ("raw-server-body", "tok-abc123", "mcp.example"):
        assert canary not in rendered
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True


# --- the reviewed model profile (ADR-0014) --------------------------------------------------------

_PROFILE_DOC = {
    "api_base": "https://proxy.example/v1",
    "api_style": "openai-responses",
    "credential_env": "CKBBENCH_LLM_API_KEY",
    "drop_unsupported_params": True,
    "evidence_utc": "2026-08-15T09:30:00Z",
    "litellm_num_retries": 0,
    "max_agent_query_attempts": 4,
    "model_stability": "moving_alias",
    "probed_response_model": "google/gemini-3.7-flash",
    "observation_max_bytes": 32768,
    "profile_id": "model-profile-gemini-3-7-flash-v1",
    "qualification_source": {
        "evidence_sha256": "b" * 64,
        "kind": "schema-8-semantic-migration-v1",
        "profile_sha256": "a" * 64,
    },
    "request_body_extensions": {
        "provider": {
            "allow_fallbacks": False,
            "order": ["google-vertex/global"],
            "require_parameters": True,
        }
    },
    "provider_request_timeout_seconds": 300,
    "provider_retry_backoff_seconds": [4, 8, 16],
    "reasoning_context": "prefix_tail_groups",
    "reasoning_effort": "high",
    "replay_max_bytes": 131072,
    "replay_policy": "prefix-tail-groups-v1",
    "store": False,
    "requested_model": "google/gemini-3.7-flash",
    "retryable_provider_failure_categories": [
        "rate_limit", "timeout", "connection", "server", "protocol", "other_provider",
    ],
    "schema_version": "9",
    "temperature": None,
    "truncation": "disabled",
    "usage_contract": "openai-responses-usage-v1",
}


def _profile(**overrides):
    return parse_model_profile({**_PROFILE_DOC, **overrides}, sha256="c" * 64)


class _CapturingModel:
    """Records exactly what the factory asked the provider client to be built with."""

    built: list[dict] = []

    def __init__(self, **kwargs):
        _CapturingModel.built.append(kwargs)
        self.config = type("C", (), {"max_query_attempts": kwargs.get("max_query_attempts")})()
        self.usage_ledger = None

    def get_template_vars(self):
        return {}


@pytest.fixture(autouse=True)
def _reset_captured_models():
    _CapturingModel.built = []
    yield
    _CapturingModel.built = []


def _profile_agent(arm, profile, monkeypatch, **factory_kwargs):
    import ckbbench.run.agent_factory as factory_mod

    monkeypatch.setattr(factory_mod, "_profile_model_builder",
                        lambda p, api_key: _CapturingModel(
                            model_name=p.litellm_model_name,
                            model_kwargs=p.model_kwargs(),
                            max_query_attempts=p.max_agent_query_attempts,
                            retry_backoff_seconds=p.provider_retry_backoff_seconds,
                            retryable_failure_categories=p.retryable_provider_failure_categories,
                            api_key=api_key,
                        ))
    factory = make_agent_factory(profile=profile, **factory_kwargs)
    mcp = _FakeMcp(_SAMPLE_TOOLS) if arm in ("C", "D") else None
    return factory(
        mount_dir=Path("/tmp/mount"), pointer="p", arm_config=resolve_arm(arm),
        mcp_client=mcp, model=profile.requested_model, suite=object(), chain="devnet",
    )


def test_a_bound_profile_preserves_litellm_and_openrouter_namespaces(monkeypatch):
    profile = _profile()
    _profile_agent("B", profile, monkeypatch)
    built = _CapturingModel.built[-1]
    assert built["model_name"] == "openai/google/gemini-3.7-flash"


def test_the_reviewed_settings_reach_the_provider_client(monkeypatch):
    _profile_agent("B", _profile(), monkeypatch)
    kwargs = _CapturingModel.built[-1]["model_kwargs"]
    assert "temperature" not in kwargs
    assert kwargs["drop_params"] is True
    assert kwargs["num_retries"] == 0
    assert kwargs["timeout"] == 300
    assert kwargs["store"] is False
    assert kwargs["api_base"] == "https://proxy.example/v1"
    assert kwargs["reasoning"] == {"effort": "high"}
    assert kwargs["extra_body"] == {
        "provider": {
            "order": ["google-vertex/global"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }
    assert "api_key" not in kwargs, "the key must not travel in the rendered config"
    assert _CapturingModel.built[-1]["max_query_attempts"] == 4
    assert _CapturingModel.built[-1]["retry_backoff_seconds"] == (4, 8, 16)
    assert _CapturingModel.built[-1]["retryable_failure_categories"] == (
        "rate_limit", "timeout", "connection", "server", "protocol", "other_provider",
    )


def test_b_and_c_receive_the_same_immutable_profile_object(monkeypatch):
    profile = _profile()
    b = _profile_agent("B", profile, monkeypatch)
    c = _profile_agent("C", profile, monkeypatch)
    assert b.model_profile is profile and c.model_profile is profile
    assert _CapturingModel.built[0]["model_kwargs"] == _CapturingModel.built[1]["model_kwargs"]
    assert b.config.step_limit == c.config.step_limit == DEFAULT_STEP_LIMIT


def test_a_cell_cannot_request_a_model_the_profile_does_not_name(monkeypatch):
    import ckbbench.run.agent_factory as factory_mod

    monkeypatch.setattr(factory_mod, "_profile_model_builder",
                        lambda p, api_key: _CapturingModel())
    factory = make_agent_factory(profile=_profile())
    with pytest.raises(ModelProfileError, match="cannot request"):
        factory(
            mount_dir=Path("/tmp/mount"), pointer="p", arm_config=resolve_arm("B"),
            mcp_client=None, model="some-other-model", suite=object(), chain="devnet",
        )
    assert _CapturingModel.built == []


@pytest.mark.parametrize("name", ["CKBBENCH_LLM_API_BASE", "BENCH_API_BASE"])
def test_a_conflicting_exported_endpoint_cannot_retarget_a_profile(monkeypatch, name):
    monkeypatch.setenv(name, "https://elsewhere.example/v1")
    _profile_agent("B", _profile(), monkeypatch)
    assert _CapturingModel.built[-1]["model_kwargs"]["api_base"] == "https://proxy.example/v1"


@pytest.mark.parametrize("name", ["CKBBENCH_LLM_API_BASE", "BENCH_API_BASE"])
def test_a_matching_exported_endpoint_is_accepted(monkeypatch, name):
    monkeypatch.setenv(name, "https://proxy.example/v1/")
    agent = _profile_agent("B", _profile(), monkeypatch)
    assert agent.model_profile.api_base == "https://proxy.example/v1"


def test_a_development_factory_carries_no_profile():
    agent = _make_agent(arm="B", mcp_client=None)
    assert agent.model_profile is None


def test_binding_a_profile_changes_nothing_else(monkeypatch):
    """Budgets, prompts, chain context, signer env and the MCP surface are untouched."""
    profile = _profile()
    plain = _make_agent(arm="C", mcp_client=_FakeMcp(_SAMPLE_TOOLS))
    bound = _profile_agent("C", profile, monkeypatch)
    assert bound.config.step_limit == plain.config.step_limit == DEFAULT_STEP_LIMIT
    assert bound.config.cost_limit == plain.config.cost_limit == DEFAULT_COST_LIMIT
    assert bound.config.wall_time_limit_seconds == plain.config.wall_time_limit_seconds
    assert bound.config.system_template == plain.config.system_template
    assert bound.mcp_surface is plain.mcp_surface
    assert bound.mcp_surface_profile == plain.mcp_surface_profile == "docs-only-v1"
    assert [t["name"] for t in bound.mcp_tools] == ["search_resources"]


# --- the accepted matrix path is the Responses model, never the chat one -------------------------

def test_the_reviewed_profile_builds_only_the_responses_model():
    """A chat model here would run a contract the controlled evidence never proved (ADR-0014)."""
    from ckb_model import CkbLitellmModel, CkbLitellmResponseModel

    from ckbbench.run.agent_factory import _profile_model_builder

    built = _profile_model_builder(_profile(), "sk-live-do-not-log")
    assert isinstance(built, CkbLitellmResponseModel)
    assert not isinstance(built, CkbLitellmModel)
    assert built.config.max_query_attempts == 4
    assert built.config.retry_backoff_seconds == (4, 8, 16)


def test_the_probe_and_production_share_the_settings_that_must_match():
    """The controlled request proves compatibility; it is not a byte-identical benchmark turn."""
    from ckbbench.run.provider_probe import completion_payload

    profile = _profile()
    probe = completion_payload(profile)
    production = profile.model_kwargs()

    assert probe["model"] == profile.requested_model
    assert "temperature" not in probe and "temperature" not in production
    assert probe["reasoning"] == production["reasoning"] == {"effort": "high"}
    assert probe["provider"] == production["extra_body"]["provider"]
    assert probe["stream"] is production["stream"] is False
    assert probe["store"] is production["store"] is False
    assert production["num_retries"] == 0
    assert production["timeout"] == 300


def test_the_deepseek_probe_and_production_share_the_pinned_route():
    deepseek_model = "deepseek/deepseek-v4-flash-0731"
    from ckbbench.run.provider_probe import completion_payload

    profile = _profile(
        requested_model=deepseek_model,
        probed_response_model=deepseek_model,
        request_body_extensions={
            "provider": {
                "order": ["open-inference/fp4"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
        },
        reasoning_effort="high",
    )
    probe = completion_payload(profile)
    production = profile.model_kwargs()

    assert profile.litellm_model_name == "openai/deepseek/deepseek-v4-flash-0731"
    assert probe["reasoning"] == production["reasoning"] == {"effort": "high"}
    assert probe["provider"] == production["extra_body"]["provider"] == {
        "order": ["open-inference/fp4"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


def test_the_luna_probe_and_production_share_the_direct_contract():
    from ckbbench.run.provider_probe import completion_payload

    profile = load_run_profile("gpt-5.6-luna")
    probe = completion_payload(profile)
    production = profile.model_kwargs()

    assert profile.litellm_model_name == "openai/gpt-5.6-luna"
    assert probe["reasoning"] == production["reasoning"] == {"effort": "high"}
    assert probe["temperature"] == production["temperature"] == 0
    assert "provider" not in probe and "extra_body" not in production


def test_the_probe_and_production_use_the_same_exact_tool_schema():
    from ckbbench.run.provider_probe import canonical_bash_tool, completion_payload

    from minisweagent.models.utils.actions_toolcall_response import BASH_TOOL_RESPONSE_API

    profile = load_run_profile("gpt-5.6-luna")
    assert completion_payload(profile)["tools"] == [
        BASH_TOOL_RESPONSE_API
    ]
    assert canonical_bash_tool() == BASH_TOOL_RESPONSE_API


def test_the_output_ceiling_is_probe_only_and_absent_from_production():
    """A per-turn cap would truncate a real coding turn and bias the five-task result."""
    from ckbbench.run.provider_probe import MAX_COMPLETION_TOKENS, completion_payload

    probe = completion_payload(load_run_profile("gpt-5.6-luna"))
    assert probe["max_output_tokens"] == MAX_COMPLETION_TOKENS == 4096
    production = _profile().model_kwargs()
    assert "max_output_tokens" not in production, "production sends no per-turn ceiling"
    assert "max_tokens" not in production


def test_the_reasoning_settings_are_pinned_by_the_profile_digest():
    """A moving alias must not choose reasoning for an accepted run."""
    profile = _profile()
    assert (profile.reasoning_effort, profile.reasoning_context) == (
        "high", "prefix_tail_groups"
    )
    assert (profile.replay_policy, profile.replay_max_bytes) == (
        "prefix-tail-groups-v1", 131072
    )
    assert profile.observation_max_bytes == 32768
    assert profile.reasoning() == {"effort": "high"}
    assert profile.model_kwargs()["truncation"] == "disabled"
    assert any("thinking level: high | reasoning context=prefix_tail_groups" in line
               for line in profile.summary_lines())


def test_the_accepted_agent_writes_no_trajectory_of_its_own():
    """Protocol history is in-memory conversation, not an artifact this harness publishes."""
    from minisweagent.agents.default import AgentConfig

    assert AgentConfig(system_template="", instance_template="").output_path is None
    built = _make_agent(arm="A", mcp_client=None)
    assert getattr(built.config, "output_path", None) is None


def test_a_profile_naming_another_api_style_cannot_build_a_model():
    from ckbbench.run.model_profile import ModelProfileError

    from ckbbench.run.agent_factory import _profile_model_builder

    chat = replace(_profile(), api_style="openai-chat-completions")
    with pytest.raises(ModelProfileError, match="openai-responses"):
        _profile_model_builder(chat, "sk-live-do-not-log")


# --- the factory tests must not depend on order or on a developer's global model config ----------

def test_the_development_model_test_does_not_leak_an_endpoint_into_the_next_test():
    """Importing the agent fork loads a GLOBAL dotenv; that must not decide a later assertion."""
    import os

    _make_agent(arm="A", mcp_client=None)  # imports the fork and its global config
    assert os.environ.get("CKBBENCH_LLM_API_BASE") is None
    assert os.environ.get("BENCH_API_BASE") is None
    assert _profile().api_base == "https://proxy.example/v1"


def test_an_exported_endpoint_cannot_override_the_profile(monkeypatch):
    monkeypatch.setenv("BENCH_API_BASE", "https://elsewhere.example/v1")
    assert _profile().model_kwargs()["api_base"] == "https://proxy.example/v1"
