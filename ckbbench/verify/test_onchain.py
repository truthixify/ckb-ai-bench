"""On-chain verifier tests: each check passes on valid proof, fails on documented cheats."""

from __future__ import annotations

from typing import Any

import pytest

from ckbbench.suite.model import OnchainVerifierSpec
from ckbbench.verify import onchain
from ckbbench.verify.onchain import (
    SECP_CODE_HASH,
    check_block_hash,
    check_constant_hex,
    check_epoch_number,
    check_script_identity,
    SECP_HASH_TYPE,
    TX_CONFIRM_BUDGET_SECONDS,
    VerificationInfrastructureError,
    R1_CAPACITY_SHANNONS,
    TYPE_ID_CODE_HASH,
    ckb_blake2b,
    check_tip_block_identity,
    check_tx_proof,
    check_type_id_data_cell,
    molecule_script,
    type_id_args,
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


# --- tip_block_identity: run-bound tip + block hash at that exact height ---

TIP_HASH = "0x" + "ab" * 32
OTHER_HASH = "0x" + "cd" * 32


class RpcLedger:
    """Records every (method, params) so call order and absence can both be asserted."""

    def __init__(self, tip: int, hashes: dict[int, Any] | None = None) -> None:
        self.tip = tip
        self.hashes = hashes if hashes is not None else {}
        self.calls: list[tuple[str, list]] = []

    def __call__(self, method: str, params: list) -> Any:
        self.calls.append((method, list(params)))
        if method == "get_tip_block_number":
            return self.tip if isinstance(self.tip, (str, type(None))) else hex(self.tip)
        if method == "get_block_hash":
            return self.hashes.get(int(params[0], 16), TIP_HASH)
        raise AssertionError(method)

    @property
    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def _tip_proof(tip: int, block_hash: str = TIP_HASH) -> str:
    return f"{hex(tip)}\n{block_hash}"


def _tip_private(harness_tip: int = 100) -> dict:
    return {"harness_tip": harness_tip}


def test_tip_identity_passes_at_the_run_start_height():
    rpc = RpcLedger(tip=100)
    v = check_tip_block_identity("t1", _tip_proof(100), _spec("tip_block_identity"),
                                 _tip_private(100), rpc)
    assert v.passed, v.reason


def test_tip_identity_early_proof_survives_a_long_cell():
    """The whole point of Card 4: an honest early read must not expire after later mining."""
    rpc = RpcLedger(tip=100_000)
    v = check_tip_block_identity("t1", _tip_proof(105), _spec("tip_block_identity"),
                                 _tip_private(100), rpc)
    assert v.passed, v.reason
    assert 100_000 - 105 > 50, "must be far beyond the removed 50-block window"


def test_tip_identity_rejects_a_hardcoded_low_tip():
    """`0x1` was passable under the old window whenever the cell started near genesis."""
    rpc = RpcLedger(tip=100_000)
    v = check_tip_block_identity("t1", _tip_proof(1, OTHER_HASH), _spec("tip_block_identity"),
                                 _tip_private(100), rpc)
    assert not v.passed
    assert "predates run start" in v.reason


@pytest.mark.parametrize(
    ("proof_tip", "why"),
    [(99, "predates run start"), (100_001, "FUTURE")],
    ids=["below-harness-tip", "above-verify-tip"],
)
def test_tip_identity_bounds_fail_without_asking_for_a_block_hash(proof_tip, why):
    rpc = RpcLedger(tip=100_000)
    v = check_tip_block_identity("t1", _tip_proof(proof_tip), _spec("tip_block_identity"),
                                 _tip_private(100), rpc)
    assert not v.passed
    assert why in v.reason
    assert "get_block_hash" not in rpc.methods


def test_tip_identity_calls_both_rpcs_with_canonical_parameters():
    rpc = RpcLedger(tip=100_000)
    assert check_tip_block_identity("t1", _tip_proof(4095), _spec("tip_block_identity"),
                                    _tip_private(100), rpc).passed
    assert rpc.calls == [("get_tip_block_number", []), ("get_block_hash", ["0xfff"])]


def test_tip_identity_rejects_a_valid_hash_from_another_height():
    rpc = RpcLedger(tip=100_000, hashes={200: OTHER_HASH})
    v = check_tip_block_identity("t1", _tip_proof(200, TIP_HASH), _spec("tip_block_identity"),
                                 _tip_private(100), rpc)
    assert not v.passed
    assert "block hash mismatch" in v.reason


def test_tip_identity_rejects_a_previous_generation_pair():
    """Simulated, not observed: a fresh DevNet generation returns a different hash at the same
    height, so a proof borrowed from an earlier cell fails."""
    generation_a_hash = OTHER_HASH
    rpc = RpcLedger(tip=100_000, hashes={150: TIP_HASH})  # generation B
    v = check_tip_block_identity("t1", _tip_proof(150, generation_a_hash),
                                 _spec("tip_block_identity"), _tip_private(100), rpc)
    assert not v.passed
    assert "block hash mismatch" in v.reason


@pytest.mark.parametrize(
    "proof",
    [
        "",
        "   \n\n  \n",
        hex(150),
        TIP_HASH,
        f"{hex(150)}\n{TIP_HASH}\nextra",
        f"tip: {hex(150)}\n{TIP_HASH}",
        f'{{"tip": "{hex(150)}"}}\n{TIP_HASH}',
        f"150\n{TIP_HASH}",
        f"0x01\n{TIP_HASH}",
        f"0x\n{TIP_HASH}",
        f"-0x96\n{TIP_HASH}",
        f"{hex(150)}\n0x" + "ab" * 31,
        f"{hex(150)}\n0x" + "ab" * 33,
        f"{hex(150)}\n0x" + "zz" * 32,
        f'"{hex(150)}\n{TIP_HASH}',
        f"{hex(150)}\r{TIP_HASH}",
        f"{TIP_HASH}\n{hex(150)}",
    ],
    ids=["empty", "blank-only", "tip-only", "hash-only", "third-line", "labelled", "json",
         "decimal-tip", "leading-zero-tip", "prefix-only-tip", "signed-tip", "short-hash",
         "long-hash", "non-hex-hash", "unmatched-quote", "bare-cr", "swapped"],
)
def test_tip_identity_malformed_proof_never_reaches_rpc(proof):
    rpc = RpcLedger(tip=100_000)
    v = check_tip_block_identity("t1", proof, _spec("tip_block_identity"), _tip_private(100), rpc)
    assert not v.passed
    assert rpc.calls == [], f"malformed proof reached RPC: {rpc.calls}"
    assert v.proof == proof


@pytest.mark.parametrize(
    "proof",
    [
        f"  {hex(150)}  \n  {TIP_HASH}  ",
        f"\n\n{hex(150)}\n\n{TIP_HASH}\n\n",
        f"{hex(150)}\r\n{TIP_HASH}\r\n",
        f'"{hex(150)}"\n"{TIP_HASH}"',
        f'" {hex(150)} "\r\n" {TIP_HASH.upper()} "\r\n',
        f"0X96\n{TIP_HASH.upper()}",
    ],
    ids=["surrounding-space", "blank-padded", "crlf", "quoted", "quoted-upper-crlf", "upper-prefix"],
)
def test_tip_identity_accepts_the_documented_normalizations(proof):
    rpc = RpcLedger(tip=100_000)
    v = check_tip_block_identity("t1", proof, _spec("tip_block_identity"), _tip_private(100), rpc)
    assert v.passed, v.reason
    assert v.proof == proof


@pytest.mark.parametrize(
    "private",
    [{}, {"harness_tip": True}, {"harness_tip": -1}, {"harness_tip": "100"},
     {"harness_tip": 1.5}, {"harness_tip": None}],
    ids=["missing", "bool", "negative", "string", "float", "none"],
)
def test_tip_identity_bad_verifier_private_is_infrastructure(private):
    """Harness misconfiguration must never be scored against the agent."""
    rpc = RpcLedger(tip=100_000)
    with pytest.raises(VerificationInfrastructureError, match="harness_tip"):
        check_tip_block_identity("t1", _tip_proof(150), _spec("tip_block_identity"), private, rpc)
    assert rpc.calls == []


@pytest.mark.parametrize("failing", ["get_tip_block_number", "get_block_hash"])
def test_tip_identity_rpc_exception_is_infrastructure_with_cause(failing):
    def rpc(method, params):
        if method == failing:
            raise ConnectionError("SENSITIVE_TRANSPORT_DETAIL")
        return hex(100_000) if method == "get_tip_block_number" else TIP_HASH

    with pytest.raises(VerificationInfrastructureError) as excinfo:
        check_tip_block_identity("t1", _tip_proof(150), _spec("tip_block_identity"),
                                 _tip_private(100), rpc)
    assert failing in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ConnectionError)
    assert "SENSITIVE_TRANSPORT_DETAIL" not in str(excinfo.value)


