from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ckbbench.run.attempt_store import AttemptStore
from ckbbench.run.campaign import (
    QUALIFIED_CAMPAIGN_SCHEMA_VERSION,
    STOPPING_RULE_ID,
    STOPPING_RULE_SHA256,
    CampaignBatch,
    CampaignQualification,
    execution_plan_sha256,
    load_campaign,
    publish_document,
)
from ckbbench.run.campaign_operator import CampaignOperator, CampaignOperatorError, main
from ckbbench.run.chain_profile import ChainProfile
from ckbbench.run.model_profile import MODEL_PROFILE_DIR, ModelProfile, load_run_profile
from ckbbench.run.model_qualification import run_model_qualification
from ckbbench.run.provider_probe import CompletionEvidence
from ckbbench.run.suite_release import (
    CampaignReleaseBinding,
    CampaignDraft,
    CampaignTrial,
    SuiteReleaseError,
    build_campaign_from_release,
    freeze_campaign_from_release,
    load_campaign_draft,
    load_chain_profile,
    load_suite_release,
    load_treatment_profile,
    validate_campaign_release,
    validate_campaign_model_qualification,
)
from ckbbench.run.task_preflight import (
    QUALIFICATION_KIND,
    READINESS_OPERATION,
    TaskPreflightRequirements,
)
from ckbbench.run.task_attempt import canonical_json_bytes
from ckbbench.run.test_campaign import _intent
from ckbbench.run.treatment_surface import TreatmentSurfaceProfile
from ckbbench.suite.execution_contract import (
    TASK_EXECUTION_SCHEMA_VERSION,
    BudgetBasisEvidence,
    BudgetCalibration,
    DeploymentPin,
    HarnessDeadlines,
    TaskBudgetProfile,
    TaskExecutionContract,
    TreatmentRequirement,
)
from ckbbench.suite.freeze import freeze, write_freeze
from ckbbench.suite.registry import load_suite
from ckbbench.suite.test_registry import build_registry
from ckbbench.run.retry_policy import RETRY_POLICY_ID, RETRY_POLICY_SHA256


CHAIN = ChainProfile(
    profile_id="ckb-testnet-pudge-v1",
    chain_track="testnet",
    chain_id="ckb_testnet",
    genesis_hash="0x" + "1" * 64,
)


def _tools(*, suffix: str = "") -> list[dict]:
    names = (
        "dev_get_genesis_hash",
        "rpc_get_block_hash",
        "rpc_get_blockchain_info",
        "rpc_get_tip_block_number",
        "search_resources",
    )
    return [
        {
            "description": f"Public operation {name}{suffix}",
            "inputSchema": {"properties": {}, "type": "object"},
            "name": name,
        }
        for name in names
    ]


def _resources(*, suffix: str = "") -> list[dict]:
    return [{
        "mimeType": "text/markdown",
        "name": f"Reference{suffix}",
        "uri": "ckb://docs/reference/transaction-structure",
    }]


def _surface(
    arm: str,
    *,
    allowed_tools: tuple[str, ...] | None = None,
    suffix: str = "",
) -> TreatmentSurfaceProfile:
    tools = () if arm == "B" else ("search_resources",)
    prefixes = () if arm == "B" else ("ckb://docs/",)
    return TreatmentSurfaceProfile.from_catalogs(
        profile_id=f"surface-{arm.lower()}-testnet-v1",
        server_name="ckb-ai-mcp",
        server_version="1.6.13",
        claims_live_chain=True,
        allowed_tools=tools if allowed_tools is None else allowed_tools,
        allowed_resource_prefixes=prefixes,
        tools=_tools(suffix=suffix),
        resources=_resources(suffix=suffix),
    )


def _contract() -> TaskExecutionContract:
    budget = TaskBudgetProfile(
        profile_id="budget-read-tip-v1",
        step_limit=20,
        wall_time_limit_seconds=480,
        provider_call_limit=24,
        output_token_limit=None,
    )
    basis = _budget_basis(budget)
    return TaskExecutionContract(
        contract_id="execution-read-tip-v1",
        chain_track="testnet",
        chain_profile_id=CHAIN.profile_id,
        chain_profile_sha256=CHAIN.sha256,
        budget=budget,
        harness_deadlines=HarnessDeadlines(120, 120, 180, 120),
        treatment=TreatmentRequirement(
            requirement_id="treatment-read-tip-v1",
            claims_live_chain=True,
            required_tools=("search_resources",),
            required_resource_prefixes=("ckb://docs/",),
        ),
        signer_required=False,
        signing_policy_id=None,
        funding=None,
        required_dependencies=(
            DeploymentPin(
                dependency_id="secp256k1-blake160",
                transaction_hash="0x" + "2" * 64,
                output_index=0,
                expected_cell_sha256="2" * 64,
            ),
        ),
        required_resource_kinds=("runtime-name", "workspace"),
        expected_output_resource_kinds=("workspace",),
        run_params_derivation="task-run-params-v1",
        resource_equivalence_policy_id="read-only-testnet-equivalence-v1",
        calibration=BudgetCalibration(
            status="calibrated",
            evidence_sha256s=(basis.sha256,),
            observed_max_steps=12,
            observed_max_wall_seconds=240,
            observed_max_provider_calls=14,
        ),
    )


