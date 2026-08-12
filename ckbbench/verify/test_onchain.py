"""On-chain verifier tests: each check passes on valid proof, fails on documented cheats."""

from __future__ import annotations

from typing import Any

import pytest

from ckbbench.suite.model import OnchainVerifierSpec
from ckbbench.verify import onchain
from ckbbench.verify.onchain import (
    FRESHNESS_WINDOW_BLOCKS,
    SECP_CODE_HASH,
    check_block_hash,
    check_constant_hex,
    check_epoch_number,
    check_script_identity,
    SECP_HASH_TYPE,
    TX_CONFIRM_BUDGET_SECONDS,
    VerificationInfrastructureError,
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
                            "hash_type": SECP_HASH_TYPE,
                            "args": recipient,
                        },
                    },
                    {
                        "capacity": hex(1000),
                        "lock": {"code_hash": "0xother", "hash_type": "type", "args": "0xdead"},
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


TX_HASH = "0x" + "11" * 32
RUN1_TX_HASH = "0x" + "22" * 32
RECIPIENT = "0x470dcdc5e44064909650113a274b3b36aecb6dc7"


def _secp_lock(args: str, hash_type: str = SECP_HASH_TYPE) -> dict:
    """The real CKB standard-lock shape: a Script is code_hash + hash_type + args."""
    return {"code_hash": SECP_CODE_HASH, "hash_type": hash_type, "args": args}


class FakeClock:
    """Virtual monotonic clock whose sleep advances time. No test may wait in real seconds."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0, seconds
        self.sleeps.append(seconds)
        self.now += seconds


class SpyRpc:
    """Scripted get_transaction responses; the last entry repeats once exhausted."""

    def __init__(self, statuses: list[Any], header: Any = None) -> None:
        self.statuses = statuses
        self.header = header if header is not None else {"number": "0x96"}
        self.calls: list[str] = []

    def __call__(self, method: str, params: list) -> Any:
        self.calls.append(method)
        if method == "get_transaction":
            i = min(len(self.tx_calls) - 1, len(self.statuses) - 1)
            entry = self.statuses[i]
            if isinstance(entry, Exception):
                raise entry
            return entry
        if method == "get_header":
            if isinstance(self.header, Exception):
                raise self.header
            return self.header
        raise AssertionError(method)

    @property
    def tx_calls(self) -> list[str]:
        return [c for c in self.calls if c == "get_transaction"]


def _status(status: str, *, outputs: list | None = None, block_hash: str | None = "0xb") -> dict:
    env: dict[str, Any] = {"status": status}
    if block_hash is not None:
        env["block_hash"] = block_hash
    return {"transaction": {"outputs": outputs if outputs is not None else []}, "tx_status": env}


def _committed_paying(recipient: str, amount: int) -> dict:
    outputs = [{"capacity": hex(amount), "lock": _secp_lock(recipient)}]
    return _status("committed", outputs=outputs)
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
        TX_HASH,
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
        "tx", TX_HASH, _spec("tx_proof"), private,
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
        TX_HASH,
        _spec("tx_proof"),
        private_with_cheat,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "STALE" in v.reason


def test_tx_proof_missing_private_fields():
    v = check_tx_proof("tx", TX_HASH, _spec("tx_proof"), {}, lambda m, p: {})
    assert not v.passed
    assert "harness_tip" in v.reason


def test_tx_proof_missing_nonce():
    v = check_tx_proof(
        "tx",
        TX_HASH,
        _spec("tx_proof"),
        {"harness_tip": 1, "recipient_args": RECIPIENT},
        lambda m, p: {},
    )
    assert not v.passed
    assert "nonce_amount_shannons" in v.reason


def test_tx_proof_missing_recipient():
    v = check_tx_proof(
        "tx",
        TX_HASH,
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
        TX_HASH,
        _spec("tx_proof"),
        private,
        lambda m, p: None,
    )
    assert not v.passed
    assert "not found" in v.reason


def test_tx_proof_uncommitted():
    """Pending through the whole budget is the agent's failure, never infrastructure."""
    private = _tx_private(recipient=RECIPIENT)
    txw = {"transaction": {"outputs": []}, "tx_status": {"status": "pending"}}
    clock = FakeClock()
    v = check_tx_proof(
        "tx",
        TX_HASH,
        _spec("tx_proof"),
        private,
        lambda m, p: txw if m == "get_transaction" else None,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )
    assert not v.passed
    assert "not committed" in v.reason


def test_tx_proof_stale_block():
    table = _good_tx_rpc(RECIPIENT, NONCE, block_number=50)
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx",
        TX_HASH,
        _spec("tx_proof"),
        private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "STALE" in v.reason


def test_tx_proof_no_block_hash():
    """A committed tx without a block hash cannot come from a healthy node, so it is unusable
    grading data rather than evidence against the agent."""
    private = _tx_private(recipient=RECIPIENT)
    txw = {"transaction": {"outputs": []}, "tx_status": {"status": "committed"}}
    with pytest.raises(VerificationInfrastructureError, match="block_hash"):
        check_tx_proof(
            "tx",
            TX_HASH,
            _spec("tx_proof"),
            private,
            lambda m, p: txw if m == "get_transaction" else None,
        )


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
        TX_HASH,
        _spec("tx_proof"),
        private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "exactly 1 output" in v.reason


def test_tx_proof_extra_output_to_recipient():
    outputs = [
        {"capacity": hex(NONCE), "lock": _secp_lock(RECIPIENT)},
        {"capacity": hex(1000), "lock": _secp_lock(RECIPIENT)},
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
        TX_HASH,
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
        TX_HASH,
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
        "tx", TX_HASH, _spec("tx_proof"), private,
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
        "tx", TX_HASH, _spec("tx_proof"), private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed
    assert "exactly 1 output" in v.reason


def test_tx_proof_right_args_but_not_a_secp_lock():
    """Matching only on lock args would accept an output under a different lock script that the
    intended recipient cannot spend."""
    outputs = [
        {"capacity": hex(NONCE), "lock": {"code_hash": "0x" + "cd" * 32,
                                          "hash_type": SECP_HASH_TYPE, "args": RECIPIENT}},
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
        "tx", TX_HASH, _spec("tx_proof"), private,
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
        "tx", RUN1_TX_HASH, _spec("tx_proof"), run2_private,
        lambda m, p: _tx_rpc_from_table(run1_tx, m, p),
    )
    assert not v.passed
    assert "STALE" in v.reason  # borrowed tx predates run 2's baseline

    # and independently: even with the freshness window satisfied, the nonce binds it to run 1
    run2_same_tip = _tx_private(harness_tip=100, nonce=NONCE + 7_777, recipient=RECIPIENT)
    v2 = check_tx_proof(
        "tx", RUN1_TX_HASH, _spec("tx_proof"), run2_same_tip,
        lambda m, p: _tx_rpc_from_table(run1_tx, m, p),
    )
    assert not v2.passed
    assert "not the nonce" in v2.reason


def test_tx_proof_malformed_rpc_payload_is_infrastructure_not_a_task_failure():
    """A node answering with an unusable shape must not be charged to the model: without a
    trustworthy observation there is no evidence the agent did anything wrong."""
    private = _tx_private(recipient=RECIPIENT)
    table = {
        "get_transaction": {
            "transaction": {"outputs": [{"capacity": hex(NONCE)}]},  # no lock at all
            "tx_status": {"status": "committed", "block_hash": "0xb"},
        },
        "get_header": {"number": "0x96"},
    }
    with pytest.raises(VerificationInfrastructureError, match="lock"):
        check_tx_proof(
            "tx", TX_HASH, _spec("tx_proof"), private,
            lambda m, p: _tx_rpc_from_table(table, m, p),
        )


def test_tx_proof_rpc_exception():
    private = _tx_private(recipient=RECIPIENT)

    def boom(m, p):
        raise RuntimeError("rpc blew up")

    with pytest.raises(VerificationInfrastructureError) as excinfo:
        check_tx_proof("tx", TX_HASH, _spec("tx_proof"), private, boom)
    assert "get_transaction" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    # The cause carries the detail; the message must not leak response or private data.
    assert "rpc blew up" not in str(excinfo.value)


# --- tx_proof: Card 3 classification (grammar, polling, infrastructure boundary) ---


class CountingRpc:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, method: str, params: list) -> Any:
        self.calls += 1
        return self.result


@pytest.mark.parametrize(
    "proof",
    [
        "",
        "   ",
        "0x",
        "0x" + "11" * 31,
        "0x" + "11" * 33,
        "11" * 32,
        "0x" + "gg" * 32,
        f'"{"0x" + "11" * 32}"',
        f"{'0x' + '11' * 32} extra",
        f"{'0x' + '11' * 32}\n{'0x' + '22' * 32}",
        f"tx: {'0x' + '11' * 32}",
    ],
    ids=["empty", "blank", "prefix-only", "short", "long", "no-prefix", "non-hex", "quoted",
         "trailing-text", "two-hashes", "labelled"],
)
def test_tx_proof_malformed_hash_never_reaches_rpc(proof):
    spy = CountingRpc()
    v = check_tx_proof("tx", proof, _spec("tx_proof"), _tx_private(recipient=RECIPIENT), spy)
    assert not v.passed
    assert spy.calls == 0, f"malformed proof reached RPC {spy.calls} times"
    assert v.proof == proof


def test_tx_proof_accepts_surrounding_whitespace_and_uppercase_and_reaches_rpc():
    table = _good_tx_rpc(RECIPIENT, NONCE, block_number=150)
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    raw = f"  {('0x' + 'AB' * 32)}  \n"
    calls: list[str] = []

    def rpc(m, p):
        calls.append(m)
        assert p[0] == "0x" + "AB" * 32 if m == "get_transaction" else True
        return _tx_rpc_from_table(table, m, p)

    v = check_tx_proof("tx", raw, _spec("tx_proof"), private, rpc)
    assert v.passed, v.reason
    assert "get_transaction" in calls
    assert v.proof == raw


@pytest.mark.parametrize("first", ["pending", "proposed"])
def test_tx_proof_pending_then_committed_passes(first):
    clock = FakeClock()
    rpc = SpyRpc([_status(first), _status(first), _committed_paying(RECIPIENT, NONCE)])
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx", TX_HASH, _spec("tx_proof"), private, rpc,
        monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
    )
    assert v.passed, v.reason
    assert len(rpc.tx_calls) == 3
    assert clock.sleeps == [2.0, 2.0]