@pytest.mark.parametrize(
    "tip_response",
    [None, 100, "0x", "0x01", "not-hex", "96"],
    ids=["none", "int", "prefix-only", "leading-zero", "non-hex", "no-prefix"],
)
def test_tip_identity_malformed_verify_tip_is_infrastructure(tip_response):
    # Returned verbatim: RpcLedger would helpfully hex-encode an int and hide the case.
    rpc = lambda m, p: tip_response if m == "get_tip_block_number" else TIP_HASH  # noqa: E731
    with pytest.raises(VerificationInfrastructureError, match="get_tip_block_number"):
        check_tip_block_identity("t1", _tip_proof(150), _spec("tip_block_identity"),
                                 _tip_private(100), rpc)


@pytest.mark.parametrize(
    "hash_response",
    [None, 7, "0x" + "ab" * 31, "0x" + "ab" * 33, "0x" + "zz" * 32, "ab" * 32],
    ids=["none", "int", "short", "long", "non-hex", "no-prefix"],
)
def test_tip_identity_malformed_block_hash_is_infrastructure(hash_response):
    rpc = RpcLedger(tip=100_000, hashes={150: hash_response})
    with pytest.raises(VerificationInfrastructureError, match="get_block_hash"):
        check_tip_block_identity("t1", _tip_proof(150), _spec("tip_block_identity"),
                                 _tip_private(100), rpc)