def _budget_basis(budget: TaskBudgetProfile | None = None) -> BudgetBasisEvidence:
    selected = budget or TaskBudgetProfile(
        profile_id="budget-read-tip-v1",
        step_limit=20,
        wall_time_limit_seconds=480,
        provider_call_limit=24,
        output_token_limit=None,
    )
    return BudgetBasisEvidence(
        status="calibrated",
        task_id="task-read-tip",
        budget_profile_id=selected.profile_id,
        budget_profile_sha256=selected.sha256,
        recorded_utc="2026-09-01T11:58:00Z",
        observed_max_steps=12,
        observed_max_wall_seconds=240,
        observed_max_provider_calls=14,
        attempt_result_sha256s=("3" * 64,),
        decision_reference=None,
        approved_by_role=None,
        rationale="A bounded pilot established the observed maxima.",
    )


def _release(tmp_path: Path, *, semver: str = "4.0.0"):
    contract = _contract()
    task = {
        "id": "task-read-tip",
        "proof_file": "proof.txt",
        "score": 10,
        "kind": "onchain",
        "check": "epoch_number",
        "rpc_method": "get_current_epoch",
        "fragment": "Read the current chain tip and write proof.txt.\n",
        "execution": contract.to_dict(),
        "budget_basis": _budget_basis(contract.budget).to_dict(),
    }
    root = build_registry(
        tmp_path / "suite",
        tasks=[task],
        manifest_overrides={
            "suite_semver": semver,
            "chain_profile": "task-scoped",
            "mcp_server_version": "1.6.13",
            "task_execution_schema_version": TASK_EXECUTION_SCHEMA_VERSION,
            "retry_policy_id": RETRY_POLICY_ID,
            "retry_policy_sha256": RETRY_POLICY_SHA256,
            "scoring_schema_version": "1",
            "toolchain_versions": {"python": "3.12.8", "rust": "1.95.0"},
        },
    )
    suite = load_suite(root)
    write_freeze(freeze(suite, root), root)
    return load_suite_release(root)


def _trial(
    control: TreatmentSurfaceProfile,
    treatment: TreatmentSurfaceProfile,
    *,
    profile: ModelProfile | None = None,
) -> CampaignTrial:
    requested_model = "provider/model" if profile is None else profile.requested_model
    thinking_level = "high" if profile is None else profile.thinking_level
    profile_id = "model-profile-synthetic-v1" if profile is None else profile.profile_id
    profile_sha256 = "4" * 64 if profile is None else profile.sha256
    return CampaignTrial(
        batch_id="batch-a",
        trial_id="trial-a",
        task_id="task-read-tip",
        control_slot_id="slot-b",
        treatment_slot_id="slot-c",
        requested_model=requested_model,
        thinking_level=thinking_level,
        model_profile_id=profile_id,
        model_profile_sha256=profile_sha256,
        trial_challenge_id="challenge-a",
        trial_challenge_sha256="5" * 64,
        control_profile_id=control.profile_id,
        control_profile_sha256=control.sha256,
        treatment_profile_id=treatment.profile_id,
        treatment_profile_sha256=treatment.sha256,
    )


def _manifest(tmp_path: Path):
    release = _release(tmp_path)
    control = _surface("B")
    treatment = _surface("C")
    manifest = build_campaign_from_release(
        release,
        campaign_id="campaign-" + "6" * 32,
        created_utc="2026-09-01T12:00:00Z",
        execution_plan_id="execution-plan-v1",
        repository_revision="7" * 40,
        source_tree_sha256="8" * 64,
        trials=(_trial(control, treatment),),
        chain_profiles=(CHAIN,),
        treatment_profiles=(control, treatment),
    )
    return release, control, treatment, manifest


