"""Preflight tests: ADR-0010 version pin and deferred-loading surface (no network)."""

from __future__ import annotations

import traceback

import pytest

from ckbbench.run.mcp_surface import McpSurfaceError

from ckbbench.run.preflight import (
    PreflightResult,
    PreflightTransportError,
    PreflightUsageError,
    PreflightVersionMismatch,
    preflight_mcp,
)


class FakeMcpClient:
    def __init__(
        self,
        *,
        version: str = "1.6.12",
        tools: list[dict] | None = None,
        init_error: Exception | None = None,
        list_error: Exception | None = None,
        instructions: str = "deferred loading is on",
    ) -> None:
        self._version = version
        self._tools = tools
        self._init_error = init_error
        self._list_error = list_error
        self._instructions = instructions

    def initialize(self) -> dict:
        if self._init_error:
            raise self._init_error
        return {
            "serverInfo": {"name": "ckb-ai-mcp", "version": self._version},
            "instructions": self._instructions,
        }

    def list_tools(self) -> list[dict]:
        if self._list_error:
            raise self._list_error
        if self._tools is not None:
            return self._tools
        return [
            {"name": "search_tools"},
            {"name": "search_resources"},
            {"name": "rpc_get_tip_block_number"},
        ]


def test_preflight_match_passes():
    result = preflight_mcp(
        "https://mcp.example/ckbai",
        "1.6.12",
        client=FakeMcpClient(),
    )
    assert isinstance(result, PreflightResult)
    assert result.server_version == "1.6.12"
    assert result.has_search_tools
    assert result.has_search_resources
    assert result.deferred_loading_documented


def test_preflight_version_mismatch_raises_distinctly():
    with pytest.raises(PreflightVersionMismatch) as exc:
        preflight_mcp("https://mcp.example/ckbai", "1.6.12", client=FakeMcpClient(version="9.9.9"))
    assert exc.value.exit_code == 2
    assert "mismatch" in str(exc.value).lower()


def test_preflight_transport_error_on_initialize_failure():
    with pytest.raises(PreflightTransportError) as exc:
        preflight_mcp(
            "https://mcp.example/ckbai",
            "1.6.12",
            client=FakeMcpClient(init_error=RuntimeError("connection refused")),
        )
    assert exc.value.exit_code == 3


def test_preflight_transport_error_missing_server_version():
    class BadInitClient(FakeMcpClient):
        def initialize(self) -> dict:
            return {"serverInfo": {"name": "x"}}

    with pytest.raises(PreflightTransportError, match="serverInfo.version"):
        preflight_mcp("https://mcp.example/ckbai", "1.6.12", client=BadInitClient())


def test_preflight_transport_error_missing_server_info():
    class NoInfoClient(FakeMcpClient):
        def initialize(self) -> dict:
            return {}

    with pytest.raises(PreflightTransportError, match="serverInfo"):
        preflight_mcp("https://mcp.example/ckbai", "1.6.12", client=NoInfoClient())


def test_preflight_passes_without_search_tools():
    """`search_tools` advertises the deferred live catalog, none of which the docs-only surface can
    call (ADR-0013). Requiring it would fail a run over a capability the treatment never uses."""
    result = preflight_mcp(
        "https://mcp.example/ckbai",
        "1.6.12",
        client=FakeMcpClient(tools=[{"name": "search_resources"}]),
    )
    assert result.has_search_resources is True
    assert result.has_search_tools is False


def test_preflight_missing_search_resources_fails_before_any_agent_runs():
    """The documentation tool IS the phase-one surface; without it there is nothing to measure."""
    with pytest.raises(PreflightTransportError, match="search_resources"):
        preflight_mcp(
            "https://mcp.example/ckbai",
            "1.6.12",
            client=FakeMcpClient(tools=[{"name": "search_tools"}]),
        )


def test_preflight_records_search_tools_as_an_observation_only():
    """Its presence is diagnostic: it must never become a gate or reach the agent's tool list."""
    result = preflight_mcp(
        "https://mcp.example/ckbai",
        "1.6.12",
        client=FakeMcpClient(tools=[{"name": "search_resources"}, {"name": "search_tools"}]),
    )
    assert result.has_search_tools is True
    assert result.tool_count == 2


def test_preflight_list_tools_transport_error():
    with pytest.raises(PreflightTransportError, match="tools/list"):
        preflight_mcp(
            "https://mcp.example/ckbai",
            "1.6.12",
            client=FakeMcpClient(list_error=OSError("timeout")),
        )


