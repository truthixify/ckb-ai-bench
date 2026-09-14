from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

import ckbbench.verify.projecttask as projecttask
from ckbbench.suite.model import PROJECT_VERIFIER_CASE_LIMITS, ProjectVerifierSpec, Task
from ckbbench.verify.codetask import CODE_CHALLENGE_ENV, RunnerInvocation, RunnerResult
from ckbbench.verify.projecttask import (
    PROJECT_CHECKS,
    _SECP_G,
    _SECP_N,
    _address,
    _check_balancer,
    _decode_address,
    _load_result,
    _point_mul,
    _validate_artifact_tree,
    _wire_bytes,
    check_project_result,
    grade_project_task,
    project_cases,
)
from ckbbench.verify.onchain import VerificationInfrastructureError


def _task(check: str = "address_codec", count: int = 2) -> Task:
    return Task(
        id="project-tool",
        prompt_fragment="Build the tool.",
        score=4,
        proof_file="build/release/project-tool",
        kind="project",
        verifier=ProjectVerifierSpec(check, count),
    )


def _expected_result(check: str, case):
    if check == "transaction_balancer":
        public = case.public
        selected = [cell["id"] for cell in public["cells"]]
        size = public["base_size"] + len(selected) * public["input_size"] + public["change_output_size"]
        fee = (size * public["fee_rate"] + 999) // 1000
        change = sum(cell["capacity"] for cell in public["cells"]) - sum(
            output["capacity"] for output in public["outputs"]
        ) - fee
        return {"change": change, "selected_input_ids": selected}
    if check == "cell_dependency_resolver":
        return {"cell_deps": [json.loads(value) for value in sorted(case.private["expected"])]}
    if check == "cell_query_indexer":
        expected = case.private["expected_cells"]
        return {
            "balance": sum(expected.values()),
            "cell_ids": sorted(expected),
            "last_cursor": case.private["expected_cursor"],
        }
    if check == "ccc_transaction_builder":
        public = case.public
        selected = []
        capacity = 0
        required = public["recipient_capacity"] + public["fee"] + public["minimum_change"]
        for candidate in public["available_inputs"]:
            selected.append(candidate)
            capacity += candidate["capacity"]
            if capacity >= required:
                break
        change = sum(row["capacity"] for row in selected) - public["recipient_capacity"] - public["fee"]
        return {
            "cell_deps": [],
            "header_deps": [],
            "inputs": [{"id": row["id"]} for row in selected],
            "outputs": [
                {"capacity": public["recipient_capacity"], "lock": public["recipient_lock"], "type": None},
                {"capacity": change, "lock": public["sender_lock"], "type": None},
            ],
            "outputs_data": ["0x", "0x"],
            "version": "0x0",
            "witnesses": ["0x" for _ in selected],
        }
    if check == "multisig_witness":
        keys = {
            "0x" + hashlib.blake2b(
                _compressed_for_test(int(value, 16)),
                digest_size=32,
                person=b"ckb-default-hash",
            ).digest()[:20].hex(): int(value, 16)
            for value in case.public["private_keys"]
        }
        signatures = []
        for key_hash in case.public["pubkey_hashes"][:case.public["threshold"]]:
            signatures.append(_sign_for_test(_wire_bytes(case.private["message"]), keys[key_hash]))
        script = case.private["multisig_script"]
        return {
            "args": case.private["args"],
            "message": case.private["message"],
            "multisig_script": "0x" + script.hex(),
            "witness_lock": "0x" + (script + b"".join(signatures)).hex(),
        }
    if check == "sighash_witness_groups":
        message = case.private["expected"]["message"]
        signature = _sign_for_test(
            _wire_bytes(message),
            int(case.public["private_key"], 16),
        )
        return {"message": message, "witness_lock": "0x" + signature.hex()}
    if "expected" in case.private:
        return case.private["expected"]
    raise AssertionError(check)


def test_ccc_builder_accepts_recipient_and_change_in_either_order():
    case = project_cases("ccc_transaction_builder", "challenge", 1)[0]
    result = _expected_result("ccc_transaction_builder", case)
    assert check_project_result("ccc_transaction_builder", case, result)

    result["outputs"].reverse()
    result["outputs_data"].reverse()
    assert check_project_result("ccc_transaction_builder", case, result)