def _draft(
    control: TreatmentSurfaceProfile,
    treatment: TreatmentSurfaceProfile,
    *,
    profile: ModelProfile | None = None,
) -> CampaignDraft:
    return CampaignDraft(
        campaign_id="campaign-" + "6" * 32,
        created_utc="2026-09-01T12:00:00Z",
        execution_plan_id="execution-plan-v1",
        repository_revision="7" * 40,
        source_tree_sha256="8" * 64,
        trials=(_trial(control, treatment, profile=profile),),
    )


class _QualificationSender:
    def __init__(self) -> None:
        self.requests_sent = 0


def _qualification(
    profile: ModelProfile,
    *,
    ordinal: int = 1,
    created_utc: str = "2026-09-01T11:58:00Z",
    completed_utc: str = "2026-09-01T11:59:00Z",
    returned_model: str | None = None,
):
    timestamps = iter((created_utc, completed_utc))

    def probe(*, profile, api_key, transport):
        assert api_key == "synthetic-key"
        transport.requests_sent += 1
        return CompletionEvidence(
            requests_sent=1,
            status_ok=True,
            status_class="2xx",
            requested_model=profile.requested_model,
            returned_model=returned_model or profile.probed_response_model,
            response_completed=True,
            exactly_one_expected_tool_call=True,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            token_identity_holds=True,
        )

    return run_model_qualification(
        profile,
        api_key="synthetic-key",
        transport_factory=_QualificationSender,
        probe=probe,
        clock=lambda: next(timestamps),
        qualification_id="qualification-" + f"{ordinal:032x}",
    )


def _write(path: Path, document: dict) -> Path:
    path.write_bytes(canonical_json_bytes(document))
    return path


def _replace_slots(manifest, **changes):
    slots = tuple(replace(slot, **changes) for slot in manifest.slots)
    batches = tuple(
        CampaignBatch(batch.batch_id, batch.slot_ids)
        for batch in manifest.batches
    )
    return replace(
        manifest,
        slots=slots,
        execution_plan_sha256=execution_plan_sha256(batches, slots),
    )


def _requirements(intent, surface: TreatmentSurfaceProfile) -> TaskPreflightRequirements:
    return TaskPreflightRequirements(
        requirements_id="requirements-a",
        intent_sha256=intent.sha256,
        model_qualification_kind=QUALIFICATION_KIND,
        model_qualification_evidence_sha256="9" * 64,
        model_qualification_utc="2026-09-01T11:59:00Z",
        model_evidence_max_age_seconds=3600,
        provider_readiness_operation=READINESS_OPERATION,
        provider_readiness_request_limit=1,
        ckb_ai_surface_id=surface.profile_id,
        ckb_ai_surface_sha256=surface.sha256,
        ckb_ai_server_version=surface.server_version,
        ckb_ai_catalog_sha256=surface.catalog_sha256,
        ckb_ai_request_limit=8,
        ckb_ai_claims_live_chain=True,
        expected_chain_id=CHAIN.chain_id,
        expected_genesis_hash=CHAIN.genesis_hash,
        signer_required=False,
        expected_signer_handle=None,
        expected_signer_address=None,
        signing_policy_id=None,
        signing_policy_sha256=None,
        funding=None,
        required_dependencies=_contract().dependency_evidence,
        required_resource_claims=(("runtime-name", "runtime-a"), ("workspace", "workspace-a")),
        expected_output_resources=(("workspace", "workspace-a"),),
    )


def test_release_loads_only_when_the_tracked_freeze_matches(tmp_path: Path):
    release = _release(tmp_path)
    assert release.suite.suite_semver == "4.0.0"
    assert release.task_content_sha256("task-read-tip")

    freeze_path = release.registry_root / "suite.freeze.json"
    freeze_path.write_text("{}\n", encoding="ascii")
    with pytest.raises(SuiteReleaseError, match="does not match"):
        load_suite_release(release.registry_root)


def test_release_refuses_semantically_equal_noncanonical_freeze_bytes(tmp_path: Path):
    release = _release(tmp_path)
    freeze_path = release.registry_root / "suite.freeze.json"
    freeze_path.write_text(json.dumps(release.freeze_document) + "\n", encoding="ascii")

    with pytest.raises(SuiteReleaseError, match="bytes are not canonical"):
        load_suite_release(release.registry_root)


