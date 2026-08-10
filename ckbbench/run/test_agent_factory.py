"""Agent factory tests: arm isolation, preamble wiring, submit sentinel (ADR-0008).

Encodes WHY each arm must see a different prompt surface and why the factory must pass
mcp_client through unchanged. No network, proxy, or LitellmModel validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import StrictUndefined, Template

from ckbbench.config import DEVNET_GENESIS_PRIVKEY, DEVNET_RPC, TESTNET_RPC
from ckbbench.run.agent_factory import (
    _DEFAULT_STEP_LIMIT_MCP,
    _DEFAULT_STEP_LIMIT_NO_MCP,
    SIGNER_ENV_NAMES,
    agent_rpc_url,
    chain_env_for,
    local_signer_sanitizer,
    make_agent_factory,
    render_mcp_tool_list,
    signer_env_for,
)
from ckbbench.run.arm import ArmConfig, resolve_arm

_SAMPLE_TOOLS = [
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
    def __init__(self, tools: list[dict] | None = None):
        self.tools = tools or []
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


def _render_system(agent) -> str:
    return Template(agent.config.system_template, undefined=StrictUndefined).render(
        **agent.get_template_vars()
    )


def _make_agent(*, arm: str, mcp_client, model_builder=_FakeModel, chain: str = "devnet",
                **factory_kwargs):
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
    )


@pytest.mark.parametrize("arm", ["C", "D"])
def test_mcp_arm_system_prompt_exposes_mcp_surface(arm):
    """BOTH MCP arms (C AND D) must offer mcp_call + the live tool list or the benchmark measures
    nothing. Parametrized over C and D so a regression that drops the surface on only one arm fails
    (codex: a C-only test would not catch a D leak)."""
    mcp = _FakeMcp(_SAMPLE_TOOLS)
    agent = _make_agent(arm=arm, mcp_client=mcp)
    rendered = _render_system(agent)

    assert "mcp_call" in agent.config.system_template
    assert "rpc_get_tip_block_number" in rendered
    assert "ckb_query_address" in rendered


@pytest.mark.parametrize("arm", ["A", "B"])
def test_off_arm_system_prompt_has_no_mcp_surface(arm):
    """Arm isolation: any MCP leak into A OR B invalidates the C-B headline. Asserted on the RENDERED
    prompt (not just the template source) and over BOTH off arms, so a leak via a template variable
    or on only one arm still fails (grok-build + codex)."""
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
    """Regression guard (codex): the factory must render the tool list from the agent's own
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


@pytest.mark.parametrize("arm", ["A", "B"])
def test_default_step_limit_larger_for_no_mcp_arms(arm):
    """No-MCP arms need more turns for direct RPC; sharing the MCP budget biases C-B."""
    agent = _make_agent(arm=arm, mcp_client=None)
    assert agent.config.step_limit == _DEFAULT_STEP_LIMIT_NO_MCP
    assert agent.config.step_limit > _DEFAULT_STEP_LIMIT_MCP


@pytest.mark.parametrize("arm", ["C", "D"])
def test_default_step_limit_for_mcp_arms(arm):
    """MCP arms keep the tighter default from the proven live path."""
    mcp = _FakeMcp(_SAMPLE_TOOLS)
    agent = _make_agent(arm=arm, mcp_client=mcp)
    assert agent.config.step_limit == _DEFAULT_STEP_LIMIT_MCP


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
def test_explicit_step_limit_overrides_uniformly_for_all_arms(arm):
    """Operators can still force one explicit budget across the matrix."""
    override = 25
    mcp = _FakeMcp(_SAMPLE_TOOLS) if arm in ("C", "D") else None
    agent = _make_agent(arm=arm, mcp_client=mcp, step_limit=override)
    assert agent.config.step_limit == override


@pytest.mark.parametrize("arm", ["A", "B"])
def test_no_mcp_step_limit_knob_only_changes_no_mcp_arms(arm):
    """The no-MCP budget is tuneable without accidentally loosening C/D."""
    agent = _make_agent(arm=arm, mcp_client=None, step_limit_no_mcp=120)
    assert agent.config.step_limit == 120

    mcp = _FakeMcp(_SAMPLE_TOOLS)
    mcp_agent = _make_agent(arm="C", mcp_client=mcp, step_limit_no_mcp=120)
    assert mcp_agent.config.step_limit == _DEFAULT_STEP_LIMIT_MCP


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


def test_docker_mode_uses_docker_environment_with_proxy_env(monkeypatch):
    """ADR-0006: production runs must execute the agent inside docker on the internal network."""
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
def test_mcp_steering_no_longer_names_a_chain(arm):
    """The steering line used to say "CKB/testnet", handing C/D a chain fact A/B never saw -- and
    the wrong one on a DevNet cell."""
    rendered = _render_system(_make_agent(arm=arm, mcp_client=_FakeMcp(_SAMPLE_TOOLS)))
    assert "mcp_call" in rendered  # the MCP surface itself is untouched
    assert "FALLBACK_RPC" in rendered
    assert "testnet" not in rendered.lower()


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
    """Task 04 needs a sender on DevNet. The key is the public dev.toml genesis fixture, chosen by
    the CELL's chain so it can never be offered on another chain."""
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


def test_devnet_cell_does_not_forward_the_testnet_signer(monkeypatch):
    """A live-chain key must not ride along into a DevNet cell that has no use for it."""
    captured = _fake_docker_env(monkeypatch)
    _make_agent(arm="B", mcp_client=None, chain="devnet")
    assert captured["forward_env"] == []
    assert not any("TESTNET" in name for name in captured["env"])


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
def test_all_arms_get_the_same_devnet_signing_capability(monkeypatch, arm):
    """If one arm could sign and another could not, task 04 would measure access, not ability."""
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
