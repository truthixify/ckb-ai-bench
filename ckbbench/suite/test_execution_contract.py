from __future__ import annotations

from dataclasses import replace

import pytest

from ckbbench.run.chain_profile import ChainProfile, LOCAL_HERMETIC_PROFILE
from ckbbench.suite.execution_contract import (
    BUDGET_BASIS_SCHEMA_VERSION,
    CALIBRATION_SCHEMA_VERSION,
    TASK_EXECUTION_SCHEMA_VERSION,
    BudgetBasisEvidence,
    BudgetCalibration,
    DeploymentPin,
    FundingPolicy,
    HarnessDeadlines,
    TaskBudgetProfile,
    TaskExecutionContract,
    TaskExecutionContractError,
    TreatmentRequirement,
)

TESTNET_PROFILE = ChainProfile(
    profile_id="ckb-testnet-pudge-v1",
    chain_track="testnet",
    chain_id="ckb_testnet",
    genesis_hash="0x" + "1" * 64,
)


def _dependency(
    dependency_id: str = "secp256k1-blake160",
    digest: str = "2" * 64,
) -> DeploymentPin:
    return DeploymentPin(
        dependency_id=dependency_id,
        transaction_hash="0x" + "3" * 64,
        output_index=0,
        expected_cell_sha256=digest,
    )


def _budget(**changes) -> TaskBudgetProfile:
    values = {
        "profile_id": "budget-read-chain-v1",
        "step_limit": 20,
        "wall_time_limit_seconds": 480,
        "provider_call_limit": 20,
        "output_token_limit": None,
    }
    values.update(changes)
    return TaskBudgetProfile(**values)


def _calibration(**changes) -> BudgetCalibration:
    values = {
        "status": "calibrated",
        "evidence_sha256s": ("1" * 64,),
        "observed_max_steps": 12,
        "observed_max_wall_seconds": 240,
        "observed_max_provider_calls": 12,
    }
    values.update(changes)
    return BudgetCalibration(**values)


def _treatment(*, live: bool = True) -> TreatmentRequirement:
    return TreatmentRequirement(
        requirement_id="ckb-ai-testnet-docs-v1" if live else "ckb-ai-local-docs-v1",
        claims_live_chain=live,
        required_tools=("search_resources",),
        required_resource_prefixes=("ckb://docs/",),
    )


def _deadlines() -> HarnessDeadlines:
    return HarnessDeadlines(120, 120, 180, 120)


def _read_contract(**changes) -> TaskExecutionContract:
    values = {
        "contract_id": "read-chain-v1",
        "chain_track": "testnet",
        "chain_profile_id": TESTNET_PROFILE.profile_id,
        "chain_profile_sha256": TESTNET_PROFILE.sha256,
        "budget": _budget(),
        "harness_deadlines": _deadlines(),
        "treatment": _treatment(),
        "signer_required": False,
        "signing_policy_id": None,
        "funding": None,
        "required_dependencies": (),
        "required_resource_kinds": ("runtime-name", "workspace"),
        "expected_output_resource_kinds": ("workspace",),
        "run_params_derivation": "task-run-params-v1",
        "resource_equivalence_policy_id": "read-only-chain-equivalence-v1",
        "calibration": _calibration(),
    }
    values.update(changes)
    return TaskExecutionContract(**values)


def test_read_only_testnet_contract_round_trips_without_signer_or_funding():
    contract = _read_contract()
    assert TaskExecutionContract.from_dict(contract.to_dict()) == contract
    assert contract.schema_version == TASK_EXECUTION_SCHEMA_VERSION
    assert contract.calibration.schema_version == CALIBRATION_SCHEMA_VERSION
    assert len(contract.sha256) == 64
    assert len(contract.budget.sha256) == 64
    assert len(contract.resource_equivalence_policy_sha256) == 64


@pytest.mark.parametrize("resource_kind", ("data-cell", "transaction"))
def test_read_only_testnet_contract_refuses_write_resources(resource_kind):
    contract = _read_contract()
    resources = tuple(sorted({*contract.required_resource_kinds, resource_kind}))

    with pytest.raises(TaskExecutionContractError, match="write resources"):
        replace(contract, required_resource_kinds=resources)


