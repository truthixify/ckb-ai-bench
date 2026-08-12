"""On-chain Task grading by direct CKB RPC (ADR-0001, ADR-0009).

Stateless integrity: run-bound lower bounds, verifier-private amount-as-nonce, and
structural assertions. Integrity inputs (harness_tip, nonce, recipient) are read
ONLY from ``verifier_private``, never from agent-written data. Proofs are bound to the run they
were produced in rather than to a verify-time age window, so an honest early observation stays
valid for the whole cell.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from ckbbench.suite.model import OnchainVerifierSpec
from ckbbench.verify.rpc import RpcCallable, rpc_hex_int

SECP_CODE_HASH = "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8"
# A CKB Script identity is code_hash + hash_type + args; the standard secp256k1-blake160 lock is a
# type script, so comparing code_hash alone would accept a different Script.
SECP_HASH_TYPE = "type"
_HASH_TYPES = frozenset({"data", "type", "data1", "data2"})

TX_CONFIRM_BUDGET_SECONDS = 90.0
TX_POLL_INTERVAL_SECONDS = 2.0

_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
# Canonical CKB JSON-RPC unsigned quantity: no leading zeroes, so `0x01` is not a tip.
_RPC_QUANTITY_RE = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]*)")
_BLOCK_HASH_RE = re.compile(r"0x[0-9a-f]{64}")
_TX_PENDING_STATUSES = frozenset({"pending", "proposed"})
_TX_KNOWN_STATUSES = frozenset({"pending", "proposed", "rejected", "committed"})


@dataclass(frozen=True)
class Verdict:
    """Per-task grade: pass/fail with a human-readable reason."""

    task_id: str
    passed: bool
    reason: str
    proof: str


class VerificationInfrastructureError(RuntimeError):
    """The independent verification channel failed, so no trustworthy grade can be produced.

    This is NOT a task result. It is raised only when the RPC callable faults or when a node
    response cannot be interpreted well enough to grade. A valid observation showing the agent did
    not satisfy the prompt -- not found, rejected, stale, structurally wrong -- stays a failed
    Verdict. Messages carry the method and failure class only: never a proof, response body, or
    verifier-private value.
    """


def _norm(proof: str) -> str:
    return (proof or "").strip().strip('"').lower()


def _fail(task_id: str, proof: str, reason: str) -> Verdict:
    return Verdict(task_id=task_id, passed=False, reason=reason, proof=proof)


def _pass(task_id: str, proof: str, reason: str) -> Verdict:
    return Verdict(task_id=task_id, passed=True, reason=reason, proof=proof)


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


def _observe(rpc: RpcCallable, method: str, params: list[Any]) -> Any:
    """Call the injected RPC, converting any transport fault into the dedicated exception."""
    try:
        return rpc(method, params)
    except VerificationInfrastructureError:
        raise
    except Exception as exc:
        raise VerificationInfrastructureError(
            f"{method} observation failed: {type(exc).__name__}"
        ) from exc


def _tx_status(txw: Any) -> str | None:
    """Status of an observed transaction, or None when the node validly reports not-found.

    A response we cannot interpret is not a negative result about the agent, so it raises rather
    than becoming a failed Verdict.
    """
    if txw is None:
        return None
    if not isinstance(txw, dict):
        raise VerificationInfrastructureError("get_transaction returned a non-object response")
    envelope = txw.get("tx_status")
    if not isinstance(envelope, dict):
        raise VerificationInfrastructureError("get_transaction response has no tx_status object")
    status = envelope.get("status")
    if not isinstance(status, str):
        raise VerificationInfrastructureError("get_transaction tx_status has no string status")
    if status not in _TX_KNOWN_STATUSES:
        # The value is response content, so it is classified but never echoed into the message.
        raise VerificationInfrastructureError(
            "get_transaction reported an unrecognized status"
        )
    return status


def _await_tx_status(
    tx_id: str,
    rpc: RpcCallable,
    monotonic_fn: Callable[[], float],
    sleep_fn: Callable[[float], Any],
) -> tuple[str | None, Any]:
    """Observe the transaction, polling pending/proposed against one fixed monotonic deadline.

    The deadline is created once, before the first observation, and is never reset or extended by a
    status change. A read may still land exactly on the deadline; no sleep may run past it.
    """
    deadline = monotonic_fn() + TX_CONFIRM_BUDGET_SECONDS
    while True:
        txw = _observe(rpc, "get_transaction", [tx_id])
        status = _tx_status(txw)
        if status not in _TX_PENDING_STATUSES:
            return status, txw
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            return status, txw
        sleep_fn(min(TX_POLL_INTERVAL_SECONDS, remaining))


def _committed_outputs(txw: dict) -> list:
    tx = txw.get("transaction")
    if not isinstance(tx, dict):
        raise VerificationInfrastructureError("committed response has no transaction object")
    outputs = tx.get("outputs")
    if not isinstance(outputs, list):
        raise VerificationInfrastructureError("committed transaction has no output list")
    return outputs


def _pays_recipient(output: Any, recipient_args: str) -> bool:
    """True when this output is a standard-lock payment to the verifier-private recipient.

    An output we cannot read is malformed grading data; an output that simply pays someone else is
    an ordinary negative.
    """
    if not isinstance(output, dict):
        raise VerificationInfrastructureError("transaction output is not an object")
    lock = output.get("lock")
    if not isinstance(lock, dict):
        raise VerificationInfrastructureError("transaction output has no lock object")
    code_hash, hash_type, args = lock.get("code_hash"), lock.get("hash_type"), lock.get("args")
    if not all(isinstance(f, str) for f in (code_hash, hash_type, args)):
        raise VerificationInfrastructureError(
            "transaction output lock is missing code_hash, hash_type, or args"
        )
    if hash_type not in _HASH_TYPES:
        raise VerificationInfrastructureError(
            "transaction output lock has an unrecognized hash_type"
        )
    return (
        code_hash.lower() == SECP_CODE_HASH.lower()
        and hash_type == SECP_HASH_TYPE
        and args.lower() == recipient_args
    )


def _hex_field(container: Any, key: str, where: str) -> int:
    # capacity and header numbers are 0x-hex strings on the real chain; a bare int() would assume
    # base 10 and misread live data.
    try:
        return rpc_hex_int(container[key])
    except VerificationInfrastructureError:
        raise
    except Exception as exc:
        raise VerificationInfrastructureError(f"{where} is not a decodable hex number") from exc


def _run_lower_bound(verifier_private: dict[str, Any]) -> int:
    """Run-start tip from verifier-private state only.

    A correctly authored Task 01 always declares this schema entry, so its absence is harness
    misconfiguration and must not be scored against the agent. bool is rejected explicitly because
    Python makes it an int subclass.
    """
    value = verifier_private.get("harness_tip")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VerificationInfrastructureError(
            "verifier-private harness_tip is missing or not a non-negative integer"
        )
    return value


def _observed_quantity(rpc: RpcCallable, method: str, params: list[Any]) -> int:
    raw = _observe(rpc, method, params)
    if not isinstance(raw, str) or not _RPC_QUANTITY_RE.fullmatch(raw.lower()):
        raise VerificationInfrastructureError(f"{method} did not return a canonical hex quantity")
    return int(raw, 16)


def _observed_block_hash(rpc: RpcCallable, height: int) -> str:
    raw = _observe(rpc, "get_block_hash", [hex(height)])
    if not isinstance(raw, str) or not _BLOCK_HASH_RE.fullmatch(raw.lower()):
        raise VerificationInfrastructureError("get_block_hash did not return a 32-byte block hash")
    return raw.lower()


def check_tip_block_identity(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
) -> Verdict:
    """Proof must be a tip observed during THIS run plus the block hash at exactly that height.

    The run-start lower bound ties the height to this cell and the paired hash makes a guessed
    height insufficient. There is deliberately no upper age or block-distance limit: an honest
    observation taken early in a long cell must not expire because the chain kept mining.
    """
    del spec
    values = [_identity_value(ln) for ln in _identity_lines(proof_text) if ln.strip()]
    if len(values) != 2:
        return _fail(
            task_id, proof_text, f"malformed proof: expected 2 non-blank lines, found {len(values)}"
        )
    tip_text, hash_text = values
    if not _RPC_QUANTITY_RE.fullmatch(tip_text):
        return _fail(task_id, proof_text, "malformed proof: line 1 is not a canonical 0x tip")
    if not _BLOCK_HASH_RE.fullmatch(hash_text):
        return _fail(task_id, proof_text, "malformed proof: line 2 is not a 32-byte block hash")
    proof_tip = int(tip_text, 16)

    harness_tip = _run_lower_bound(verifier_private)
    verify_tip = _observed_quantity(rpc, "get_tip_block_number", [])
    if proof_tip > verify_tip:
        return _fail(
            task_id, proof_text, f"tip {proof_tip} is in the FUTURE of verify-time tip {verify_tip}"
        )
    if proof_tip < harness_tip:
        # The run-start height is verifier-private and the reason is persisted in the result, so
        # only the agent's own value may appear here.
        return _fail(task_id, proof_text, f"tip {proof_tip} predates run start")

    observed = _observed_block_hash(rpc, proof_tip)
    if observed != hash_text:
        return _fail(task_id, proof_text, f"block hash mismatch at tip {proof_tip}")
    return _pass(
        task_id, proof_text, f"tip {hex(proof_tip)} is run-bound and its block hash matches"
    )


def check_tx_proof(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
    *,
    monotonic_fn: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], Any] | None = None,
) -> Verdict:
    """EXISTS + FRESH + STRUCTURE for a committed tx (ADR-0001, verify-proof.js spike).

    A valid observation that the agent did not deliver is a failed Verdict; an inability to obtain
    or interpret that observation raises VerificationInfrastructureError instead of charging the
    model for the harness.
    """
    del spec
    tx_id = (proof_text or "").strip()
    if not tx_id:
        return _fail(task_id, proof_text, "proof is empty")
    if not _TX_HASH_RE.fullmatch(tx_id):
        return _fail(
            task_id,
            proof_text,
            "proof is not a single 0x-prefixed 32-byte transaction hash",
        )

    private = _tx_private(verifier_private)
    if isinstance(private, str):
        return _fail(task_id, proof_text, private)
    harness_tip, nonce_shannons, recipient_args = private

    status, txw = _await_tx_status(
        tx_id,
        rpc,
        monotonic_fn or time.monotonic,
        sleep_fn or time.sleep,
    )
    if status is None:
        return _fail(task_id, proof_text, "transaction not found on chain")
    if status != "committed":
        return _fail(task_id, proof_text, f"tx status is {status!r}, not committed")

    block_hash = txw["tx_status"].get("block_hash")
    if not isinstance(block_hash, str) or not block_hash:
        raise VerificationInfrastructureError("committed tx_status has no usable block_hash")
    header = _observe(rpc, "get_header", [block_hash])
    if not isinstance(header, dict):
        raise VerificationInfrastructureError("get_header returned a non-object response")
    block_number = _hex_field(header, "number", "get_header number")
    if block_number < harness_tip:
        return _fail(
            task_id,
            proof_text,
            f"STALE: tx block {block_number} < harness tip {harness_tip} (tx predates the run)",
        )

    to_recipient = [o for o in _committed_outputs(txw) if _pays_recipient(o, recipient_args)]
    if len(to_recipient) != 1:
        return _fail(
            task_id,
            proof_text,
            f"STRUCTURE: expected exactly 1 output to {recipient_args}, found {len(to_recipient)}",
        )
    cap = _hex_field(to_recipient[0], "capacity", "transaction output capacity")
    if cap != nonce_shannons:
        return _fail(
            task_id,
            proof_text,
            f"STRUCTURE: output to recipient is {cap} shannons, not the nonce {nonce_shannons}",
        )

    return _pass(
        task_id,
        proof_text,
        "exists + fresh + exactly-one-nonce-output structure",
    )


_ONCHAIN_CHECKS: dict[str, Callable[..., Verdict]] = {
    "tip_block_identity": check_tip_block_identity,
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
    *,
    monotonic_fn: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], Any] | None = None,
) -> Verdict:
    """Dispatch an on-chain check by ``spec.check`` with per-check failure isolation.

    Only ``tx_proof`` polls, so only it receives the time seams; every other checker keeps its
    existing signature.
    """
    checker = _ONCHAIN_CHECKS.get(spec.check)
    if checker is None:
        return _fail(task_id, proof_text, f"unknown on-chain check {spec.check!r}")
    if checker is check_tx_proof:
        return checker(
            task_id,
            proof_text,
            spec,
            verifier_private,
            rpc,
            monotonic_fn=monotonic_fn,
            sleep_fn=sleep_fn,
        )
    return checker(task_id, proof_text, spec, verifier_private, rpc)