def test_tip_identity_stale_reason_never_leaks_the_private_lower_bound():
    """The reason is copied verbatim into the persisted result, so the verifier-private run-start
    height must not appear in it."""
    sentinel = 987654321
    rpc = RpcLedger(tip=sentinel + 10)
    v = check_tip_block_identity("t1", _tip_proof(1), _spec("tip_block_identity"),
                                 {"harness_tip": sentinel}, rpc)
    assert not v.passed
    assert "predates run start" in v.reason
    assert str(sentinel) not in v.reason
    assert hex(sentinel) not in v.reason


def test_tip_identity_preserves_raw_proof_on_both_outcomes():
    raw_ok = f'  "{hex(150)}"  \r\n  {TIP_HASH}  \r\n'
    rpc = RpcLedger(tip=100_000)
    ok = check_tip_block_identity("t1", raw_ok, _spec("tip_block_identity"), _tip_private(100), rpc)
    assert ok.passed and ok.proof == raw_ok
    raw_bad = f"  {hex(150)}  \n  {OTHER_HASH}  \n"
    bad = check_tip_block_identity("t1", raw_bad, _spec("tip_block_identity"),
                                   _tip_private(100), RpcLedger(tip=100_000))
    assert not bad.passed and bad.proof == raw_bad


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
            "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32},
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
            "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32},
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
            "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32},
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
            "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32},
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
        ({"get_transaction": {"tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}},
          "get_header": {"number": "0x96"}}, "no-transaction-object"),
        ({"get_transaction": {"transaction": {}, "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}},
          "get_header": {"number": "0x96"}}, "no-output-list"),
        ({"get_transaction": {"transaction": {"outputs": "nope"},
                              "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}},
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
            "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32},
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



R1_TX = "0x" + "aa" * 32
R1_PREV = "0x" + "11" * 32
R1_PAYLOAD = "0x" + "5e" * 32
R1_RECIP = "0x470dcdc5e44064909650113a274b3b36aecb6dc7"
R1_SPEC = _spec("type_id_data_cell")
R1_PRIVATE = {"harness_tip": 100, "expected_payload_hex": R1_PAYLOAD, "expected_recipient_args": R1_RECIP}

R1_INPUT0 = {"since": "0x0", "previous_output": {"tx_hash": R1_PREV, "index": "0x0"}}
R1_ARGS = type_id_args(R1_INPUT0, 0)
R1_SCRIPT_HASH = "0x" + ckb_blake2b(
    molecule_script(bytes.fromhex(TYPE_ID_CODE_HASH[2:]), "type", R1_ARGS)
).hex()


def _r1_tx(outputs=None, data=None, inputs=None):
    return {
        "transaction": {
            "inputs": inputs if inputs is not None else [R1_INPUT0],
            "outputs": outputs if outputs is not None else [_r1_out()],
            "outputs_data": data if data is not None else [R1_PAYLOAD],
        },
        "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32},
    }


def _r1_out(**kw):
    out = {
        "capacity": hex(R1_CAPACITY_SHANNONS),
        "lock": {"code_hash": SECP_CODE_HASH, "hash_type": SECP_HASH_TYPE, "args": R1_RECIP},
        "type": {"code_hash": TYPE_ID_CODE_HASH, "hash_type": "type", "args": "0x" + R1_ARGS.hex()},
    }
    out.update(kw)
    return out


def _r1_rpc(payload, block=150):
    def call(m, p):
        if m == "get_transaction":
            return payload
        if m == "get_header":
            return {"number": hex(block)}
        raise AssertionError(m)
    return call


def test_r1_molecule_script_matches_the_pinned_vector():
    """Fixture derived from ckb-gen-types 1.1.0 BytesBuilder/ScriptBuilder, not from these helpers.

    The Molecule Bytes header is the ITEM COUNT (0x20 for 32 args bytes), per
    `writer.write_all(&molecule::pack_number(self.0.len()))` in the generated builder.
    """
    args = type_id_args(
        {"since": "0x0", "previous_output": {"tx_hash": "0x" + "11" * 32, "index": "0x0"}}, 0
    )
    assert "0x" + args.hex() == (
        "0x95104db1ddfd88d13f9b38346ad1a273905f4d097a0aabefe0b9f6123568dd65"
    )
    ser = molecule_script(bytes.fromhex(TYPE_ID_CODE_HASH[2:]), "type", args)
    assert "0x" + ser.hex() == (
        "0x5500000010000000300000003100000000000000000000000000000000000000000000000000000000"
        "545950455f4944012000000095104db1ddfd88d13f9b38346ad1a273905f4d097a0aabefe0b9f6123568dd65"
    )
    assert "0x" + ckb_blake2b(ser).hex() == (
        "0x28259b82f3f7fc28418d6b01a61b2717afcd2fd39e6b07815233ea7e3ebfedf9"
    )


def test_r1_args_depend_on_input_bytes_and_output_index():
    base = {"since": "0x0", "previous_output": {"tx_hash": "0x" + "11" * 32, "index": "0x0"}}
    other_input = {"since": "0x0", "previous_output": {"tx_hash": "0x" + "22" * 32, "index": "0x0"}}
    other_index = {"since": "0x0", "previous_output": {"tx_hash": "0x" + "11" * 32, "index": "0x1"}}
    assert type_id_args(base, 0) != type_id_args(other_input, 0)
    assert type_id_args(base, 0) != type_id_args(other_index, 0)
    assert type_id_args(base, 0) != type_id_args(base, 1), "output index must be load-bearing"


def test_r1_valid_deployment_passes():
    v = check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                _r1_rpc(_r1_tx()))
    assert v.passed, v.reason


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda: _r1_tx(outputs=[_r1_out(capacity="0x1")]), "wrong-capacity"),
        (lambda: _r1_tx(data=["0x" + "ff" * 32]), "wrong-payload"),
        (lambda: _r1_tx(data=["0x" + "5e" * 31]), "short-payload"),
        (lambda: _r1_tx(outputs=[_r1_out(type=None)]), "no-type-script"),
        (lambda: _r1_tx(outputs=[_r1_out(type={"code_hash": "0x" + "cd" * 32, "hash_type": "type",
                                               "args": "0x" + R1_ARGS.hex()})]), "wrong-code-hash"),
        (lambda: _r1_tx(outputs=[_r1_out(type={"code_hash": TYPE_ID_CODE_HASH, "hash_type": "data1",
                                               "args": "0x" + R1_ARGS.hex()})]), "wrong-hash-type"),
        (lambda: _r1_tx(inputs=[{"since": "0x0", "previous_output": {
            "tx_hash": "0x" + "22" * 32, "index": "0x0"}}]), "args-from-wrong-input"),
        (lambda: _r1_tx(outputs=[_r1_out(), _r1_out()], data=[R1_PAYLOAD, R1_PAYLOAD]),
         "two-type-id-outputs"),
        (lambda: _r1_tx(outputs=[{"capacity": "0x1", "lock": _r1_out()["lock"]}, _r1_out()],
                        data=["0x", R1_PAYLOAD]), "not-at-index-0"),
    ],
)
def test_r1_agent_mistakes_are_task_failures(mutate, why):
    v = check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                _r1_rpc(mutate()))
    assert not v.passed, why


