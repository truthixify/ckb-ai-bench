"""Offline black-box grading for native project tasks."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import stat
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from ckbbench.suite.model import (
    PROJECT_VERIFIER_CASE_LIMITS,
    PROJECT_VERIFIER_CHECKS,
    ProjectVerifierSpec,
    Task,
)
from ckbbench.verify.codetask import (
    BENCH_PASSWORD_ENV,
    CODE_CHALLENGE_ENV,
    DEFAULT_BUILD_COMMAND,
    DEFAULT_VERIFY_COMMAND,
    RunnerCallable,
    RunnerInvocation,
    _runner_result,
    parse_libtest_diagnostics,
    prepare_artifact_dir,
)
from ckbbench.verify.diagnostics import VerificationDiagnostics
from ckbbench.verify.onchain import VerificationInfrastructureError, Verdict, ckb_blake2b

MAX_PROJECT_DOCUMENT_BYTES = 1 << 20
MAX_PROJECT_ARTIFACT_FILE_BYTES = 16 << 20
MAX_PROJECT_ARTIFACT_FILES = 256
MAX_PROJECT_ARTIFACT_BYTES = 64 << 20
MAX_PROJECT_ARTIFACT_ENTRIES = 512
PROJECT_CHECKS = PROJECT_VERIFIER_CHECKS

_BECH32_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_VALUES = {char: index for index, char in enumerate(_BECH32_ALPHABET)}
_HASH_TYPE_BYTES = {"data": 0, "type": 1, "data1": 2, "data2": 4}
_SECP_CODE_HASH = "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8"
_SIGHASH_DATA_HASH = "0x709f3fda12f561cfacf92273c57a98fede188a3f1a59b1f888d113f9cce08649"
_MULTISIG_DATA_HASH = "0x43400de165f0821abf63dcac299bbdf7fd73898675ee4ddb099b0a0d8db63bfb"
_ALWAYS_SUCCESS_DATA_HASH = "0xe683b04139344768348499c23eb1326d5a52d6db006c0d2fece00a831f3660d7"
_SECP_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


@dataclass(frozen=True)
class ProjectCase:
    public: dict[str, Any]
    private: dict[str, Any]


def _exact(value: Any, keys: set[str]) -> dict[str, Any] | None:
    return value if isinstance(value, dict) and set(value) == keys else None


def _strict_json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without allowing integers, booleans, and floats to collapse."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (
            left.keys() == right.keys()
            and all(_strict_json_equal(left[key], right[key]) for key in left)
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_json_equal(item, other) for item, other in zip(left, right, strict=True)
        )
    return left == right


def _rng(challenge: str, check: str) -> random.Random:
    seed = hashlib.sha256(f"{check}\0{challenge}".encode()).digest()
    return random.Random(int.from_bytes(seed, "big"))


def _hex(rng: random.Random, size: int) -> str:
    return "0x" + bytes(rng.randrange(256) for _ in range(size)).hex()


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "little")


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def _fixvec(items: list[bytes]) -> bytes:
    body = b"".join(items)
    return _u32(len(items)) + body


def _dynvec(items: list[bytes]) -> bytes:
    if not items:
        return _u32(4)
    header = 4 + 4 * len(items)
    offsets: list[int] = []
    cursor = header
    for item in items:
        offsets.append(cursor)
        cursor += len(item)
    return _u32(cursor) + b"".join(_u32(offset) for offset in offsets) + b"".join(items)


def _table(fields: list[bytes]) -> bytes:
    return _dynvec(fields)


def _bytes(value: bytes) -> bytes:
    return _fixvec([bytes((byte,)) for byte in value])


def _wire_bytes(value: Any, size: int | None = None) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"0x(?:[0-9a-f]{2})*", value) is None:
        raise ValueError("invalid canonical bytes")
    raw = bytes.fromhex(value[2:])
    if size is not None and len(raw) != size:
        raise ValueError("invalid byte length")
    return raw


def _quantity(value: Any, bits: int) -> int:
    if not isinstance(value, str) or re.fullmatch(r"0x(?:0|[1-9a-f][0-9a-f]*)", value) is None:
        raise ValueError("invalid canonical quantity")
    number = int(value, 16)
    if number >= 1 << bits:
        raise ValueError("quantity overflow")
    return number


def _script_bytes(script: dict[str, Any]) -> bytes:
    row = _exact(script, {"args", "code_hash", "hash_type"})
    if row is None or row["hash_type"] not in _HASH_TYPE_BYTES:
        raise ValueError("invalid script")
    return _table([
        _wire_bytes(row["code_hash"], 32),
        bytes((_HASH_TYPE_BYTES[row["hash_type"]],)),
        _bytes(_wire_bytes(row["args"])),
    ])


def _out_point_bytes(value: dict[str, Any]) -> bytes:
    row = _exact(value, {"index", "tx_hash"})
    if row is None:
        raise ValueError("invalid out point")
    return _wire_bytes(row["tx_hash"], 32) + _u32(_quantity(row["index"], 32))


def _raw_transaction_bytes(tx: dict[str, Any]) -> bytes:
    row = _exact(tx, {"cell_deps", "header_deps", "inputs", "outputs", "outputs_data", "version"})
    if row is None or len(row["outputs"]) != len(row["outputs_data"]):
        raise ValueError("invalid raw transaction")
    cell_deps = []
    for dep in row["cell_deps"]:
        dep_row = _exact(dep, {"dep_type", "out_point"})
        if dep_row is None or dep_row["dep_type"] not in {"code", "dep_group"}:
            raise ValueError("invalid cell dependency")
        cell_deps.append(
            _out_point_bytes(dep_row["out_point"])
            + bytes((0 if dep_row["dep_type"] == "code" else 1,))
        )
    inputs = []
    for cell_input in row["inputs"]:
        input_row = _exact(cell_input, {"previous_output", "since"})
        if input_row is None:
            raise ValueError("invalid input")
        inputs.append(_u64(_quantity(input_row["since"], 64)) + _out_point_bytes(input_row["previous_output"]))
    outputs = []
    for output in row["outputs"]:
        output_row = _exact(output, {"capacity", "lock", "type"})
        if output_row is None:
            raise ValueError("invalid output")
        type_script = b"" if output_row["type"] is None else _script_bytes(output_row["type"])
        outputs.append(_table([
            _u64(_quantity(output_row["capacity"], 64)),
            _script_bytes(output_row["lock"]),
            type_script,
        ]))
    return _table([
        _u32(_quantity(row["version"], 32)),
        _fixvec(cell_deps),
        _fixvec([_wire_bytes(value, 32) for value in row["header_deps"]]),
        _fixvec(inputs),
        _dynvec(outputs),
        _dynvec([_bytes(_wire_bytes(value)) for value in row["outputs_data"]]),
    ])


def _bech32_polymod(values: list[int]) -> int:
    result = 1
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    for value in values:
        top = result >> 25
        result = ((result & 0x1FFFFFF) << 5) ^ value
        for bit, generator in enumerate(generators):
            if (top >> bit) & 1:
                result ^= generator
    return result


def _convert_bits(data: bytes, source: int, target: int, pad: bool) -> list[int]:
    accumulator = 0
    bits = 0
    output: list[int] = []
    maximum = (1 << target) - 1
    for value in data:
        if value < 0 or value >> source:
            raise ValueError("invalid base conversion value")
        accumulator = (accumulator << source) | value
        bits += source
        while bits >= target:
            bits -= target
            output.append((accumulator >> bits) & maximum)
    if pad:
        if bits:
            output.append((accumulator << (target - bits)) & maximum)
    elif bits >= source or ((accumulator << (target - bits)) & maximum):
        raise ValueError("invalid base conversion padding")
    return output


def _address(hrp: str, payload: bytes, bech32m: bool) -> str:
    data = _convert_bits(payload, 8, 5, True)
    expanded = [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]
    constant = 0x2BC830A3 if bech32m else 1
    polymod = _bech32_polymod(expanded + data + [0] * 6) ^ constant
    checksum = [(polymod >> (5 * (5 - index))) & 31 for index in range(6)]
    return hrp + "1" + "".join(_BECH32_ALPHABET[value] for value in data + checksum)


def _decode_address(value: str) -> tuple[str, bytes, bool]:
    if not isinstance(value, str) or value.rfind("1") < 1:
        raise ValueError("invalid address")
    if value.lower() != value and value.upper() != value:
        raise ValueError("invalid address")
    value = value.lower()
    split = value.rfind("1")
    hrp = value[:split]
    chars = value[split + 1 :]
    if len(chars) < 7 or any(char not in _BECH32_VALUES for char in chars):
        raise ValueError("invalid address")
    data = [_BECH32_VALUES[char] for char in chars]
    expanded = [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]
    checksum = _bech32_polymod(expanded + data)
    if checksum == 1:
        bech32m = False
    elif checksum == 0x2BC830A3:
        bech32m = True
    else:
        raise ValueError("invalid checksum")
    return hrp, bytes(_convert_bits(bytes(data[:-6]), 5, 8, False)), bech32m


def _address_cases(rng: random.Random) -> list[ProjectCase]:
    cases: list[ProjectCase] = []
    full_rows = []
    for network, hash_type in (
        ("mainnet", "data"),
        ("testnet", "type"),
        ("mainnet", "data1"),
        ("testnet", "data2"),
    ):
        script = {"args": _hex(rng, 20 + rng.randrange(8)), "code_hash": _hex(rng, 32), "hash_type": hash_type}
        payload = b"\x00" + _wire_bytes(script["code_hash"]) + bytes((_HASH_TYPE_BYTES[hash_type],)) + _wire_bytes(script["args"])
        address = _address("ckb" if network == "mainnet" else "ckt", payload, True)
        public = {"format": "full", "network": network, "operation": "encode", "script": script}
        cases.append(ProjectCase(public, {"expected": {"address": address}}))
        full_rows.append((network, address, script))
    short_rows = []
    for network, index in (("mainnet", 0), ("testnet", 1), ("testnet", 2)):
        args = _hex(rng, 20)
        payload = bytes((1, index)) + _wire_bytes(args)
        address = _address("ckb" if network == "mainnet" else "ckt", payload, False)
        cases.append(ProjectCase(
            {"args": args, "code_hash_index": index, "format": "short", "network": network, "operation": "encode"},
            {"expected": {"address": address}},
        ))
        short_rows.append((network, address, args, index))
    for network, address, script in full_rows:
        cases.append(ProjectCase(
            {"address": address, "network": network, "operation": "decode"},
            {"expected": {"format": "full", "script": script}},
        ))
    for network, address, args, index in short_rows:
        cases.append(ProjectCase(
            {"address": address, "network": network, "operation": "decode"},
            {"expected": {"args": args, "code_hash_index": index, "format": "short"}},
        ))
    full_network, full_address, full_script = full_rows[0]
    short_network, short_address, short_args, short_index = short_rows[1]
    cases.extend((
        ProjectCase(
            {"address": full_address.upper(), "network": full_network, "operation": "decode"},
            {"expected": {"format": "full", "script": full_script}},
        ),
        ProjectCase(
            {"address": short_address.upper(), "network": short_network, "operation": "decode"},
            {"expected": {
                "args": short_args,
                "code_hash_index": short_index,
                "format": "short",
            }},
        ),
    ))
    cases.append(ProjectCase(
        {"address": full_address, "network": "testnet", "operation": "decode"},
        {"expected": {"error": "network_mismatch"}},
    ))
    broken = full_address[:-1] + ("q" if full_address[-1] != "q" else "p")
    cases.append(ProjectCase(
        {"address": broken, "network": full_network, "operation": "decode"},
        {"expected": {"error": "invalid_address"}},
    ))
    mixed = full_address[:3].upper() + full_address[3:]
    cases.append(ProjectCase(
        {"address": mixed, "network": full_network, "operation": "decode"},
        {"expected": {"error": "invalid_address"}},
    ))
    unsupported = _address("ckt", bytes((1, 3)) + _wire_bytes(_hex(rng, 20)), False)
    cases.append(ProjectCase(
        {"address": unsupported, "network": "testnet", "operation": "decode"},
        {"expected": {"error": "unsupported_script_template"}},
    ))
    invalid_hash_type = _address("ckt", b"\x00" + bytes(32) + b"\x03" + bytes(20), True)
    cases.append(ProjectCase(
        {"address": invalid_hash_type, "network": "testnet", "operation": "decode"},
        {"expected": {"error": "invalid_address"}},
    ))
    short_full_payload = _address("ckb", b"\x00" + bytes(31), True)
    cases.append(ProjectCase(
        {"address": short_full_payload, "network": "mainnet", "operation": "decode"},
        {"expected": {"error": "invalid_address"}},
    ))
    wrong_checksum_variant = _address(
        "ckb",
        b"\x00" + _wire_bytes(full_script["code_hash"]) + b"\x00" + _wire_bytes(full_script["args"]),
        False,
    )
    cases.append(ProjectCase(
        {"address": wrong_checksum_variant, "network": "mainnet", "operation": "decode"},
        {"expected": {"error": "invalid_address"}},
    ))
    return cases


def _balancer_cases(rng: random.Random) -> list[ProjectCase]:
    cases = []
    for case_index in range(8):
        count = 3 + case_index % 4
        cells = [
            {"capacity": 12_000_000_000 + rng.randrange(5_000_000_000), "id": f"cell-{case_index}-{index}"}
            for index in range(count)
        ]
        target = sum(row["capacity"] for row in cells[: 1 + case_index % 2]) - 300_000_000
        public = {
            "base_size": 180 + rng.randrange(40),
            "cells": cells,
            "change_output_size": 61,
            "fee_rate": 1000 + 100 * (case_index % 3),
            "input_size": 44,
            "minimum_change": 6_100_000_000 if case_index % 3 == 0 else 100_000_000,
            "outputs": [{"capacity": target}],
        }
        cases.append(ProjectCase(public, {}))
    return cases


def _transaction_fixture(rng: random.Random, index: int) -> dict[str, Any]:
    hash_types = tuple(_HASH_TYPE_BYTES)
    cell_deps = [
        {
            "dep_type": "code" if (index + dep_index) % 2 == 0 else "dep_group",
            "out_point": {
                "index": hex((index + dep_index) % 4),
                "tx_hash": _hex(rng, 32),
            },
        }
        for dep_index in range(1 + index % 3)
    ]
    inputs = [
        {
            "previous_output": {
                "index": hex((index + input_index) % 3),
                "tx_hash": _hex(rng, 32),
            },
            "since": hex((index << 8) + input_index),
        }
        for input_index in range(1 + index % 3)
    ]
    outputs = []
    outputs_data = []
    for output_index in range(1 + index % 3):
        lock_hash_type = hash_types[(index + output_index) % len(hash_types)]
        lock = {
            "args": _hex(rng, (index + output_index) % 29),
            "code_hash": _hex(rng, 32),
            "hash_type": lock_hash_type,
        }
        type_script = None
        if (index + output_index) % 2:
            type_script = {
                "args": _hex(rng, (index * 3 + output_index) % 33),
                "code_hash": _hex(rng, 32),
                "hash_type": hash_types[(index + output_index + 2) % len(hash_types)],
            }
        outputs.append({
            "capacity": hex(10_000_000_000 + index * 100 + output_index),
            "lock": lock,
            "type": type_script,
        })
        outputs_data.append(_hex(rng, (index * 5 + output_index * 7) % 35))
    return {
        "cell_deps": cell_deps,
        "header_deps": [_hex(rng, 32) for _ in range(index % 3)],
        "inputs": inputs,
        "outputs": outputs,
        "outputs_data": outputs_data,
        "version": hex(index),
    }


def _molecule_cases(rng: random.Random) -> list[ProjectCase]:
    cases = []
    for index in range(8):
        transaction = _transaction_fixture(rng, index)
        encoded = _raw_transaction_bytes(transaction)
        cases.append(ProjectCase(
            {"raw_transaction": transaction},
            {"expected": {"hash": "0x" + ckb_blake2b(encoded).hex(), "serialized": "0x" + encoded.hex()}},
        ))
    return cases


def _witness_args(value: dict[str, Any]) -> bytes:
    row = _exact(value, {"input_type", "lock", "output_type"})
    if row is None:
        raise ValueError("invalid witness args")
    fields = []
    for name in ("lock", "input_type", "output_type"):
        field = row[name]
        fields.append(b"" if field is None else _bytes(_wire_bytes(field)))
    return _table(fields)


def _sighash_message(case: dict[str, Any]) -> str:
    transaction_hash = _wire_bytes(case["transaction_hash"], 32)
    witnesses = case["witnesses"]
    group = case["group_indices"]
    input_count = case["input_count"]
    first = dict(witnesses[group[0]])
    lock = _wire_bytes(first["lock"])
    first["lock"] = "0x" + bytes(len(lock)).hex()
    selected = [_witness_args(first)]
    selected.extend(_witness_args(witnesses[index]) for index in group[1:])
    selected.extend(_witness_args(witnesses[index]) for index in range(input_count, len(witnesses)))
    payload = transaction_hash + b"".join(_u64(len(row)) + row for row in selected)
    return "0x" + ckb_blake2b(payload).hex()


def _multisig_message(case: dict[str, Any], multisig_script: bytes) -> str:
    witnesses = case["witnesses"]
    group = case["group_indices"]
    first = dict(witnesses[group[0]])
    lock_size = len(_wire_bytes(first["lock"]))
    if lock_size < len(multisig_script):
        raise ValueError("multisig lock is shorter than its policy")
    first["lock"] = "0x" + (
        multisig_script + bytes(lock_size - len(multisig_script))
    ).hex()
    selected = [_witness_args(first)]
    selected.extend(_witness_args(witnesses[index]) for index in group[1:])
    selected.extend(
        _witness_args(witnesses[index])
        for index in range(case["input_count"], len(witnesses))
    )
    payload = _wire_bytes(case["transaction_hash"], 32)
    payload += b"".join(_u64(len(row)) + row for row in selected)
    return "0x" + ckb_blake2b(payload).hex()


def _system_lock_fixture(
    rng: random.Random,
    index: int,
    *,
    code_hash: str,
    args: str,
    first_lock_size: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[int]]:
    input_count = 3 + index % 2
    group = [0, 2] if input_count == 3 else [1, 3]
    target_lock = {"args": args, "code_hash": code_hash, "hash_type": "data"}
    pass_lock = {
        "args": _hex(rng, 4),
        "code_hash": _ALWAYS_SUCCESS_DATA_HASH,
        "hash_type": "data",
    }
    dep_hashes = [code_hash, "0x9799bee251b975b82c45a02154ce28cec89c5853ecc14d12b7b8cccfc19e0af4", _ALWAYS_SUCCESS_DATA_HASH]
    deps = [
        {
            "dep_type": "code",
            "out_point": {"index": "0x0", "tx_hash": _hex(rng, 32)},
        }
        for _ in dep_hashes
    ]
    inputs = []
    input_cells = []
    input_capacity = 100_000_000_000
    for input_index in range(input_count):
        out_point = {"index": hex(input_index), "tx_hash": _hex(rng, 32)}
        inputs.append({"previous_output": out_point, "since": "0x0"})
        input_cells.append({
            "data": "0x",
            "out_point": out_point,
            "output": {
                "capacity": hex(input_capacity),
                "lock": target_lock if input_index in group else pass_lock,
                "type": None,
            },
        })
    raw_transaction = {
        "cell_deps": deps,
        "header_deps": [],
        "inputs": inputs,
        "outputs": [{
            "capacity": hex(input_capacity * input_count),
            "lock": pass_lock,
            "type": None,
        }],
        "outputs_data": ["0x"],
        "version": "0x0",
    }
    witnesses = []
    for witness_index in range(input_count + 1 + index % 2):
        witnesses.append({
            "input_type": _hex(rng, 4) if witness_index % 2 == 0 else None,
            "lock": "0x" + (
                bytes(first_lock_size)
                if witness_index == group[0]
                else bytes(rng.randrange(5))
            ).hex(),
            "output_type": _hex(rng, 3) if witness_index % 3 == 0 else None,
        })
    return raw_transaction, input_cells, witnesses, group


def _sighash_cases(rng: random.Random) -> list[ProjectCase]:
    cases = []
    for index in range(8):
        private_key = rng.randrange(1, _SECP_N)
        args = "0x" + ckb_blake2b(_compressed_pubkey(private_key))[:20].hex()
        raw_transaction, input_cells, witnesses, group = _system_lock_fixture(
            rng,
            index,
            code_hash=_SIGHASH_DATA_HASH,
            args=args,
            first_lock_size=65,
        )
        public = {
            "group_indices": group,
            "input_count": len(raw_transaction["inputs"]),
            "private_key": "0x" + private_key.to_bytes(32, "big").hex(),
            "raw_transaction": raw_transaction,
            "transaction_hash": "0x" + ckb_blake2b(_raw_transaction_bytes(raw_transaction)).hex(),
            "witnesses": witnesses,
        }
        cases.append(ProjectCase(
            public,
            {
                "args": args,
                "expected": {"message": _sighash_message(public)},
                "input_cells": input_cells,
            },
        ))
    return cases


def _point_add(
    left: tuple[int, int] | None,
    right: tuple[int, int] | None,
) -> tuple[int, int] | None:
    if left is None:
        return right
    if right is None:
        return left
    if left[0] == right[0]:
        if (left[1] + right[1]) % _SECP_P == 0:
            return None
        slope = (3 * left[0] * left[0]) * pow(2 * left[1], -1, _SECP_P)
    else:
        slope = (right[1] - left[1]) * pow(right[0] - left[0], -1, _SECP_P)
    slope %= _SECP_P
    x = (slope * slope - left[0] - right[0]) % _SECP_P
    y = (slope * (left[0] - x) - left[1]) % _SECP_P
    return x, y


def _point_mul(scalar: int, point: tuple[int, int]) -> tuple[int, int] | None:
    result = None
    addend: tuple[int, int] | None = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _compressed_pubkey(private_key: int) -> bytes:
    point = _point_mul(private_key, _SECP_G)
    if point is None:
        raise ValueError("invalid private key")
    return bytes((2 | (point[1] & 1),)) + point[0].to_bytes(32, "big")


def _recover_pubkey(message: bytes, signature: bytes) -> bytes:
    if len(message) != 32 or len(signature) != 65:
        raise ValueError("invalid recoverable signature")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:64], "big")
    recovery = signature[64]
    if not 1 <= r < _SECP_N or not 1 <= s <= _SECP_N // 2 or recovery > 3:
        raise ValueError("invalid recoverable signature")
    x = r + (recovery >> 1) * _SECP_N
    if x >= _SECP_P:
        raise ValueError("invalid recovery point")
    alpha = (pow(x, 3, _SECP_P) + 7) % _SECP_P
    y = pow(alpha, (_SECP_P + 1) // 4, _SECP_P)
    if pow(y, 2, _SECP_P) != alpha:
        raise ValueError("invalid recovery point")
    if (y & 1) != (recovery & 1):
        y = _SECP_P - y
    recovered = (x, y)
    if _point_mul(_SECP_N, recovered) is not None:
        raise ValueError("invalid recovery point")
    z = int.from_bytes(message, "big") % _SECP_N
    public = _point_mul(
        pow(r, -1, _SECP_N),
        _point_add(_point_mul(s, recovered), _point_mul((-z) % _SECP_N, _SECP_G)),
    )
    if public is None:
        raise ValueError("invalid recovered key")
    return bytes((2 | (public[1] & 1),)) + public[0].to_bytes(32, "big")


def _multisig_cases(rng: random.Random) -> list[ProjectCase]:
    cases = []
    for index in range(8):
        count = 2 + index % 4
        threshold = 1 + index % count
        require_first = min(index % 3, threshold)
        keys = []
        while len(keys) < count:
            candidate = rng.randrange(1, _SECP_N)
            if candidate not in keys:
                keys.append(candidate)
        hashes = ["0x" + ckb_blake2b(_compressed_pubkey(key))[:20].hex() for key in keys]
        script = bytes((0, require_first, threshold, count)) + b"".join(
            _wire_bytes(row, 20) for row in hashes
        )
        lock_size = len(script) + threshold * 65
        args = "0x" + ckb_blake2b(script)[:20].hex()
        raw_transaction, input_cells, witnesses, group = _system_lock_fixture(
            rng,
            index,
            code_hash=_MULTISIG_DATA_HASH,
            args=args,
            first_lock_size=lock_size,
        )
        signing = {
            "group_indices": group,
            "input_count": len(raw_transaction["inputs"]),
            "raw_transaction": raw_transaction,
            "transaction_hash": "0x" + ckb_blake2b(_raw_transaction_bytes(raw_transaction)).hex(),
            "witnesses": witnesses,
        }
        shuffled_keys = keys[:]
        rng.shuffle(shuffled_keys)
        cases.append(ProjectCase(
            {
                "private_keys": ["0x" + key.to_bytes(32, "big").hex() for key in shuffled_keys],
                "pubkey_hashes": hashes,
                "require_first_n": require_first,
                "signing": signing,
                "threshold": threshold,
            },
            {
                "args": args,
                "input_cells": input_cells,
                "message": _multisig_message(signing, script),
                "multisig_script": script,
            },
        ))
    return cases


def _pack_epoch(number: int, index: int, length: int) -> int:
    return number | (index << 24) | (length << 40)


def _dao_since(deposit: tuple[int, int, int], withdrawing: tuple[int, int, int]) -> int:
    deposit_value = Fraction(deposit[0] * deposit[2] + deposit[1], deposit[2])
    withdrawing_value = Fraction(withdrawing[0] * withdrawing[2] + withdrawing[1], withdrawing[2])
    delta = max(Fraction(0), withdrawing_value - deposit_value)
    periods = max(1, (delta.numerator + 180 * delta.denominator - 1) // (180 * delta.denominator))
    candidate_number = deposit[0] + periods * 180
    if Fraction(candidate_number * deposit[2] + deposit[1], deposit[2]) < withdrawing_value:
        candidate_number += 180
    return (1 << 61) | _pack_epoch(candidate_number, deposit[1], deposit[2])


def _dao_cases(rng: random.Random) -> list[ProjectCase]:
    cases = []
    for index in range(8):
        length = 1000 + rng.randrange(800)
        deposit = (2 + rng.randrange(20), rng.randrange(length), length)
        withdraw_length = 900 + rng.randrange(900)
        withdrawing = (deposit[0] + 1 + rng.randrange(350), rng.randrange(withdraw_length), withdraw_length)
        total = 100_000_000_000 + rng.randrange(200_000_000_000)
        occupied = 10_200_000_000
        deposit_ar = 10_000_000_000_000_000 + rng.randrange(1_000_000)
        withdrawing_ar = deposit_ar + rng.randrange(1_000_000, 50_000_000)
        deposit_header = _hex(rng, 32)
        withdrawing_header = _hex(rng, 32)
        public = {
            "deposit_ar": deposit_ar,
            "deposit_epoch": {"index": deposit[1], "length": deposit[2], "number": deposit[0]},
            "deposit_header_hash": deposit_header,
            "occupied_capacity": occupied,
            "total_capacity": total,
            "withdrawing_ar": withdrawing_ar,
            "withdrawing_epoch": {"index": withdrawing[1], "length": withdrawing[2], "number": withdrawing[0]},
            "withdrawing_header_hash": withdrawing_header,
        }
        expected_capacity = (total - occupied) * withdrawing_ar // deposit_ar + occupied
        expected = {
            "deposit_header_index": 0,
            "header_deps": [deposit_header, withdrawing_header],
            "maximum_withdraw_capacity": expected_capacity,
            "since": hex(_dao_since(deposit, withdrawing)),
            "witness_input_type": "0x" + (0).to_bytes(8, "little").hex(),
        }
        cases.append(ProjectCase(public, {"expected": expected}))
    return cases


def _dependency_cases(rng: random.Random) -> list[ProjectCase]:
    cases = []
    for case_index in range(8):
        always_dep = {
            "dep_type": "dep_group" if case_index % 2 else "code",
            "out_point": {"index": "0x0", "tx_hash": _hex(rng, 32)},
        }
        always_script = {"code_hash": _ALWAYS_SUCCESS_DATA_HASH, "hash_type": "data"}
        registry = [{"cell_dep": always_dep, "script": always_script}]
        requested = [always_script]
        expected = {json.dumps(always_dep, sort_keys=True, separators=(",", ":"))}
        for index in range(1, 5):
            script = {
                "code_hash": _hex(rng, 32),
                "hash_type": "type" if index % 2 else "data1",
            }
            dep = {
                "dep_type": "code",
                "out_point": {"index": hex(index % 2), "tx_hash": _hex(rng, 32)},
            }
            registry.append({"cell_dep": dep, "script": script})
            if index <= case_index % 5:
                requested.append(script)
                expected.add(json.dumps(dep, sort_keys=True, separators=(",", ":")))
        rng.shuffle(requested)
        cases.append(ProjectCase(
            {"deployments": registry, "scripts": requested},
            {
                "always_success_member": {
                    "index": "0x0",
                    "tx_hash": _hex(rng, 32),
                },
                "expected": expected,
            },
        ))
    return cases


def _indexer_cases(rng: random.Random) -> list[ProjectCase]:
    cases = []
    for case_index in range(8):
        wanted_lock = _hex(rng, 32)
        wanted_type = None if case_index % 2 else _hex(rng, 32)
        pages = []
        expected: dict[str, int] = {}
        for page_index in range(3):
            objects = []
            for cell_index in range(4):
                cell_id = f"0x{case_index:02x}{page_index:02x}{cell_index:02x}"
                include = (page_index + cell_index + case_index) % 3 != 0
                row = {
                    "capacity": (1 << 63) + rng.randrange(1 << 40),
                    "id": cell_id,
                    "lock_hash": wanted_lock if include else _hex(rng, 32),
                    "type_hash": wanted_type if include else (None if wanted_type is not None else _hex(rng, 32)),
                }
                objects.append(row)
                if include:
                    expected[cell_id] = row["capacity"]
            if page_index == 2:
                duplicate = dict(pages[0]["objects"][1])
                objects.append(duplicate)
            pages.append({"last_cursor": f"cursor-{page_index + 1}", "objects": objects})
        cases.append(ProjectCase(
            {"filter": {"lock_hash": wanted_lock, "type_hash": wanted_type}, "pages": pages},
            {"expected_cells": expected, "expected_cursor": "cursor-3"},
        ))
    return cases


def _ccc_cases(rng: random.Random) -> list[ProjectCase]:
    cases = []
    for index in range(8):
        inputs = [
            {"capacity": 15_000_000_000 + rng.randrange(5_000_000_000), "id": f"input-{index}-{cell}"}
            for cell in range(2 + index % 3)
        ]
        recipient = {"args": _hex(rng, 20), "code_hash": _SECP_CODE_HASH, "hash_type": "type"}
        amount = 7_000_000_000 if index % 2 == 0 else sum(row["capacity"] for row in inputs) - 7_000_000_000
        cases.append(ProjectCase({
            "available_inputs": inputs,
            "fee": 100_000 + index,
            "minimum_change": 6_100_000_000,
            "recipient_capacity": amount,
            "recipient_lock": recipient,
            "sender_lock": {"args": _hex(rng, 20), "code_hash": _SECP_CODE_HASH, "hash_type": "type"},
        }, {}))
    return cases


_GENERATORS: dict[str, Callable[[random.Random], list[ProjectCase]]] = {
    "address_codec": _address_cases,
    "cell_dependency_resolver": _dependency_cases,
    "cell_query_indexer": _indexer_cases,
    "ccc_transaction_builder": _ccc_cases,
    "dao_withdrawal_planner": _dao_cases,
    "molecule_transaction": _molecule_cases,
    "multisig_witness": _multisig_cases,
    "sighash_witness_groups": _sighash_cases,
    "transaction_balancer": _balancer_cases,
}


def project_cases(check: str, challenge: str, count: int) -> tuple[ProjectCase, ...]:
    if check not in PROJECT_CHECKS:
        raise ValueError("unsupported project verifier")
    generated = _GENERATORS[check](_rng(challenge, check))
    expected_count = PROJECT_VERIFIER_CASE_LIMITS[check]
    if len(generated) != expected_count:
        raise RuntimeError("project verifier case generator differs from its declared limit")
    if count > expected_count:
        raise ValueError("project verifier requested more cases than it defines")
    return tuple(generated[:count])


def _check_balancer(public: dict[str, Any], result: Any) -> bool:
    row = _exact(result, {"change", "selected_input_ids"})
    if row is None or isinstance(row["change"], bool) or not isinstance(row["change"], int):
        return False
    selected = row["selected_input_ids"]
    if not isinstance(selected, list) or not selected or not all(isinstance(item, str) for item in selected):
        return False
    if len(selected) != len(set(selected)):
        return False
    capacities = {cell["id"]: cell["capacity"] for cell in public["cells"]}
    if not set(selected) <= set(capacities):
        return False
    change = row["change"]
    if change < 0 or (change and change < public["minimum_change"]):
        return False
    size = public["base_size"] + len(selected) * public["input_size"]
    if change:
        size += public["change_output_size"]
    required_fee = (size * public["fee_rate"] + 999) // 1000
    paid_fee = sum(capacities[item] for item in selected) - sum(item["capacity"] for item in public["outputs"]) - change
    return required_fee <= paid_fee <= required_fee + 1000


def _check_dependencies(case: ProjectCase, result: Any) -> bool:
    row = _exact(result, {"cell_deps"})
    if row is None or not isinstance(row["cell_deps"], list):
        return False
    try:
        observed = {json.dumps(dep, sort_keys=True, separators=(",", ":")) for dep in row["cell_deps"]}
    except (TypeError, ValueError):
        return False
    return observed == case.private["expected"] and len(observed) == len(row["cell_deps"])


def _check_indexer(case: ProjectCase, result: Any) -> bool:
    row = _exact(result, {"balance", "cell_ids", "last_cursor"})
    if (
        row is None
        or isinstance(row["balance"], bool)
        or not isinstance(row["balance"], int)
        or not isinstance(row["cell_ids"], list)
        or not all(isinstance(cell_id, str) for cell_id in row["cell_ids"])
        or not isinstance(row["last_cursor"], str)
    ):
        return False
    expected = case.private["expected_cells"]
    return (
        row["cell_ids"] == sorted(expected)
        and row["balance"] == sum(expected.values())
        and row["last_cursor"] == case.private["expected_cursor"]
    )


def _check_ccc(public: dict[str, Any], result: Any) -> bool:
    row = _exact(result, {"cell_deps", "header_deps", "inputs", "outputs", "outputs_data", "version", "witnesses"})
    if row is None or row["version"] != "0x0" or row["cell_deps"] != [] or row["header_deps"] != []:
        return False
    if not isinstance(row["inputs"], list) or not isinstance(row["outputs"], list) or len(row["outputs"]) != 2:
        return False
    if not all(_exact(item, {"id"}) is not None for item in row["inputs"]):
        return False
    ids = [item["id"] for item in row["inputs"]]
    available = {item["id"]: item["capacity"] for item in public["available_inputs"]}
    if len(ids) != len(set(ids)) or not ids or not set(ids) <= set(available):
        return False
    recipient_output = {
        "capacity": public["recipient_capacity"],
        "lock": public["recipient_lock"],
        "type": None,
    }
    recipient_indices = [
        index
        for index, output in enumerate(row["outputs"])
        if _strict_json_equal(output, recipient_output)
    ]
    if len(recipient_indices) != 1:
        return False
    recipient_index = recipient_indices[0]
    change_index = 1 - recipient_index
    change = row["outputs"][change_index]
    if (
        _exact(change, {"capacity", "lock", "type"}) is None
        or not _strict_json_equal(change["lock"], public["sender_lock"])
        or change["type"] is not None
    ):
        return False
    expected_change = sum(available[item] for item in ids) - public["recipient_capacity"] - public["fee"]
    return (
        type(change.get("capacity")) is int
        and change["capacity"] == expected_change
        and expected_change >= public["minimum_change"]
        and isinstance(row["outputs_data"], list)
        and len(row["outputs_data"]) == 2
        and row["outputs_data"][recipient_index] == "0x"
        and row["outputs_data"][change_index] == "0x"
        and row["witnesses"] == ["0x" for _ in ids]
    )


def _check_multisig(case: ProjectCase, result: Any) -> bool:
    row = _exact(result, {"args", "message", "multisig_script", "witness_lock"})
    if row is None:
        return False
    try:
        expected_script = case.private["multisig_script"]
        script = _wire_bytes(row["multisig_script"])
        witness = _wire_bytes(row["witness_lock"])
        message = _wire_bytes(row["message"], 32)
    except (KeyError, TypeError, ValueError):
        return False
    if (
        script != expected_script
        or row["args"] != case.private["args"]
        or row["message"] != case.private["message"]
        or not witness.startswith(script)
    ):
        return False
    threshold = case.public["threshold"]
    signatures = witness[len(script):]
    if len(signatures) != threshold * 65:
        return False
    policy_hashes = case.public["pubkey_hashes"]
    recovered_indices: list[int] = []
    try:
        for offset in range(0, len(signatures), 65):
            public_key = _recover_pubkey(message, signatures[offset:offset + 65])
            key_hash = "0x" + ckb_blake2b(public_key)[:20].hex()
            recovered_indices.append(policy_hashes.index(key_hash))
    except (ValueError, TypeError):
        return False
    required = case.public["require_first_n"]
    return (
        recovered_indices == sorted(set(recovered_indices))
        and set(range(required)) <= set(recovered_indices)
    )


def _check_sighash(case: ProjectCase, result: Any) -> bool:
    row = _exact(result, {"message", "witness_lock"})
    if row is None or row["message"] != case.private["expected"]["message"]:
        return False
    try:
        message = _wire_bytes(row["message"], 32)
        signature = _wire_bytes(row["witness_lock"], 65)
        public_key = _recover_pubkey(message, signature)
    except (TypeError, ValueError):
        return False
    return "0x" + ckb_blake2b(public_key)[:20].hex() == case.private["args"]


def check_project_result(check: str, case: ProjectCase, result: Any) -> bool:
    if check in {"address_codec", "dao_withdrawal_planner", "molecule_transaction"}:
        return _strict_json_equal(result, case.private["expected"])
    if check == "sighash_witness_groups":
        return _check_sighash(case, result)
    if check == "multisig_witness":
        return _check_multisig(case, result)
    if check == "transaction_balancer":
        return _check_balancer(case.public, result)
    if check == "cell_dependency_resolver":
        return _check_dependencies(case, result)
    if check == "cell_query_indexer":
        return _check_indexer(case, result)
    if check == "ccc_transaction_builder":
        return _check_ccc(case.public, result)
    raise ValueError("unsupported project verifier")


def _validate_artifact_tree(root: Path, entry: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("project artifact root is invalid")
    pending = [root]
    entry_count = 0
    file_count = 0
    total_bytes = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                entry_count += 1
                if entry_count > MAX_PROJECT_ARTIFACT_ENTRIES:
                    raise ValueError("project artifact entry count is invalid")
                if child.is_symlink():
                    raise ValueError("project artifact contains an unsafe entry")
                if child.is_dir(follow_symlinks=False):
                    pending.append(Path(child.path))
                    continue
                if not child.is_file(follow_symlinks=False):
                    raise ValueError("project artifact contains an unsafe entry")
                file_count += 1
                if file_count > MAX_PROJECT_ARTIFACT_FILES:
                    raise ValueError("project artifact file count is invalid")
                size = child.stat(follow_symlinks=False).st_size
                if size <= 0 or size > MAX_PROJECT_ARTIFACT_FILE_BYTES:
                    raise ValueError("project artifact size is invalid")
                total_bytes += size
                if total_bytes > MAX_PROJECT_ARTIFACT_BYTES:
                    raise ValueError("project artifact size is invalid")
    if file_count == 0:
        raise ValueError("project artifact file count is invalid")
    if entry.is_symlink() or not entry.is_file() or not entry.stat().st_mode & 0o111:
        raise ValueError("project entrypoint is missing or not executable")


def _load_result(path: Path) -> Any:
    with os.scandir(path.parent) as entries:
        first = next(entries, None)
        second = next(entries, None)
    if first is None or first.name != path.name or second is not None or first.is_symlink():
        raise ValueError("candidate result is missing or oversized")
    before = first.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_PROJECT_DOCUMENT_BYTES:
        raise ValueError("candidate result is missing or oversized")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("candidate result is not a stable regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            content = source.read(MAX_PROJECT_DOCUMENT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not 0 < len(content) <= MAX_PROJECT_DOCUMENT_BYTES:
        raise ValueError("candidate result is missing or oversized")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for key, value in pairs:
            if key in row:
                raise ValueError("candidate result has duplicate keys")
            row[key] = value
        return row

    return json.loads(content.decode("utf-8"), object_pairs_hook=unique)


def _oracle_document(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, set):
        return sorted(_oracle_document(item) for item in value)
    if isinstance(value, tuple):
        return [_oracle_document(item) for item in value]
    if isinstance(value, list):
        return [_oracle_document(item) for item in value]
    if isinstance(value, dict):
        return {key: _oracle_document(item) for key, item in value.items()}
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError("project oracle contains an unsupported value")


def grade_project_task(
    task: Task,
    mount: Path,
    spec: ProjectVerifierSpec,
    verifier_private: dict[str, Any],
    runner: RunnerCallable,
    *,
    artifact_dir: Path | None = None,
    suite_dir: Path | None = None,
) -> Verdict:
    challenge = verifier_private.get(CODE_CHALLENGE_ENV)
    legacy = verifier_private.get(BENCH_PASSWORD_ENV)
    if challenge and legacy and challenge != legacy:
        return Verdict(task.id, False, "verifier-private challenge aliases do not match", "")
    challenge = challenge or legacy
    if not isinstance(challenge, str) or not challenge:
        return Verdict(task.id, False, "verifier-private challenge is missing", "")
    hidden: Path | None = None
    protocol_criteria = 0
    if spec.verifier_dir is not None:
        if suite_dir is None:
            return Verdict(task.id, False, "project task requires its hidden verifier", "")
        hidden = suite_dir / spec.verifier_dir
        if not hidden.is_dir():
            return Verdict(task.id, False, "project hidden verifier is missing", "")
    out = artifact_dir if artifact_dir is not None else mount.parent / ".ckbbench-artifact"
    prepare_artifact_dir(out)
    build = RunnerInvocation(
        stage="build",
        mounts={str(mount.resolve()): "/sources:ro", str(out.resolve()): "/artifact"},
        env={},
        command=DEFAULT_BUILD_COMMAND,
    )
    if _runner_result(runner(build)).exit_code != 0:
        return Verdict(task.id, False, "rebuild from sources failed", "")
    entry = Path(task.proof_file)
    if entry.is_absolute() or ".." in entry.parts:
        return Verdict(task.id, False, "project entrypoint is unsafe", "")
    try:
        _validate_artifact_tree(out, out / entry)
    except (OSError, ValueError):
        return Verdict(task.id, False, "project build artifact is invalid", "")
    cases = project_cases(spec.check, challenge, spec.case_count)
    observed_cases: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        with tempfile.TemporaryDirectory(prefix="ckbbench-project-", dir=out.parent) as raw:
            root = Path(raw)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir(mode=0o755)
            output_dir.mkdir(mode=0o755)
            (input_dir / "case.json").write_text(
                json.dumps(case.public, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            invocation = RunnerInvocation(
                stage="exercise",
                mounts={
                    str(out.resolve()): "/artifact:ro",
                    str(input_dir.resolve()): "/input:ro",
                    str(output_dir.resolve()): "/output",
                },
                env={},
                command=(f"/artifact/{entry.as_posix()}", "/input/case.json", "/output/result.json"),
            )
            executed = _runner_result(runner(invocation))
            if executed.exit_code != 0:
                return Verdict(
                    task.id,
                    False,
                    "candidate execution failed",
                    task.proof_file,
                    VerificationDiagnostics.stopped_at_failure(index, len(cases)),
                )
            try:
                result = _load_result(output_dir / "result.json")
                accepted = check_project_result(spec.check, case, result)
            except (
                OSError,
                UnicodeError,
                ValueError,
                TypeError,
                json.JSONDecodeError,
                RecursionError,
            ):
                accepted = False
            if not accepted:
                return Verdict(
                    task.id,
                    False,
                    "hidden behavioral case failed",
                    task.proof_file,
                    VerificationDiagnostics.stopped_at_failure(index, len(cases)),
                )
            observed_cases.append({
                "input": case.public,
                "oracle": _oracle_document(case.private),
                "result": result,
            })
    if spec.verifier_dir is not None:
        assert hidden is not None
        with tempfile.TemporaryDirectory(prefix="ckbbench-project-proof-", dir=out.parent) as raw:
            proof_dir = Path(raw)
            (proof_dir / "cases.json").write_text(
                json.dumps(
                    {"cases": observed_cases},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            verify = RunnerInvocation(
                stage="verify",
                mounts={
                    str(hidden.resolve()): "/suite:ro",
                    str(proof_dir.resolve()): "/artifact:ro",
                },
                env={CODE_CHALLENGE_ENV: challenge},
                command=DEFAULT_VERIFY_COMMAND,
            )
            verified = _runner_result(runner(verify))
            if verified.exit_code != 0:
                diagnostics = parse_libtest_diagnostics(verified.output, verified.exit_code)
                return Verdict(
                    task.id,
                    False,
                    "hidden protocol execution failed",
                    task.proof_file,
                    diagnostics,
                )
            diagnostics = parse_libtest_diagnostics(verified.output, verified.exit_code)
            if (
                diagnostics.status != "complete"
                or diagnostics.criteria_failed != 0
                or diagnostics.criteria_passed == 0
            ):
                raise VerificationInfrastructureError(
                    "project hidden verifier did not produce trustworthy diagnostics"
                )
            protocol_criteria = diagnostics.criteria_passed
    return Verdict(
        task.id,
        True,
        "all hidden behavioral cases passed",
        task.proof_file,
        VerificationDiagnostics.completed(len(cases) + protocol_criteria, 0),
    )