def test_tx_proof_pending_through_budget_is_a_task_failure_with_virtual_time():
    clock = FakeClock()
    rpc = SpyRpc([_status("pending")])
    private = _tx_private(recipient=RECIPIENT)
    v = check_tx_proof(
        "tx", TX_HASH, _spec("tx_proof"), private, rpc,
        monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
    )
    assert not v.passed
    assert "not committed" in v.reason
    # 90s budget at 2s intervals: 45 sleeps, 46 reads, and the last read lands on the deadline.
    assert clock.sleeps == [2.0] * 45
    assert len(rpc.tx_calls) == 46
    assert clock.now == 1000.0 + TX_CONFIRM_BUDGET_SECONDS


def test_tx_proof_final_sleep_never_overruns_the_deadline():
    """A budget that is not a multiple of the interval must not sleep past the deadline."""
    clock = FakeClock()
    rpc = SpyRpc([_status("proposed")])
    original = TX_CONFIRM_BUDGET_SECONDS
    try:
        onchain.TX_CONFIRM_BUDGET_SECONDS = 5.0
        check_tx_proof(
            "tx", TX_HASH, _spec("tx_proof"), _tx_private(recipient=RECIPIENT), rpc,
            monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
        )
    finally:
        onchain.TX_CONFIRM_BUDGET_SECONDS = original
    assert clock.sleeps == [2.0, 2.0, 1.0]
    assert clock.now == 1005.0


