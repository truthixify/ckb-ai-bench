from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from ckbbench.suite.model import OnchainVerifierSpec
from ckbbench.verify.onchain import (
    ACP_CODE_HASH,
    R1_CAPACITY_SHANNONS,
    SECP_CODE_HASH,
    SECP_HASH_TYPE,
    SHANNONS_PER_CKB,
    SPORE_CODE_HASH,
    TYPE_ID_CODE_HASH,
    TYPE_ID_HASH_TYPE,
    XUDT_CODE_HASH,
    _MAX_SPORE_WITNESS_BYTES,
    ckb_blake2b,
    grade_onchain_task,
    molecule_script,
    onchain_criteria_total,
    script_hash,
    type_id_args,
)


TX_HASH = "0x" + "11" * 32
BLOCK_HASH = "0x" + "22" * 32
INPUT_HASH = "0x" + "33" * 32
OWN_ARGS = "0x" + "aa" * 20
RECIPIENTS = tuple("0x" + marker * 20 for marker in ("bb", "cc", "dd"))
MINIMUM_FEE = 100_000
MAXIMUM_FEE = 200_000


def _script(code_hash: str, hash_type: str, args: str) -> dict[str, str]:
    return {"args": args, "code_hash": code_hash, "hash_type": hash_type}


def _secp(args: str) -> dict[str, str]:
    return _script(SECP_CODE_HASH, SECP_HASH_TYPE, args)


OWN_LOCK = _secp(OWN_ARGS)


def _lease(
    capacity: int,
    *,
    index: int = 0,
    lock: dict[str, Any] | None = None,
    type_script: dict[str, Any] | None = None,
    data: str = "0x",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "capacity_shannons": capacity,
        "index": index,
        "tx_hash": INPUT_HASH,
    }
    if lock is not None or type_script is not None or data != "0x":
        row.update({"lock": lock, "output_data": data, "type": type_script})
    return row


def _input(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "previous_output": {"index": hex(row["index"]), "tx_hash": row["tx_hash"]},
        "since": "0x0",
    }


def _output(
    capacity: int,
    lock: dict[str, Any],
    *,
    type_script: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"capacity": hex(capacity), "lock": deepcopy(lock), "type": deepcopy(type_script)}


def _policy(
    leased: list[dict[str, Any]],
    dependencies: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "cell_deps": deepcopy(dependencies),
        "header_deps": [],
        "leased_inputs": deepcopy(leased),
        "maximum_fee_shannons": MAXIMUM_FEE,
        "minimum_fee_shannons": MINIMUM_FEE,
        "own_lock": deepcopy(OWN_LOCK),
    }


def _transaction(
    leased: list[dict[str, Any]],
    dependencies: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    data: list[str],
) -> dict[str, Any]:
    return {
        "cell_deps": deepcopy(dependencies),
        "header_deps": [],
        "inputs": [_input(row) for row in leased],
        "outputs": deepcopy(outputs),
        "outputs_data": list(data),
        "version": "0x0",
        "witnesses": ["0x" for _row in leased],
    }


class _Rpc:
    def __init__(self, transaction: dict[str, Any] | None, *, status: str = "committed", height: int = 101):
        self.transaction = transaction
        self.status = status
        self.height = height
        self.calls: list[tuple[str, list[Any]]] = []

    def __call__(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, list(params)))
        if method == "get_transaction":
            if self.status == "unknown":
                return {"transaction": None, "tx_status": {"status": "unknown"}}
            return {
                "transaction": deepcopy(self.transaction),
                "tx_status": {"block_hash": BLOCK_HASH, "status": self.status},
            }
        if method == "get_header":
            return {"number": hex(self.height)}
        raise AssertionError(method)


@dataclass
class _WorkflowCase:
    check: str
    private: dict[str, Any]
    transaction: dict[str, Any]


def _molecule_bytes(value: bytes) -> bytes:
    return len(value).to_bytes(4, "little") + value


