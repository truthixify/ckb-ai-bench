"""Preflight tests: ADR-0010 version pin and deferred-loading surface (no network)."""

from __future__ import annotations

import pytest

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


def test_preflight_missing_search_tools_fails():
    with pytest.raises(PreflightTransportError, match="search_tools"):
        preflight_mcp(
            "https://mcp.example/ckbai",
            "1.6.12",
            client=FakeMcpClient(tools=[{"name": "search_resources"}]),
        )


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


def test_preflight_skips_non_dict_tool_entries():
    result = preflight_mcp(
        "https://mcp.example/ckbai",
        "1.6.12",
        client=FakeMcpClient(tools=["bad", {"name": "search_tools"}, {"name": "search_resources"}]),
    )
    assert result.tool_count == 3


def test_preflight_default_client_import(monkeypatch):
    import sys
    from types import ModuleType

    fake_mod = ModuleType("ckb_mcp")
    fake_mod.CkbMcpClient = lambda url: FakeMcpClient()
    monkeypatch.setitem(sys.modules, "ckb_mcp", fake_mod)
    result = preflight_mcp("https://mcp.example/ckbai", "1.6.12")
    assert result.server_version == "1.6.12"