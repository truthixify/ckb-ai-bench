"""On-chain Task grading by direct CKB RPC (ADR-0001, ADR-0009).

Stateless integrity: run-bound lower bounds, verifier-private amount-as-nonce, and
structural assertions. Integrity inputs (harness_tip, nonce, recipient) are read
ONLY from ``verifier_private``, never from agent-written data. Proofs are bound to the run they
were produced in rather than to a verify-time age window, so an honest early observation stays
valid for the whole cell.
"""

from __future__ import annotations

import hashlib
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

# Canonical Type-ID identity (CKB core 17d7db5bb423a1b2177e14a132a41d5a91a515f3).
TYPE_ID_CODE_HASH = "0x00000000000000000000000000000000000000000000000000545950455f4944"
TYPE_ID_HASH_TYPE = "type"
R1_CAPACITY_SHANNONS = 20_000_000_000
_HASH_TYPE_BYTE = {"data": 0, "type": 1, "data1": 2, "data2": 4}
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
    if status == "unknown":
        # Empirically the pinned nervos/ckb v0.207.0 stack reports a transaction it has never seen
        # (e.g. after a DevNet reset) as status "unknown" with no body. That is a valid negative
        # observation, not an uninterpretable one. An "unknown" carrying a body is still malformed.
        if txw.get("transaction") is None:
            return None
        raise VerificationInfrastructureError(
            "get_transaction reported unknown status with a transaction body"
        )
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


def ckb_blake2b(data: bytes) -> bytes:
    """CKB's hash: BLAKE2b-256 personalized with ``ckb-default-hash``."""
    return hashlib.blake2b(data, digest_size=32, person=b"ckb-default-hash").digest()


_WIRE_BYTES_RE = re.compile(r"0x(?:[0-9a-fA-F]{2})*")


def _hex_bytes(value: Any, want_len: int, where: str) -> bytes:
    """Strict 0x byte string. bytes.fromhex() alone would silently ignore embedded whitespace."""
    if not isinstance(value, str) or not _WIRE_BYTES_RE.fullmatch(value):
        raise VerificationInfrastructureError(f"{where} is not a canonical 0x byte string")
    raw = bytes.fromhex(value[2:])
    if want_len >= 0 and len(raw) != want_len:
        raise VerificationInfrastructureError(f"{where} is not {want_len} bytes")
    return raw


def _wire_quantity(container: Any, key: str, where: str, bits: int) -> int:
    """Canonical CKB unsigned quantity: lowercase 0x, no sign, no leading zeroes, width-bounded."""
    if not isinstance(container, dict):
        raise VerificationInfrastructureError(f"{where} container is not an object")
    value = container.get(key)
    # Match the original: lowering first would accept "0X0" and uppercase digits, which are not
    # canonical CKB wire quantities. Proof-text normalization is a separate concern.
    if not isinstance(value, str) or not _RPC_QUANTITY_RE.fullmatch(value):
        raise VerificationInfrastructureError(f"{where} is not a canonical hex quantity")
    parsed = int(value, 16)
    if parsed >= 1 << bits:
        raise VerificationInfrastructureError(f"{where} exceeds its {bits}-bit range")
    return parsed


def type_id_args(input0: Any, output_index: int) -> bytes:
    """Canonical Type-ID args: blake2b(CellInput struct bytes || u64_le(output_index)).

    The CellInput Molecule struct is fixed-size: since || previous_output.tx_hash || index.
    """
    if not isinstance(input0, dict):
        raise VerificationInfrastructureError("transaction input 0 is not an object")
    out_point = input0.get("previous_output")
    if not isinstance(out_point, dict):
        raise VerificationInfrastructureError("input 0 has no previous_output object")
    since = _wire_quantity(input0, "since", "input 0 since", 64)
    tx_hash = _hex_bytes(out_point.get("tx_hash"), 32, "input 0 previous_output.tx_hash")
    index = _wire_quantity(out_point, "index", "input 0 previous_output.index", 32)
    packed = since.to_bytes(8, "little") + tx_hash + index.to_bytes(4, "little")
    return ckb_blake2b(packed + output_index.to_bytes(8, "little"))