def _molecule_table(fields: tuple[bytes, ...]) -> bytes:
    header_size = 4 + 4 * len(fields)
    offsets: list[int] = []
    cursor = header_size
    for field in fields:
        offsets.append(cursor)
        cursor += len(field)
    return (
        cursor.to_bytes(4, "little")
        + b"".join(offset.to_bytes(4, "little") for offset in offsets)
        + b"".join(fields)
    )


def _molecule_vector(items: tuple[bytes, ...]) -> bytes:
    return _molecule_table(items)


def _spore_cobuild_witness(
    transaction: dict[str, Any], *, duplicate_action: bool = False
) -> str:
    output = transaction["outputs"][0]
    _type_code, _type_hash_type, type_args, type_digest = script_hash(
        output["type"], "fixture Spore type"
    )
    lock_code, lock_hash_type, lock_args, _lock_digest = script_hash(
        output["lock"], "fixture Spore lock"
    )
    output_data = bytes.fromhex(transaction["outputs_data"][0][2:])
    address = (0).to_bytes(4, "little") + molecule_script(
        lock_code, lock_hash_type, lock_args
    )
    action_data = (0).to_bytes(4, "little") + _molecule_table(
        (
            type_args,
            address,
            ckb_blake2b(output_data),
        )
    )
    action = _molecule_table(
        (
            ckb_blake2b(b"CCC_DEFAULT_COBUILD_INFO"),
            type_digest,
            _molecule_bytes(action_data),
        )
    )
    actions = (action, action) if duplicate_action else (action,)
    message = _molecule_table((_molecule_vector(actions),))
    sighash_all = _molecule_table((_molecule_bytes(b""), message))
    witness = (0xFF000001).to_bytes(4, "little") + sighash_all
    return "0x" + witness.hex()


