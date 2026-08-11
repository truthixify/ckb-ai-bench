"""On-chain verifier tests: each check passes on valid proof, fails on documented cheats."""

from __future__ import annotations

from typing import Any

import pytest

from ckbbench.suite.model import OnchainVerifierSpec
from ckbbench.verify.onchain import (
    FRESHNESS_WINDOW_BLOCKS,
    SECP_CODE_HASH,
    check_block_hash,
    check_constant_hex,
    check_epoch_number,
    check_script_identity,
    check_tip_hex,
    check_tx_proof,
    grade_onchain_task,
)


def _spec(check: str, *, rpc_params: tuple[Any, ...] = ()) -> OnchainVerifierSpec:
    return OnchainVerifierSpec(check=check, rpc_method="unused", rpc_params=rpc_params)


def _tx_private(*, harness_tip: int = 100, nonce: int = 10_000_000_100, recipient: str) -> dict:
    return {
        "harness_tip": harness_tip,
        "nonce_amount_shannons": str(nonce),
        "recipient_args": recipient,
    }


def _good_tx_rpc(recipient: str, nonce: int, *, block_number: int = 100) -> dict:
    return {
        "get_transaction": {
            "transaction": {
                "outputs": [
                    {
                        # capacity is a 0x-hex string on the real CKB chain (matches the wire
                        # format the verifier parses); decimal here would mask the parse path.
                        "capacity": hex(nonce),
                        "lock": {
                            "code_hash": SECP_CODE_HASH,
                            "args": recipient,
                        },
                    },
                    {
                        "capacity": hex(1000),
                        "lock": {"code_hash": "0xother", "args": "0xdead"},
                    },
                ],
            },
            "tx_status": {"status": "committed", "block_hash": "0xblock"},
        },
        "get_header": {"number": hex(block_number)},
    }


# --- tip_hex ---


def test_tip_hex_passes_within_window():
    rpc = lambda m, p: "0x64" if m == "get_tip_block_number" else None
    v = check_tip_hex("t1", "0x50", _spec("tip_hex"), {}, rpc)
    assert v.passed
    assert "freshness window" in v.reason


def test_tip_hex_fails_not_hex():
    v = check_tip_hex("t1", "not-hex", _spec("tip_hex"), {}, lambda m, p: "0x10")
    assert not v.passed
    assert "not a hex number" in v.reason


def test_tip_hex_fails_future_tip():
    rpc = lambda m, p: "0x10"
    v = check_tip_hex("t1", "0x20", _spec("tip_hex"), {}, rpc)
    assert not v.passed
    assert "FUTURE" in v.reason


def test_tip_hex_fails_stale_tip():
    now = 200
    rpc = lambda m, p: hex(now)
    stale = now - FRESHNESS_WINDOW_BLOCKS - 1
    v = check_tip_hex("t1", hex(stale), _spec("tip_hex"), {}, rpc)
    assert not v.passed
    assert "stale" in v.reason


def test_tip_hex_rpc_error_isolated():
    def boom(m, p):
        raise RuntimeError("node down")

    v = check_tip_hex("t1", "0x1", _spec("tip_hex"), {}, boom)
    assert not v.passed
    assert "node down" in v.reason


# --- epoch_number ---


def test_epoch_number_passes():
    rpc = lambda m, p: {"number": "0x7"} if m == "get_current_epoch" else None
    v = check_epoch_number("t2", "0x7", _spec("epoch_number"), {}, rpc)
    assert v.passed


def test_epoch_number_fails_wrong():
    rpc = lambda m, p: {"number": "0x7"}
    v = check_epoch_number("t2", "0x8", _spec("epoch_number"), {}, rpc)
    assert not v.passed
    assert "!=" in v.reason


def test_epoch_number_fails_not_hex():
    v = check_epoch_number("t2", "epoch", _spec("epoch_number"), {}, lambda m, p: {"number": "0x1"})
    assert not v.passed


def test_epoch_number_rpc_error():
    def boom(m, p):
        raise RuntimeError("epoch rpc down")

    v = check_epoch_number("t2", "0x1", _spec("epoch_number"), {}, boom)
    assert not v.passed
    assert "epoch rpc down" in v.reason


# --- block_hash ---


def test_block_hash_passes():
    rpc = lambda m, p: "0xabc123" if m == "get_block_hash" else None
    v = check_block_hash("t3", "0xAbC123", _spec("block_hash", rpc_params=(5,)), {}, rpc)
    assert v.passed


def test_block_hash_fails_wrong_hash():
    rpc = lambda m, p: "0xaaa"
    v = check_block_hash("t3", "0xbbb", _spec("block_hash", rpc_params=(1,)), {}, rpc)
    assert not v.passed