def _compressed_for_test(private_key: int) -> bytes:
    point = _point_mul(private_key, _SECP_G)
    assert point is not None
    return bytes((2 | (point[1] & 1),)) + point[0].to_bytes(32, "big")


def _sign_for_test(message: bytes, private_key: int) -> bytes:
    z = int.from_bytes(message, "big") % _SECP_N
    nonce = 1
    while True:
        point = _point_mul(nonce, _SECP_G)
        assert point is not None
        r = point[0] % _SECP_N
        s = pow(nonce, -1, _SECP_N) * (z + r * private_key) % _SECP_N
        if r and s:
            recovery = (2 if point[0] >= _SECP_N else 0) | (point[1] & 1)
            if s > _SECP_N // 2:
                s = _SECP_N - s
                recovery ^= 1
            return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes((recovery,))
        nonce += 1


def test_every_project_checker_has_repeatable_cases_and_a_valid_solution():
    for check in sorted(PROJECT_CHECKS):
        assert len(project_cases(check, "challenge", PROJECT_VERIFIER_CASE_LIMITS[check])) == (
            PROJECT_VERIFIER_CASE_LIMITS[check]
        )
        first = project_cases(check, "challenge", 8)
        second = project_cases(check, "challenge", 8)
        assert first == second
        assert first != project_cases(check, "different", 8)
        for case in first:
            result = _expected_result(check, case)
            assert check_project_result(check, case, result)
            assert not check_project_result(check, case, {"wrong": True})


def test_balancer_accepts_any_valid_selection_and_rejects_bad_fee_or_duplicates():
    case = project_cases("transaction_balancer", "challenge", 1)[0]
    valid = _expected_result("transaction_balancer", case)
    assert _check_balancer(case.public, valid)
    assert not _check_balancer(case.public, {**valid, "change": valid["change"] + 2_000})
    duplicate = [valid["selected_input_ids"][0]] * 2
    assert not _check_balancer(case.public, {"change": valid["change"], "selected_input_ids": duplicate})


def test_balancer_cases_keep_requested_outputs_above_occupied_capacity():
    cases = project_cases("transaction_balancer", "challenge", 8)

    assert all(
        output["capacity"] >= 6_100_000_000
        for case in cases
        for output in case.public["outputs"]
    )


def test_ccc_cases_cover_single_and_multiple_input_selection():
    cases = project_cases("ccc_transaction_builder", "challenge", 8)
    selected_counts = []
    for case in cases:
        result = _expected_result("ccc_transaction_builder", case)
        assert check_project_result("ccc_transaction_builder", case, result)
        selected_counts.append(len(result["inputs"]))
        assert result["outputs"][1]["capacity"] >= case.public["minimum_change"]
    assert 1 in selected_counts
    assert any(count > 1 for count in selected_counts)


def test_ccc_checker_rejects_non_transaction_output_fields():
    case = project_cases("ccc_transaction_builder", "challenge", 1)[0]
    result = _expected_result("ccc_transaction_builder", case)
    result["outputs"][1]["metadata"] = "not-a-cell-output-field"

    assert not check_project_result("ccc_transaction_builder", case, result)


def test_project_checkers_reject_integer_looking_floats():
    case = project_cases("dao_withdrawal_planner", "challenge", 1)[0]
    expected = _expected_result("dao_withdrawal_planner", case)
    candidate = deepcopy(expected)
    candidate["maximum_withdraw_capacity"] = float(candidate["maximum_withdraw_capacity"])
    assert not check_project_result("dao_withdrawal_planner", case, candidate)

    ccc_case = project_cases("ccc_transaction_builder", "challenge", 1)[0]
    expected = _expected_result("ccc_transaction_builder", ccc_case)
    candidate = deepcopy(expected)
    candidate["outputs"][0]["capacity"] = float(candidate["outputs"][0]["capacity"])
    assert not check_project_result("ccc_transaction_builder", ccc_case, candidate)

    candidate = deepcopy(expected)
    candidate["outputs"][1]["capacity"] = float(candidate["outputs"][1]["capacity"])
    assert not check_project_result("ccc_transaction_builder", ccc_case, candidate)


