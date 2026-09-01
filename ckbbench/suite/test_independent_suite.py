"""Repository-level invariants for the independently executed Task suite."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from ckbbench.run.chain_profile import ChainProfile
from ckbbench.run.retry_policy import RETRY_POLICY_ID, RETRY_POLICY_SHA256
from ckbbench.run.suite_release import load_chain_profile, load_suite_release
from ckbbench.suite.execution_contract import TASK_EXECUTION_SCHEMA_VERSION, TaskBudgetProfile
from ckbbench.suite.freeze import freeze, freeze_sha256
from ckbbench.suite.registry import load_suite


ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = ROOT / "suites" / "ckb-independent-v1"
LEGACY_ROOT = ROOT / "suites" / "ckb-v1"

TASK_IDS = (
    "task-01-tip",
    "task-04-send-tx",
    "task-06-sudt-script",
    "task-08-type-id-data-cell",
    "task-05-hashlock",
)
TASK_SCORES = (10, 25, 10, 25, 30)
EXPECTED_CONTRACTS = {
    "task-01-tip": ("testnet", 40, 600, 160, False),
    "task-04-send-tx": ("testnet", 80, 1200, 320, True),
    "task-06-sudt-script": ("local-hermetic", 40, 600, 160, False),
    "task-08-type-id-data-cell": ("testnet", 100, 1800, 400, True),
    "task-05-hashlock": ("local-hermetic", 120, 2400, 480, False),
}
EXPECTED_CEILINGS = {
    "arm_count": 2,
    "maximum_agent_wall_seconds": 26400,
    "maximum_attempts": 20,
    "maximum_end_to_end_seconds": 63180,
    "maximum_grading_seconds": 15120,
    "maximum_harness_seconds": 36480,
    "maximum_output_tokens": None,
    "maximum_preflight_seconds": 6000,
    "maximum_provider_calls": 6080,
    "maximum_retry_cooldown_seconds": 300,
    "maximum_setup_seconds": 6960,
    "maximum_steps": 1520,
    "maximum_teardown_seconds": 8400,
    "planned_slots": 10,
    "schema_version": "ckbbench-campaign-ceilings-v1",
    "scope": "one-trial-per-task-per-arm",
    "whole_task_attempts_per_slot": 2,
}
TOOLCHAINS = {
    "@ckb-ccc/core": "1.12.5",
    "cargo-generate": "0.21.2",
    "ckb-testtool": "1.1.1",
    "litellm": "1.72.0",
    "nodejs": "22.14.0",
    "python": "3.12.8",
    "rust": "1.95.0",
    "tenacity": "9.1.2",
}
ROLE_PIN = re.compile(r"sha256:[0-9a-f]{64}")
LEGACY_AGENT = "sha256:b8ee8b4d09c89aaaa3dd8f79ca670ebe6c9f3396515965238344a78358a4cdb7"
LEGACY_VERIFIER = "sha256:464b1b77b69dd1bfbe136a801b5781156d44f5eee41547523339c28f7a10d857"
LEGACY_MANIFEST_SHA256 = "24291f0ed6e87efb31dcd183374f2f27c7cabcd762647134953b72aa1010395d"
LEGACY_FREEZE_SHA256 = "7fd47d80733a762fa516741ecbf789806da64042ac25da37d350e380172431b3"


@pytest.fixture(scope="module")
def suite():
    return load_suite(SUITE_ROOT)


@pytest.fixture(scope="module")
def release():
    return load_suite_release(SUITE_ROOT)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _authored_files(root: Path) -> dict[str, tuple[bytes, int]]:
    result: dict[str, tuple[bytes, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "target" in relative.parts or not path.is_file() or path.is_symlink():
            continue
        result[relative.as_posix()] = (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
    return result


def test_release_identity_and_role_images_are_fresh(suite):
    assert suite.suite_semver == "4.0.0"
    assert suite.chain_profile == "task-scoped-v1"
    assert suite.task_execution_schema_version == TASK_EXECUTION_SCHEMA_VERSION
    assert suite.mcp_server_version == "1.6.13"
    assert suite.pins.scoring_schema_version == "1"
    assert suite.pins.retry_policy_id == RETRY_POLICY_ID
    assert suite.pins.retry_policy_sha256 == RETRY_POLICY_SHA256
    assert suite.pins.toolchain_versions == TOOLCHAINS

    agent = suite.pins.agent_image_digest
    verifier = suite.pins.verifier_image_digest
    assert agent and ROLE_PIN.fullmatch(agent)
    assert verifier and ROLE_PIN.fullmatch(verifier)
    assert agent != verifier
    assert agent != LEGACY_AGENT
    assert verifier != LEGACY_VERIFIER


def test_release_has_the_fixed_scored_task_order(suite):
    assert tuple(task.id for task in suite.tasks) == TASK_IDS
    assert tuple(task.score for task in suite.tasks) == TASK_SCORES
    assert all(task.scored for task in suite.tasks)
    assert sum(task.score for task in suite.tasks) == 100


def test_every_task_has_one_model_neutral_execution_contract(suite):
    contract_ids: set[str] = set()
    for task in suite.tasks:
        contract = task.execution
        assert contract is not None
        assert contract.contract_id not in contract_ids
        contract_ids.add(contract.contract_id)

        track, steps, wall_seconds, provider_calls, signer_required = EXPECTED_CONTRACTS[task.id]
        assert (
            contract.chain_track,
            contract.budget.step_limit,
            contract.budget.wall_time_limit_seconds,
            contract.budget.provider_call_limit,
            contract.signer_required,
        ) == (track, steps, wall_seconds, provider_calls, signer_required)
        assert contract.budget.output_token_limit is None
        assert contract.calibration.status == "owner-approved-exception"
        assert contract.calibration.observed_max_steps is None
        assert contract.calibration.observed_max_wall_seconds is None
        assert contract.calibration.observed_max_provider_calls is None
        assert contract.treatment.required_tools == ("search_resources",)
        assert contract.treatment.required_resource_prefixes == ("ckb://docs/",)


def test_chain_profiles_match_every_execution_contract(suite):
    profiles: dict[str, ChainProfile] = {
        profile.profile_id: profile
        for profile in (
            load_chain_profile(ROOT / "configs" / "chains" / "ckb-testnet-pudge-v1.json"),
            load_chain_profile(ROOT / "configs" / "chains" / "local-hermetic-v1.json"),
        )
    }
    for task in suite.tasks:
        contract = task.execution
        assert contract is not None
        profile = profiles[contract.chain_profile_id]
        assert profile.chain_track == contract.chain_track
        assert profile.sha256 == contract.chain_profile_sha256


def test_signed_and_read_only_resources_are_declared_separately(suite):
    by_id = {task.id: task.execution for task in suite.tasks}
    tip = by_id["task-01-tip"]
    assert tip is not None
    assert tip.funding is None
    assert tip.signing_policy_id is None
    assert not {"signer", "spendable-input", "transaction", "data-cell"} & set(
        tip.required_resource_kinds
    )

    for task_id in ("task-04-send-tx", "task-08-type-id-data-cell"):
        contract = by_id[task_id]
        assert contract is not None and contract.funding is not None
        assert contract.signing_policy_id is not None
        assert {"signer", "spendable-input", "transaction"} <= set(
            contract.required_resource_kinds
        )
        assert contract.required_dependencies

    for task_id in ("task-06-sudt-script", "task-05-hashlock"):
        contract = by_id[task_id]
        assert contract is not None
        assert contract.required_dependencies == ()
        assert contract.funding is None
        assert not {"signer", "spendable-input", "transaction", "data-cell"} & set(
            contract.required_resource_kinds
        )


def test_budget_basis_is_complete_and_bound_to_each_contract(release):
    for task in release.suite.tasks:
        contract = task.execution
        assert contract is not None
        basis = release.budget_basis_for(task.id)
        assert basis.task_id == task.id
        assert basis.status == "owner-approved-exception"
        assert basis.budget_profile_id == contract.budget.profile_id
        assert basis.budget_profile_sha256 == contract.budget.sha256
        assert contract.calibration.evidence_sha256s == (basis.sha256,)
        assert basis.attempt_result_sha256s == ()
        assert basis.decision_reference == "independent-task-budget-policy-v1"


def test_release_freeze_rebuilds_byte_for_byte(release):
    rebuilt = freeze(release.suite, SUITE_ROOT)
    tracked_path = SUITE_ROOT / "suite.freeze.json"
    tracked = json.loads(tracked_path.read_text(encoding="ascii"))
    assert rebuilt == tracked == release.freeze_document
    assert freeze_sha256(rebuilt) == release.freeze_sha256
    assert tracked["task_order"] == list(TASK_IDS)
    assert tracked["campaign_ceilings"] == EXPECTED_CEILINGS
    assert set(tracked["tasks"]) == set(TASK_IDS)
    for task in release.suite.tasks:
        contract = task.execution
        assert contract is not None
        assert tracked["tasks"][task.id]["execution_contract_sha256"] == contract.sha256


def test_changing_a_real_task_budget_changes_its_release_identity(suite):
    task = next(task for task in suite.tasks if task.id == "task-05-hashlock")
    contract = task.execution
    assert contract is not None
    changed_budget = TaskBudgetProfile(
        profile_id=contract.budget.profile_id,
        step_limit=contract.budget.step_limit + 1,
        wall_time_limit_seconds=contract.budget.wall_time_limit_seconds,
        provider_call_limit=contract.budget.provider_call_limit,
        output_token_limit=contract.budget.output_token_limit,
    )
    changed_contract = replace(contract, budget=changed_budget)
    assert changed_contract.sha256 != contract.sha256


def test_authored_prompts_and_hashlock_verifier_match_the_legacy_release():
    current = load_suite(SUITE_ROOT)
    legacy = load_suite(LEGACY_ROOT)
    assert tuple(
        replace(task, execution=None) for task in current.tasks
    ) == legacy.tasks
    for task_id in TASK_IDS:
        assert (
            SUITE_ROOT / task_id / "prompt.txt"
        ).read_bytes() == (
            LEGACY_ROOT / task_id / "prompt.txt"
        ).read_bytes()
    for relative in ("hidden", "reference"):
        assert _authored_files(
            SUITE_ROOT / "task-05-hashlock" / relative
        ) == _authored_files(
            LEGACY_ROOT / "task-05-hashlock" / relative
        )


def test_legacy_release_files_remain_byte_identical():
    assert _sha256(LEGACY_ROOT / "manifest.json") == LEGACY_MANIFEST_SHA256
    assert _sha256(LEGACY_ROOT / "suite.freeze.json") == LEGACY_FREEZE_SHA256


def test_release_tree_contains_no_symlink():
    assert not [path for path in SUITE_ROOT.rglob("*") if path.is_symlink()]
