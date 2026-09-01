"""Regression tests for the fork's MCP dispatch (ckb_agent.py).

These encode WHY the dispatch must behave as it does (Rule 9), not just that it
does: the `mcp_call` convention must (a) route only genuine MCP calls and never
hijack ordinary bash, (b) preserve JSON arguments byte-for-byte even when they
contain spaces or quotes (the bug an adversarial review surfaced), and (c) return
an output dict matching the env.execute contract so the upstream observation
templates render. Run: PYTHONPATH=. .venv/bin/python -m pytest test_ckb_agent.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ckb_agent import CkbMcpAgent
from ckbbench.run.task_sequence import SUBMISSION_COMMAND, TaskSequenceController, TaskStage
from minisweagent.exceptions import InterruptAgentFlow, Submitted


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
_TX_HASH = "0x" + "a" * 64


class _FakeSigner:
    def __init__(self, *, result=None, error=None):
        self.requests = []
        self.result = {"tx_hash": _TX_HASH} if result is None else result
        self.error = error
        self.protocol_violation_count = 0

    def sign_and_submit(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


def _agent(mcp):
    return CkbMcpAgent(_FakeModel(), _FakeEnv(), mcp=mcp, **_CFG)


def test_is_mcp_action_matches_only_the_reserved_keyword_boundary():
    ag = _agent(_FakeMcp())
    assert ag._is_mcp_action("mcp_call rpc_get_tip {}")
    assert ag._is_mcp_action("  mcp_call rpc_get_tip {}")  # leading ws ok
    assert ag._is_mcp_action("mcp_call")
    assert ag._is_mcp_action("mcp_call\trpc_get_tip {}")
    # must NOT hijack ordinary bash
    assert not ag._is_mcp_action("mcp_callfoo bar")  # different command
    assert not ag._is_mcp_action("echo mcp_call something")  # keyword mid-line


@pytest.mark.parametrize("command", ["mcp_call", "mcp_call\tsearch_resources {}"])
def test_reserved_mcp_command_variants_never_fall_through_to_shell(command):
    agent = _agent(_FakeMcp())
    assert agent._is_mcp_action(command)
    result = agent._run_mcp_action(command)
    assert result["returncode"] in {0, 2}
    assert agent.env.calls == []


def test_signer_action_is_available_only_when_a_broker_is_bound():
    signer = _FakeSigner()
    agent = CkbMcpAgent(_FakeModel(), _FakeEnv(), signer=signer, **_CFG)
    request = {"transaction": {"version": "0x0"}}
    command = "ckb_sign_and_submit " + json.dumps(request)

    assert agent._is_signer_action(command)
    output = agent._run_signer_action(command)
    assert output["returncode"] == 0
    assert output["output"] == '{"tx_hash":"' + _TX_HASH + '"}'
    assert output["extra"] == {"signer_action": "ckb_sign_and_submit"}
    assert signer.requests == [request]

    without_signer = CkbMcpAgent(_FakeModel(), _FakeEnv(), **_CFG)
    assert without_signer._is_signer_action(command)
    refused = without_signer._run_signer_action(command)
    assert refused["returncode"] == 2
    assert without_signer.env.calls == []
    assert without_signer.protocol_violation_count == 1


@pytest.mark.parametrize("command", ["ckb_sign_and_submit", "ckb_sign_and_submit\t{}"])
def test_reserved_signer_command_never_falls_through_to_the_shell(command):
    agent = CkbMcpAgent(_FakeModel(), _FakeEnv(), **_CFG)
    assert agent._is_signer_action(command)
    output = agent._run_signer_action(command)
    assert output["returncode"] == 2
    assert agent.env.calls == []


@pytest.mark.parametrize("payload", ["", "null", "[]", "not-json"])
def test_malformed_signer_actions_never_reach_the_broker(payload):
    signer = _FakeSigner()
    agent = CkbMcpAgent(_FakeModel(), _FakeEnv(), signer=signer, **_CFG)
    output = agent._run_signer_action(f"ckb_sign_and_submit {payload}")
    assert output["returncode"] == 2
    assert signer.requests == []
    assert agent.protocol_violation_count == 1


def test_signer_failures_and_malformed_results_retain_no_private_content():
    secret = "PRIVATE-SIGNER-CONTENT"
    failing = CkbMcpAgent(
        _FakeModel(), _FakeEnv(), signer=_FakeSigner(error=RuntimeError(secret)), **_CFG
    )
    failed = failing._run_signer_action('ckb_sign_and_submit {"transaction":{}}')
    assert failed["returncode"] == 1
    assert secret not in str(failed)

    malformed = CkbMcpAgent(
        _FakeModel(), _FakeEnv(), signer=_FakeSigner(result={"tx_hash": secret}), **_CFG
    )
    rejected = malformed._run_signer_action('ckb_sign_and_submit {"transaction":{}}')
    assert rejected == {
        "output": "signing request returned malformed public evidence",
        "returncode": 1,
        "exception_info": "",
    }


def test_agent_protocol_violation_count_combines_surface_signer_and_local_refusals():
    class Surface:
        violation_count = 2

    signer = _FakeSigner()
    signer.protocol_violation_count = 3
    agent = CkbMcpAgent(
        _FakeModel(), _FakeEnv(), signer=signer, surface=Surface(), **_CFG
    )
    agent._run_signer_action("ckb_sign_and_submit not-json")
    assert agent.protocol_violation_count == 6


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
    """The OFF arm exposes zero MCP tools and never treats mcp_call as MCP."""
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
    mcp = _FakeMcp(resource_error=RuntimeError("SENSITIVE-RESOURCE-BODY"))
    out = _run(_agent(mcp), 'mcp_call resources/read {"uri": "ckb://a"}')
    assert out["returncode"] == 1
    assert "failed" in out["output"]
    assert "SENSITIVE-RESOURCE-BODY" not in out["output"]
    assert "ckb://a" not in out["output"]
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


# --- docs-only surface enforcement (ADR-0013) ----------------------------------------------------

from ckbbench.run.mcp_surface import (  # noqa: E402 - the harness package, imported after the fork
    McpSurfaceError,
    policy_for_profile,
    PROFILE_DOCS_ONLY,
)

_DOCS_POLICY = policy_for_profile(PROFILE_DOCS_ONLY)

_SERVER_CATALOG = [
    {"name": "search_resources", "description": "Search CKB documentation"},
    {"name": "search_tools", "description": "Discover deferred live tools"},
    {"name": "rpc_get_tip_block_number", "description": "Current tip height"},
    {"name": "rpc_send_transaction", "description": "Broadcast a transaction"},
    {"name": "dev_faucet_claim", "description": "Claim testnet funds"},
    {"name": "ckb_query_address", "description": "Look up an address"},
    {"name": "some_future_tool", "description": "Not known to this harness"},
]


class _CountingMcp(_FakeMcp):
    """Counts every client method so a local rejection can be proven to reach none of them."""

    def __init__(self, tools=None, **kwargs):
        super().__init__(**kwargs)
        self._tools = _SERVER_CATALOG if tools is None else tools
        self.call_tool_calls = 0
        self.read_resource_calls = 0

    def list_tools(self):
        return list(self._tools)

    def call_tool(self, tool, args):
        self.call_tool_calls += 1
        return super().call_tool(tool, args)

    def read_resource(self, uri):
        self.read_resource_calls += 1
        return super().read_resource(uri)


def _docs_agent(mcp=None):
    return CkbMcpAgent(
        _FakeModel(), _FakeEnv(), mcp=mcp or _CountingMcp(), surface=_DOCS_POLICY, **_CFG
    )


def _requests(mcp) -> int:
    return mcp.call_tool_calls + mcp.read_resource_calls


def test_docs_surface_shows_only_the_documentation_tool():
    """The catalog the model sees must not advertise a chain-bound path it cannot take."""
    mcp = _CountingMcp()
    agent = _docs_agent(mcp)
    assert [t["name"] for t in agent.mcp_tools] == ["search_resources"]


def test_docs_surface_allows_the_documentation_tool_exactly_once():
    mcp = _CountingMcp()
    agent = _docs_agent(mcp)
    out = agent._run_mcp_action('mcp_call search_resources {"query": "type id"}')
    assert out["returncode"] == 0
    assert mcp.call_tool_calls == 1
    assert mcp.last == ("search_resources", {"query": "type id"})


def test_docs_surface_allows_a_documentation_resource_read_exactly_once():
    mcp = _CountingMcp()
    agent = _docs_agent(mcp)
    uri = "ckb://docs/reference/token-script-hashes"
    out = agent._run_mcp_action(f'mcp_call resources/read {{"uri": "{uri}"}}')
    assert out["returncode"] == 0
    assert mcp.read_resource_calls == 1
    assert mcp.read_uris == [uri]


@pytest.mark.parametrize("tool", [
    "search_tools",
    "rpc_get_tip_block_number",
    "rpc_send_transaction",
    "dev_faucet_claim",
    "ckb_query_address",
    "some_future_tool",
    "Search_Resources",
    "SEARCH_RESOURCES",
    "search_resources_extra",
    "pre_search_resources",
    "search_resources.read",
    "search-resources",
    "",
])
def test_every_other_tool_is_rejected_locally_with_zero_requests(tool):
    """Exact allowlist: a case variant, prefix, suffix or unknown future tool is a different tool."""
    mcp = _CountingMcp()
    agent = _docs_agent(mcp)
    out = agent._run_mcp_action(f"mcp_call {tool} {{}}")
    assert out["returncode"] != 0
    assert _requests(mcp) == 0
    assert set(out) >= {"output", "returncode", "exception_info"}


def test_whitespace_padding_cannot_smuggle_a_denied_tool():
    """The parser normalizes the spacing around a command, so padding resolves to the same name.

    That is not a bypass: a padded ALLOWED name is still the allowed tool, and a padded DENIED name
    is still denied. Both directions are asserted so a future parser change cannot make padding
    meaningful.
    """
    mcp = _CountingMcp()
    agent = _docs_agent(mcp)
    allowed = agent._run_mcp_action("mcp_call   search_resources    {}")
    assert allowed["returncode"] == 0
    assert mcp.call_tool_calls == 1
    assert mcp.last[0] == "search_resources"

    denied = agent._run_mcp_action("mcp_call   rpc_send_transaction    {}")
    assert denied["returncode"] != 0
    assert mcp.call_tool_calls == 1  # unchanged: the refusal reached no client method


@pytest.mark.parametrize("uri", [
    "ckb://chain/tip",
    "ckb://docs",
    "ckb://docs/",
    "CKB://DOCS/reference/x",
    "ckb://Docs/reference/x",
    "https://docs.example/ckb",
    "http://ckb://docs/x",
    "file:///etc/passwd",
    "  ckb://docs/reference/x",
    "ckb://docsx/reference",
])
def test_non_documentation_resource_uris_are_rejected_locally(uri):
    mcp = _CountingMcp()
    agent = _docs_agent(mcp)
    out = agent._run_mcp_action(f'mcp_call resources/read {{"uri": "{uri}"}}')
    assert out["returncode"] != 0
    assert _requests(mcp) == 0


@pytest.mark.parametrize("raw", [
    'mcp_call resources/read {"uri": ""}',
    'mcp_call resources/read {"uri": null}',
    'mcp_call resources/read {"uri": 7}',
    'mcp_call resources/read {"uri": ["ckb://docs/x"]}',
    'mcp_call resources/read {}',
    'mcp_call resources/read {"uri": "ckb://docs/x", "extra": 1}',
    'mcp_call resources/read {not json}',
    'mcp_call search_resources {not json}',
    "mcp_call",
])
def test_malformed_resource_and_argument_forms_are_rejected_locally(raw):
    mcp = _CountingMcp()
    agent = _docs_agent(mcp)
    out = agent._run_mcp_action(raw) if raw != "mcp_call" else {"returncode": 2}
    assert out["returncode"] != 0
    assert _requests(mcp) == 0


def test_rejections_preserve_the_environment_output_contract():
    """A refusal must render like any failed observation, not raise out of the agent loop."""
    mcp = _CountingMcp()
    agent = _docs_agent(mcp)
    message = {"extra": {"actions": [
        {"command": "mcp_call rpc_send_transaction {}"},
        {"command": 'mcp_call resources/read {"uri": "ckb://chain/tip"}'},
        {"command": "echo hello"},
    ]}}
    agent.messages = []
    agent.execute_actions(message)
    assert _requests(mcp) == 0
    assert agent.env.calls == ["echo hello"]


def test_a_missing_required_tool_fails_before_any_model_call():
    """A server that stopped advertising the documentation surface must not start a run."""
    with pytest.raises(McpSurfaceError, match="search_resources"):
        _docs_agent(_CountingMcp(tools=[{"name": "rpc_get_tip_block_number"}]))


def test_a_malformed_required_tool_entry_fails_construction():
    with pytest.raises(McpSurfaceError, match="malformed"):
        _docs_agent(_CountingMcp(tools=[{"name": "search_resources", "description": 42}]))


def test_an_off_agent_keeps_no_mcp_interception_or_vocabulary():
    agent = CkbMcpAgent(_FakeModel(), _FakeEnv(), mcp=None, **_CFG)
    assert agent.mcp is None
    assert agent.mcp_tools == []
    assert not agent._is_mcp_action("mcp_call search_resources {}")
    agent.messages = []
    agent.execute_actions({"extra": {"actions": [{"command": "mcp_call search_resources {}"}]}})
    assert agent.env.calls == ["mcp_call search_resources {}"]


def test_tool_transport_failure_does_not_echo_provider_content():
    class FailingMcp(_FakeMcp):
        def call_tool(self, tool, args):
            raise RuntimeError("SENSITIVE-PROVIDER-BODY")

    out = _run(_agent(FailingMcp()), "mcp_call rpc_get_tip {}")
    assert out["returncode"] == 1
    assert "RuntimeError" in out["output"]
    assert "SENSITIVE-PROVIDER-BODY" not in out["output"]


def test_task_surface_validates_resources_and_records_local_refusals():
    from ckbbench.run.treatment_surface import (
        TreatmentSurfaceProfile,
        ScopedMcpClient,
        TaskMcpSurfacePolicy,
    )

    tools = [
        {
            "name": "search_resources",
            "description": "Search docs",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "dev_request_testnet_funds",
            "description": "Faucet",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    resources = [{"uri": "ckb://docs/reference", "name": "Reference"}]

    class TreatmentMcp(_FakeMcp):
        def list_tools(self):
            return tools

        def list_resources(self):
            return resources

    profile = TreatmentSurfaceProfile.from_catalogs(
        profile_id="docs-synthetic-v1",
        server_name="ckb-ai-mcp",
        server_version="1.6.13",
        claims_live_chain=False,
        allowed_tools=("search_resources",),
        allowed_resource_prefixes=("ckb://docs/",),
        tools=tools,
        resources=resources,
    )
    policy = TaskMcpSurfacePolicy(profile)
    client = ScopedMcpClient(TreatmentMcp(), policy)
    agent = CkbMcpAgent(_FakeModel(), _FakeEnv(), mcp=client, surface=policy, **_CFG)

    assert [tool["name"] for tool in agent.mcp_tools] == ["search_resources"]
    denied = agent._run_mcp_action("mcp_call dev_request_testnet_funds {}")
    assert denied["returncode"] == 2
    assert policy.violation_count == 1


def _sequence(tmp_path: Path, count: int = 2) -> TaskSequenceController:
    stages = tuple(
        TaskStage(
            task_id=f"task-{index}",
            proof_file=f"proof-{index}.txt",
            param_filename=f"task-{index}.json",
            prompt_injected={},
            instructions=f"STAGE {index}\n",
        )
        for index in range(1, count + 1)
    )
    controller = TaskSequenceController(tmp_path, stages)
    controller.start()
    return controller


class _ProofEnv(_FakeEnv):
    def __init__(self, mount: Path):
        super().__init__()
        self.mount = mount

    def execute(self, action):
        command = action.get("command", "")
        self.calls.append(command)
        if command == "make first proof":
            (self.mount / "proof-1.txt").write_text("proof")
        return {"output": "(bash)", "returncode": 0, "exception_info": ""}


class _SubmittingEnv(_FakeEnv):
    def execute(self, action):
        self.calls.append(action.get("command", ""))
        raise Submitted(
            {
                "role": "exit",
                "content": "",
                "extra": {"exit_status": "Submitted", "submission": ""},
            }
        )


def test_exact_early_submission_is_refused_without_reaching_the_environment(tmp_path: Path):
    sequence = _sequence(tmp_path)
    env = _FakeEnv()
    agent = CkbMcpAgent(_FakeModel(), env, task_sequence=sequence, **_CFG)

    messages = agent.execute_actions(
        {"extra": {"actions": [{"command": SUBMISSION_COMMAND}]}}
    )

    assert env.calls == []
    assert "unavailable until every task" in messages[0]["content"]


def test_a_release_stops_the_rest_of_the_same_model_action_batch(tmp_path: Path):
    sequence = _sequence(tmp_path)
    env = _ProofEnv(tmp_path)
    agent = CkbMcpAgent(_FakeModel(), env, task_sequence=sequence, **_CFG)

    messages = agent.execute_actions(
        {
            "extra": {
                "actions": [
                    {"command": "make first proof"},
                    {"command": "work on unreleased task"},
                ]
            }
        }
    )

    assert env.calls == ["make first proof"]
    assert sequence.current_task_id == "task-2"
    assert "next task is task-2" in messages[0]["content"]
    assert "Read INSTRUCTIONS.md" in messages[1]["content"]


def test_an_alias_that_emits_the_submit_sentinel_is_still_refused_early(tmp_path: Path):
    sequence = _sequence(tmp_path)
    env = _SubmittingEnv()
    agent = CkbMcpAgent(_FakeModel(), env, task_sequence=sequence, **_CFG)

    messages = agent.execute_actions({"extra": {"actions": [{"command": "submit alias"}]}})

    assert env.calls == ["submit alias"]
    assert "unavailable until every task" in messages[0]["content"]
    assert sequence.current_task_id == "task-1"


def test_submit_propagates_only_after_the_final_proof_is_observed(tmp_path: Path):
    sequence = _sequence(tmp_path, count=1)
    (tmp_path / "proof-1.txt").write_text("proof")
    env = _SubmittingEnv()
    agent = CkbMcpAgent(_FakeModel(), env, task_sequence=sequence, **_CFG)

    with pytest.raises(Submitted):
        agent.execute_actions({"extra": {"actions": [{"command": "submit alias"}]}})
    assert sequence.complete is True


def test_an_unreleased_artifact_becomes_an_explicit_agent_exit(tmp_path: Path):
    sequence = _sequence(tmp_path)
    (tmp_path / "proof-2.txt").write_text("early")
    agent = CkbMcpAgent(_FakeModel(), _FakeEnv(), task_sequence=sequence, **_CFG)

    with pytest.raises(InterruptAgentFlow) as raised:
        agent.execute_actions({"extra": {"actions": [{"command": "true"}]}})

    assert raised.value.messages[0]["extra"] == {
        "exit_status": "TaskOrderViolation",
        "submission": "",
    }