def test_indexer_checker_rejects_json_values_that_are_not_schema_types():
    case = project_cases("cell_query_indexer", "challenge", 1)[0]
    valid = _expected_result("cell_query_indexer", case)
    assert not check_project_result("cell_query_indexer", case, {**valid, "balance": True})
    assert not check_project_result(
        "cell_query_indexer", case, {**valid, "cell_ids": [1]}
    )
    assert not check_project_result(
        "cell_query_indexer", case, {**valid, "last_cursor": 1}
    )


def test_indexer_cases_require_wide_integer_arithmetic():
    cases = project_cases("cell_query_indexer", "challenge", 8)
    capacities = [
        cell["capacity"]
        for case in cases
        for page in case.public["pages"]
        for cell in page["objects"]
    ]

    assert all(0 < value < 1 << 64 for value in capacities)
    assert any(value > 1 << 53 for value in capacities)
    assert any(sum(case.private["expected_cells"].values()) > 1 << 64 for case in cases)
    assert any(
        int(sum(float(value) for value in case.private["expected_cells"].values()))
        != sum(case.private["expected_cells"].values())
        for case in cases
    )


def test_address_codec_matches_rfc_full_vector_and_rejects_checksum_change():
    payload = bytes.fromhex(
        "00"
        "9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8"
        "01"
        "b39bbc0b3673c7d36450bc14cfcdad2d559c6c64"
    )
    address = _address("ckb", payload, True)
    assert address == "ckb1qzda0cr08m85hc8jlnfp3zer7xulejywt49kt2rr0vthywaa50xwsqdnnw7qkdnnclfkg59uzn8umtfd2kwxceqxwquc4"
    assert _decode_address(address) == ("ckb", payload, True)
    assert _decode_address(address.upper()) == ("ckb", payload, True)
    with pytest.raises(ValueError, match="checksum"):
        _decode_address(address[:-1] + "q")


def test_address_cases_cover_both_formats_networks_and_invalid_forms():
    cases = project_cases("address_codec", "challenge", 23)
    operations = [case.public["operation"] for case in cases]
    assert operations.count("encode") == 7
    assert operations.count("decode") == 16
    encoded_hash_types = {
        case.public["script"]["hash_type"]
        for case in cases
        if case.public["operation"] == "encode" and case.public["format"] == "full"
    }
    assert encoded_hash_types == {"data", "type", "data1", "data2"}
    uppercase = [
        case for case in cases
        if case.public["operation"] == "decode" and case.public["address"].isupper()
    ]
    assert {case.private["expected"]["format"] for case in uppercase} == {"full", "short"}
    expected_errors = {
        case.private["expected"].get("error")
        for case in cases
        if "error" in case.private["expected"]
    }
    assert expected_errors == {
        "invalid_address",
        "network_mismatch",
        "unsupported_script_template",
    }


def test_molecule_cases_cover_raw_transaction_vector_and_script_variants():
    cases = project_cases("molecule_transaction", "challenge", 8)
    transactions = [case.public["raw_transaction"] for case in cases]

    assert {dep["dep_type"] for tx in transactions for dep in tx["cell_deps"]} == {
        "code",
        "dep_group",
    }
    assert {script["hash_type"] for tx in transactions for output in tx["outputs"]
            for script in (output["lock"], output["type"]) if script is not None} == {
        "data",
        "type",
        "data1",
        "data2",
    }
    assert any(len(tx["cell_deps"]) > 1 for tx in transactions)
    assert any(len(tx["header_deps"]) > 1 for tx in transactions)
    assert any(len(tx["inputs"]) > 1 for tx in transactions)
    assert any(len(tx["outputs"]) > 1 for tx in transactions)
    assert any(value != "0x" for tx in transactions for value in tx["outputs_data"])
    assert {tx["version"] for tx in transactions} == {hex(index) for index in range(8)}


def test_multisig_checker_verifies_signatures_order_and_required_keys():
    case = project_cases("multisig_witness", "challenge", 8)[7]
    valid = _expected_result("multisig_witness", case)
    assert check_project_result("multisig_witness", case, valid)

    witness = bytearray(_wire_bytes(valid["witness_lock"]))
    witness[-2] ^= 1
    assert not check_project_result(
        "multisig_witness",
        case,
        {**valid, "witness_lock": "0x" + witness.hex()},
    )


