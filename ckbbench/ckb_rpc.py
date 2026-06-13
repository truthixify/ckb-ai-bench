"""Direct CKB JSON-RPC client, shared by the run-params pre-step and the Verifier.

There is exactly ONE direct-RPC client in the harness (DRY): the pre-step draws the harness_tip
(suite/runparams) and the Verifier grades on-chain Proofs (verify/onchain) both by DIRECT RPC,
never the MCP server under test. The client uses an injectable callable seam so unit tests mock
RPC without network I/O.

CKB JSON-RPC returns numeric fields (capacity, block number, tip, epoch number) as 0x-prefixed
hex strings; use ``rpc_hex_int`` to parse them, NOT a bare ``int()`` (which assumes base 10).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

RpcCallable = Callable[[str, list[Any]], Any]

DEFAULT_RPC_TIMEOUT = 30.0


def rpc_hex_int(value: str) -> int:
    """Parse a CKB RPC 0x-hex numeric field (capacity, number, epoch) to int."""
    return int(value, 16)


def make_rpc_client(rpc_url: str, *, timeout: float = DEFAULT_RPC_TIMEOUT) -> RpcCallable:
    """Build a direct CKB JSON-RPC client bound to ``rpc_url``.

    ``timeout`` bounds each request so a caller (the pre-step gating a run, or verify) cannot
    hang forever on a slow or unreachable node.
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