def test_block_hash_missing_rpc_params():
    v = check_block_hash("t3", "0x1", _spec("block_hash"), {}, lambda m, p: "0x1")
    assert not v.passed
    assert "rpc_params" in v.reason


def test_block_hash_rpc_error():
    def boom(m, p):
        raise RuntimeError("hash rpc down")

    v = check_block_hash("t3", "0x1", _spec("block_hash", rpc_params=(3,)), {}, boom)
    assert not v.passed
    assert "hash rpc down" in v.reason


# --- constant_hex ---

FROZEN_CONSTANT = (
    "0xc35396b3053610327a1d7638567a6e7e04d5e7f378e7f189c3e550e8c3bee42"
)
SPORE_LOCK_CODE_HASH = (
    "0x9c23a6097b2c27e5cb47d1dade5ebb5acaa8a4233a204b6eeaa741eb6de49e0a"
)


def test_constant_hex_passes_exact():
    v = check_constant_hex(
        "t-const",
        FROZEN_CONSTANT,
        _spec("constant_hex", rpc_params=(FROZEN_CONSTANT,)),
        {},
        lambda m, p: None,
    )
    assert v.passed


def test_constant_hex_passes_case_insensitive():
    upper = FROZEN_CONSTANT.upper()
    v = check_constant_hex(
        "t-const",
        upper,
        _spec("constant_hex", rpc_params=(FROZEN_CONSTANT,)),
        {},
        lambda m, p: None,
    )
    assert v.passed


def test_constant_hex_passes_quoted_proof():
    v = check_constant_hex(
        "t-const",
        f'"{SPORE_LOCK_CODE_HASH}"',
        _spec("constant_hex", rpc_params=(SPORE_LOCK_CODE_HASH,)),
        {},
        lambda m, p: None,
    )
    assert v.passed


def test_constant_hex_fails_wrong_hash():
    v = check_constant_hex(
        "t-const",
        "0xdeadbeef",
        _spec("constant_hex", rpc_params=(FROZEN_CONSTANT,)),
        {},
        lambda m, p: None,
    )
    assert not v.passed
    assert "!=" in v.reason


def test_constant_hex_fails_empty_proof():
    v = check_constant_hex(
        "t-const",
        "  ",
        _spec("constant_hex", rpc_params=(FROZEN_CONSTANT,)),
        {},
        lambda m, p: None,
    )
    assert not v.passed
    assert "empty" in v.reason


def test_constant_hex_missing_rpc_params():
    v = check_constant_hex(
        "t-const",
        FROZEN_CONSTANT,
        _spec("constant_hex"),
        {},
        lambda m, p: None,
    )
    assert not v.passed
    assert "rpc_params" in v.reason


def test_constant_hex_ignores_rpc_errors():
    def boom(m, p):
        raise RuntimeError("rpc should not be called")

    v = check_constant_hex(
        "t-const",
        FROZEN_CONSTANT,
        _spec("constant_hex", rpc_params=(FROZEN_CONSTANT,)),
        {},
        boom,
    )
    assert v.passed


# --- tx_proof ---


RECIPIENT = "0x470dcdc5e44064909650113a274b3b36aecb6dc7"
NONCE = 10_000_000_123


def _tx_rpc_from_table(table: dict, method: str, params: list) -> Any:
    if method == "get_transaction":
        return table["get_transaction"]
    if method == "get_header":
        return table["get_header"]
    raise AssertionError(method)


def test_tx_proof_passes():
    table = _good_tx_rpc(RECIPIENT, NONCE, block_number=150)
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx",
        "0xtxid",
        _spec("tx_proof"),
        private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert v.passed