def test_preflight_usage_errors():
    with pytest.raises(PreflightUsageError) as exc:
        preflight_mcp("", "1.6.12", client=FakeMcpClient())
    assert exc.value.exit_code == 4

    with pytest.raises(PreflightUsageError):
        preflight_mcp("https://mcp.example/ckbai", "", client=FakeMcpClient())


def test_preflight_non_dict_initialize_result():
    class WeirdClient(FakeMcpClient):
        def initialize(self):
            return None  # type: ignore[return-value]

    with pytest.raises(PreflightTransportError, match="serverInfo"):
        preflight_mcp("https://mcp.example/ckbai", "1.6.12", client=WeirdClient())


@pytest.mark.parametrize("tools,match", [
    ("search_resources", "must be a list"),
    (["bad", {"name": "search_resources"}], "must be an object"),
    ([{"name": []}, {"name": "search_resources"}], "no usable name"),
    ([{"name": ""}, {"name": "search_resources"}], "no usable name"),
    ([{"name": 7}], "no usable name"),
    ([{"no_name": True}], "no usable name"),
    ([{"name": "search_resources", "description": 42}], "malformed description"),
    ([{"name": "search_resources"}, {"name": "search_resources"}], "repeats an earlier tool name"),
])
def test_a_malformed_catalog_is_a_classified_transport_failure(tools, match):
    """A raw TypeError here would abort the matrix instead of writing a pre-agent infra_fail row."""
    with pytest.raises(PreflightTransportError, match=match):
        preflight_mcp("https://mcp.example/ckbai", "1.6.12", client=FakeMcpClient(tools=tools))


def test_a_null_catalog_is_a_classified_transport_failure():
    """`tools=None` on the shared fake means "use the default", so this needs its own client."""

    class _NullCatalogClient(FakeMcpClient):
        def list_tools(self):
            return None

    with pytest.raises(PreflightTransportError, match="must be a list"):
        preflight_mcp("https://mcp.example/ckbai", "1.6.12", client=_NullCatalogClient())


def test_the_malformed_catalog_message_carries_no_server_body():
    with pytest.raises(PreflightTransportError) as exc:
        preflight_mcp(
            "https://mcp.example/ckbai", "1.6.12",
            client=FakeMcpClient(tools=[{"name": "search_resources", "secret": "sk-live-xyz"},
                                        {"name": "search_resources"}]),
        )
    assert "sk-live-xyz" not in str(exc.value)


def test_preflight_counts_only_well_formed_entries():
    result = preflight_mcp(
        "https://mcp.example/ckbai",
        "1.6.12",
        client=FakeMcpClient(tools=[{"name": "search_tools"}, {"name": "search_resources"}]),
    )
    assert result.tool_count == 2


def test_preflight_default_client_import(monkeypatch):
    import sys
    from types import ModuleType

    fake_mod = ModuleType("ckb_mcp")
    fake_mod.CkbMcpClient = lambda url: FakeMcpClient()
    monkeypatch.setitem(sys.modules, "ckb_mcp", fake_mod)
    result = preflight_mcp("https://mcp.example/ckbai", "1.6.12")
    assert result.server_version == "1.6.12"