def _case(check: str, *, alternate: bool = False) -> _WorkflowCase:
    secp_dep = {
        "dep_type": "dep_group",
        "out_point": {"index": "0x0", "tx_hash": "0x" + "44" * 32},
    }
    xudt_dep = {
        "dep_type": "code",
        "out_point": {"index": "0x0", "tx_hash": "0x" + "55" * 32},
    }
    acp_dep = {
        "dep_type": "dep_group",
        "out_point": {"index": "0x0", "tx_hash": "0x" + "66" * 32},
    }
    spore_dep = {
        "dep_type": "code",
        "out_point": {"index": "0x0", "tx_hash": "0x" + "77" * 32},
    }
    fee = MAXIMUM_FEE if alternate else MINIMUM_FEE

    if check == "multi_recipient_transfer":
        leased = [_lease(30_000_000_000)]
        amounts = (6_200_000_001, 6_300_000_003, 6_400_000_007)
        payments = [
            _output(amount, _secp(recipient))
            for recipient, amount in zip(RECIPIENTS, amounts, strict=True)
        ]
        if alternate:
            payments.reverse()
        outputs = payments + [_output(leased[0]["capacity_shannons"] - sum(amounts) - fee, OWN_LOCK)]
        private = {
            **{f"recipient_args_{index}": value for index, value in enumerate(RECIPIENTS, 1)},
            **{f"send_amount_shannons_{index}": str(value) for index, value in enumerate(amounts, 1)},
        }
        data = ["0x"] * 4
        deps = [secp_dep]
    elif check == "type_id_upgrade":
        predecessor_type = _script(TYPE_ID_CODE_HASH, TYPE_ID_HASH_TYPE, "0x" + "88" * 32)
        leased = [_lease(20_000_000_000, lock=OWN_LOCK, type_script=predecessor_type, data="0x" + "99" * 32)]
        payload = "0x" + "ab" * 32
        outputs = [_output(leased[0]["capacity_shannons"] - fee, OWN_LOCK, type_script=predecessor_type)]
        data = [payload]
        private = {"expected_payload_hex": payload}
        deps = [secp_dep]
    elif check == "xudt_issuance":
        leased = [_lease(30_000_000_000)]
        amount = 987_654_321
        recipient_capacity = 15_000_000_000
        owner_hash = "0x" + script_hash(OWN_LOCK, "owner lock")[3].hex()
        token_type = _script(XUDT_CODE_HASH, "type", owner_hash)
        outputs = [
            _output(recipient_capacity, _secp(RECIPIENTS[0]), type_script=token_type),
            _output(leased[0]["capacity_shannons"] - recipient_capacity - fee, OWN_LOCK),
        ]
        data = ["0x" + amount.to_bytes(16, "little").hex(), "0x"]
        if alternate:
            outputs.reverse()
            data.reverse()
        private = {
            "recipient_args": RECIPIENTS[0],
            "recipient_capacity_shannons": str(recipient_capacity),
            "token_amount": str(amount),
        }
        deps = [secp_dep, xudt_dep]
    elif check == "xudt_transfer":
        initial = 1_000_000_000
        amount = 123_456_789
        recipient_capacity = 15_000_000_000
        token_type = _script(XUDT_CODE_HASH, "type", "0x" + "88" * 32)
        leased = [_lease(30_000_000_000, lock=OWN_LOCK, type_script=token_type, data="0x" + initial.to_bytes(16, "little").hex())]
        outputs = [
            _output(recipient_capacity, _secp(RECIPIENTS[0]), type_script=token_type),
            _output(leased[0]["capacity_shannons"] - recipient_capacity - fee, OWN_LOCK, type_script=token_type),
        ]
        data = [
            "0x" + amount.to_bytes(16, "little").hex(),
            "0x" + (initial - amount).to_bytes(16, "little").hex(),
        ]
        if alternate:
            outputs.reverse()
            data.reverse()
        private = {
            "recipient_args": RECIPIENTS[0],
            "recipient_capacity_shannons": str(recipient_capacity),
            "token_amount": str(amount),
        }
        deps = [secp_dep, xudt_dep]
    elif check == "acp_deposit":
        acp_lock = _script(ACP_CODE_HASH, "type", OWN_ARGS)
        leased = [
            _lease(14_200_000_000, index=0, lock=acp_lock),
            _lease(20_000_000_000, index=1, lock=OWN_LOCK),
        ]
        increase = 1_000_000_000
        outputs = [
            _output(leased[0]["capacity_shannons"] + increase, acp_lock),
            _output(leased[1]["capacity_shannons"] - increase - fee, OWN_LOCK),
        ]
        data = ["0x", "0x"]
        if alternate:
            outputs.reverse()
            data.reverse()
        private = {"capacity_increase_shannons": str(increase)}
        deps = [acp_dep, secp_dep]
    elif check == "spore_creation":
        leased = [_lease(40_000_000_000)]
        content = bytes.fromhex("12" * 32)
        content_type = "application/octet-stream"
        encoded = _molecule_table(
            (_molecule_bytes(content_type.encode()), _molecule_bytes(content), b"")
        )
        input_zero = _input(leased[0])
        spore_type = _script(
            SPORE_CODE_HASH,
            "data1",
            "0x" + type_id_args(input_zero, 0).hex(),
        )
        capacity = (8 + 53 + 65 + len(encoded)) * SHANNONS_PER_CKB
        outputs = [
            _output(capacity, _secp(RECIPIENTS[0]), type_script=spore_type),
            _output(leased[0]["capacity_shannons"] - capacity - fee, OWN_LOCK),
        ]
        data = ["0x" + encoded.hex(), "0x"]
        private = {
            "expected_content_hex": "0x" + content.hex(),
            "expected_content_type": content_type,
            "recipient_args": RECIPIENTS[0],
            "spore_capacity_shannons": str(capacity),
        }
        deps = [secp_dep, spore_dep]
    else:
        raise AssertionError(check)

    transaction = _transaction(leased, deps, outputs, data)
    if check == "spore_creation":
        transaction["witnesses"].append(_spore_cobuild_witness(transaction))
    private.update({
        "_submitted_transaction_hash": TX_HASH,
        "harness_tip": 100,
        "signing_policy": _policy(leased, deps),
    })
    return _WorkflowCase(
        check=check,
        private=private,
        transaction=transaction,
    )


WORKFLOWS = (
    "multi_recipient_transfer",
    "type_id_upgrade",
    "xudt_issuance",
    "xudt_transfer",
    "acp_deposit",
    "spore_creation",
)