def test_tx_proof_parses_hex_capacity_from_wire():
    # Regression for the capacity-parse blocker: the chain sends capacity as a 0x-hex string.
    # The verifier must hex-decode it before comparing to the (decimal) verifier-private nonce.
    # A bare int() would read "0x...". -> ValueError, or read the hex digits as decimal, both wrong.
    nonce = 0xABCDEF12  # a value whose hex and decimal readings differ
    table = _good_tx_rpc(RECIPIENT, nonce, block_number=150)
    # sanity: the wire really carries the hex form, not decimal
    assert table["get_transaction"]["transaction"]["outputs"][0]["capacity"] == hex(nonce)
    private = _tx_private(harness_tip=100, nonce=nonce, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx", "0xtxid", _spec("tx_proof"), private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert v.passed, v.reason


def test_tx_proof_ignores_forged_agent_harness_tip():
    # Verifier reads harness_tip from verifier_private only; a low forged value in a fake
    # agent-written field must not relax freshness.
    table = _good_tx_rpc(RECIPIENT, NONCE, block_number=50)
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    private_with_cheat = {**private, "agent_forged_harness_tip": 1}
    v = check_tx_proof(
        "tx",
        "0xtxid",
        _spec("tx_proof"),
        private_with_cheat,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "STALE" in v.reason


def test_tx_proof_missing_private_fields():
    v = check_tx_proof("tx", "0x1", _spec("tx_proof"), {}, lambda m, p: {})
    assert not v.passed
    assert "harness_tip" in v.reason


def test_tx_proof_missing_nonce():
    v = check_tx_proof(
        "tx",
        "0x1",
        _spec("tx_proof"),
        {"harness_tip": 1, "recipient_args": RECIPIENT},
        lambda m, p: {},
    )
    assert not v.passed
    assert "nonce_amount_shannons" in v.reason


def test_tx_proof_missing_recipient():
    v = check_tx_proof(
        "tx",
        "0x1",
        _spec("tx_proof"),
        {"harness_tip": 1, "nonce_amount_shannons": "1"},
        lambda m, p: {},
    )
    assert not v.passed
    assert "recipient_args" in v.reason


def test_tx_proof_empty_proof():
    v = check_tx_proof("tx", "  ", _spec("tx_proof"), _tx_private(recipient=RECIPIENT), lambda m, p: {})
    assert not v.passed
    assert "empty" in v.reason


def test_tx_proof_missing_tx():
    private = _tx_private(recipient=RECIPIENT)
    v = check_tx_proof(
        "tx",
        "0xmissing",
        _spec("tx_proof"),
        private,
        lambda m, p: None,
    )
    assert not v.passed
    assert "not found" in v.reason


def test_tx_proof_uncommitted():
    private = _tx_private(recipient=RECIPIENT)
    txw = {"transaction": {"outputs": []}, "tx_status": {"status": "pending"}}
    v = check_tx_proof(
        "tx",
        "0xtx",
        _spec("tx_proof"),
        private,
        lambda m, p: txw if m == "get_transaction" else None,
    )
    assert not v.passed
    assert "not committed" in v.reason


def test_tx_proof_stale_block():
    table = _good_tx_rpc(RECIPIENT, NONCE, block_number=50)
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx",
        "0xtx",
        _spec("tx_proof"),
        private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "STALE" in v.reason


def test_tx_proof_no_block_hash():
    private = _tx_private(recipient=RECIPIENT)
    txw = {"transaction": {"outputs": []}, "tx_status": {"status": "committed"}}
    v = check_tx_proof(
        "tx",
        "0xtx",
        _spec("tx_proof"),
        private,
        lambda m, p: txw if m == "get_transaction" else None,
    )
    assert not v.passed
    assert "no block_hash" in v.reason


def test_tx_proof_wrong_output_count_zero():
    table = {
        "get_transaction": {
            "transaction": {"outputs": []},
            "tx_status": {"status": "committed", "block_hash": "0xb"},
        },
        "get_header": {"number": "0x64"},
    }
    private = _tx_private(recipient=RECIPIENT)
    v = check_tx_proof(
        "tx",
        "0xtx",
        _spec("tx_proof"),
        private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "exactly 1 output" in v.reason


def test_tx_proof_extra_output_to_recipient():
    outputs = [
        {"capacity": hex(NONCE), "lock": {"code_hash": SECP_CODE_HASH, "args": RECIPIENT}},
        {"capacity": hex(1000), "lock": {"code_hash": SECP_CODE_HASH, "args": RECIPIENT}},
    ]
    table = {
        "get_transaction": {
            "transaction": {"outputs": outputs},
            "tx_status": {"status": "committed", "block_hash": "0xb"},
        },
        "get_header": {"number": "0x64"},
    }
    private = _tx_private(nonce=NONCE, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx",
        "0xtx",
        _spec("tx_proof"),
        private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "exactly 1 output" in v.reason


def test_tx_proof_wrong_nonce_amount():
    table = _good_tx_rpc(RECIPIENT, NONCE + 1, block_number=150)
    private = _tx_private(nonce=NONCE, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx",
        "0xtx",
        _spec("tx_proof"),
        private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "not the nonce" in v.reason


def test_tx_proof_rejected_status():
    """A node-rejected tx is not a Proof. Only "committed" may pass; "pending" is covered above,
    and a rejected tx is the case an agent is most likely to submit and walk away from."""
    private = _tx_private(recipient=RECIPIENT)
    txw = {"transaction": {"outputs": []}, "tx_status": {"status": "rejected"}}
    v = check_tx_proof(
        "tx", "0xtx", _spec("tx_proof"), private,
        lambda m, p: txw if m == "get_transaction" else None,
    )
    assert not v.passed
    assert "not committed" in v.reason
    assert "rejected" in v.reason


def test_tx_proof_wrong_recipient():
    """A committed tx paying the exact nonce to SOMEONE ELSE must fail: the recipient is a
    verifier-private expectation, not something the agent may choose."""
    table = _good_tx_rpc("0x" + "ab" * 20, NONCE, block_number=150)
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx", "0xtx", _spec("tx_proof"), private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "exactly 1 output" in v.reason


def test_tx_proof_right_args_but_not_a_secp_lock():
    """Matching only on lock args would accept an output under a different lock script that the
    intended recipient cannot spend."""
    outputs = [
        {"capacity": hex(NONCE), "lock": {"code_hash": "0x" + "cd" * 32, "args": RECIPIENT}},
    ]
    table = {
        "get_transaction": {
            "transaction": {"outputs": outputs},
            "tx_status": {"status": "committed", "block_hash": "0xb"},
        },
        "get_header": {"number": "0x96"},
    }
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx", "0xtx", _spec("tx_proof"), private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "exactly 1 output" in v.reason


def test_tx_proof_borrowed_from_an_earlier_run():
    """The ADR-0001 attack: a real committed transaction from run 1 replayed as run 2's Proof. It
    must fail on BOTH independent integrity inputs -- run 2's later harness tip and its own
    high-entropy nonce -- so neither check alone carries the anti-cheat."""
    run1_tx = _good_tx_rpc(RECIPIENT, NONCE, block_number=150)
    run2_private = _tx_private(harness_tip=200, nonce=NONCE + 7_777, recipient=RECIPIENT)

    v = check_tx_proof(
        "tx", "0xrun1tx", _spec("tx_proof"), run2_private,
        lambda m, p: _tx_rpc_from_table(run1_tx, m, p),
    )
    assert not v.passed
    assert "STALE" in v.reason  # borrowed tx predates run 2's baseline

    # and independently: even with the freshness window satisfied, the nonce binds it to run 1
    run2_same_tip = _tx_private(harness_tip=100, nonce=NONCE + 7_777, recipient=RECIPIENT)
    v2 = check_tx_proof(
        "tx", "0xrun1tx", _spec("tx_proof"), run2_same_tip,
        lambda m, p: _tx_rpc_from_table(run1_tx, m, p),
    )
    assert not v2.passed
    assert "not the nonce" in v2.reason


def test_tx_proof_malformed_rpc_payload_is_a_failure_not_a_crash():
    """A node that answers with an unexpected shape must fail the task cleanly rather than raise
    through the grader and abort the remaining tasks."""
    private = _tx_private(recipient=RECIPIENT)
    table = {
        "get_transaction": {
            "transaction": {"outputs": [{"capacity": hex(NONCE)}]},  # no lock at all
            "tx_status": {"status": "committed", "block_hash": "0xb"},
        },
        "get_header": {},  # no number
    }
    v = check_tx_proof(
        "tx", "0xtx", _spec("tx_proof"), private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "verify error" in v.reason


def test_tx_proof_rpc_exception():
    private = _tx_private(recipient=RECIPIENT)

    def boom(m, p):
        raise RuntimeError("rpc blew up")

    v = check_tx_proof("tx", "0xtx", _spec("tx_proof"), private, boom)
    assert not v.passed
    assert "rpc blew up" in v.reason


def test_grade_onchain_unknown_check():
    v = grade_onchain_task("t", "0x1", _spec("nope"), {}, lambda m, p: None)
    assert not v.passed
    assert "unknown" in v.reason


# --- script_identity: two-field Simple UDT protocol identity, no RPC --------------------------

SUDT_CODE_HASH = "0x5e7a36a77e68eecc013dfa2fe6a23f3b6c344b04005808694ae6dd45eea4cfd5"
XUDT_CODE_HASH = "0x50bd8d6680b8b9cf98b73f3c08faf8b2a21914311954118ad6609be6e78a1b95"
MALFORMED_LITERAL = "0xc35396b3053610327a1d7638567a6e7e04d5e7f378e7f189c3e550e8c3bee42"


def _identity_spec(params=(SUDT_CODE_HASH, "type")):
    return _spec("script_identity", rpc_params=tuple(params))


def _no_rpc(method, params):
    raise AssertionError("script_identity must never call RPC")


@pytest.mark.parametrize(
    "proof",
    [
        f"{SUDT_CODE_HASH}\ntype",
        f"{SUDT_CODE_HASH.upper()}\nTYPE",
        f"  {SUDT_CODE_HASH}  \n\n  type  \n",
        f'"{SUDT_CODE_HASH}"\n"type"',
        f'" {SUDT_CODE_HASH} "\n" type "',
        f"\n\n{SUDT_CODE_HASH}\n\ntype\n\n",
    ],
    ids=["exact", "uppercase", "whitespace-blanks", "quoted", "quoted-inner-space", "blank-padded"],
)
def test_script_identity_accepts_the_canonical_identity(proof):
    assert check_script_identity("t", proof, _identity_spec(), {}, _no_rpc).passed


@pytest.mark.parametrize(
    ("proof", "why"),
    [
        (f"{XUDT_CODE_HASH}\ntype", "xudt-with-type"),
        (f"{XUDT_CODE_HASH}\ndata1", "xudt-with-data1"),
        (f"{SUDT_CODE_HASH}\ndata1", "right-hash-wrong-type"),
        (f"{MALFORMED_LITERAL}\ntype", "former-malformed-literal"),
        (SUDT_CODE_HASH, "single-line"),
        ("", "empty"),
        ("   \n\n  \n", "whitespace-only"),
        (f"{SUDT_CODE_HASH}\ntype\nextra", "third-line"),
        (f"code_hash: {SUDT_CODE_HASH}\nhash_type: type", "labelled"),
        (f"type\n{SUDT_CODE_HASH}", "swapped"),
        (f'"{SUDT_CODE_HASH}\ntype', "unmatched-open-quote"),
        (f'{SUDT_CODE_HASH}"\ntype', "unmatched-close-quote"),
        (f"{SUDT_CODE_HASH[:-2]}\ntype", "truncated-hash"),
        (f'{SUDT_CODE_HASH}\ntype\n""', "third-line-empty-quotes"),
        (f'{SUDT_CODE_HASH}\ntype\n"   "', "third-line-quoted-spaces"),
        (f'{SUDT_CODE_HASH}\ntype\n"\t"', "third-line-quoted-tab"),
    ],
)
def test_script_identity_rejects_everything_else(proof, why):
    assert not check_script_identity("t", proof, _identity_spec(), {}, _no_rpc).passed, why


def test_script_identity_accepts_crlf():
    proof = f"{SUDT_CODE_HASH}\r\ntype\r\n"
    assert check_script_identity("t", proof, _identity_spec(), {}, _no_rpc).passed


@pytest.mark.parametrize(
    "separator",
    ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\u0085", "\u2028", "\u2029"],
    ids=["cr", "vt", "ff", "fs", "gs", "rs", "nel", "line-sep", "para-sep"],
)
def test_script_identity_needs_a_real_line_break(separator):
    """`str.splitlines()` breaks on all of these; a proof with no LF must not yield two fields."""
    verdict = check_script_identity(
        "t", f"{SUDT_CODE_HASH}{separator}type", _identity_spec(), {}, _no_rpc
    )
    assert not verdict.passed
    assert "found 1" in verdict.reason


@pytest.mark.parametrize(
    "params",
    [(), (SUDT_CODE_HASH,), (SUDT_CODE_HASH, "type", "extra")],
    ids=["none", "one", "three"],
)
def test_script_identity_requires_exactly_two_parameters(params):
    verdict = check_script_identity(
        "t", f"{SUDT_CODE_HASH}\ntype", _identity_spec(params), {}, _no_rpc
    )
    assert not verdict.passed
    assert "exactly 2" in verdict.reason


def test_script_identity_preserves_the_raw_proof_text():
    raw = f'  "{SUDT_CODE_HASH}"  \n type \n'
    assert check_script_identity("t", raw, _identity_spec(), {}, _no_rpc).proof == raw
    bad = "nonsense"
    assert check_script_identity("t", bad, _identity_spec(), {}, _no_rpc).proof == bad


def test_script_identity_ignores_verifier_private():
    verdict = check_script_identity(
        "t", f"{SUDT_CODE_HASH}\ntype", _identity_spec(),
        {"harness_tip": 1, "anything": "else"}, _no_rpc,
    )
    assert verdict.passed


def test_constant_hex_is_unchanged_by_the_new_check():
    """Task 07 still uses constant_hex; this redesign must not disturb it."""
    spec = _spec("constant_hex", rpc_params=(FROZEN_CONSTANT,))
    assert check_constant_hex("t", FROZEN_CONSTANT, spec, {}, _no_rpc).passed
    assert not check_constant_hex("t", SUDT_CODE_HASH, spec, {}, _no_rpc).passed
