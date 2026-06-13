"""Agent factory tests: arm isolation, preamble wiring, submit sentinel (ADR-0008).

Encodes WHY each arm must see a different prompt surface and why the factory must pass
mcp_client through unchanged. No network, proxy, or LitellmModel validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import StrictUndefined, Template

from ckbbench.run.agent_factory import make_agent_factory, render_mcp_tool_list
from ckbbench.run.arm import ArmConfig, resolve_arm

_SAMPLE_TOOLS = [
    {"name": "rpc_get_tip_block_number", "description": "Current tip height"},
    {"name": "ckb_query_address", "description": "Look up an address"},
]


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

    def initialize(self):
        return {}

    def list_tools(self):
        return list(self.tools)

    def call_tool(self, tool, args):
        return {"content": [{"type": "text", "text": "ok"}]}


def _render_system(agent) -> str:
    return Template(agent.config.system_template, undefined=StrictUndefined).render(
        **agent.get_template_vars()
    )


def _make_agent(*, arm: str, mcp_client, model_builder=_FakeModel):
    factory = make_agent_factory(model_builder=lambda _m, _b, _k: model_builder())
    return factory(
        mount_dir=Path("/tmp/mount"),
        pointer="do the task",
        arm_config=resolve_arm(arm),
        mcp_client=mcp_client,
        model="grok-test",
        suite=object(),
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
    )
    assert hasattr(agent, "run")
    assert hasattr(agent, "messages")
    assert isinstance(agent.messages, list)


def test_mcp_client_identity_passthrough_and_off_arm_mcp_is_none():
    """run_cell owns the client lifecycle; the factory must not create a second one."""
    mcp = _FakeMcp(_SAMPLE_TOOLS)
    on_agent = _make_agent(arm="C", mcp_client=mcp)
    assert on_agent.mcp is mcp

    off_agent = _make_agent(arm="A", mcp_client=None)
    assert off_agent.mcp is None


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