@pytest.mark.parametrize(
    "proof",
    ["", R1_TX, f"{R1_TX}\n{R1_SCRIPT_HASH}\nextra", f"0x{'aa' * 31}\n{R1_SCRIPT_HASH}",
     f"tx: {R1_TX}\n{R1_SCRIPT_HASH}"],
    ids=["empty", "one-line", "three-lines", "short-hash", "labelled"],
)
def test_r1_malformed_proof_never_reaches_rpc(proof):
    spy = CountingRpc()
    v = check_type_id_data_cell("t", proof, R1_SPEC, R1_PRIVATE, spy)
    assert not v.passed
    assert spy.calls == 0
    assert v.proof == proof


@pytest.mark.parametrize(
    "private",
    [{}, {"harness_tip": 100, "expected_recipient_args": R1_RECIP},
     {"harness_tip": 100, "expected_payload_hex": "nope", "expected_recipient_args": R1_RECIP},
     {"harness_tip": True, "expected_payload_hex": R1_PAYLOAD, "expected_recipient_args": R1_RECIP}],
    ids=["empty", "missing-payload", "malformed-payload", "bool-tip"],
)
def test_r1_bad_verifier_private_is_infrastructure_without_rpc(private):
    spy = CountingRpc()
    with pytest.raises(VerificationInfrastructureError) as exc:
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, private, spy)
    assert spy.calls == 0
    assert R1_PAYLOAD not in str(exc.value) and R1_RECIP not in str(exc.value)


def test_r1_swapped_fields_fail_after_observation():
    """Both lines are valid 32-byte hex, so shape cannot catch a swap; the tx lookup does."""
    v = check_type_id_data_cell("t", f"{R1_SCRIPT_HASH}\n{R1_TX}", R1_SPEC, R1_PRIVATE,
                                lambda m, p: None)
    assert not v.passed


def test_r1_preserves_raw_proof_on_both_outcomes():
    raw = f'  "{R1_TX}"  \r\n  {R1_SCRIPT_HASH}  \r\n'
    ok = check_type_id_data_cell("t", raw, R1_SPEC, R1_PRIVATE, _r1_rpc(_r1_tx()))
    assert ok.passed and ok.proof == raw
    bad_raw = f"{R1_TX}\n0x{'ff' * 32}"
    bad = check_type_id_data_cell("t", bad_raw, R1_SPEC, R1_PRIVATE, _r1_rpc(_r1_tx()))
    assert not bad.passed and bad.proof == bad_raw


