"""Release invariants for the complete CKB development suite."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from ckbbench.run.chain_profile import ChainProfile
from ckbbench.run.retry_policy import RETRY_POLICY_ID, RETRY_POLICY_SHA256
from ckbbench.run.suite_release import load_chain_profile, load_suite_release
from ckbbench.suite.execution_contract import TASK_EXECUTION_SCHEMA_VERSION
from ckbbench.suite.freeze import freeze, freeze_sha256
from ckbbench.suite.registry import load_suite


ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = ROOT / "suites" / "ckb-core-v1"
PREVIOUS_ROOT = ROOT / "suites" / "ckb-independent-v1"
HISTORICAL_ROOT = ROOT / "suites" / "ckb-v1"
REFERENCE_WORKSPACE = ROOT / "spikes" / "code-task" / "ws"

TASK_IDS = (
    "task-01-tip",
    "task-06-sudt-script",
    "task-04-send-tx",
    "task-08-type-id-data-cell",
    "task-05-hashlock",
    "task-09-since-lock",
    "task-10-data-guard",
    "task-11-token-conservation",
)
TASK_SCORES = (5, 5, 15, 15, 15, 15, 10, 20)
EXPECTED_CONTRACTS = {
    "task-01-tip": ("testnet", 40, 600, 160, False),
    "task-06-sudt-script": ("local-hermetic", 40, 600, 160, False),
    "task-04-send-tx": ("testnet", 80, 1200, 320, True),
    "task-08-type-id-data-cell": ("testnet", 100, 1800, 400, True),
    "task-05-hashlock": ("local-hermetic", 120, 2400, 480, False),
    "task-09-since-lock": ("local-hermetic", 100, 1800, 400, False),
    "task-10-data-guard": ("local-hermetic", 100, 1800, 400, False),
    "task-11-token-conservation": ("local-hermetic", 120, 2400, 480, False),
}
EXPECTED_CEILINGS = {
    "arm_count": 2,
    "maximum_agent_wall_seconds": 50400,
    "maximum_attempts": 32,
    "maximum_end_to_end_seconds": 118320,
    "maximum_grading_seconds": 36720,
    "maximum_harness_seconds": 67440,
    "maximum_output_tokens": None,
    "maximum_preflight_seconds": 9600,
    "maximum_provider_calls": 11200,
    "maximum_retry_cooldown_seconds": 480,
    "maximum_setup_seconds": 9120,
    "maximum_steps": 2800,
    "maximum_teardown_seconds": 12000,
    "planned_slots": 16,
    "schema_version": "ckbbench-campaign-ceilings-v1",
    "scope": "one-trial-per-task-per-arm",
    "whole_task_attempts_per_slot": 2,
}
MUTANTS = {
    "task-09-since-lock": {
        "accepts-everything",
        "checks-first-input-only",
        "compares-raw-values",
        "uses-global-inputs",
    },
    "task-10-data-guard": {
        "accepts-everything",
        "ignores-group-shape",
        "ignores-input-data",
        "uses-global-cells",
    },
    "task-11-token-conservation": {
        "accepts-owner-output",
        "checks-first-cell-only",
        "rejects-burns",
        "uses-global-cells",
        "wraps-overflow",
    },
}
HISTORICAL_SHA256 = {
    "suites/ckb-independent-v1/manifest.json": "24dfb4afc82d7e9daf66ecd8a5f3ded5990ff196c144778cf64984523580e3a5",
    "suites/ckb-independent-v1/suite.freeze.json": "f194e16fdc4469c702bb52924551e17ddf32d1f6165d15e7fab820c1569d2b2c",
    "suites/ckb-v1/manifest.json": "24291f0ed6e87efb31dcd183374f2f27c7cabcd762647134953b72aa1010395d",
    "suites/ckb-v1/suite.freeze.json": "7fd47d80733a762fa516741ecbf789806da64042ac25da37d350e380172431b3",
}


@pytest.fixture(scope="module")
def suite():
    return load_suite(SUITE_ROOT)


@pytest.fixture(scope="module")
def release():
    return load_suite_release(SUITE_ROOT)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_identity_order_weights_and_pins(suite):
    previous = load_suite(PREVIOUS_ROOT)
    assert suite.suite_semver == "5.0.0"
    assert suite.chain_profile == "task-scoped-v1"
    assert suite.task_execution_schema_version == TASK_EXECUTION_SCHEMA_VERSION
    assert suite.mcp_server_version == "1.6.13"
    assert suite.pins.retry_policy_id == RETRY_POLICY_ID
    assert suite.pins.retry_policy_sha256 == RETRY_POLICY_SHA256
    assert suite.pins == previous.pins
    assert tuple(task.id for task in suite.tasks) == TASK_IDS
    assert tuple(task.score for task in suite.tasks) == TASK_SCORES
    assert sum(task.score for task in suite.tasks) == 100
    assert all(task.scored for task in suite.tasks)


def test_every_task_has_the_exact_execution_contract(suite):
    contract_ids: set[str] = set()
    for task in suite.tasks:
        contract = task.execution
        assert contract is not None
        assert contract.contract_id not in contract_ids
        contract_ids.add(contract.contract_id)
        expected = EXPECTED_CONTRACTS[task.id]
        assert (
            contract.chain_track,
            contract.budget.step_limit,
            contract.budget.wall_time_limit_seconds,
            contract.budget.provider_call_limit,
            contract.signer_required,
        ) == expected
        assert contract.budget.output_token_limit is None
        assert contract.calibration.status == "owner-approved-exception"
        assert contract.treatment.required_tools == ("search_resources",)
        assert contract.treatment.required_resource_prefixes == ("ckb://docs/",)


def test_chain_funding_and_signing_requirements_match_task_tracks(suite):
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
        assert (profile.chain_track, profile.sha256) == (
            contract.chain_track,
            contract.chain_profile_sha256,
        )
        if task.id in {"task-04-send-tx", "task-08-type-id-data-cell"}:
            assert contract.funding is not None
            assert contract.signing_policy_id is not None
            assert {"signer", "spendable-input", "transaction"} <= set(
                contract.required_resource_kinds
            )
        else:
            assert contract.funding is None
            assert contract.signing_policy_id is None


def test_budget_basis_binds_every_contract(release):
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
        expected_decision = (
            "core-suite-budget-policy-v1"
            if task.id in MUTANTS
            else "independent-task-budget-policy-v1"
        )
        assert basis.decision_reference == expected_decision


def test_release_freeze_rebuilds_byte_for_byte(release):
    rebuilt = freeze(release.suite, SUITE_ROOT)
    tracked = json.loads((SUITE_ROOT / "suite.freeze.json").read_text(encoding="ascii"))
    assert rebuilt == tracked == release.freeze_document
    assert freeze_sha256(rebuilt) == release.freeze_sha256
    assert tracked["task_order"] == list(TASK_IDS)
    assert tracked["campaign_ceilings"] == EXPECTED_CEILINGS
    assert set(tracked["tasks"]) == set(TASK_IDS)


def test_retained_tasks_match_the_previous_release_except_for_weight(suite):
    previous = {task.id: task for task in load_suite(PREVIOUS_ROOT).tasks}
    current = {task.id: task for task in suite.tasks}
    for task_id, prior in previous.items():
        assert replace(current[task_id], score=prior.score) == prior
        assert (SUITE_ROOT / task_id / "prompt.txt").read_bytes() == (
            PREVIOUS_ROOT / task_id / "prompt.txt"
        ).read_bytes()


def test_code_task_reference_and_mutant_inventory(suite):
    code_tasks = [task for task in suite.tasks if task.kind == "code"]
    assert [task.id for task in code_tasks] == [
        "task-05-hashlock",
        "task-09-since-lock",
        "task-10-data-guard",
        "task-11-token-conservation",
    ]
    for task in code_tasks:
        task_dir = SUITE_ROOT / task.id
        reference = task_dir / "reference" / Path(task.proof_file).name
        assert reference.read_bytes().startswith(b"\x7fELF")
        actual = (
            {path.name for path in (task_dir / "mutants").iterdir()}
            if (task_dir / "mutants").is_dir()
            else set()
        )
        assert actual == MUTANTS.get(task.id, set())


def test_new_hidden_suites_use_only_the_generic_private_challenge():
    for task_id in MUTANTS:
        source = (SUITE_ROOT / task_id / "hidden" / "src" / "tests.rs").read_text()
        assert "CKBBENCH_CHALLENGE" in source
        assert "BENCH_PASSWORD" not in source


def test_prompts_state_the_scored_contract_boundaries(suite):
    prompts = {task.id: task.prompt_fragment for task in suite.tasks}
    since = prompts["task-09-since-lock"]
    assert "8-byte little-endian relative since threshold" in since
    assert "every input" in since.lower() and "GroupInput" in since
    data = prompts["task-10-data-guard"]
    assert "zero GroupInputs" in data and "exactly one GroupOutput" in data
    token = prompts["task-11-token-conservation"]
    assert "first 16 bytes" in token and "checked addition" in token
    assert "input cell lock hash" in token


def test_historical_release_files_remain_byte_identical():
    for relative, expected in HISTORICAL_SHA256.items():
        assert _sha256(ROOT / relative) == expected


def test_release_tree_is_regular_and_bounded():
    assert not [path for path in SUITE_ROOT.rglob("*") if path.is_symlink()]
    oversized = [
        path
        for path in SUITE_ROOT.rglob("*")
        if path.is_file() and path.stat().st_size > 1 << 20
    ]
    assert not oversized


def test_reference_workspace_tracks_locked_sources_for_every_new_contract():
    workspace = tomllib.loads(
        (REFERENCE_WORKSPACE / "Cargo.toml").read_text(encoding="utf-8")
    )
    members = set(workspace["workspace"]["members"])
    expected = {
        "contracts/since-lock",
        "contracts/data-guard",
        "contracts/token-conservation",
    }
    assert expected <= members
    assert (REFERENCE_WORKSPACE / "Cargo.lock").is_file()
    for relative in expected:
        assert (REFERENCE_WORKSPACE / relative / "Cargo.toml").is_file()
        assert (REFERENCE_WORKSPACE / relative / "src" / "main.rs").is_file()