class _CountingPreflightClient(FakeMcpClient):
    """Counts every method so preflight can be proven to add no live call."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.initialize_calls = 0
        self.list_tools_calls = 0
        self.read_resource_calls = 0
        self.call_tool_calls = 0

    def initialize(self):
        self.initialize_calls += 1
        return super().initialize()

    def list_tools(self):
        self.list_tools_calls += 1
        return super().list_tools()

    def read_resource(self, uri):  # pragma: no cover - must never run
        self.read_resource_calls += 1
        raise AssertionError("preflight must not read a resource")

    def call_tool(self, tool, args):  # pragma: no cover - must never run
        self.call_tool_calls += 1
        raise AssertionError("preflight must not call a tool")


def test_preflight_adds_no_resource_read_or_tool_call():
    """The pinned resource method has separate integration coverage.

    Repeating a document read per cell would add external state and cost without strengthening the
    client-side boundary, so preflight stays exactly initialize + tools/list.
    """
    client = _CountingPreflightClient(tools=[{"name": "search_resources"}])
    preflight_mcp("https://mcp.example/ckbai", "1.6.12", client=client)
    assert (client.initialize_calls, client.list_tools_calls) == (1, 1)
    assert client.read_resource_calls == 0
    assert client.call_tool_calls == 0


_CANARIES = ("sk-live-do-not-log", "raw-server-body", "tok-abc123", "secret-tool-name")


def _no_canary(text: str) -> bool:
    return not any(c in text for c in _CANARIES)


def test_a_malformed_initialize_body_is_not_rendered_into_the_diagnostic():
    class _BodyClient(FakeMcpClient):
        def initialize(self):
            return {"secret": "sk-live-do-not-log", "payload": "raw-server-body"}

    with pytest.raises(PreflightTransportError) as exc:
        preflight_mcp("https://mcp.example/ckbai", "1.6.12", client=_BodyClient())
    assert _no_canary(str(exc.value))
    assert "serverInfo" in str(exc.value)


@pytest.mark.parametrize("url", [
    "https://user:tok-abc123@mcp.example/ckbai",
    "https://mcp.example/ckbai?token=tok-abc123",
])
def test_a_credentialed_endpoint_is_never_rendered(url):
    """A configured URL can carry userinfo or a token query; diagnostics must not echo it."""
    with pytest.raises(PreflightTransportError) as reach:
        preflight_mcp(url, "1.6.12", client=FakeMcpClient(init_error=OSError("boom")))
    assert _no_canary(str(reach.value)) and "mcp.example" not in str(reach.value)

    with pytest.raises(PreflightTransportError) as listing:
        preflight_mcp(url, "1.6.12", client=FakeMcpClient(list_error=OSError("boom")))
    assert _no_canary(str(listing.value)) and "mcp.example" not in str(listing.value)


def test_transport_exception_text_is_reduced_to_its_class():
    """An HTTP library's exception string can itself contain a response body or the URL."""
    with pytest.raises(PreflightTransportError) as exc:
        preflight_mcp(
            "https://mcp.example/ckbai", "1.6.12",
            client=FakeMcpClient(init_error=OSError("raw-server-body sk-live-do-not-log")),
        )
    assert _no_canary(str(exc.value))
    assert "OSError" in str(exc.value)


@pytest.mark.parametrize("tools", [
    [{"name": "secret-tool-name", "description": 42}],
    [{"name": "secret-tool-name"}, {"name": "secret-tool-name"}],
    [{"name": "search_resources", "description": "sk-live-do-not-log"}, {"name": []}],
])
def test_malformed_catalog_diagnostics_echo_no_server_value(tools):
    with pytest.raises(PreflightTransportError) as exc:
        preflight_mcp("https://mcp.example/ckbai", "1.6.12", client=FakeMcpClient(tools=tools))
    assert _no_canary(str(exc.value))
    assert "tools/list[" in str(exc.value)


def test_the_version_mismatch_distinction_is_preserved():
    """Sanitizing must not blur the one diagnostic an operator acts on differently."""
    with pytest.raises(PreflightVersionMismatch) as exc:
        preflight_mcp("https://mcp.example/ckbai", "1.6.13", client=FakeMcpClient(version="1.6.12"))
    assert "1.6.12" in str(exc.value) and "1.6.13" in str(exc.value)


_UNSAFE = OSError(
    "raw-server-body sk-live-do-not-log https://user:tok-abc123@mcp.example/ckbai?token=tok-abc123"
)


def _formatted(error: BaseException) -> str:
    """What an operator actually sees: the complete rendered traceback, not just str(error)."""
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


@pytest.mark.parametrize("phase,client_kwargs", [
    ("initialize", {"init_error": _UNSAFE}),
    ("tools/list", {"list_error": _UNSAFE}),
])
def test_no_transport_canary_survives_into_the_formatted_traceback(phase, client_kwargs):
    """`raise ... from exc` kept the unsafe original, which the traceback formatter renders."""
    with pytest.raises(PreflightTransportError) as exc:
        preflight_mcp(
            "https://user:tok-abc123@mcp.example/ckbai", "1.6.12",
            client=FakeMcpClient(**client_kwargs),
        )
    rendered = _formatted(exc.value)
    for canary in ("raw-server-body", "sk-live-do-not-log", "tok-abc123", "mcp.example"):
        assert canary not in str(exc.value)
        assert canary not in rendered
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True
    assert "OSError" in str(exc.value) and phase.split("/")[0] in str(exc.value)


def test_the_sanitized_catalog_cause_is_this_harness_own_message():
    """The malformed-catalog wrapper keeps its cause: that cause is our own sanitized error."""
    with pytest.raises(PreflightTransportError) as exc:
        preflight_mcp(
            "https://mcp.example/ckbai", "1.6.12",
            client=FakeMcpClient(tools=[{"name": "secret-tool-name", "description": 42}]),
        )
    rendered = _formatted(exc.value)
    assert isinstance(exc.value.__cause__, McpSurfaceError)
    for canary in ("secret-tool-name", "mcp.example"):
        assert canary not in rendered
