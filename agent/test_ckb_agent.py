"""Regression tests for the fork's MCP dispatch (ckb_agent.py).

These encode WHY the dispatch must behave as it does (Rule 9), not just that it
does: the `mcp_call` convention must (a) route only genuine MCP calls and never
hijack ordinary bash, (b) preserve JSON arguments byte-for-byte even when they
contain spaces or quotes (the bug an adversarial review surfaced), and (c) return
an output dict matching the env.execute contract so the upstream observation
templates render. Run: PYTHONPATH=. .venv/bin/python -m pytest test_ckb_agent.py
"""

from __future__ import annotations

import pytest

from ckb_agent import CkbMcpAgent


class _FakeEnv:
    def __init__(self):
        self.calls = []

    def execute(self, action):
        self.calls.append(action.get("command", ""))
        return {"output": "(bash)", "returncode": 0, "exception_info": ""}

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {}


class _FakeMcp:
    _DEFAULT_RESOURCE = {"contents": [{"text": "doc"}]}

    def __init__(self, *, resource=_DEFAULT_RESOURCE, resource_error=None):
        self.last = None
        self.read_uris: list[str] = []
        self._resource = resource
        self._resource_error = resource_error

    def initialize(self):
        return {}

    def list_tools(self):
        return []

    def call_tool(self, tool, args):
        self.last = (tool, args)
        return {"content": [{"type": "text", "text": "ok"}]}

    def read_resource(self, uri):
        self.read_uris.append(uri)
        if self._resource_error is not None:
            raise self._resource_error
        return self._resource


class _FakeModel:
    def format_observation_messages(self, message, outputs, template_vars):
        return [
            {"role": "user", "content": o.get("output", ""), "extra": o.get("extra", {})}
            for o in outputs
        ]

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {}


_CFG = {"system_template": "x", "instance_template": "x"}


def _agent(mcp):
    return CkbMcpAgent(_FakeModel(), _FakeEnv(), mcp=mcp, **_CFG)


def test_is_mcp_action_only_matches_keyword_plus_space():
    ag = _agent(_FakeMcp())
    assert ag._is_mcp_action("mcp_call rpc_get_tip {}")
    assert ag._is_mcp_action("  mcp_call rpc_get_tip {}")  # leading ws ok
    # must NOT hijack ordinary bash
    assert not ag._is_mcp_action("mcp_call")  # bare word, no space
    assert not ag._is_mcp_action("mcp_callfoo bar")  # different command
    assert not ag._is_mcp_action("echo mcp_call something")  # keyword mid-line


def test_json_args_with_spaces_and_quotes_survive():
    """The bug an adversarial review caught: shlex-splitting the whole line corrupts
    JSON values containing spaces/quotes. The parser must pass the JSON verbatim."""
    mcp = _FakeMcp()
    ag = _agent(mcp)
    cases = [
        ("mcp_call rpc_get_tip {}", ("rpc_get_tip", {})),
        (
            'mcp_call ckb_query_address {"address": "ckt1 with spaces"}',
            ("ckb_query_address", {"address": "ckt1 with spaces"}),
        ),
        ('mcp_call t {"a": "x y", "b": [1, 2, 3]}', ("t", {"a": "x y", "b": [1, 2, 3]})),
        ('mcp_call t {"q": "he said \\"hi\\""}', ("t", {"q": 'he said "hi"'})),
    ]
    for cmd, expected in cases:
        out = ag._run_mcp_action(cmd)
        assert out["returncode"] == 0, cmd
        assert mcp.last == expected, (cmd, mcp.last)


def test_malformed_args_fail_cleanly_not_crash():
    ag = _agent(_FakeMcp())
    for bad in ("mcp_call t {bad}", "mcp_call t [1,2,3]", "mcp_call"):
        out = ag._run_mcp_action(bad)
        assert out["returncode"] == 2, bad
        assert "exception_info" in out  # env-output contract preserved


def test_output_matches_env_contract():
    """Every _run_mcp_action return path must carry exception_info so the default
    observation_template (which renders {% if output.exception_info %}) does not crash."""
    ag = _agent(_FakeMcp())
    out = ag._run_mcp_action("mcp_call rpc_get_tip {}")
    for key in ("output", "returncode", "exception_info"):
        assert key in out
    assert out["extra"]["mcp_tool"] == "rpc_get_tip"