@pytest.mark.parametrize(
    ("later", "expected"),
    [(_status("rejected"), "rejected"), (None, "not found")],
    ids=["becomes-rejected", "becomes-not-found"],
)
def test_tx_proof_pending_then_terminal_failure(later, expected):
    clock = FakeClock()
    rpc = SpyRpc([_status("pending"), later])
    v = check_tx_proof(
        "tx", TX_HASH, _spec("tx_proof"), _tx_private(recipient=RECIPIENT), rpc,
        monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
    )
    assert not v.passed
    assert expected in v.reason
    assert len(rpc.tx_calls) == 2


@pytest.mark.parametrize(
    "envelope",
    [
        {},
        {"tx_status": "committed"},
        {"tx_status": {}},
        {"tx_status": {"status": 7}},
        {"tx_status": {"status": "unknown_status"}},
        "not-an-object",
        42,
    ],
    ids=["no-tx_status", "tx_status-not-object", "no-status", "status-not-string",
         "unrecognized-status", "response-not-object", "response-is-int"],
)
def test_tx_proof_malformed_status_envelope_is_infrastructure(envelope):
    with pytest.raises(VerificationInfrastructureError):
        check_tx_proof(
            "tx", TX_HASH, _spec("tx_proof"), _tx_private(recipient=RECIPIENT),
            lambda m, p: envelope,
        )