def test_grade_project_task_keeps_hidden_material_out_of_candidate_stage(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    artifact = tmp_path / ".ckbbench-artifact"
    calls: list[RunnerInvocation] = []
    cases = project_cases("address_codec", "private-challenge", 2)

    def runner(invocation: RunnerInvocation):
        calls.append(invocation)
        if invocation.stage == "build":
            entry = artifact / "build" / "release" / "project-tool"
            entry.parent.mkdir(parents=True)
            entry.write_text("executable")
            entry.chmod(0o755)
            return 0
        input_host = next(Path(host) for host, target in invocation.mounts.items() if target == "/input:ro")
        output_host = next(Path(host) for host, target in invocation.mounts.items() if target == "/output")
        public = json.loads((input_host / "case.json").read_text())
        case = next(row for row in cases if row.public == public)
        (output_host / "result.json").write_text(json.dumps(case.private["expected"]))
        return 0

    verdict = grade_project_task(
        _task(),
        mount,
        ProjectVerifierSpec("address_codec", 2),
        {CODE_CHALLENGE_ENV: "private-challenge"},
        runner,
    )
    assert verdict.passed
    assert verdict.diagnostics.criteria_passed == 2
    assert [call.stage for call in calls] == ["build", "exercise", "exercise"]
    for call in calls[1:]:
        assert not call.env
        assert set(call.mounts.values()) == {"/artifact:ro", "/input:ro", "/output"}
        assert all("hidden" not in value for value in call.mounts)


def test_grade_project_task_rejects_failed_or_malformed_candidate(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    artifact = tmp_path / ".ckbbench-artifact"

    def failed(invocation: RunnerInvocation):
        if invocation.stage == "build":
            entry = artifact / "build" / "release" / "project-tool"
            entry.parent.mkdir(parents=True)
            entry.write_text("executable")
            entry.chmod(0o755)
            return 0
        return 2

    verdict = grade_project_task(
        _task(count=1),
        mount,
        ProjectVerifierSpec("address_codec", 1),
        {CODE_CHALLENGE_ENV: "challenge"},
        failed,
    )
    assert not verdict.passed
    assert verdict.reason == "candidate execution failed"


def test_grade_project_task_treats_deep_json_as_candidate_failure(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    artifact = tmp_path / ".ckbbench-artifact"

    def runner(invocation: RunnerInvocation):
        if invocation.stage == "build":
            entry = artifact / "build" / "release" / "project-tool"
            entry.parent.mkdir(parents=True)
            entry.write_text("executable")
            entry.chmod(0o755)
            return 0
        output_host = next(
            Path(host) for host, target in invocation.mounts.items() if target == "/output"
        )
        (output_host / "result.json").write_text("[" * 2_000 + "0" + "]" * 2_000)
        return 0

    verdict = grade_project_task(
        _task(count=1),
        mount,
        ProjectVerifierSpec("address_codec", 1),
        {CODE_CHALLENGE_ENV: "challenge"},
        runner,
    )

    assert not verdict.passed
    assert verdict.reason == "hidden behavioral case failed"


def test_project_artifact_tree_has_file_count_and_size_bounds(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(projecttask, "MAX_PROJECT_ARTIFACT_FILE_BYTES", 4)
    monkeypatch.setattr(projecttask, "MAX_PROJECT_ARTIFACT_FILES", 3)
    monkeypatch.setattr(projecttask, "MAX_PROJECT_ARTIFACT_BYTES", 8)
    root = tmp_path / "artifact"
    entry = root / "build" / "release" / "tool"
    entry.parent.mkdir(parents=True)
    entry.write_text("x")
    entry.chmod(0o755)
    _validate_artifact_tree(root, entry)

    oversized = root / "oversized"
    oversized.write_bytes(b"x" * 5)
    with pytest.raises(ValueError, match="artifact size"):
        _validate_artifact_tree(root, entry)
    oversized.unlink()

    for index in range(3):
        (root / f"extra-{index}").write_text("x")
    with pytest.raises(ValueError, match="file count"):
        _validate_artifact_tree(root, entry)

    for path in root.glob("extra-*"):
        path.unlink()
    companion = root / "companion"
    companion.write_bytes(b"x" * 4)
    entry.write_bytes(b"x" * 4)
    (root / "third").write_bytes(b"x")
    with pytest.raises(ValueError, match="artifact size"):
        _validate_artifact_tree(root, entry)


def test_project_artifact_tree_rejects_symlinks_and_non_executable_entry(tmp_path: Path):
    root = tmp_path / "artifact"
    entry = root / "build" / "release" / "tool"
    entry.parent.mkdir(parents=True)
    entry.write_text("tool")
    with pytest.raises(ValueError, match="entrypoint"):
        _validate_artifact_tree(root, entry)

    entry.chmod(0o755)
    (root / "link").symlink_to(entry)
    with pytest.raises(ValueError, match="unsafe entry"):
        _validate_artifact_tree(root, entry)


def test_project_artifact_tree_bounds_directories_before_collecting_them(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(projecttask, "MAX_PROJECT_ARTIFACT_ENTRIES", 3)
    root = tmp_path / "artifact"
    entry = root / "tool"
    root.mkdir()
    entry.write_text("tool")
    entry.chmod(0o755)
    (root / "empty-1").mkdir()
    (root / "empty-2").mkdir()
    _validate_artifact_tree(root, entry)

    (root / "empty-3").mkdir()
    with pytest.raises(ValueError, match="entry count"):
        _validate_artifact_tree(root, entry)


def test_project_result_reader_stops_after_detecting_a_second_entry(
    tmp_path: Path,
    monkeypatch,
):
    output = tmp_path / "output"
    output.mkdir()
    result = output / "result.json"
    result.write_text("{}")
    sibling = output / "extra"
    sibling.write_text("x")

    class NamedEntry:
        def __init__(self, name: str):
            self.name = name

    class BoundedEntries:
        def __init__(self):
            self._entries = iter((NamedEntry(result.name), NamedEntry(sibling.name)))
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            self.calls += 1
            if self.calls > 2:
                raise AssertionError("result reader traversed beyond the second entry")
            return next(self._entries)

    entries = BoundedEntries()
    monkeypatch.setattr(projecttask.os, "scandir", lambda _path: entries)

    with pytest.raises(ValueError, match="missing or oversized"):
        _load_result(result)
    assert entries.calls == 2


@pytest.mark.parametrize("extra_kind", ["file", "directory", "oversized"])
def test_grade_project_task_rejects_noncanonical_case_output(
    tmp_path: Path,
    extra_kind: str,
    monkeypatch,
):
    monkeypatch.setattr(projecttask, "MAX_PROJECT_DOCUMENT_BYTES", 8)
    mount = tmp_path / "mount"
    mount.mkdir()
    artifact = tmp_path / ".ckbbench-artifact"
    case = project_cases("address_codec", "challenge", 1)[0]

    def runner(invocation: RunnerInvocation):
        if invocation.stage == "build":
            entry = artifact / "build" / "release" / "project-tool"
            entry.parent.mkdir(parents=True)
            entry.write_text("#!/bin/sh\n")
            entry.chmod(0o755)
            return 0
        output_host = next(
            Path(host) for host, target in invocation.mounts.items() if target == "/output"
        )
        result = output_host / "result.json"
        if extra_kind == "oversized":
            result.write_bytes(b" " * 9)
        else:
            result.write_text(json.dumps(case.private["expected"]))
            extra = output_host / "extra"
            extra.write_text("x") if extra_kind == "file" else extra.mkdir()
        return 0

    verdict = grade_project_task(
        _task(count=1),
        mount,
        ProjectVerifierSpec("address_codec", 1),
        {CODE_CHALLENGE_ENV: "challenge"},
        runner,
    )

    assert not verdict.passed
    assert verdict.reason == "hidden behavioral case failed"


def test_hidden_project_verifier_runs_after_cases_without_reaching_candidate(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite_dir = tmp_path / "task"
    hidden = suite_dir / "hidden"
    hidden.mkdir(parents=True)
    artifact = tmp_path / ".ckbbench-artifact"
    calls: list[RunnerInvocation] = []
    cases = project_cases("address_codec", "private-challenge", 2)

    def runner(invocation: RunnerInvocation):
        calls.append(invocation)
        if invocation.stage == "build":
            entry = artifact / "build" / "release" / "project-tool"
            entry.parent.mkdir(parents=True)
            entry.write_text("executable")
            entry.chmod(0o755)
            return 0
        if invocation.stage == "exercise":
            input_host = next(
                Path(host)
                for host, target in invocation.mounts.items()
                if target == "/input:ro"
            )
            output_host = next(
                Path(host)
                for host, target in invocation.mounts.items()
                if target == "/output"
            )
            public = json.loads((input_host / "case.json").read_text())
            case = next(row for row in cases if row.public == public)
            (output_host / "result.json").write_text(json.dumps(case.private["expected"]))
            assert all("hidden" not in target for target in invocation.mounts.values())
            return 0
        proof_host = next(
            Path(host)
            for host, target in invocation.mounts.items()
            if target == "/artifact:ro"
        )
        document = json.loads((proof_host / "cases.json").read_text())
        assert len(document["cases"]) == 2
        assert invocation.mounts[str(hidden.resolve())] == "/suite:ro"
        assert invocation.env == {CODE_CHALLENGE_ENV: "private-challenge"}
        return RunnerResult(
            0,
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; "
            "0 filtered out; finished in 0.01s\n",
        )

    verdict = grade_project_task(
        _task(),
        mount,
        ProjectVerifierSpec("address_codec", 2, "hidden"),
        {CODE_CHALLENGE_ENV: "private-challenge"},
        runner,
        suite_dir=suite_dir,
    )

    assert verdict.passed
    assert verdict.diagnostics.criteria_passed == 3
    assert [call.stage for call in calls] == ["build", "exercise", "exercise", "verify"]


def test_hidden_project_verifier_must_emit_trustworthy_success_diagnostics(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite_dir = tmp_path / "task"
    (suite_dir / "hidden").mkdir(parents=True)
    artifact = tmp_path / ".ckbbench-artifact"
    case = project_cases("address_codec", "challenge", 1)[0]

    def runner(invocation: RunnerInvocation):
        if invocation.stage == "build":
            entry = artifact / "build" / "release" / "project-tool"
            entry.parent.mkdir(parents=True)
            entry.write_text("executable")
            entry.chmod(0o755)
            return 0
        if invocation.stage == "exercise":
            output_host = next(
                Path(host)
                for host, target in invocation.mounts.items()
                if target == "/output"
            )
            (output_host / "result.json").write_text(json.dumps(case.private["expected"]))
            return 0
        return RunnerResult(0, "")

    with pytest.raises(VerificationInfrastructureError, match="trustworthy diagnostics"):
        grade_project_task(
            _task(count=1),
            mount,
            ProjectVerifierSpec("address_codec", 1, "hidden"),
            {CODE_CHALLENGE_ENV: "challenge"},
            runner,
            suite_dir=suite_dir,
        )


def test_hidden_project_verifier_is_required_and_can_reject(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()

    missing = grade_project_task(
        _task(),
        mount,
        ProjectVerifierSpec("address_codec", 2, "hidden"),
        {CODE_CHALLENGE_ENV: "challenge"},
        lambda invocation: 0,
    )
    assert not missing.passed
    assert missing.reason == "project task requires its hidden verifier"

    suite_dir = tmp_path / "task"
    (suite_dir / "hidden").mkdir(parents=True)
    artifact = tmp_path / ".ckbbench-artifact"
    cases = project_cases("address_codec", "challenge", 1)

    def runner(invocation: RunnerInvocation):
        if invocation.stage == "build":
            entry = artifact / "build" / "release" / "project-tool"
            entry.parent.mkdir(parents=True)
            entry.write_text("executable")
            entry.chmod(0o755)
            return 0
        if invocation.stage == "exercise":
            output_host = next(
                Path(host)
                for host, target in invocation.mounts.items()
                if target == "/output"
            )
            (output_host / "result.json").write_text(json.dumps(cases[0].private["expected"]))
            return 0
        return RunnerResult(
            101,
            "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; "
            "0 filtered out; finished in 0.01s\n",
        )

    rejected = grade_project_task(
        _task(count=1),
        mount,
        ProjectVerifierSpec("address_codec", 1, "hidden"),
        {CODE_CHALLENGE_ENV: "challenge"},
        runner,
        suite_dir=suite_dir,
    )
    assert not rejected.passed
    assert rejected.reason == "hidden protocol execution failed"
    assert rejected.diagnostics.criteria_failed == 1


def test_unknown_checker_and_excess_case_count_fail_closed():
    with pytest.raises(ValueError, match="unsupported"):
        project_cases("unknown", "challenge", 1)
    with pytest.raises(ValueError, match="more cases"):
        project_cases("address_codec", "challenge", 24)