def test_release_refuses_duplicate_freeze_keys(tmp_path: Path):
    release = _release(tmp_path)
    freeze_path = release.registry_root / "suite.freeze.json"
    payload = freeze_path.read_text(encoding="ascii")
    freeze_path.write_text(
        payload.replace("{\n", '{\n  "suite_semver": "4.0.0",\n', 1),
        encoding="ascii",
    )

    with pytest.raises(SuiteReleaseError, match="duplicate JSON key"):
        load_suite_release(release.registry_root)


@pytest.mark.parametrize(
    "target",
    ("manifest.json", "task-read-tip/meta.json", "task-read-tip/prompt.txt"),
)
def test_release_refuses_symlinked_release_content(tmp_path: Path, target: str):
    release = _release(tmp_path)
    path = release.registry_root / target
    replacement = tmp_path / f"replacement-{path.name}"
    replacement.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(replacement)

    with pytest.raises(SuiteReleaseError, match="symlink"):
        load_suite_release(release.registry_root)


def test_release_resolves_the_exact_budget_basis_instead_of_trusting_a_digest(tmp_path: Path):
    release = _release(tmp_path)
    basis = release.budget_basis_for("task-read-tip")
    assert basis.sha256 == _contract().calibration.evidence_sha256s[0]

    basis_path = release.registry_root / "task-read-tip" / "budget-basis.json"
    document = basis.to_dict()
    document["task_id"] = "task-other"
    basis_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="ascii")
    suite = load_suite(release.registry_root)
    write_freeze(freeze(suite, release.registry_root), release.registry_root)
    with pytest.raises(SuiteReleaseError, match="differs from its Task contract"):
        load_suite_release(release.registry_root)


def test_campaign_builder_derives_every_released_task_field(tmp_path: Path):
    release, control, treatment, manifest = _manifest(tmp_path)
    contract = _contract()
    assert [slot.arm for slot in manifest.ordered_slots] == ["B", "C"]
    for slot in manifest.slots:
        assert slot.task_content_sha256 == release.task_content_sha256(slot.task_id)
        assert slot.max_score == 10
        assert slot.budget.profile_sha256 == contract.budget.sha256
        assert slot.chain_profile_sha256 == CHAIN.sha256
        assert slot.resource_equivalence_policy_sha256 == (
            contract.resource_equivalence_policy_sha256
        )
    binding = validate_campaign_release(
        manifest,
        release,
        chain_profiles=(CHAIN,),
        treatment_profiles=(control, treatment),
    )
    assert binding.release == release
    assert binding.campaign_ceilings(manifest) == {
        "arm_count": 2,
        "maximum_agent_wall_seconds": 1920,
        "maximum_attempts": 4,
        "maximum_end_to_end_seconds": 4140,
        "maximum_grading_seconds": 720,
        "maximum_harness_seconds": 2160,
        "maximum_output_tokens": None,
        "maximum_preflight_seconds": 480,
        "maximum_provider_calls": 96,
        "maximum_retry_cooldown_seconds": 60,
        "maximum_setup_seconds": 480,
        "maximum_steps": 80,
        "maximum_teardown_seconds": 480,
        "planned_slots": 2,
        "schema_version": "ckbbench-campaign-ceilings-v1",
        "scope": "scheduled-campaign",
        "whole_task_attempts_per_slot": 2,
    }


def test_release_campaign_freeze_uses_only_the_compact_draft_and_exact_profiles(tmp_path: Path):
    release = _release(tmp_path)
    control = _surface("B")
    treatment = _surface("C")
    draft = _draft(control, treatment)
    draft_path = _write(tmp_path / "draft.json", draft.to_dict())
    chain_path = _write(tmp_path / "chain.json", CHAIN.to_dict())
    control_path = _write(tmp_path / "control.json", control.to_dict())
    treatment_path = _write(tmp_path / "treatment.json", treatment.to_dict())
    output = tmp_path / "campaign.json"

    manifest, binding = freeze_campaign_from_release(
        draft_path,
        output,
        suite_root=release.registry_root,
        chain_profile_paths=(chain_path,),
        treatment_profile_paths=(control_path, treatment_path),
    )

    assert load_campaign(output) == manifest
    assert binding.release.freeze_sha256 == release.freeze_sha256
    assert manifest.slots[0].budget.profile_sha256 == _contract().budget.sha256
    assert manifest.slots[0].max_score == 10
    assert manifest.stopping_rule_id == STOPPING_RULE_ID
    assert manifest.stopping_rule_sha256 == STOPPING_RULE_SHA256
    assert manifest.pauses_on_infrastructure_failure
    assert output.read_bytes() == canonical_json_bytes(manifest.to_dict())