def test_signed_testnet_contract_requires_funding_and_reserved_chain_resources():
    funding = FundingPolicy(
        maximum_transfer_shannons=20_000_000_000,
        fee_reserve_shannons=100_000_000,
        safety_margin_shannons=2_000_000_000,
        minimum_cell_count=1,
        minimum_confirmations=24,
    )
    contract = _read_contract(
        contract_id="send-transaction-v1",
        signer_required=True,
        signing_policy_id="bounded-transfer-v1",
        funding=funding,
        required_dependencies=(_dependency(),),
        required_resource_kinds=(
            "runtime-name",
            "signer",
            "spendable-input",
            "workspace",
        ),
        resource_equivalence_policy_id="signed-capacity-equivalence-v1",
    )
    assert contract.funding.required_capacity_shannons == 22_100_000_000

    with pytest.raises(TaskExecutionContractError, match="funding"):
        replace(contract, funding=None)
    with pytest.raises(TaskExecutionContractError, match="reserve"):
        replace(contract, required_resource_kinds=("runtime-name", "workspace"))


def test_local_hermetic_contract_refuses_chain_state_and_live_treatment():
    local = _read_contract(
        contract_id="build-code-v1",
        chain_track="local-hermetic",
        chain_profile_id=LOCAL_HERMETIC_PROFILE.profile_id,
        chain_profile_sha256=LOCAL_HERMETIC_PROFILE.sha256,
        treatment=_treatment(live=False),
        resource_equivalence_policy_id="local-workspace-equivalence-v1",
    )
    assert local.chain_track == "local-hermetic"

    with pytest.raises(TaskExecutionContractError, match="live chain"):
        replace(local, treatment=_treatment(live=True))
    with pytest.raises(TaskExecutionContractError, match="signer, funding"):
        replace(
            local,
            required_dependencies=(_dependency("remote-deployment", "3" * 64),),
        )

    for resource_kind in ("data-cell", "signer", "spendable-input", "transaction"):
        resources = tuple(sorted({*local.required_resource_kinds, resource_kind}))
        with pytest.raises(TaskExecutionContractError, match="chain resources"):
            replace(local, required_resource_kinds=resources)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"step_limit": True}, "positive integer"),
        ({"step_limit": 0}, "positive integer"),
        ({"step_limit": 1_001}, "hard ceiling"),
        ({"wall_time_limit_seconds": 14_401}, "hard ceiling"),
        ({"provider_call_limit": 19}, "lower than"),
        ({"provider_call_limit": 4_001}, "hard ceiling"),
        ({"output_token_limit": 10_000_001}, "hard ceiling"),
    ),
)
def test_budget_numeric_contract_fails_closed(changes, message):
    with pytest.raises(TaskExecutionContractError, match=message):
        _budget(**changes)


def test_release_identity_digests_refuse_zero_placeholders():
    with pytest.raises(TaskExecutionContractError, match="nonzero"):
        _read_contract(chain_profile_sha256="0" * 64)
    with pytest.raises(TaskExecutionContractError, match="nonzero"):
        _dependency(digest="0" * 64)
    with pytest.raises(TaskExecutionContractError, match="nonzero"):
        _calibration(evidence_sha256s=("0" * 64,))
    with pytest.raises(TaskExecutionContractError, match="nonzero"):
        _basis(budget_profile_sha256="0" * 64)


def test_calibration_must_be_evidenced_and_fit_inside_the_candidate_budget():
    with pytest.raises(TaskExecutionContractError, match="non-empty"):
        _calibration(evidence_sha256s=())
    with pytest.raises(TaskExecutionContractError, match="positive integer"):
        _calibration(observed_max_steps=True)
    with pytest.raises(TaskExecutionContractError, match="step limit"):
        _read_contract(calibration=_calibration(observed_max_steps=21))
    with pytest.raises(TaskExecutionContractError, match="wall-time limit"):
        _read_contract(calibration=_calibration(observed_max_wall_seconds=481))
    with pytest.raises(TaskExecutionContractError, match="provider-call limit"):
        _read_contract(calibration=_calibration(observed_max_provider_calls=21))


def test_owner_approved_exception_carries_evidence_but_no_invented_observations():
    exception = _calibration(
        status="owner-approved-exception",
        observed_max_steps=None,
        observed_max_wall_seconds=None,
        observed_max_provider_calls=None,
    )
    assert _read_contract(calibration=exception).calibration.status == "owner-approved-exception"
    with pytest.raises(TaskExecutionContractError, match="cannot invent"):
        replace(exception, observed_max_steps=1)


