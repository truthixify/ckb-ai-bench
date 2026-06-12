"""Minimal native MCP client for the CKB AI benchmark agent.

Speaks Streamable HTTP MCP (protocol 2025-06-18) directly: POST JSON-RPC, read the
single SSE `data:` line back. The ckb-ai-mcp server is stateless (it issues no
Mcp-Session-Id and does not require the `initialized` notification), so this client
needs no session tracking -- every call is an independent POST.

This is the piece that mini-swe-agent lacks. It is deliberately dependency-light
(stdlib + requests) so it is trivial to pin and audit.

Verified against the live server https://mcp.ckbdev.com/ckbai (ckb-ai-mcp v1.6.12).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import requests

PROTOCOL_VERSION = "2025-06-18"
_ACCEPT = "application/json, text/event-stream"


class McpError(RuntimeError):
    """Raised when the MCP server returns a JSON-RPC error or a malformed response."""


def _parse_sse_or_json(text: str) -> dict:
    """The server replies as `text/event-stream` with one `data: {...}` line (it may
    also reply as plain JSON). Return the decoded JSON-RPC envelope either way."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    # Fall back to treating the whole body as JSON.
    return json.loads(text)


@dataclass
class CkbMcpClient:
    """A native Streamable-HTTP MCP client scoped to one server URL.

    url:     the MCP endpoint, e.g. https://mcp.ckbdev.com/ckbai
    timeout: per-request timeout in seconds
    """

    url: str
    timeout: float = 60.0
    client_name: str = "ckb-bench-agent"
    client_version: str = "0.0.1"
    _id: int = field(default=0, init=False, repr=False)
    _session: requests.Session = field(default_factory=requests.Session, init=False, repr=False)

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        resp = self._session.post(
            self.url,
            headers={"Content-Type": "application/json", "Accept": _ACCEPT},
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        env = _parse_sse_or_json(resp.text)
        if "error" in env:
            raise McpError(f"{method} -> {env['error']}")
        return env.get("result", {})

    def initialize(self) -> dict:
        """Perform the MCP initialize handshake. Returns serverInfo + capabilities."""
        return self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": self.client_version},
            },
        )

    def list_tools(self) -> list[dict]:
        """Return the full tool list (name, description, inputSchema)."""
        return self._rpc("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Call a tool. Returns the MCP result dict: {content: [...], isError: bool}."""
        return self._rpc("tools/call", {"name": name, "arguments": arguments or {}})

    @staticmethod
    def result_text(result: dict) -> str:
        """Flatten an MCP tool result's content blocks into plain text."""
        parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts)