def _grade(case: _WorkflowCase, rpc: _Rpc | None = None, proof: str = TX_HASH):
    return grade_onchain_task(
        "workflow",
        proof,
        OnchainVerifierSpec(check=case.check, rpc_method="get_transaction"),
        case.private,
        rpc or _Rpc(case.transaction),
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _seconds: None,
    )


def _legacy_transfer_case() -> _WorkflowCase:
    leased = [_lease(30_000_000_000)]
    dependencies = [{
        "dep_type": "dep_group",
        "out_point": {"index": "0x0", "tx_hash": "0x" + "44" * 32},
    }]
    amount = 10_000_000_000
    fee = MINIMUM_FEE
    transaction = _transaction(
        leased,
        dependencies,
        [
            _output(amount, _secp(RECIPIENTS[0])),
            _output(leased[0]["capacity_shannons"] - amount - fee, OWN_LOCK),
        ],
        ["0x", "0x"],
    )
    return _WorkflowCase(
        check="tx_proof",
        private={
            "_submitted_transaction_hash": TX_HASH,
            "harness_tip": 100,
            "nonce_amount_shannons": str(amount),
            "recipient_args": RECIPIENTS[0],
            "signing_policy": _policy(leased, dependencies),
        },
        transaction=transaction,
    )


def _legacy_type_id_case() -> tuple[_WorkflowCase, str]:
    leased = [_lease(30_000_000_000)]
    dependencies = [{
        "dep_type": "dep_group",
        "out_point": {"index": "0x0", "tx_hash": "0x" + "44" * 32},
    }]
    input_zero = _input(leased[0])
    type_script = _script(
        TYPE_ID_CODE_HASH,
        TYPE_ID_HASH_TYPE,
        "0x" + type_id_args(input_zero, 0).hex(),
    )
    payload = "0x" + "ab" * 32
    fee = MINIMUM_FEE
    transaction = _transaction(
        leased,
        dependencies,
        [
            _output(R1_CAPACITY_SHANNONS, _secp(RECIPIENTS[0]), type_script=type_script),
            _output(leased[0]["capacity_shannons"] - R1_CAPACITY_SHANNONS - fee, OWN_LOCK),
        ],
        [payload, "0x"],
    )
    script_digest = "0x" + ckb_blake2b(
        molecule_script(
            bytes.fromhex(TYPE_ID_CODE_HASH[2:]),
            TYPE_ID_HASH_TYPE,
            type_id_args(input_zero, 0),
        )
    ).hex()
    return (
        _WorkflowCase(
            check="type_id_data_cell",
            private={
                "_submitted_transaction_hash": TX_HASH,
                "expected_payload_hex": payload,
                "expected_recipient_args": RECIPIENTS[0],
                "harness_tip": 100,
                "signing_policy": _policy(leased, dependencies),
            },
            transaction=transaction,
        ),
        f"{TX_HASH}\n{script_digest}",
    )


def test_legacy_transfer_verifier_binds_proof_to_the_submitted_transaction_and_policy():
    case = _legacy_transfer_case()
    assert _grade(case).passed

    case.private["_submitted_transaction_hash"] = "0x" + "99" * 32
    refused = _grade(case)
    assert not refused.passed
    assert "submitted transaction" in refused.reason

    case = _legacy_transfer_case()
    case.transaction["outputs"][1]["lock"] = _secp(RECIPIENTS[1])
    refused = _grade(case)
    assert not refused.passed
    assert "change output" in refused.reason


def test_legacy_type_id_verifier_binds_proof_to_the_submitted_transaction_and_policy():
    case, proof = _legacy_type_id_case()
    assert _grade(case, proof=proof).passed

    case.private["_submitted_transaction_hash"] = "0x" + "99" * 32
    refused = _grade(case, proof=proof)
    assert not refused.passed
    assert "submitted transaction" in refused.reason

    case, proof = _legacy_type_id_case()
    case.transaction["outputs"][0]["type"]["args"] = "0x" + "00" * 32
    refused = _grade(case, proof=proof)
    assert not refused.passed
    assert "Type-ID script" in refused.reason