def test_r1_devnet_reset_absence_is_an_ordinary_not_found():
    """The pinned nervos/ckb v0.207.0 stack reports a never-seen tx as status "unknown" with no
    body. After a DevNet reset that is a valid negative observation, not an uninterpretable one."""
    v = check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                lambda m, p: {"tx_status": {"status": "unknown"}})
    assert not v.passed
    assert "not found" in v.reason


def test_r1_unknown_status_with_a_body_is_still_infrastructure():
    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell(
            "t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
            lambda m, p: {"transaction": {}, "tx_status": {"status": "unknown"}},
        )


def test_r1_stale_reason_never_leaks_the_private_lower_bound():
    sentinel = 987654321
    private = {**R1_PRIVATE, "harness_tip": sentinel}
    v = check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, private,
                                _r1_rpc(_r1_tx(), block=5))
    assert not v.passed and "STALE" in v.reason
    assert str(sentinel) not in v.reason and hex(sentinel) not in v.reason
    assert "harness_tip" not in v.reason


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda: _r1_tx(inputs=[{"since": "0x00", "previous_output": {
            "tx_hash": "0x" + "11" * 32, "index": "0x0"}}]), "leading-zero since"),
        (lambda: _r1_tx(inputs=[{"since": "0x0", "previous_output": {
            "tx_hash": "0x" + "11" * 32, "index": "0x00"}}]), "leading-zero index"),
        (lambda: _r1_tx(inputs=[{"since": "0x0", "previous_output": {
            "tx_hash": "0x" + "11" * 16 + " " + "11" * 16, "index": "0x0"}}]), "spaced hash"),
        (lambda: _r1_tx(inputs=[{"since": "0X0", "previous_output": {
            "tx_hash": "0x" + "11" * 32, "index": "0x0"}}]), "uppercase 0X since"),
        (lambda: _r1_tx(inputs=[{"since": "0x0", "previous_output": {
            "tx_hash": "0x" + "11" * 32, "index": "0X1"}}]), "uppercase 0X index"),
        (lambda: _r1_tx(outputs=[_r1_out(capacity="0X4A817C800")]), "uppercase capacity"),
        (lambda: _r1_tx(inputs=[{"since": "0x0", "previous_output": {
            "tx_hash": "0x" + "11" * 32, "index": "0x100000000"}}]), "u32 overflow index"),
        (lambda: _r1_tx(inputs=[{"since": "0x1" + "0" * 17, "previous_output": {
            "tx_hash": "0x" + "11" * 32, "index": "0x0"}}]), "oversize since"),
        (lambda: _r1_tx(outputs=[_r1_out(capacity="0x04a817c800")]), "leading-zero capacity"),
        (lambda: _r1_tx(data=["0x5"]), "odd-length wire data"),
        (lambda: _r1_tx(data=["0xzz"]), "non-hex wire data"),
    ],
)
def test_r1_non_canonical_wire_data_is_infrastructure_not_an_agent_failure(mutate, why):
    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                _r1_rpc(mutate())), why


def test_r1_valid_but_wrong_length_payload_stays_an_agent_failure():
    """Decodable-but-wrong is the agent's mistake; undecodable is the node's."""
    for data in ("0x" + "5e" * 31, "0x" + "5e" * 33):
        v = check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                    _r1_rpc(_r1_tx(data=[data])))
        assert not v.passed


@pytest.mark.parametrize(
    ("header", "why"),
    [
        ({"number": "0X96"}, "uppercase header prefix"),
        ({"number": "0x096"}, "leading-zero header"),
        ({"number": 150}, "non-string header"),
        ({}, "missing header number"),
        ("nope", "header not an object"),
    ],
)
def test_r1_malformed_header_is_infrastructure(header, why):
    def rpc(m, p):
        return _r1_tx() if m == "get_transaction" else header

    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE, rpc), why


@pytest.mark.parametrize(
    ("block_hash", "why"),
    [("0xb", "short block hash"), ("", "empty block hash"), (None, "missing block hash"),
     ("0x" + "bb" * 33, "long block hash")],
)
def test_r1_malformed_committed_block_hash_is_infrastructure(block_hash, why):
    payload = _r1_tx()
    payload["tx_status"]["block_hash"] = block_hash
    with pytest.raises(VerificationInfrastructureError, match="block_hash"):
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                lambda m, p: payload), why


@pytest.mark.parametrize("first", ["pending", "proposed"])
def test_r1_pending_then_committed_passes_on_virtual_time(first):
    clock = FakeClock()
    seen = []

    def rpc(m, p):
        if m == "get_header":
            return {"number": "0x96"}
        seen.append(m)
        return {"tx_status": {"status": first}} if len(seen) < 3 else _r1_tx()

    v = check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE, rpc,
                                monotonic_fn=clock.monotonic, sleep_fn=clock.sleep)
    assert v.passed, v.reason
    assert clock.sleeps == [2.0, 2.0]


def test_r1_pending_through_budget_is_a_task_failure():
    clock = FakeClock()
    v = check_type_id_data_cell(
        "t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
        lambda m, p: {"tx_status": {"status": "pending"}},
        monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
    )
    assert not v.passed and "not committed" in v.reason
    assert clock.sleeps == [2.0] * 45


