"""Direct CKB JSON-RPC for the Verifier (ADR-0005): re-exports the shared client.

The Verifier grades by DIRECT RPC to the chain, never the MCP server under test. The single
implementation lives in ckbbench.ckb_rpc (shared with the run-params pre-step); this module
re-exports it so verify/* import paths stay local and stable.
"""

from __future__ import annotations

from ckbbench.ckb_rpc import DEFAULT_RPC_TIMEOUT, RpcCallable, make_rpc_client, rpc_hex_int

__all__ = ["DEFAULT_RPC_TIMEOUT", "RpcCallable", "make_rpc_client", "rpc_hex_int"]