def test_current_suite_freeze_binds_exact_qualifications_in_canonical_order(tmp_path: Path):
    release = _release(tmp_path, semver="5.0.0")
    control = _surface("B")
    treatment = _surface("C")
    luna = load_run_profile("gpt-5.6-luna")
    sol = load_run_profile("gpt-5.6-sol")
    luna_trial = _trial(control, treatment, profile=luna)
    sol_trial = replace(
        _trial(control, treatment, profile=sol),
        batch_id="batch-b",
        trial_id="trial-b",
        control_slot_id="slot-b-sol",
        treatment_slot_id="slot-c-sol",
        trial_challenge_id="challenge-b",
        trial_challenge_sha256="6" * 64,
    )
    draft = replace(_draft(control, treatment), trials=(luna_trial, sol_trial))
    draft_path = _write(tmp_path / "draft.json", draft.to_dict())
    chain_path = _write(tmp_path / "chain.json", CHAIN.to_dict())
    control_path = _write(tmp_path / "control.json", control.to_dict())
    treatment_path = _write(tmp_path / "treatment.json", treatment.to_dict())
    luna_qualification = _qualification(luna, ordinal=1)
    sol_qualification = _qualification(sol, ordinal=2)
    luna_path = tmp_path / "luna-qualification.json"
    sol_path = tmp_path / "sol-qualification.json"
    publish_document(luna_path, luna_qualification.to_dict(), "model qualification")
    publish_document(sol_path, sol_qualification.to_dict(), "model qualification")
    release_args = {
        "suite_root": release.registry_root,
        "chain_profile_paths": (chain_path,),
        "treatment_profile_paths": (control_path, treatment_path),
    }

    forward, _ = freeze_campaign_from_release(
        draft_path,
        tmp_path / "campaign-forward.json",
        model_profile_paths=(
            MODEL_PROFILE_DIR / "gpt-5.6-luna.json",
            MODEL_PROFILE_DIR / "gpt-5.6-sol.json",
        ),
        model_qualification_paths=(luna_path, sol_path),
        **release_args,
    )
    reversed_inputs, _ = freeze_campaign_from_release(
        draft_path,
        tmp_path / "campaign-reversed.json",
        model_profile_paths=(
            MODEL_PROFILE_DIR / "gpt-5.6-sol.json",
            MODEL_PROFILE_DIR / "gpt-5.6-luna.json",
        ),
        model_qualification_paths=(sol_path, luna_path),
        **release_args,
    )

    assert forward == reversed_inputs
    assert forward.schema_version == QUALIFIED_CAMPAIGN_SCHEMA_VERSION
    assert [row.model_profile_id for row in forward.model_qualifications] == sorted(
        (luna.profile_id, sol.profile_id)
    )
    assert forward.qualification_for_profile(luna.profile_id, luna.sha256) is not None
    assert forward.qualification_for_profile(sol.profile_id, sol.sha256) is not None
    first, second = forward.model_qualifications
    with pytest.raises(SuiteReleaseError):
        build_campaign_from_release(
            release,
            campaign_id=forward.campaign_id,
            created_utc=forward.created_utc,
            execution_plan_id=forward.execution_plan_id,
            repository_revision=forward.execution_source.repository_revision,
            source_tree_sha256=forward.execution_source.source_tree_sha256,
            trials=draft.trials,
            chain_profiles=(CHAIN,),
            treatment_profiles=(control, treatment),
            model_qualifications=(
                first,
                replace(second, qualification_id=first.qualification_id),
            ),
        )


