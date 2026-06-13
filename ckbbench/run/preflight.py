"""MCP preflight: pinned version enforcement before scoring (ADR-0010).

Ports spikes/mcp-preflight/mcp-preflight.mjs to Python using the fork's
``CkbMcpClient``. Called once per MCP-enabled run before the agent wakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class McpPreflightClient(Protocol):
    """Injectable seam for tests (no network)."""

    def initialize(self) -> dict[str, Any]: ...  # pragma: no cover

    def list_tools(self) -> list[dict[str, Any]]: ...  # pragma: no cover


@dataclass(frozen=True)
class PreflightResult:
    """Successful preflight outcome (exit-code 0 equivalent)."""

    server_version: str
    server_name: str
    tool_count: int
    has_search_tools: bool
    has_search_resources: bool
    deferred_loading_documented: bool


class PreflightError(Exception):
    """Base for distinct preflight refusal reasons (maps to spike exit codes)."""

    exit_code: int = 1

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PreflightUsageError(PreflightError):
    """Missing URL or pinned version (spike exit 4)."""

    exit_code = 4


class PreflightVersionMismatch(PreflightError):
    """Server version != pin (spike exit 2, ADR-0010 hard gate)."""

    exit_code = 2


class PreflightTransportError(PreflightError):
    """Handshake/transport/shape failure (spike exit 3)."""

    exit_code = 3


def _require_usage(url: str, pinned_version: str) -> None:
    if not url:
        raise PreflightUsageError("no MCP_URL")
    if not pinned_version:
        raise PreflightUsageError("no MCP_PINNED_VERSION")


def preflight_mcp(
    url: str,
    pinned_version: str,
    *,
    client: McpPreflightClient | None = None,
) -> PreflightResult:
    """Assert MCP server version equals the pin and deferred-loading tools are present.

    Raises ``PreflightVersionMismatch`` on version drift (ADR-0010), distinct transport
    errors on handshake failure, and ``PreflightUsageError`` on missing config.
    """
    _require_usage(url, pinned_version)

    if client is None:
        from ckb_mcp import CkbMcpClient

        client = CkbMcpClient(url=url)

    try:
        init = client.initialize()
    except Exception as exc:
        raise PreflightTransportError(f"cannot reach {url} (initialize): {exc}") from exc

    server_info = init.get("serverInfo") if isinstance(init, dict) else None
    if not isinstance(server_info, dict):
        raise PreflightTransportError(
            f"initialize result missing serverInfo: {init!r}"
        )
    version = server_info.get("version")
    if not version:
        raise PreflightTransportError(
            f"initialize result missing serverInfo.version: {init!r}"
        )
    server_name = str(server_info.get("name") or "(unnamed)")

    if version != pinned_version:
        raise PreflightVersionMismatch(
            f'MCP version mismatch: server reports "{version}", suite pins "{pinned_version}". '
            "Refusing to score against the wrong server."
        )

    try:
        tools = client.list_tools()
    except Exception as exc:
        raise PreflightTransportError(f"cannot reach {url} (tools/list): {exc}") from exc

    names = {t.get("name") for t in tools if isinstance(t, dict)}
    has_search_tools = "search_tools" in names
    has_search_resources = "search_resources" in names
    if not has_search_tools or not has_search_resources:
        raise PreflightTransportError(
            "deferred-loading signal missing: search_tools/search_resources not in tools/list"
        )

    instructions = init.get("instructions") if isinstance(init, dict) else None
    deferred_loading_documented = (
        isinstance(instructions, str) and "deferred loading" in instructions.lower()
    )

    return PreflightResult(
        server_version=str(version),
        server_name=server_name,
        tool_count=len(tools),
        has_search_tools=has_search_tools,
        has_search_resources=has_search_resources,
        deferred_loading_documented=deferred_loading_documented,
    )