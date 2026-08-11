"""On-chain Task grading by direct CKB RPC (ADR-0001, ADR-0009).

Stateless integrity: freshness window, verifier-private amount-as-nonce, and
structural assertions. Integrity inputs (harness_tip, nonce, recipient) are read
ONLY from ``verifier_private``, never from agent-written data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ckbbench.suite.model import OnchainVerifierSpec
from ckbbench.verify.rpc import RpcCallable, rpc_hex_int

FRESHNESS_WINDOW_BLOCKS = 50

SECP_CODE_HASH = "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8"


@dataclass(frozen=True)
class Verdict:
    """Per-task grade: pass/fail with a human-readable reason."""

    task_id: str
    passed: bool
    reason: str
    proof: str


def _norm(proof: str) -> str:
    return (proof or "").strip().strip('"').lower()


def _fail(task_id: str, proof: str, reason: str) -> Verdict:
    return Verdict(task_id=task_id, passed=False, reason=reason, proof=proof)


def _pass(task_id: str, proof: str, reason: str) -> Verdict:
    return Verdict(task_id=task_id, passed=True, reason=reason, proof=proof)


def check_tip_hex(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
) -> Verdict:
    """Proof tip must parse as hex, be <= verify-time tip, and within freshness window."""
    del spec, verifier_private
    try:
        got = int(_norm(proof_text), 16)
    except ValueError:
        return _fail(task_id, proof_text, "proof is not a hex number")
    try:
        now = int(rpc("get_tip_block_number", []), 16)
    except Exception as exc:
        return _fail(task_id, proof_text, f"verify error: {type(exc).__name__}: {exc}")
    if got > now:
        return _fail(task_id, proof_text, f"proof tip {got} is in the FUTURE of verify-time tip {now}")
    if now - got > FRESHNESS_WINDOW_BLOCKS:
        return _fail(
            task_id,
            proof_text,
            f"proof tip {got} is stale vs verify-time tip {now} (>{FRESHNESS_WINDOW_BLOCKS} blocks)",
        )
    return _pass(task_id, proof_text, f"tip {hex(got)} within freshness window of {hex(now)}")


def check_epoch_number(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
) -> Verdict:
    """Proof must equal the current epoch number."""
    del spec, verifier_private
    try:
        got = int(_norm(proof_text), 16)
    except ValueError:
        return _fail(task_id, proof_text, "proof is not a hex number")
    try:
        cur = rpc("get_current_epoch", [])
        want = int(cur["number"], 16)
    except Exception as exc:
        return _fail(task_id, proof_text, f"verify error: {type(exc).__name__}: {exc}")
    if got != want:
        return _fail(task_id, proof_text, f"epoch {hex(got)} != current epoch {hex(want)}")
    return _pass(task_id, proof_text, f"epoch {hex(got)} matches current epoch")


def check_block_hash(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
) -> Verdict:
    """Proof must equal ``get_block_hash`` for ``spec.rpc_params[0]``."""
    del verifier_private
    if not spec.rpc_params:
        return _fail(task_id, proof_text, "block_hash check requires rpc_params[0]")
    try:
        want = _norm(rpc("get_block_hash", [hex(spec.rpc_params[0])]))
    except Exception as exc:
        return _fail(task_id, proof_text, f"verify error: {type(exc).__name__}: {exc}")
    got = _norm(proof_text)
    if got != want:
        return _fail(
            task_id,
            proof_text,
            f"hash {got[:18]}... != block {spec.rpc_params[0]} hash {want[:18]}...",
        )
    return _pass(task_id, proof_text, f"hash matches block {spec.rpc_params[0]}")


def _tx_private(verifier_private: dict[str, Any]) -> tuple[int, int, str] | str:
    """Read harness_tip, nonce, recipient from verifier-private params only."""
    if "harness_tip" not in verifier_private:
        return "verifier-private missing harness_tip"
    if "nonce_amount_shannons" not in verifier_private:
        return "verifier-private missing nonce_amount_shannons"
    if "recipient_args" not in verifier_private:
        return "verifier-private missing recipient_args"
    harness_tip = int(verifier_private["harness_tip"])
    nonce_shannons = int(verifier_private["nonce_amount_shannons"])
    recipient_args = str(verifier_private["recipient_args"]).lower()
    return harness_tip, nonce_shannons, recipient_args


def check_constant_hex(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
) -> Verdict:
    """Proof must equal the constant in ``spec.rpc_params[0]`` (case-insensitive hex)."""
    del verifier_private, rpc
    if not spec.rpc_params:
        return _fail(task_id, proof_text, "constant_hex check requires rpc_params[0]")
    want = _norm(str(spec.rpc_params[0]))
    got = _norm(proof_text)
    if not got:
        return _fail(task_id, proof_text, "proof is empty")
    if got != want:
        return _fail(
            task_id,
            proof_text,
            f"constant {got[:18]}... != expected {want[:18]}...",
        )
    return _pass(task_id, proof_text, f"constant matches expected {want[:18]}...")


def _identity_value(line: str) -> str:
    """Normalize one proof line: trim, remove at most one MATCHED pair of surrounding double quotes
    (trimming inside it), lowercase. A lone or unmatched quote is not a quoted wrapper and is left in
    place, so `"type` fails rather than being silently repaired into a valid value."""
    value = line.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].strip()
    return value.lower()


def _identity_lines(proof_text: str) -> list[str]:
    """Split a proof into physical lines on LF and CRLF only.

    `str.splitlines()` also breaks on CR, VT, FF, FS, GS, RS, U+0085, U+2028 and U+2029, which would
    let a proof containing no real line break manufacture two fields out of one line.
    """
    return (proof_text or "").replace("\r\n", "\n").split("\n")


def check_script_identity(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
) -> Verdict:
    """Proof must be exactly two non-blank physical lines: expected code_hash, then expected
    hash_type.

    A code hash alone cannot be interpreted without its hash type, so both are required and compared
    exactly. This check is documentation-only: it never calls the chain.
    """
    del verifier_private, rpc
    if len(spec.rpc_params or ()) != 2:
        return _fail(task_id, proof_text, "script_identity check requires exactly 2 rpc_params")
    want_code_hash = _identity_value(str(spec.rpc_params[0]))
    want_hash_type = _identity_value(str(spec.rpc_params[1]))

    # Blankness is judged on the raw line, before quote removal, so a physically nonblank line such
    # as `""` counts as a field and fails the count instead of being silently dropped.
    values = [_identity_value(ln) for ln in _identity_lines(proof_text) if ln.strip()]
    if len(values) != 2:
        return _fail(task_id, proof_text, f"expected 2 non-empty lines, found {len(values)}")
    got_code_hash, got_hash_type = values
    if got_code_hash != want_code_hash:
        return _fail(task_id, proof_text,
                     f"code_hash {got_code_hash[:18]}... != expected {want_code_hash[:18]}...")
    if got_hash_type != want_hash_type:
        return _fail(task_id, proof_text,
                     f"hash_type {got_hash_type!r} != expected {want_hash_type!r}")
    return _pass(task_id, proof_text,
                 f"script identity matches {want_code_hash[:18]}... / {want_hash_type}")


def check_tx_proof(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
) -> Verdict:
    """EXISTS + FRESH + STRUCTURE for a committed tx (ADR-0001, verify-proof.js spike)."""
    del spec
    tx_id = proof_text.strip()
    if not tx_id:
        return _fail(task_id, proof_text, "proof is empty")

    private = _tx_private(verifier_private)
    if isinstance(private, str):
        return _fail(task_id, proof_text, private)
    harness_tip, nonce_shannons, recipient_args = private

    try:
        txw = rpc("get_transaction", [tx_id])
        if not txw or not txw.get("transaction"):
            return _fail(task_id, proof_text, "transaction not found on chain")
        status = txw.get("tx_status", {}).get("status")
        if status != "committed":
            return _fail(task_id, proof_text, f"tx status is {status!r}, not committed")

        block_hash = txw["tx_status"].get("block_hash")
        if not block_hash:
            return _fail(task_id, proof_text, "committed tx has no block_hash")
        header = rpc("get_header", [block_hash])
        block_number = int(header["number"], 16)
        if block_number < harness_tip:
            return _fail(
                task_id,
                proof_text,
                f"STALE: tx block {block_number} < harness tip {harness_tip} (tx predates the run)",
            )

        outputs = txw["transaction"]["outputs"]
        to_recipient = [
            o
            for o in outputs
            if o["lock"]["code_hash"].lower() == SECP_CODE_HASH.lower()
            and o["lock"]["args"].lower() == recipient_args
        ]
        if len(to_recipient) != 1:
            return _fail(
                task_id,
                proof_text,
                f"STRUCTURE: expected exactly 1 output to {recipient_args}, found {len(to_recipient)}",
            )
        # capacity is a 0x-hex string on the real chain; parse with the RPC hex helper, not
        # bare int() (which would assume base 10 and raise on real data).
        cap = rpc_hex_int(to_recipient[0]["capacity"])
        if cap != nonce_shannons:
            return _fail(
                task_id,
                proof_text,
                f"STRUCTURE: output to recipient is {cap} shannons, not the nonce {nonce_shannons}",
            )
    except Exception as exc:
        return _fail(task_id, proof_text, f"verify error: {type(exc).__name__}: {exc}")

    return _pass(
        task_id,
        proof_text,
        "exists + fresh + exactly-one-nonce-output structure",
    )


_ONCHAIN_CHECKS: dict[str, Callable[..., Verdict]] = {
    "tip_hex": check_tip_hex,
    "epoch_number": check_epoch_number,
    "block_hash": check_block_hash,
    "constant_hex": check_constant_hex,
    "script_identity": check_script_identity,
    "tx_proof": check_tx_proof,
}


def grade_onchain_task(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
) -> Verdict:
    """Dispatch an on-chain check by ``spec.check`` with per-check failure isolation."""
    checker = _ONCHAIN_CHECKS.get(spec.check)
    if checker is None:
        return _fail(task_id, proof_text, f"unknown on-chain check {spec.check!r}")
    return checker(task_id, proof_text, spec, verifier_private, rpc)