def test_current_suite_freeze_refuses_missing_rejected_stale_and_mismatched_evidence(
    tmp_path: Path,
):
    release = _release(tmp_path, semver="5.0.0")
    control = _surface("B")
    treatment = _surface("C")
    luna = load_run_profile("gpt-5.6-luna")
    sol = load_run_profile("gpt-5.6-sol")
    draft = _draft(control, treatment, profile=luna)
    draft_path = _write(tmp_path / "draft.json", draft.to_dict())
    chain_path = _write(tmp_path / "chain.json", CHAIN.to_dict())
    control_path = _write(tmp_path / "control.json", control.to_dict())
    treatment_path = _write(tmp_path / "treatment.json", treatment.to_dict())
    common = {
        "suite_root": release.registry_root,
        "chain_profile_paths": (chain_path,),
        "treatment_profile_paths": (control_path, treatment_path),
        "model_profile_paths": (MODEL_PROFILE_DIR / "gpt-5.6-luna.json",),
    }

    with pytest.raises(SuiteReleaseError, match="needs model profiles and qualification"):
        freeze_campaign_from_release(
            draft_path,
            tmp_path / "missing.json",
            model_qualification_paths=(),
            **common,
        )

    records = {
        "rejected": _qualification(luna, returned_model="different-model"),
        "stale": _qualification(
            luna,
            created_utc="2026-07-01T11:58:00Z",
            completed_utc="2026-07-01T11:59:00Z",
        ),
        "mismatched": _qualification(sol),
    }
    for label, record in records.items():
        path = tmp_path / f"{label}.json"
        publish_document(path, record.to_dict(), "model qualification")
        with pytest.raises(SuiteReleaseError):
            freeze_campaign_from_release(
                draft_path,
                tmp_path / f"campaign-{label}.json",
                model_qualification_paths=(path,),
                **common,
            )

    valid_path = tmp_path / "noncanonical.json"
    valid_path.write_text(
        json.dumps(_qualification(luna).to_dict(), indent=2),
        encoding="utf-8",
    )
    with pytest.raises(SuiteReleaseError, match="inputs are invalid"):
        freeze_campaign_from_release(
            draft_path,
            tmp_path / "campaign-noncanonical.json",
            model_qualification_paths=(valid_path,),
            **common,
        )


def test_live_qualification_revalidation_requires_the_exact_campaign_artifact(tmp_path: Path):
    release = _release(tmp_path, semver="5.0.0")
    control = _surface("B")
    treatment = _surface("C")
    profile = load_run_profile("gpt-5.6-luna")
    record = _qualification(profile)
    binding = CampaignQualification(
        qualification_id=record.qualification_id,
        qualification_kind=record.qualification_kind,
        qualification_schema_version=record.schema_version,
        qualification_sha256=record.sha256,
        completed_utc=record.completed_utc,
        model_profile_id=record.profile_id,
        model_profile_sha256=record.profile_sha256,
        model_variant_id=record.model_variant_id,
    )
    manifest = build_campaign_from_release(
        release,
        campaign_id="campaign-" + "7" * 32,
        created_utc="2026-09-01T12:00:00Z",
        execution_plan_id="execution-plan-qualified-v1",
        repository_revision="7" * 40,
        source_tree_sha256="8" * 64,
        trials=(_trial(control, treatment, profile=profile),),
        chain_profiles=(CHAIN,),
        treatment_profiles=(control, treatment),
        model_qualifications=(binding,),
    )
    path = tmp_path / "qualification.json"
    publish_document(path, record.to_dict(), "model qualification")

    assert validate_campaign_model_qualification(
        manifest,
        profile,
        path,
        checked_utc="2026-09-01T12:00:00Z",
    ) == binding
    with pytest.raises(SuiteReleaseError, match="requires its model qualification"):
        validate_campaign_model_qualification(
            manifest,
            profile,
            None,
            checked_utc="2026-09-01T12:00:00Z",
        )
    altered = replace(record, qualification_id="qualification-" + "9" * 32)
    altered_path = tmp_path / "altered.json"
    publish_document(altered_path, altered.to_dict(), "model qualification")
    with pytest.raises(SuiteReleaseError, match="differs from the campaign"):
        validate_campaign_model_qualification(
            manifest,
            profile,
            altered_path,
            checked_utc="2026-09-01T12:00:00Z",
        )


def test_legacy_release_freeze_refuses_qualification_inputs(tmp_path: Path):
    release = _release(tmp_path)
    control = _surface("B")
    treatment = _surface("C")
    draft_path = _write(tmp_path / "draft.json", _draft(control, treatment).to_dict())
    chain_path = _write(tmp_path / "chain.json", CHAIN.to_dict())
    control_path = _write(tmp_path / "control.json", control.to_dict())
    treatment_path = _write(tmp_path / "treatment.json", treatment.to_dict())

    with pytest.raises(SuiteReleaseError, match="legacy campaign freezing"):
        freeze_campaign_from_release(
            draft_path,
            tmp_path / "campaign.json",
            suite_root=release.registry_root,
            chain_profile_paths=(chain_path,),
            treatment_profile_paths=(control_path, treatment_path),
            model_profile_paths=(MODEL_PROFILE_DIR / "gpt-5.6-luna.json",),
        )