def molecule_script(code_hash: bytes, hash_type: str, args: bytes) -> bytes:
    """Canonical Molecule ``Script`` table bytes (blockchain.mol).

    Script = table(code_hash: Byte32, hash_type: byte, args: Bytes); the three field offsets are
    fixed at 16/48/49 because the first two fields are fixed-size.
    """
    if len(code_hash) != 32:
        raise VerificationInfrastructureError("script code_hash is not 32 bytes")
    if hash_type not in _HASH_TYPE_BYTE:
        raise VerificationInfrastructureError("script hash_type is not a recognized CKB value")
    args_field = len(args).to_bytes(4, "little") + args
    total = 16 + 32 + 1 + len(args_field)
    return (
        total.to_bytes(4, "little")
        + (16).to_bytes(4, "little")
        + (48).to_bytes(4, "little")
        + (49).to_bytes(4, "little")
        + code_hash
        + bytes([_HASH_TYPE_BYTE[hash_type]])
        + args_field
    )


def script_hash(script: Any, where: str) -> tuple[bytes, str, bytes, bytes]:
    """Return (code_hash, hash_type, args, ckb script hash) for an observed JSON Script."""
    if not isinstance(script, dict):
        raise VerificationInfrastructureError(f"{where} is not a script object")
    hash_type = script.get("hash_type")
    if not isinstance(hash_type, str):
        raise VerificationInfrastructureError(f"{where} hash_type is not a string")
    code_hash = _hex_bytes(script.get("code_hash"), 32, f"{where} code_hash")
    args = _hex_bytes(script.get("args"), -1, f"{where} args")
    return code_hash, hash_type, args, ckb_blake2b(molecule_script(code_hash, hash_type, args))


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


def _private_hex(private: dict[str, Any], key: str, nybbles: int) -> str:
    """Canonical lowercase 0x hex from verifier-private state. Never echoes the value."""
    value = private.get(key)
    if not isinstance(value, str) or not re.fullmatch(rf"0x[0-9a-fA-F]{{{nybbles}}}", value):
        raise VerificationInfrastructureError(
            f"verifier-private {key} is missing or not a canonical {nybbles // 2}-byte hex value"
        )
    return value.lower()