def test_off_arm_has_no_mcp_surface():
    """The OFF arm (mcp=None) exposes zero MCP tools and never treats mcp_call as MCP,
    so an OFF-arm agent is byte-for-byte upstream behavior."""
    off = _agent(None)
    assert off.mcp_tools == []
    assert not off._is_mcp_action("mcp_call rpc_get_tip {}")
    # an mcp_call command in the OFF arm routes to bash like any other string
    outputs = off.execute_actions(
        {"extra": {"actions": [{"command": "mcp_call rpc_get_tip {}"}]}}
    )
    assert off.env.calls == ["mcp_call rpc_get_tip {}"]  # went to bash
    assert outputs  # produced an observation


def _run(agent, command):
    return agent._run_mcp_action(command)


def test_reserved_action_reads_a_resource_instead_of_calling_a_tool():
    mcp = _FakeMcp()
    out = _run(_agent(mcp), 'mcp_call resources/read {"uri": "ckb://docs/reference/x"}')
    assert mcp.read_uris == ["ckb://docs/reference/x"]
    assert mcp.last is None, "must not reach tools/call"
    assert out["returncode"] == 0
    assert out["output"] == "doc"


def test_reserved_action_records_provenance_without_the_body():
    out = _run(_agent(_FakeMcp()), 'mcp_call resources/read {"uri": "ckb://a"}')
    assert out["extra"] == {"mcp_tool": "resources/read", "mcp_resource_uri": "ckb://a"}
    assert "doc" not in str(out["extra"])


def test_multiple_text_contents_join_in_order():
    mcp = _FakeMcp(resource={"contents": [{"text": "one"}, {"text": "two"}]})
    assert _run(_agent(mcp), 'mcp_call resources/read {"uri": "ckb://a"}')["output"] == "one\ntwo"


@pytest.mark.parametrize(
    "resource",
    [{"contents": [{"blob": "x"}]}, {"contents": [{"text": "   "}]}, {"contents": []}, None, "x"],
    ids=["no-text", "whitespace", "empty", "none", "not-a-dict"],
)
def test_unusable_resource_content_is_a_visible_failure(resource):
    mcp = _FakeMcp(resource=resource)
    out = _run(_agent(mcp), 'mcp_call resources/read {"uri": "ckb://a"}')
    assert out["returncode"] == 1
    assert out["output"] != ""


@pytest.mark.parametrize(
    "args",
    ['{}', '{"uri": ""}', '{"uri": "   "}', '{"uri": 5}', '{"uri": null}',
     '{"uri": "ckb://a", "extra": 1}', '{"url": "ckb://a"}'],
    ids=["missing", "empty", "blank", "non-string", "null", "extra-field", "wrong-field"],
)
def test_invalid_resource_arguments_fail_locally_without_a_request(args):
    mcp = _FakeMcp()
    out = _run(_agent(mcp), f"mcp_call resources/read {args}")
    assert out["returncode"] == 2
    assert mcp.read_uris == []


def test_resource_transport_failure_is_a_failed_observation():
    mcp = _FakeMcp(resource_error=RuntimeError("boom"))
    out = _run(_agent(mcp), 'mcp_call resources/read {"uri": "ckb://a"}')
    assert out["returncode"] == 1
    assert "failed" in out["output"]
    assert out["exception_info"] == ""


def test_ordinary_tools_still_dispatch_through_call_tool():
    mcp = _FakeMcp()
    out = _run(_agent(mcp), 'mcp_call ckb_query_address {"address": "ckt1 with spaces"}')
    assert mcp.last == ("ckb_query_address", {"address": "ckt1 with spaces"})
    assert mcp.read_uris == []
    assert out["extra"] == {"mcp_tool": "ckb_query_address"}


def test_every_resource_return_path_keeps_the_output_contract():
    mcp = _FakeMcp()
    for command in (
        'mcp_call resources/read {"uri": "ckb://a"}',
        'mcp_call resources/read {}',
        'mcp_call resources/read not-json',
    ):
        out = _run(_agent(mcp), command)
        assert {"output", "returncode", "exception_info"} <= set(out)
        assert isinstance(out["output"], str)
        assert isinstance(out["returncode"], int)


def test_off_arm_exposes_no_resource_action():
    agent = _agent(None)
    assert not agent._is_mcp_action('mcp_call resources/read {"uri": "ckb://a"}')
    agent.execute_actions(
        {"extra": {"actions": [{"command": 'mcp_call resources/read {"uri": "ckb://a"}'}]}}
    )
    assert agent.env.calls == ['mcp_call resources/read {"uri": "ckb://a"}']