def test_current_release_cli_requires_and_forwards_qualification_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    release = _release(tmp_path, semver="5.0.0")
    control = _surface("B")
    treatment = _surface("C")
    profile = load_run_profile("gpt-5.6-luna")
    draft_path = _write(
        tmp_path / "draft.json",
        _draft(control, treatment, profile=profile).to_dict(),
    )
    chain_path = _write(tmp_path / "chain.json", CHAIN.to_dict())
    control_path = _write(tmp_path / "control.json", control.to_dict())
    treatment_path = _write(tmp_path / "treatment.json", treatment.to_dict())
    qualification_path = tmp_path / "qualification.json"
    record = _qualification(profile)
    publish_document(qualification_path, record.to_dict(), "model qualification")
    common = [
        "--draft", str(draft_path),
        "--suite", str(release.registry_root),
        "--chain-profile", str(chain_path),
        "--treatment-profile", str(control_path),
        "--treatment-profile", str(treatment_path),
    ]
    missing_stderr = io.StringIO()
    assert main(
        ["freeze", *common, "--output", str(tmp_path / "missing.json")],
        stderr=missing_stderr,
    ) == 1
    assert "model profiles and qualification" in missing_stderr.getvalue()

    output = tmp_path / "campaign.json"
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert main(
        [
            "freeze",
            *common,
            "--output", str(output),
            "--model-profile", str(MODEL_PROFILE_DIR / "gpt-5.6-luna.json"),
            "--model-qualification", str(qualification_path),
        ],
        stdout=stdout,
        stderr=stderr,
    ) == 0
    manifest = load_campaign(output)
    assert manifest.schema_version == QUALIFIED_CAMPAIGN_SCHEMA_VERSION
    assert manifest.model_qualifications[0].qualification_sha256 == record.sha256
    assert stderr.getvalue() == ""

    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    live_stderr = io.StringIO()
    attempt_root = tmp_path / "attempts"
    assert main(
        [
            "run-task",
            "--manifest", str(output),
            "--attempt-root", str(attempt_root),
            "--slot", manifest.ordered_slots[0].slot_id,
            "--suite", str(release.registry_root),
            "--chain-profile", str(chain_path),
            "--treatment-profile", str(control_path),
            "--treatment-profile", str(treatment_path),
            "--model-profile", str(MODEL_PROFILE_DIR / "gpt-5.6-luna.json"),
            "--private-runtime-root", str(tmp_path / "private-runtime"),
            "--authorized-by-user",
        ],
        stderr=live_stderr,
    ) == 1
    assert "requires its model qualification" in live_stderr.getvalue()
    assert not attempt_root.exists()


def test_release_input_loaders_reject_unknown_duplicate_symlink_and_oversized_data(tmp_path: Path):
    control = _surface("B")
    draft = _draft(control, _surface("C"))
    unknown = {**draft.to_dict(), "invented_budget": 1}
    with pytest.raises(SuiteReleaseError, match="invalid"):
        load_campaign_draft(_write(tmp_path / "unknown.json", unknown))

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"profile_id":"first","profile_id":"second"}', encoding="ascii")
    with pytest.raises(SuiteReleaseError, match="duplicate JSON key"):
        load_chain_profile(duplicate)

    control_path = _write(tmp_path / "control.json", control.to_dict())
    symlink = tmp_path / "surface-link.json"
    symlink.symlink_to(control_path)
    with pytest.raises(SuiteReleaseError, match="non-symlink"):
        load_treatment_profile(symlink)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * ((1 << 20) + 1))
    with pytest.raises(SuiteReleaseError, match="byte limit"):
        load_campaign_draft(oversized)