@pytest.mark.parametrize("status", ["rejected"])
def test_r1_rejected_is_a_task_failure(status):
    v = check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                lambda m, p: {"tx_status": {"status": status}})
    assert not v.passed and status in v.reason


@pytest.mark.parametrize("failing", ["get_transaction", "get_header"])
def test_r1_transport_faults_are_infrastructure_with_cause(failing):
    def rpc(m, p):
        if m == failing:
            raise ConnectionError("R1_SENSITIVE_TRANSPORT")
        return _r1_tx() if m == "get_transaction" else {"number": "0x96"}

    with pytest.raises(VerificationInfrastructureError) as exc:
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE, rpc)
    assert isinstance(exc.value.__cause__, ConnectionError)
    assert "R1_SENSITIVE_TRANSPORT" not in str(exc.value)


@pytest.mark.parametrize(
    ("lock", "why"),
    [
        ({"code_hash": "0x" + "cd" * 32, "hash_type": SECP_HASH_TYPE, "args": R1_RECIP}, "code_hash"),
        ({"code_hash": SECP_CODE_HASH, "hash_type": "data1", "args": R1_RECIP}, "hash_type"),
        ({"code_hash": SECP_CODE_HASH, "hash_type": SECP_HASH_TYPE, "args": "0x" + "ab" * 20}, "args"),
    ],
)
def test_r1_each_lock_field_is_load_bearing(lock, why):
    v = check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                _r1_rpc(_r1_tx(outputs=[_r1_out(lock=lock)])))
    assert not v.passed, why


@pytest.mark.parametrize(
    "proof",
    [f"  {R1_TX}  \n  {R1_SCRIPT_HASH}  ", f"{R1_TX}\r\n{R1_SCRIPT_HASH}\r\n",
     f'"{R1_TX}"\n"{R1_SCRIPT_HASH}"', f"\n\n{R1_TX}\n\n{R1_SCRIPT_HASH}\n\n",
     f"{R1_TX.upper()}\n{R1_SCRIPT_HASH.upper()}"],
    ids=["spaces", "crlf", "quoted", "blank-padded", "uppercase"],
)
def test_r1_documented_proof_normalizations_are_accepted(proof):
    v = check_type_id_data_cell("t", proof, R1_SPEC, R1_PRIVATE, _r1_rpc(_r1_tx()))
    assert v.passed, v.reason
    assert v.proof == proof


def test_r1_long_data_and_hex_text_are_agent_failures():
    for data in ("0x" + "5e" * 40, "0x" + R1_PAYLOAD[2:].encode().hex()):
        v = check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                    _r1_rpc(_r1_tx(data=[data])))
        assert not v.passed


def test_r1_infrastructure_messages_redact_every_private_value():
    private = {"harness_tip": 424242, "expected_payload_hex": "0x" + "7a" * 32,
               "expected_recipient_args": "0x" + "9f" * 20}
    with pytest.raises(VerificationInfrastructureError) as exc:
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC,
                                {**private, "expected_payload_hex": "bad"},
                                lambda m, p: None)
    text = str(exc.value)
    assert "424242" not in text and "7a" * 32 not in text and "9f" * 20 not in text



def _r1_change_output(type_script=None):
    out = {"capacity": "0x1", "lock": _r1_out()["lock"]}
    if type_script is not None:
        out["type"] = type_script
    return out


@pytest.mark.parametrize(
    ("outputs", "why"),
    [
        ([_r1_out(), _r1_change_output()], "untyped change output"),
        ([_r1_out(), _r1_change_output(
            {"code_hash": "0x" + "cd" * 32, "hash_type": "type", "args": "0x"})],
         "unrelated typed change output"),
        ([_r1_out(), _r1_change_output(
            {"code_hash": TYPE_ID_CODE_HASH, "hash_type": "data1", "args": "0x" + "11" * 32})],
         "non-canonical type-id hash_type elsewhere"),
    ],
)
def test_r1_allowed_change_outputs_still_pass(outputs, why):
    v = check_type_id_data_cell(
        "t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
        _r1_rpc(_r1_tx(outputs=outputs, data=[R1_PAYLOAD] + ["0x"] * (len(outputs) - 1))),
    )
    assert v.passed, f"{why}: {v.reason}"


@pytest.mark.parametrize(
    ("args_hex", "why"),
    [
        ("0x" + R1_ARGS.hex()[:-4], "short args"),
        ("0x" + R1_ARGS.hex() + "ff", "long args"),
        ("0x" + R1_ARGS[::-1].hex(), "reversed-endianness args"),
        ("0x" + type_id_args(R1_INPUT0, 2).hex(), "args from output index 2"),
        ("0x" + "00" * 32, "zero args"),
    ],
)
def test_r1_wrong_type_id_args_are_agent_failures(args_hex, why):
    v = check_type_id_data_cell(
        "t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
        _r1_rpc(_r1_tx(outputs=[_r1_out(
            type={"code_hash": TYPE_ID_CODE_HASH, "hash_type": "type", "args": args_hex})])),
    )
    assert not v.passed, why