def _basis(**changes) -> BudgetBasisEvidence:
    values = {
        "status": "calibrated",
        "task_id": "task-read-chain",
        "budget_profile_id": _budget().profile_id,
        "budget_profile_sha256": _budget().sha256,
        "recorded_utc": "2026-09-01T12:00:00Z",
        "observed_max_steps": 12,
        "observed_max_wall_seconds": 240,
        "observed_max_provider_calls": 12,
        "attempt_result_sha256s": ("2" * 64,),
        "decision_reference": None,
        "approved_by_role": None,
        "rationale": "A bounded pilot established the observed maxima.",
    }
    values.update(changes)
    return BudgetBasisEvidence(**values)


def test_calibrated_budget_basis_round_trips_with_attempt_evidence():
    basis = _basis()
    assert BudgetBasisEvidence.from_dict(basis.to_dict()) == basis
    assert basis.schema_version == BUDGET_BASIS_SCHEMA_VERSION
    assert len(basis.sha256) == 64


def test_owner_exception_basis_needs_an_explicit_public_decision():
    basis = _basis(
        status="owner-approved-exception",
        observed_max_steps=None,
        observed_max_wall_seconds=None,
        observed_max_provider_calls=None,
        attempt_result_sha256s=(),
        decision_reference="initial-independent-task-budget-approval",
        approved_by_role="project-owner",
        rationale="Conservative limits were approved before paid calibration.",
    )
    assert BudgetBasisEvidence.from_dict(basis.to_dict()) == basis
    with pytest.raises(TaskExecutionContractError, match="decision reference"):
        replace(basis, decision_reference=None)
    with pytest.raises(TaskExecutionContractError, match="cannot claim calibration"):
        replace(basis, attempt_result_sha256s=("3" * 64,))


def test_budget_basis_refuses_malformed_or_contradictory_evidence():
    with pytest.raises(TaskExecutionContractError, match="canonical UTC"):
        _basis(recorded_utc="yesterday")
    with pytest.raises(TaskExecutionContractError, match="unique and sorted"):
        _basis(attempt_result_sha256s=("3" * 64, "2" * 64))
    with pytest.raises(TaskExecutionContractError, match="approval exception"):
        _basis(decision_reference="invented-approval")
    document = _basis().to_dict()
    document["extra"] = True
    with pytest.raises(TaskExecutionContractError, match="exactly"):
        BudgetBasisEvidence.from_dict(document)


def test_exact_key_parsers_refuse_missing_extra_and_wrong_container_shapes():
    document = _read_contract().to_dict()
    for mutation in (
        {key: value for key, value in document.items() if key != "budget"},
        {**document, "extra": True},
    ):
        with pytest.raises(TaskExecutionContractError, match="exactly"):
            TaskExecutionContract.from_dict(mutation)

    document = _read_contract().to_dict()
    document["required_dependencies"] = "dependency"
    with pytest.raises(TaskExecutionContractError, match="must be an array"):
        TaskExecutionContract.from_dict(document)


@pytest.mark.parametrize(
    "prefix",
    (
        "ckb://docs",
        "ckb://docs/../private/",
        "ckb://docs/%2e%2e/private/",
        "ckb://user:pass@docs/",
        "ckb://docs/?query=yes",
    ),
)
def test_treatment_resource_prefixes_are_canonical(prefix):
    with pytest.raises(TaskExecutionContractError, match="canonical"):
        TreatmentRequirement(
            requirement_id="docs-v1",
            claims_live_chain=False,
            required_tools=("search_resources",),
            required_resource_prefixes=(prefix,),
        )


def test_contract_resources_and_identifiers_are_unique_sorted_and_consistent():
    with pytest.raises(TaskExecutionContractError, match="unique and sorted"):
        _read_contract(required_resource_kinds=("workspace", "runtime-name"))
    with pytest.raises(TaskExecutionContractError, match="must be reserved"):
        _read_contract(expected_output_resource_kinds=("report",))
    with pytest.raises(TaskExecutionContractError, match="unique and sorted"):
        _read_contract(
            required_dependencies=(
                _dependency("z", "1" * 64),
                _dependency("a", "2" * 64),
            )
        )
    with pytest.raises(TaskExecutionContractError, match="unique and sorted"):
        _read_contract(
            treatment=replace(
                _treatment(),
                required_tools=("search_resources", "search_resources"),
            )
        )
