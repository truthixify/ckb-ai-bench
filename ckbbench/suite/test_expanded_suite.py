"""Release invariants for the expanded CKB task registry."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ckbbench.suite.execution_contract import BudgetBasisEvidence
from ckbbench.suite.freeze import freeze, freeze_sha256, hash_task_dir
from ckbbench.suite.registry import load_suite


ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = ROOT / "suites" / "ckb-core-v3"
PREVIOUS_ROOT = ROOT / "suites" / "ckb-core-v2"
LEGACY_ROOT = ROOT / "suites" / "ckb-v1"
TASK_IDS = (
    "task-01-tip",
    "task-address-tool",
    "task-06-sudt-script",
    "task-cell-query-indexer",
    "task-transaction-balancing",
    "task-molecule-transaction-encoding",
    "task-sighash-witness-groups",
    "task-multisig-transaction-builder",
    "task-dao-withdrawal-planner",
    "task-cell-dependency-resolver",
    "task-ccc-transaction-builder-repair",
    "task-04-send-tx",
    "task-multi-recipient-transfer",
    "task-08-type-id-data-cell",
    "task-type-id-upgrade",
    "task-xudt-issuance",
    "task-xudt-transfer",
    "task-acp-deposit",
    "task-spore-creation",
    "task-05-hashlock",
    "task-09-since-lock",
    "task-10-data-guard",
    "task-11-token-conservation",
    "task-grouped-cell-contract-repair",
    "task-javascript-state-transition",
)
RETAINED = {
    "task-01-tip",
    "task-04-send-tx",
    "task-05-hashlock",
    "task-06-sudt-script",
    "task-08-type-id-data-cell",
    "task-09-since-lock",
    "task-10-data-guard",
    "task-11-token-conservation",
}
HISTORICAL_TREE_SHA256 = {
    "ckb-v1": "7c4124eb502410b9f166c78ce84860c0d267982a96d26504a054d54fe56aad66",
    "ckb-core-v2": "83c7dc02133807e1e747973ff64e7f2998b98a1bf2943003c4da6f66e4dad5cb",
}


def _suite():
    return load_suite(SUITE_ROOT)


def _authored_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and not {
            "alternate",
            "build",
            "mutants",
            "reference",
            "target",
            "__pycache__",
        } & set(path.relative_to(root).parts)
        and path.name != ".DS_Store"
    }


def test_release_has_the_deliberate_order_and_equal_weights():
    suite = _suite()
    assert suite.suite_semver == "6.0.0"
    assert tuple(task.id for task in suite.tasks) == TASK_IDS
    assert len(suite.tasks) == 25
    assert all(task.scored and task.score == 4 for task in suite.tasks)
    assert sum(task.score for task in suite.tasks) == 100
    assert {task.kind for task in suite.tasks} == {"onchain", "code", "project"}


def test_every_task_has_report_metadata_and_an_independent_contract():
    suite = _suite()
    contract_ids: set[str] = set()
    budget_ids: set[str] = set()
    for task in suite.tasks:
        assert task.report is not None
        assert all(task.report.to_dict().values())
        assert task.execution is not None
        contract = task.execution
        assert contract.contract_id not in contract_ids
        assert contract.budget.profile_id not in budget_ids
        contract_ids.add(contract.contract_id)
        budget_ids.add(contract.budget.profile_id)
        basis = BudgetBasisEvidence.from_dict(
            json.loads((SUITE_ROOT / task.id / "budget-basis.json").read_text())
        )
        assert basis.task_id == task.id
        assert basis.budget_profile_id == contract.budget.profile_id
        assert basis.budget_profile_sha256 == contract.budget.sha256
        assert contract.calibration.evidence_sha256s == (basis.sha256,)
        assert contract.treatment.required_tools == ("search_resources",)
        assert contract.treatment.required_resource_prefixes == ("ckb://docs/",)


def test_reader_facing_metadata_has_no_delivery_labels():
    delivery_label = re.compile(r"(?i)\b(?:phase[- ]?(?:one|two)|roadmap|delivery)\b")
    for task in _suite().tasks:
        assert task.report is not None
        for value in task.report.to_dict().values():
            assert delivery_label.search(value) is None


def test_local_and_testnet_capabilities_have_their_required_boundaries():
    for task in _suite().tasks:
        contract = task.execution
        assert contract is not None
        if contract.chain_track == "testnet":
            assert contract.treatment.claims_live_chain
            if contract.signer_required:
                assert contract.funding is not None
                assert contract.signing_policy_id is not None
                assert "signer" in contract.required_resource_kinds
        else:
            assert contract.chain_track == "local-hermetic"
            assert not contract.treatment.claims_live_chain
            assert contract.funding is None
            assert contract.signing_policy_id is None
            assert contract.required_dependencies == ()


def test_repair_tasks_seed_only_public_starter_sources():
    suite = {task.id: task for task in _suite().tasks}
    assert suite["task-ccc-transaction-builder-repair"].starter_dir == "starter"
    assert suite["task-grouped-cell-contract-repair"].starter_dir == "starter"
    assert suite["task-javascript-state-transition"].starter_dir == "starter"
    for task in suite.values():
        if task.starter_dir is None:
            continue
        starter = SUITE_ROOT / task.id / task.starter_dir
        assert starter.is_dir() and not starter.is_symlink()
        assert not any("hidden" in path.parts for path in starter.rglob("*"))


def test_retained_task_content_is_byte_equivalent_outside_release_metadata():
    for task_id in RETAINED:
        current = _authored_files(SUITE_ROOT / task_id)
        previous = _authored_files(PREVIOUS_ROOT / task_id)
        for metadata in ("meta.json", "budget-basis.json"):
            current.pop(metadata, None)
            previous.pop(metadata, None)
        if task_id == "task-09-since-lock":
            current = {
                path: content for path, content in current.items()
                if not path.startswith("hidden/")
            }
            previous = {
                path: content for path, content in previous.items()
                if not path.startswith("hidden/")
            }
        assert current == previous


def test_historical_suite_trees_remain_byte_identical():
    for name, expected in HISTORICAL_TREE_SHA256.items():
        assert hash_task_dir(ROOT / "suites" / name) == expected


def test_release_freeze_rebuilds_byte_for_byte():
    suite = _suite()
    freeze_path = SUITE_ROOT / "suite.freeze.json"
    tracked_bytes = freeze_path.read_bytes()
    tracked = json.loads(tracked_bytes)
    rebuilt = freeze(suite, SUITE_ROOT)
    serialized = (json.dumps(rebuilt, indent=2, sort_keys=True) + "\n").encode("ascii")

    assert tracked == rebuilt
    assert tracked_bytes == serialized
    assert freeze_sha256(tracked) == freeze_sha256(rebuilt)


def test_release_tree_contains_no_generated_or_unsafe_entries():
    entries = tuple(SUITE_ROOT.rglob("*"))
    assert not [path for path in entries if path.is_symlink()]
    assert not [
        path
        for path in entries
        if path.is_dir() and path.name in {"build", "target", "__pycache__"}
    ]
    assert not [
        path
        for path in entries
        if path.is_dir() and path.name in {"alternate", "mutants", "reference"}
    ]
    assert not [
        path
        for path in entries
        if path.is_file() and path.stat().st_size > 1 << 20
    ]
    assert not any("phase" in task_id.lower() for task_id in TASK_IDS)