def check_type_id_data_cell(
    task_id: str,
    proof_text: str,
    spec: OnchainVerifierSpec,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
    *,
    monotonic_fn: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], Any] | None = None,
) -> Verdict:
    """Proof is a committed deployment tx hash plus the Type-ID Script hash of its output 0.

    The Script hash is recomputed from the observed Script bytes; a node-reported hash is never
    trusted. Type-ID args are re-derived from input 0 and output index 0, so a well-formed but
    wrongly authored deployment fails as an ordinary task failure.
    """
    del spec
    values = [_identity_value(ln) for ln in _identity_lines(proof_text) if ln.strip()]
    if len(values) != 2:
        return _fail(
            task_id, proof_text, f"malformed proof: expected 2 non-blank lines, found {len(values)}"
        )
    tx_id, claimed_hash = values
    for label, value in (("line 1 tx hash", tx_id), ("line 2 script hash", claimed_hash)):
        if not _BLOCK_HASH_RE.fullmatch(value):
            return _fail(task_id, proof_text, f"malformed proof: {label} is not a 32-byte hex value")

    harness_tip = _run_lower_bound(verifier_private)
    want_payload = _private_hex(verifier_private, "expected_payload_hex", 64)
    want_recipient = _private_hex(verifier_private, "expected_recipient_args", 40)

    status, txw = _await_tx_status(
        tx_id, rpc, monotonic_fn or time.monotonic, sleep_fn or time.sleep
    )
    if status is None:
        return _fail(task_id, proof_text, "transaction not found on chain")
    if status != "committed":
        return _fail(task_id, proof_text, f"tx status is {status!r}, not committed")

    block_hash = txw["tx_status"].get("block_hash")
    _hex_bytes(block_hash, 32, "committed tx_status block_hash")
    header = _observe(rpc, "get_header", [block_hash])
    if not isinstance(header, dict):
        raise VerificationInfrastructureError("get_header returned a non-object response")
    block_number = _wire_quantity(header, "number", "get_header number", 64)
    if block_number < harness_tip:
        # harness_tip is verifier-private and this reason is persisted in the result row.
        return _fail(task_id, proof_text, f"STALE: tx block {block_number} predates run start")

    tx = txw.get("transaction")
    if not isinstance(tx, dict):
        raise VerificationInfrastructureError("committed response has no transaction object")
    inputs, outputs = tx.get("inputs"), tx.get("outputs")
    data = tx.get("outputs_data")
    if not isinstance(inputs, list) or not inputs:
        raise VerificationInfrastructureError("committed transaction has no input list")
    if not isinstance(outputs, list) or not isinstance(data, list):
        raise VerificationInfrastructureError("committed transaction has no output/data lists")
    if len(outputs) != len(data):
        raise VerificationInfrastructureError("outputs and outputs_data lengths differ")
    if not outputs:
        return _fail(task_id, proof_text, "transaction has no outputs")

    # Order is load-bearing: input 0 is node-supplied observation data, so a malformed one is an
    # unusable reading and must surface as infrastructure failure. Deriving it before grading
    # output 0 stops an agent's ordinary output mistake from masking it.
    expected_args = type_id_args(inputs[0], 0)

    deployed = outputs[0]
    if not isinstance(deployed, dict):
        raise VerificationInfrastructureError("output 0 is not an object")
    type_script = deployed.get("type")
    if type_script is None:
        return _fail(task_id, proof_text, "output 0 has no type script")
    code_hash, hash_type, args, observed_hash = script_hash(type_script, "output 0 type")
    if code_hash.hex() != TYPE_ID_CODE_HASH[2:] or hash_type != TYPE_ID_HASH_TYPE:
        return _fail(task_id, proof_text, "output 0 type script is not canonical Type-ID")
    if len(args) != 32:
        return _fail(task_id, proof_text, "output 0 Type-ID args are not 32 bytes")
    if args != expected_args:
        return _fail(
            task_id, proof_text, "output 0 Type-ID args do not derive from input 0 and index 0"
        )

    # Exactly one canonical Type-ID output, and it must be the deployment at index 0. Unrelated
    # typed outputs stay allowed so ordinary funding change is not penalized.
    for i, other in enumerate(outputs[1:], start=1):
        if not isinstance(other, dict):
            raise VerificationInfrastructureError(f"output {i} is not an object")
        extra = other.get("type")
        if extra is None:
            continue
        e_code, e_hash_type, _e_args, _e = script_hash(extra, f"output {i} type")
        if e_code.hex() == TYPE_ID_CODE_HASH[2:] and e_hash_type == TYPE_ID_HASH_TYPE:
            return _fail(task_id, proof_text, f"a second canonical Type-ID output exists at index {i}")

    # Undecodable wire data is an observation failure; a decodable but wrong/short/long payload is
    # an ordinary agent mistake.
    payload_bytes = _hex_bytes(data[0], -1, "outputs_data[0]")
    payload = "0x" + payload_bytes.hex()
    if payload != want_payload:
        return _fail(task_id, proof_text, "output 0 data is not the required 32-byte payload")

    capacity = _wire_quantity(deployed, "capacity", "output 0 capacity", 64)
    if capacity != R1_CAPACITY_SHANNONS:
        return _fail(
            task_id, proof_text,
            f"output 0 capacity is {capacity} shannons, not {R1_CAPACITY_SHANNONS}",
        )

    lock_code, lock_hash_type, lock_args, _lock_hash = script_hash(deployed.get("lock"), "output 0 lock")
    if (
        lock_code.hex() != SECP_CODE_HASH[2:]
        or lock_hash_type != SECP_HASH_TYPE
        or "0x" + lock_args.hex() != want_recipient
    ):
        return _fail(task_id, proof_text, "output 0 lock is not the required recipient lock")

    if "0x" + observed_hash.hex() != claimed_hash:
        return _fail(task_id, proof_text, "reported script hash does not match the deployed type script")
    return _pass(task_id, proof_text, "type-id data cell deployed and script hash matches")


_ONCHAIN_CHECKS: dict[str, Callable[..., Verdict]] = {
    "tip_block_identity": check_tip_block_identity,
    "type_id_data_cell": check_type_id_data_cell,
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

    Only the polling checkers (``tx_proof`` and ``type_id_data_cell``) receive the time seams;
    every other checker keeps its existing signature.
    """
    checker = _ONCHAIN_CHECKS.get(spec.check)
    if checker is None:
        return _fail(task_id, proof_text, f"unknown on-chain check {spec.check!r}")
    if checker in (check_tx_proof, check_type_id_data_cell):
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
