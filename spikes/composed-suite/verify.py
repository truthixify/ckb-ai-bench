"""Spike (NOT production): the independent per-task Verifier (ADR-0008 + project rule).

Grades each Task's Proof INDEPENDENTLY, by DIRECT CKB RPC -- never the MCP server (the
verifier must not depend on the thing under test). Each check is self-contained: one
task failing does not affect another's grade (failure isolation).

The verifier targets the chain purely by RPC URL, so DevNet and TestNet verification
differ only by URL (ADR-0005 symmetry). Here the testnet archive node is used.
"""

from __future__ import annotations

import json
import os
import urllib.request

# Direct CKB RPC (NOT the MCP). The testnet archive node from the inventory.
RPC_URL = os.getenv("VERIFY_RPC", "http://192.168.0.73:18114")


def _rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["result"]


def _norm(s: str) -> str:
    return (s or "").strip().strip('"').lower()


def _check_tip(proof: str) -> tuple[bool, str]:
    # Freshness window: the proof tip must be <= the current tip and within a sane window
    # of it (the chain advances between agent-read and verify). We assert it parses as the
    # same kind of value and is not in the future.
    try:
        got = int(_norm(proof), 16)
    except ValueError:
        return False, "proof is not a hex number"
    now = int(_rpc("get_tip_block_number", []), 16)
    if got > now:
        return False, f"proof tip {got} is in the FUTURE of verify-time tip {now}"
    if now - got > 50:
        return False, f"proof tip {got} is stale vs verify-time tip {now} (>50 blocks)"
    return True, f"tip {hex(got)} within freshness window of {hex(now)}"


def _check_epoch(proof: str) -> tuple[bool, str]:
    try:
        got = int(_norm(proof), 16)
    except ValueError:
        return False, "proof is not a hex number"
    cur = _rpc("get_current_epoch", [])
    want = int(cur["number"], 16)
    if got != want:
        return False, f"epoch {hex(got)} != current epoch {hex(want)}"
    return True, f"epoch {hex(got)} matches current epoch"


def _check_blockhash(proof: str, params: list) -> tuple[bool, str]:
    want = _norm(_rpc("get_block_hash", [hex(params[0])]))
    got = _norm(proof)
    if got != want:
        return False, f"hash {got[:18]}... != block {params[0]} hash {want[:18]}..."
    return True, f"hash matches block {params[0]}"


def verify_one(meta: dict, mount) -> dict:
    proof_path = mount / meta["proof_file"]
    if not proof_path.exists():
        return {"id": meta["id"], "proof": "", "pass": False, "reason": "proof file missing"}
    proof = proof_path.read_text().strip()
    check = meta["check"]
    try:
        if check == "tip_hex":
            ok, reason = _check_tip(proof)
        elif check == "epoch_number":
            ok, reason = _check_epoch(proof)
        elif check == "block_hash":
            ok, reason = _check_blockhash(proof, meta.get("rpc_params", [1]))
        else:
            ok, reason = False, f"unknown check {check}"
    except Exception as e:  # one task's RPC failure must not crash the others
        ok, reason = False, f"verify error: {type(e).__name__}: {e}"
    return {"id": meta["id"], "proof": proof, "pass": ok, "reason": reason}


def verify_all(metas: list, mount, _mcp_url_unused: str) -> list:
    # _mcp_url is intentionally ignored: the verifier uses direct RPC, never the MCP.
    return [verify_one(m, mount) for m in metas]