@pytest.mark.parametrize(
    ("table", "why"),
    [
        ({"get_transaction": {"tx_status": {"status": "committed", "block_hash": "0xb"}},
          "get_header": {"number": "0x96"}}, "no-transaction-object"),
        ({"get_transaction": {"transaction": {}, "tx_status": {"status": "committed", "block_hash": "0xb"}},
          "get_header": {"number": "0x96"}}, "no-output-list"),
        ({"get_transaction": {"transaction": {"outputs": "nope"},
                              "tx_status": {"status": "committed", "block_hash": "0xb"}},
          "get_header": {"number": "0x96"}}, "outputs-not-a-list"),
        ({"get_transaction": _committed_paying(RECIPIENT, NONCE), "get_header": {}}, "header-no-number"),
        ({"get_transaction": _committed_paying(RECIPIENT, NONCE), "get_header": "nope"},
         "header-not-an-object"),
        ({"get_transaction": _committed_paying(RECIPIENT, NONCE), "get_header": {"number": "zz"}},
         "header-number-not-hex"),
        ({"get_transaction": _status("committed", outputs=[
            {"capacity": "zz", "lock": _secp_lock(RECIPIENT)}]),
          "get_header": {"number": "0x96"}}, "capacity-not-hex"),
        ({"get_transaction": _status("committed", outputs=["not-an-object"]),
          "get_header": {"number": "0x96"}}, "output-not-an-object"),
        ({"get_transaction": _status("committed", outputs=[
            {"capacity": hex(NONCE), "lock": {"code_hash": 7, "hash_type": SECP_HASH_TYPE,
                                              "args": RECIPIENT}}]),
          "get_header": {"number": "0x96"}}, "lock-fields-wrong-type"),
    ],
)
def test_tx_proof_malformed_committed_payload_is_infrastructure(table, why):
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    with pytest.raises(VerificationInfrastructureError):
        check_tx_proof(
            "tx", TX_HASH, _spec("tx_proof"), private,
            lambda m, p: _tx_rpc_from_table(table, m, p),
        )


def test_tx_proof_retry_and_header_exceptions_preserve_cause():
    clock = FakeClock()
    retry = SpyRpc([_status("pending"), TimeoutError("node timed out")])
    with pytest.raises(VerificationInfrastructureError) as exc_retry:
        check_tx_proof(
            "tx", TX_HASH, _spec("tx_proof"), _tx_private(recipient=RECIPIENT), retry,
            monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
        )
    assert isinstance(exc_retry.value.__cause__, TimeoutError)

    header = SpyRpc([_committed_paying(RECIPIENT, NONCE)], header=ConnectionError("no route"))
    with pytest.raises(VerificationInfrastructureError) as exc_header:
        check_tx_proof(
            "tx", TX_HASH, _spec("tx_proof"),
            _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT), header,
        )
    assert "get_header" in str(exc_header.value)
    assert isinstance(exc_header.value.__cause__, ConnectionError)


@pytest.mark.parametrize(
    ("table", "why"),
    [
        (_good_tx_rpc(RECIPIENT, NONCE, block_number=50), "stale"),
        (_good_tx_rpc("0x" + "ab" * 20, NONCE, block_number=150), "wrong-recipient"),
        (_good_tx_rpc(RECIPIENT, NONCE + 1, block_number=150), "wrong-amount"),
    ],
)
def test_tx_proof_semantically_wrong_but_readable_is_never_infrastructure(table, why):
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    v = check_tx_proof(
        "tx", TX_HASH, _spec("tx_proof"), private,
        lambda m, p: _tx_rpc_from_table(table, m, p),
    )
    assert not v.passed, why


