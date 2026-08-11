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
import re
from dataclasses import dataclass, field

import requests

PROTOCOL_VERSION = "2025-06-18"
_ACCEPT = "application/json, text/event-stream"


class McpError(RuntimeError):
    """Raised when the MCP server returns a JSON-RPC error or a malformed response."""


# SSE frames are separated by CR and/or LF only. `str.splitlines()` also breaks on Unicode
# separators such as U+0085, which a payload character can legitimately contain, so it must not be
# used to find frame boundaries.
_SSE_LINE_SPLIT = re.compile(r"\r\n|\r|\n")
_ERROR_MESSAGE_CHARS = 200


def _decode_utf8(raw: bytes) -> str:
    """MCP bodies are UTF-8. `text/event-stream` carries no charset, so requests would otherwise
    fall back to ISO-8859-1 and mis-decode multi-byte characters."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpError(f"response was not valid UTF-8: {exc.reason}") from None


def _loads_envelope(payload: str, where: str) -> dict:
    try:
        env = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise McpError(f"malformed JSON in {where}: {exc.msg}") from None
    if not isinstance(env, dict):
        raise McpError(f"{where} is not a JSON-RPC object")
    return env


def _format_rpc_error(err: object) -> str:
    """Bounded rendering: never echo `error.data`, an arbitrary object, or an unbounded code."""
    if not isinstance(err, dict):
        return "error (malformed error object)"
    raw_code = err.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else "(invalid)"
    message = str(err.get("message", ""))[:_ERROR_MESSAGE_CHARS]
    return f"error {code}: {message}"


def _parse_sse_or_json(text: str) -> dict:
    """The server replies as `text/event-stream` with one `data: {...}` line (it may
    also reply as plain JSON). Return the decoded JSON-RPC envelope either way."""
    for line in _SSE_LINE_SPLIT.split(text):
        line = line.strip(" \t")
        if line.startswith("data:"):
            return _loads_envelope(line[len("data:"):].strip(" \t"), "SSE data frame")
    return _loads_envelope(text.strip(" \t"), "response body")


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
        request_id = self._id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        resp = self._session.post(
            self.url,
            headers={"Content-Type": "application/json", "Accept": _ACCEPT},
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        env = _parse_sse_or_json(_decode_utf8(resp.content))
        if env.get("jsonrpc") != "2.0":
            raise McpError(f"{method} -> envelope is not JSON-RPC 2.0")
        # JSON-RPC ids are numbers or strings; Python would otherwise accept True as 1.
        env_id = env.get("id")
        if isinstance(env_id, bool) or not isinstance(env_id, int) or env_id != request_id:
            raise McpError(f"{method} -> response id does not match the request")
        has_result, has_error = "result" in env, "error" in env
        if has_result == has_error:
            raise McpError(f"{method} -> envelope must carry exactly one of result or error")
        if has_error:
            raise McpError(f"{method} -> {_format_rpc_error(env['error'])}")
        result = env["result"]
        if not isinstance(result, dict):
            raise McpError(f"{method} -> result is {type(result).__name__}, expected an object")
        return result

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

    def read_resource(self, uri: str) -> dict:
        """Read one MCP resource by URI. Returns the result dict: {contents: [...]}."""
        return self._rpc("resources/read", {"uri": uri})

    @staticmethod
    def resource_text(result: object) -> str | None:
        """Join a resources/read result's text bodies in order; None when none are usable.

        Defensive about shape: malformed server data must not raise out of the caller's failed
        observation boundary, and a whitespace-only block is not a usable body.
        """
        if not isinstance(result, dict):
            return None
        contents = result.get("contents")
        if not isinstance(contents, list):
            return None
        parts = [
            c["text"] for c in contents
            if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip()
        ]
        return "\n".join(parts) if parts else None

    @staticmethod
    def result_text(result: dict) -> str:
        """Flatten an MCP tool result's content blocks into plain text."""
        parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts)