def test_release_cli_freeze_and_plan_require_and_validate_all_release_inputs(tmp_path: Path):
    release = _release(tmp_path)
    control = _surface("B")
    treatment = _surface("C")
    draft_path = _write(tmp_path / "draft.json", _draft(control, treatment).to_dict())
    chain_path = _write(tmp_path / "chain.json", CHAIN.to_dict())
    control_path = _write(tmp_path / "control.json", control.to_dict())
    treatment_path = _write(tmp_path / "treatment.json", treatment.to_dict())
    output = tmp_path / "campaign.json"
    stdout = io.StringIO()
    stderr = io.StringIO()
    release_args = [
        "--suite", str(release.registry_root),
        "--chain-profile", str(chain_path),
        "--treatment-profile", str(control_path),
        "--treatment-profile", str(treatment_path),
    ]

    assert main(
        ["freeze", "--draft", str(draft_path), "--output", str(output), *release_args],
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert main(
        ["plan", "--manifest", str(output), *release_args],
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert "task-read-tip" in stdout.getvalue()
    assert "CEILINGS\t" in stdout.getvalue()
    assert '"scope":"scheduled-campaign"' in stdout.getvalue()
    assert stderr.getvalue() == ""

    missing = io.StringIO()
    assert main(["plan", "--manifest", str(output)], stderr=missing) == 1
    assert "requires its release inputs" in missing.getvalue()

    partial = io.StringIO()
    assert main(
        ["plan", "--manifest", str(output), "--suite", str(release.registry_root)],
        stderr=partial,
    ) == 1
    assert "needs suite, chain and treatment profiles" in partial.getvalue()


@pytest.mark.parametrize(
    "changes",
    (
        {"task_content_sha256": "a" * 64},
        {"max_score": 11},
        {"chain_profile_sha256": "b" * 64},
        {"run_params_derivation": "invented-run-params-v1"},
        {"resource_equivalence_policy_sha256": "c" * 64},
    ),
)
def test_release_binding_refuses_caller_invented_slot_fields(tmp_path: Path, changes):
    release, control, treatment, manifest = _manifest(tmp_path)
    mutated = _replace_slots(manifest, **changes)
    with pytest.raises(SuiteReleaseError):
        validate_campaign_release(
            mutated,
            release,
            chain_profiles=(CHAIN,),
            treatment_profiles=(control, treatment),
        )


def test_release_binding_requires_a_treatment_free_control_and_matched_catalogs(tmp_path: Path):
    release = _release(tmp_path)
    treatment = _surface("C")
    widened_control = _surface("B", allowed_tools=("search_resources",))
    with pytest.raises(SuiteReleaseError, match="treatment-free"):
        build_campaign_from_release(
            release,
            campaign_id="campaign-" + "a" * 32,
            created_utc="2026-09-01T12:00:00Z",
            execution_plan_id="execution-plan-v1",
            repository_revision="b" * 40,
            source_tree_sha256="c" * 64,
            trials=(_trial(widened_control, treatment),),
            chain_profiles=(CHAIN,),
            treatment_profiles=(widened_control, treatment),
        )

    control = _surface("B")
    changed_catalog = _surface("C", suffix=" changed")
    with pytest.raises(SuiteReleaseError, match="outside model-visible"):
        build_campaign_from_release(
            release,
            campaign_id="campaign-" + "d" * 32,
            created_utc="2026-09-01T12:00:00Z",
            execution_plan_id="execution-plan-v1",
            repository_revision="e" * 40,
            source_tree_sha256="f" * 64,
            trials=(_trial(control, changed_catalog),),
            chain_profiles=(CHAIN,),
            treatment_profiles=(control, changed_catalog),
        )


def test_preflight_requirements_are_derived_from_the_exact_released_contract(tmp_path: Path):
    release, control, treatment, manifest = _manifest(tmp_path)
    binding = CampaignReleaseBinding(release, (CHAIN,), (control, treatment))
    for slot, surface in zip(manifest.ordered_slots, (control, treatment), strict=True):
        intent = _intent(manifest, slot)
        requirements = _requirements(intent, surface)
        binding.validate_preflight(manifest, slot, intent, requirements)

        with pytest.raises(SuiteReleaseError, match="released task contract"):
            binding.validate_preflight(
                manifest,
                slot,
                intent,
                replace(requirements, required_dependencies=()),
            )


def test_release_profile_identifiers_cannot_have_competing_digests(tmp_path: Path):
    release, control, treatment, manifest = _manifest(tmp_path)
    competing = replace(CHAIN, genesis_hash="0x" + "a" * 64)
    with pytest.raises(SuiteReleaseError, match="unique"):
        validate_campaign_release(
            manifest,
            release,
            chain_profiles=(CHAIN, competing),
            treatment_profiles=(control, treatment),
        )


def test_new_suite_execution_requires_its_validated_release_binding(tmp_path: Path):
    release, control, treatment, manifest = _manifest(tmp_path)
    store = AttemptStore(tmp_path / "attempts")
    runtime = object()
    with pytest.raises(CampaignOperatorError, match="release binding"):
        CampaignOperator(manifest, store, runtime, tmp_path / "coordination")  # type: ignore[arg-type]

    binding = CampaignReleaseBinding(release, (CHAIN,), (control, treatment))
    operator = CampaignOperator(
        manifest,
        store,
        runtime,  # type: ignore[arg-type]
        tmp_path / "coordination",
        release_binding=binding,
    )
    assert operator.release_binding == binding