@pytest.mark.parametrize("check", WORKFLOWS)
def test_workflow_reference_and_alternate_are_repeatable(check: str):
    reference = _case(check)
    alternate = _case(check, alternate=True)
    for _iteration in range(3):
        first = _grade(reference)
        second = _grade(alternate)
        assert first.passed, first.reason
        assert second.passed, second.reason
        assert first.diagnostics.criteria_total == onchain_criteria_total(check)
        assert second.diagnostics.criteria_failed == 0


@pytest.mark.parametrize("check", WORKFLOWS)
@pytest.mark.parametrize(
    "mutation",
    ("malformed-proof", "missing", "rejected", "stale", "input", "dependency", "fee"),
)
def test_workflow_common_mutants_fail(check: str, mutation: str):
    case = _case(check)
    proof = TX_HASH
    rpc = _Rpc(case.transaction)
    if mutation == "malformed-proof":
        proof = "not-a-transaction"
    elif mutation == "missing":
        rpc = _Rpc(None, status="unknown")
    elif mutation == "rejected":
        rpc = _Rpc(case.transaction, status="rejected")
    elif mutation == "stale":
        rpc = _Rpc(case.transaction, height=99)
    elif mutation == "input":
        case.transaction["inputs"][0]["previous_output"]["tx_hash"] = "0x" + "fe" * 32
    elif mutation == "dependency":
        case.transaction["cell_deps"] = []
    elif mutation == "fee":
        current = int(case.transaction["outputs"][-1]["capacity"], 16)
        case.transaction["outputs"][-1]["capacity"] = hex(current - MAXIMUM_FEE)
    else:
        raise AssertionError(mutation)

    verdict = _grade(case, rpc=rpc, proof=proof)
    assert not verdict.passed
    assert verdict.diagnostics.criteria_failed == 1


def _mutate_multi(case: _WorkflowCase, mutation: str) -> None:
    if mutation == "output-count":
        case.transaction["outputs"].pop(0)
        case.transaction["outputs_data"].pop(0)
    elif mutation == "recipient":
        case.transaction["outputs"][0]["lock"] = _secp("0x" + "ee" * 20)
    elif mutation == "amount":
        case.transaction["outputs"][0]["capacity"] = hex(
            int(case.transaction["outputs"][0]["capacity"], 16) + 1
        )
        case.transaction["outputs"][-1]["capacity"] = hex(
            int(case.transaction["outputs"][-1]["capacity"], 16) - 1
        )
    elif mutation == "typed-payment":
        case.transaction["outputs"][0]["type"] = _script(XUDT_CODE_HASH, "type", "0x" + "11" * 32)
    elif mutation == "change-lock":
        case.transaction["outputs"][-1]["lock"] = _secp("0x" + "ef" * 20)
    elif mutation == "zero-change":
        case.transaction["outputs"][-1]["capacity"] = "0x0"
    else:
        raise AssertionError(mutation)


@pytest.mark.parametrize(
    "mutation",
    ("output-count", "recipient", "amount", "typed-payment", "change-lock", "zero-change"),
)
def test_multi_recipient_mutants_fail(mutation: str):
    case = _case("multi_recipient_transfer")
    _mutate_multi(case, mutation)
    assert not _grade(case).passed


@pytest.mark.parametrize(
    "mutation",
    ("untyped-input", "wrong-input-type", "extra-output", "changed-type", "changed-lock", "old-data"),
)
def test_type_id_upgrade_mutants_fail(mutation: str):
    case = _case("type_id_upgrade")
    leased = case.private["signing_policy"]["leased_inputs"][0]
    if mutation == "untyped-input":
        leased["type"] = None
    elif mutation == "wrong-input-type":
        leased["type"]["code_hash"] = XUDT_CODE_HASH
    elif mutation == "extra-output":
        case.transaction["outputs"].append(_output(1, OWN_LOCK))
        case.transaction["outputs_data"].append("0x")
    elif mutation == "changed-type":
        case.transaction["outputs"][0]["type"]["args"] = "0x" + "ee" * 32
    elif mutation == "changed-lock":
        case.transaction["outputs"][0]["lock"] = _secp(RECIPIENTS[0])
    elif mutation == "old-data":
        case.transaction["outputs_data"][0] = leased["output_data"]
    else:
        raise AssertionError(mutation)
    assert not _grade(case).passed


