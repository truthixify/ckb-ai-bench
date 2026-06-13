"""Direct CKB JSON-RPC client for the Verifier (ADR-0005).

The Verifier grades by DIRECT RPC to the chain, never the MCP server under test.
Uses an injectable callable seam so unit tests mock RPC without network I/O.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

RpcCallable = Callable[[str, list[Any]], Any]

DEFAULT_RPC_TIMEOUT = 30.0


def make_rpc_client(rpc_url: str, *, timeout: float = DEFAULT_RPC_TIMEOUT) -> RpcCallable:
    """Build a direct CKB JSON-RPC client bound to ``rpc_url``.

    ``timeout`` bounds each request so verify cannot hang forever on a slow node.
    """

    def call(method: str, params: list[Any]) -> Any:
        body = json.dumps({"id": 1, "jsonrpc": "2.0", "method": method, "params": params}).encode()
        req = urllib.request.Request(
            rpc_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise RuntimeError(f"RPC {method} to {rpc_url} failed: {exc}") from exc
        if "error" in payload:
            raise RuntimeError(f"RPC {method} error: {payload['error']}")
        return payload["result"]

    return call