@pytest.mark.parametrize(
    ("lock", "expect"),
    [
        (_secp_lock(RECIPIENT, "type"), "pass"),
        (_secp_lock(RECIPIENT, "data"), "fail"),
        (_secp_lock(RECIPIENT, "data1"), "fail"),
        (_secp_lock(RECIPIENT, "data2"), "fail"),
        ({"code_hash": SECP_CODE_HASH, "args": RECIPIENT}, "infra"),
        ({"code_hash": SECP_CODE_HASH, "hash_type": 1, "args": RECIPIENT}, "infra"),
        ({"code_hash": SECP_CODE_HASH, "hash_type": "bogus", "args": RECIPIENT}, "infra"),
    ],
    ids=["type", "data", "data1", "data2", "missing", "not-a-string", "unrecognized"],
)
def test_tx_proof_requires_the_full_secp_script_identity(lock, expect):
    """A Script is code_hash + hash_type + args: matching only two of the three would accept an
    output under a different Script that the intended recipient cannot spend."""
    table = {
        "get_transaction": {
            "transaction": {"outputs": [{"capacity": hex(NONCE), "lock": lock}]},
            "tx_status": {"status": "committed", "block_hash": "0xb"},
        },
        "get_header": {"number": "0x96"},
    }
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    call = lambda: check_tx_proof(  # noqa: E731
        "tx", TX_HASH, _spec("tx_proof"), private, lambda m, p: _tx_rpc_from_table(table, m, p)
    )
    if expect == "infra":
        with pytest.raises(VerificationInfrastructureError, match="hash_type"):
            call()
        return
    verdict = call()
    assert verdict.passed is (expect == "pass"), verdict.reason
    if expect == "fail":
        assert "exactly 1 output" in verdict.reason


def test_tx_proof_preserves_raw_proof_text_in_the_verdict():
    raw = f"  {TX_HASH}  \n"
    table = _good_tx_rpc(RECIPIENT, NONCE, block_number=150)
    private = _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT)
    ok = check_tx_proof("tx", raw, _spec("tx_proof"), private,
                        lambda m, p: _tx_rpc_from_table(table, m, p))
    assert ok.passed and ok.proof == raw
    bad = check_tx_proof("tx", raw, _spec("tx_proof"),
                         _tx_private(harness_tip=100, nonce=NONCE + 1, recipient=RECIPIENT),
                         lambda m, p: _tx_rpc_from_table(table, m, p))
    assert not bad.passed and bad.proof == raw


def test_unrecognized_status_value_is_never_echoed_into_the_message():
    sentinel = "SENSITIVE_RESPONSE_FRAGMENT"
    with pytest.raises(VerificationInfrastructureError) as excinfo:
        check_tx_proof(
            "tx", TX_HASH, _spec("tx_proof"), _tx_private(recipient=RECIPIENT),
            lambda m, p: {"transaction": {"outputs": []}, "tx_status": {"status": sentinel}},
        )
    assert sentinel not in str(excinfo.value)
    assert "unrecognized status" in str(excinfo.value)


def test_grade_onchain_forwards_time_seams_only_to_tx_proof():
    clock = FakeClock()
    rpc = SpyRpc([_status("pending"), _committed_paying(RECIPIENT, NONCE)])
    v = grade_onchain_task(
        "tx", TX_HASH, _spec("tx_proof"),
        _tx_private(harness_tip=100, nonce=NONCE, recipient=RECIPIENT), rpc,
        monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
    )
    assert v.passed, v.reason
    assert clock.sleeps == [2.0]

    # A non-polling check must not be handed the seams, so its signature stays unchanged.
    other = grade_onchain_task(
        "c", FROZEN_CONSTANT, _spec("constant_hex", rpc_params=(FROZEN_CONSTANT,)), {},
        lambda m, p: None, monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
    )
    assert other.passed


def test_grade_onchain_propagates_infrastructure_error():
    """The dispatcher's isolation must not swallow the dedicated exception into a verdict."""
    def boom(m, p):
        raise OSError("socket down")

    with pytest.raises(VerificationInfrastructureError):
        grade_onchain_task(
            "tx", TX_HASH, _spec("tx_proof"), _tx_private(recipient=RECIPIENT), boom
        )


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