@pytest.mark.parametrize(
    "mutation",
    ("typed-input", "output-count", "wrong-owner", "wrong-type", "capacity", "amount", "change"),
)
def test_xudt_issuance_mutants_fail(mutation: str):
    case = _case("xudt_issuance")
    if mutation == "typed-input":
        case.private["signing_policy"]["leased_inputs"][0]["type"] = _script(XUDT_CODE_HASH, "type", "0x" + "11" * 32)
    elif mutation == "output-count":
        case.transaction["outputs"].pop()
        case.transaction["outputs_data"].pop()
    elif mutation == "wrong-owner":
        case.transaction["outputs"][0]["lock"] = _secp(RECIPIENTS[1])
    elif mutation == "wrong-type":
        case.transaction["outputs"][0]["type"]["args"] = "0x" + "99" * 32
    elif mutation == "capacity":
        case.transaction["outputs"][0]["capacity"] = hex(
            int(case.transaction["outputs"][0]["capacity"], 16) + 1
        )
        case.transaction["outputs"][1]["capacity"] = hex(
            int(case.transaction["outputs"][1]["capacity"], 16) - 1
        )
    elif mutation == "amount":
        case.transaction["outputs_data"][0] = "0x" + (1).to_bytes(16, "little").hex()
    elif mutation == "change":
        case.transaction["outputs"][1]["lock"] = _secp(RECIPIENTS[1])
    else:
        raise AssertionError(mutation)
    assert not _grade(case).passed


@pytest.mark.parametrize(
    "mutation",
    ("untyped-input", "wrong-input-type", "bad-input-data", "output-count", "recipient", "change", "amount", "mint"),
)
def test_xudt_transfer_mutants_fail(mutation: str):
    case = _case("xudt_transfer")
    leased = case.private["signing_policy"]["leased_inputs"][0]
    if mutation == "untyped-input":
        leased["type"] = None
    elif mutation == "wrong-input-type":
        leased["type"]["code_hash"] = TYPE_ID_CODE_HASH
    elif mutation == "bad-input-data":
        leased["output_data"] = "0x01"
    elif mutation == "output-count":
        case.transaction["outputs"].pop()
        case.transaction["outputs_data"].pop()
    elif mutation == "recipient":
        case.transaction["outputs"][0]["lock"] = _secp(RECIPIENTS[1])
    elif mutation == "change":
        case.transaction["outputs"][1]["type"]["args"] = "0x" + "99" * 32
    elif mutation == "amount":
        case.transaction["outputs_data"][0] = "0x" + (1).to_bytes(16, "little").hex()
    elif mutation == "mint":
        value = int.from_bytes(bytes.fromhex(case.transaction["outputs_data"][1][2:]), "little")
        case.transaction["outputs_data"][1] = "0x" + (value + 1).to_bytes(16, "little").hex()
    else:
        raise AssertionError(mutation)
    assert not _grade(case).passed


@pytest.mark.parametrize(
    "mutation",
    ("one-input", "wrong-lock", "typed-acp", "output-count", "increase", "change"),
)
def test_acp_deposit_mutants_fail(mutation: str):
    case = _case("acp_deposit")
    leased = case.private["signing_policy"]["leased_inputs"]
    if mutation == "one-input":
        leased.pop()
    elif mutation == "wrong-lock":
        leased[0]["lock"] = OWN_LOCK
    elif mutation == "typed-acp":
        leased[0]["type"] = _script(XUDT_CODE_HASH, "type", "0x" + "11" * 32)
    elif mutation == "output-count":
        case.transaction["outputs"].pop()
        case.transaction["outputs_data"].pop()
    elif mutation == "increase":
        case.transaction["outputs"][0]["capacity"] = hex(
            int(case.transaction["outputs"][0]["capacity"], 16) + 1
        )
        case.transaction["outputs"][1]["capacity"] = hex(
            int(case.transaction["outputs"][1]["capacity"], 16) - 1
        )
    elif mutation == "change":
        case.transaction["outputs"][1]["lock"] = _secp(RECIPIENTS[0])
    else:
        raise AssertionError(mutation)
    assert not _grade(case).passed