@pytest.mark.parametrize(
    ("proof", "why"),
    [
        (f"{R1_TX}\n0x{'cc' * 33}", "long script hash"),
        (f"{R1_TX}\n0x{'gg' * 32}", "non-hex script hash"),
        ('{"tx": "0x' + "aa" * 32 + '"}\n0x' + "cc" * 32, "json proof"),
        (f"{R1_TX} {R1_SCRIPT_HASH}", "single line, space separated"),
        (f"{R1_TX}\r{R1_SCRIPT_HASH}", "bare CR, no real line break"),
        (f'"{R1_TX}\n{R1_SCRIPT_HASH}', "unmatched quote"),
        ("0x\n0x", "prefixes only"),
    ],
)
def test_r1_more_malformed_proofs_never_reach_rpc(proof, why):
    spy = CountingRpc()
    v = check_type_id_data_cell("t", proof, R1_SPEC, R1_PRIVATE, spy)
    assert not v.passed, why
    assert spy.calls == 0, why
    assert v.proof == proof


@pytest.mark.parametrize(
    ("private", "why"),
    [
        ({"harness_tip": 100, "expected_payload_hex": R1_PAYLOAD}, "missing recipient args"),
        ({"harness_tip": 100, "expected_payload_hex": R1_PAYLOAD,
          "expected_recipient_args": "0x" + "ab" * 19}, "short recipient args"),
        ({"harness_tip": 100, "expected_payload_hex": R1_PAYLOAD,
          "expected_recipient_args": "nope"}, "malformed recipient args"),
        ({"harness_tip": -1, "expected_payload_hex": R1_PAYLOAD,
          "expected_recipient_args": R1_RECIP}, "negative harness tip"),
        ({"harness_tip": "100", "expected_payload_hex": R1_PAYLOAD,
          "expected_recipient_args": R1_RECIP}, "string harness tip"),
        ({"harness_tip": 1.5, "expected_payload_hex": R1_PAYLOAD,
          "expected_recipient_args": R1_RECIP}, "float harness tip"),
        ({"harness_tip": 100, "expected_payload_hex": R1_PAYLOAD.upper(),
          "expected_recipient_args": R1_RECIP}, "uppercase payload is not canonical"),
    ],
)
def test_r1_more_bad_private_state_is_infrastructure_without_rpc(private, why):
    spy = CountingRpc()
    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, private, spy), why
    assert spy.calls == 0, why


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"transaction": None, "tx_status": {"status": "committed",
                                             "block_hash": "0x" + "bb" * 32}}, "no transaction"),
        ({"transaction": {"inputs": [], "outputs": [], "outputs_data": []},
          "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}}, "empty inputs"),
        ({"transaction": {"inputs": [R1_INPUT0], "outputs": "nope", "outputs_data": []},
          "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}}, "outputs not a list"),
        ({"transaction": {"inputs": [R1_INPUT0], "outputs": [], "outputs_data": "nope"},
          "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}}, "data not a list"),
        ({"transaction": {"inputs": ["nope"], "outputs": [_r1_out()], "outputs_data": [R1_PAYLOAD]},
          "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}}, "input 0 not object"),
        ({"transaction": {"inputs": [{"since": "0x0"}], "outputs": [_r1_out()],
                          "outputs_data": [R1_PAYLOAD]},
          "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}}, "no previous_output"),
        ({"transaction": {"inputs": [R1_INPUT0], "outputs": ["nope"], "outputs_data": [R1_PAYLOAD]},
          "tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}}, "output 0 not object"),
        ({"tx_status": {"status": "committed", "block_hash": "0x" + "bb" * 32}}, "no transaction key"),
    ],
)
def test_r1_malformed_transaction_structure_is_infrastructure(payload, why):
    def rpc(m, p):
        return payload if m == "get_transaction" else {"number": "0x96"}

    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE, rpc), why


@pytest.mark.parametrize(
    ("script", "why"),
    [
        ({"code_hash": TYPE_ID_CODE_HASH, "hash_type": "bogus", "args": "0x" + R1_ARGS.hex()},
         "unrecognized hash_type"),
        ({"code_hash": TYPE_ID_CODE_HASH, "hash_type": 1, "args": "0x" + R1_ARGS.hex()},
         "non-string hash_type"),
        ({"code_hash": "0xzz", "hash_type": "type", "args": "0x" + R1_ARGS.hex()},
         "non-hex code_hash"),
        ({"code_hash": TYPE_ID_CODE_HASH, "hash_type": "type", "args": "0xz"}, "non-hex args"),
        ({"hash_type": "type", "args": "0x" + R1_ARGS.hex()}, "missing code_hash"),
    ],
)
def test_r1_malformed_type_script_fields_are_infrastructure(script, why):
    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                _r1_rpc(_r1_tx(outputs=[_r1_out(type=script)]))), why


def test_r1_malformed_extra_output_script_is_infrastructure():
    """A later output we cannot read is unusable grading data, not an agent mistake."""
    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell(
            "t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
            _r1_rpc(_r1_tx(outputs=[_r1_out(), _r1_change_output({"hash_type": "type"})],
                           data=[R1_PAYLOAD, "0x"])),
        )


def test_r1_dispatcher_forwards_the_time_seams_to_type_id():
    """grade_onchain_task() must hand the clock to this checker, or production would really sleep."""
    clock = FakeClock()
    seen: list[str] = []

    def rpc(m, p):
        if m == "get_header":
            return {"number": "0x96"}
        seen.append(m)
        return {"tx_status": {"status": "pending"}} if len(seen) < 3 else _r1_tx()

    v = grade_onchain_task("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", _spec("type_id_data_cell"),
                           R1_PRIVATE, rpc,
                           monotonic_fn=clock.monotonic, sleep_fn=clock.sleep)
    assert v.passed, v.reason
    assert clock.sleeps == [2.0, 2.0]


@pytest.mark.parametrize(
    ("outputs", "data", "why"),
    [
        ([_r1_out()], [], "no data for one output"),
        ([_r1_out()], [R1_PAYLOAD, "0x"], "more data than outputs"),
        ([_r1_out(), _r1_change_output()], [R1_PAYLOAD], "more outputs than data"),
    ],
)
def test_r1_outputs_and_data_length_mismatch_is_infrastructure(outputs, data, why):
    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                _r1_rpc(_r1_tx(outputs=outputs, data=data))), why


@pytest.mark.parametrize(
    ("lock", "why"),
    [
        (None, "null lock"),
        (7, "scalar lock"),
        ("0xdeadbeef", "string lock"),
        ({}, "empty lock object"),
        ({"code_hash": SECP_CODE_HASH, "args": R1_RECIP}, "lock missing hash_type"),
        ({"code_hash": SECP_CODE_HASH, "hash_type": SECP_HASH_TYPE}, "lock missing args"),
        ({"code_hash": SECP_CODE_HASH, "hash_type": SECP_HASH_TYPE, "args": "0xz"}, "non-hex args"),
    ],
)
def test_r1_malformed_output_0_lock_is_infrastructure(lock, why):
    out = _r1_out()
    if lock is None:
        out.pop("lock")
    else:
        out["lock"] = lock
    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                _r1_rpc(_r1_tx(outputs=[out]))), why


def test_r1_explicit_null_output_0_lock_is_infrastructure():
    """Distinct from a missing key: an explicit JSON null lock must also be unusable data."""
    out = _r1_out()
    out["lock"] = None
    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell("t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE,
                                _r1_rpc(_r1_tx(outputs=[out])))


@pytest.mark.parametrize(
    ("output", "data", "masked_reason"),
    [
        (_r1_out(type=None), [R1_PAYLOAD], "output 0 has no type script"),
        (
            _r1_out(type={"code_hash": "0x" + "cd" * 32, "hash_type": "type",
                          "args": "0x" + R1_ARGS.hex()}),
            [R1_PAYLOAD],
            "output 0 type script is not canonical Type-ID",
        ),
        (
            _r1_out(type={"code_hash": TYPE_ID_CODE_HASH, "hash_type": "type",
                          "args": "0x" + R1_ARGS.hex()[:-2]}),
            [R1_PAYLOAD],
            "output 0 Type-ID args are not 32 bytes",
        ),
        (_r1_out(capacity="0x1"), [R1_PAYLOAD], "wrong capacity"),
        (_r1_out(), ["0x" + "ff" * 32], "wrong payload"),
    ],
)
@pytest.mark.parametrize(
    "bad_input",
    ["bad", None, 7, {}, {"previous_output": {"tx_hash": "0xzz", "index": "0x0"}}],
    ids=["string", "none", "int", "empty", "non-hex-tx-hash"],
)
def test_r1_malformed_input_zero_is_never_masked_by_an_output_failure(
    output, data, masked_reason, bad_input
):
    """A malformed input 0 is unusable observation data, not something to charge to the agent.

    Every one of these output-0 faults would produce an ordinary task failure on its own; combined
    with a malformed input 0 the result must still be an infrastructure failure.
    """
    payload = _r1_tx(outputs=[output], data=data, inputs=[bad_input])
    with pytest.raises(VerificationInfrastructureError):
        check_type_id_data_cell(
            "t", f"{R1_TX}\n{R1_SCRIPT_HASH}", R1_SPEC, R1_PRIVATE, _r1_rpc(payload)
        )


def test_r1_a_sound_input_zero_still_grades_output_failures_as_task_failures():
    """The reordering must not turn ordinary agent mistakes into infrastructure failures."""
    verdict = check_type_id_data_cell(
        "t",
        f"{R1_TX}\n{R1_SCRIPT_HASH}",
        R1_SPEC,
        R1_PRIVATE,
        _r1_rpc(_r1_tx(outputs=[_r1_out(type=None)], data=[R1_PAYLOAD])),
    )
    assert not verdict.passed
    assert verdict.reason == "output 0 has no type script"