@pytest.mark.parametrize(
    "mutation",
    (
        "typed-input",
        "output-count",
        "wrong-id",
        "wrong-owner",
        "content",
        "capacity",
        "change",
        "duplicate",
        "missing-cobuild",
        "wrong-cobuild",
        "duplicate-cobuild",
    ),
)
def test_spore_creation_mutants_fail(mutation: str):
    case = _case("spore_creation")
    if mutation == "typed-input":
        case.private["signing_policy"]["leased_inputs"][0]["type"] = _script(XUDT_CODE_HASH, "type", "0x" + "11" * 32)
    elif mutation == "output-count":
        case.transaction["outputs"].pop()
        case.transaction["outputs_data"].pop()
    elif mutation == "wrong-id":
        case.transaction["outputs"][0]["type"]["args"] = "0x" + "99" * 32
    elif mutation == "wrong-owner":
        case.transaction["outputs"][0]["lock"] = _secp(RECIPIENTS[1])
    elif mutation == "content":
        case.transaction["outputs_data"][0] = case.transaction["outputs_data"][0][:-2] + "ff"
    elif mutation == "capacity":
        case.transaction["outputs"][0]["capacity"] = hex(
            int(case.transaction["outputs"][0]["capacity"], 16) - SHANNONS_PER_CKB
        )
        case.transaction["outputs"][1]["capacity"] = hex(
            int(case.transaction["outputs"][1]["capacity"], 16) + SHANNONS_PER_CKB
        )
    elif mutation == "change":
        case.transaction["outputs"][1]["lock"] = _secp(RECIPIENTS[1])
    elif mutation == "duplicate":
        case.transaction["outputs"].append(deepcopy(case.transaction["outputs"][0]))
        case.transaction["outputs_data"].append(case.transaction["outputs_data"][0])
    elif mutation == "missing-cobuild":
        case.transaction["witnesses"].pop()
    elif mutation == "wrong-cobuild":
        witness = bytearray(bytes.fromhex(case.transaction["witnesses"][-1][2:]))
        witness[-1] ^= 1
        case.transaction["witnesses"][-1] = "0x" + witness.hex()
    elif mutation == "duplicate-cobuild":
        case.transaction["witnesses"][-1] = _spore_cobuild_witness(
            case.transaction, duplicate_action=True
        )
    else:
        raise AssertionError(mutation)
    assert not _grade(case).passed


def test_spore_creation_rejects_an_oversized_cobuild_witness():
    case = _case("spore_creation")
    case.transaction["witnesses"][-1] = "0x" + "00" * (_MAX_SPORE_WITNESS_BYTES + 1)
    assert not _grade(case).passed


def test_verifier_private_workflow_values_never_appear_in_failure_reasons():
    sentinel = "0x" + "de" * 20
    case = _case("multi_recipient_transfer")
    case.private["recipient_args_1"] = sentinel
    verdict = _grade(case)
    assert not verdict.passed
    assert sentinel not in verdict.reason


def test_acp_predecessor_must_belong_to_the_signer():
    case = _case("acp_deposit")
    case.private["signing_policy"]["leased_inputs"][0]["lock"]["args"] = RECIPIENTS[0]
    verdict = _grade(case)
    assert not verdict.passed
    assert "signer's plain-capacity deposit cell" in verdict.reason


def test_acp_change_is_the_thirteenth_independent_criterion():
    case = _case("acp_deposit")
    case.transaction["outputs"][1]["lock"] = _secp(RECIPIENTS[0])
    verdict = _grade(case)
    assert not verdict.passed
    assert verdict.diagnostics.criteria_passed == 12
    assert verdict.diagnostics.criteria_failed == 1
    assert verdict.diagnostics.criteria_total == 13
