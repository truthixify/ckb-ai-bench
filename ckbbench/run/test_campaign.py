from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ckbbench.run.campaign import (
    CAMPAIGN_SCHEMA_VERSION,
    RETRY_POLICY_ID,
    RETRY_POLICY_SHA256,
    STOPPING_RULE_ID,
    STOPPING_RULE_SHA256,
    AcceptedReportResolution,
    AttemptArtifactReference,
    CampaignBatch,
    CampaignError,
    CampaignManifest,
    CampaignSlot,
    ExploratoryAttemptSummary,
    ExploratoryPreview,
    ResolvedCampaignSlot,
    execution_plan_sha256,
    freeze_campaign,
    load_campaign,
    load_exploratory_preview,
    load_report_resolution,
    publish_document,
    validate_intent_for_slot,
    validate_report_resolution,
)
from ckbbench.run.model_profile import model_variant_id
from ckbbench.run.task_attempt import (
    VERIFIER_PRIVATE_COMMITMENT_SCHEME,
    AttemptIdentity,
    ExecutionSource,
    TaskAttemptIntent,
    TaskBudget,
    canonical_json_bytes,
)

MODEL = "provider/synthetic-model"
PROFILE = "model-profile-synthetic-v1"
PROFILE_SHA = "1" * 64
VARIANT = model_variant_id(
    requested_model=MODEL,
    thinking_level="high",
    profile_id=PROFILE,
    profile_sha256=PROFILE_SHA,
)


def _source() -> ExecutionSource:
    return ExecutionSource(
        repository_revision="1" * 40,
        source_tree_sha256="2" * 64,
        agent_image_digest="sha256:" + "3" * 64,
        verifier_image_digest="sha256:" + "4" * 64,
        toolchain_sha256="5" * 64,
    )


def _budget(task: str) -> TaskBudget:
    return TaskBudget(
        profile_id=f"budget-{task}-v1",
        profile_sha256=("6" if task == "read-tip" else "7") * 64,
        step_limit=40,
        wall_time_limit_seconds=900,
        provider_call_limit=80,
        output_token_limit=None,
    )


def _slot(pair: int, arm: str, position: int) -> CampaignSlot:
    task = "read-tip" if pair == 1 else "build-code"
    treatment = "web-only-v1" if arm == "B" else "ckb-ai-v1"
    return CampaignSlot(
        slot_id=f"slot-{position}",
        batch_id="batch-a",
        trial_id=f"trial-{pair}",
        task_id=f"task-{task}",
        task_content_sha256=("8" if pair == 1 else "9") * 64,
        arm=arm,  # type: ignore[arg-type]
        treatment_profile_id=treatment,
        treatment_profile_sha256=("a" if arm == "B" else "b") * 64,
        chain_track="local-hermetic",
        chain_profile_id="local-hermetic-v1",
        chain_profile_sha256="c" * 64,
        requested_model=MODEL,
        thinking_level="high",
        model_variant_id=VARIANT,
        model_profile_id=PROFILE,
        model_profile_sha256=PROFILE_SHA,
        budget=_budget(task),
        max_score=10 if pair == 1 else 30,
        trial_challenge_id=f"challenge-{pair}",
        trial_challenge_sha256=("d" if pair == 1 else "e") * 64,
        run_params_derivation="task-run-params-v1",
        resource_equivalence_policy_id="local-equivalence-v1",
        resource_equivalence_policy_sha256="f" * 64,
    )


def _manifest() -> CampaignManifest:
    slots = (
        _slot(1, "B", 1),
        _slot(1, "C", 2),
        _slot(2, "C", 3),
        _slot(2, "B", 4),
    )
    batches = (CampaignBatch("batch-a", tuple(slot.slot_id for slot in slots)),)
    return CampaignManifest(
        campaign_id="campaign-" + "a" * 32,
        created_utc="2026-09-01T00:00:00Z",
        suite_semver="3.0.0",
        suite_freeze_sha256="0" * 64,
        execution_plan_id="execution-plan-v1",
        execution_plan_sha256=execution_plan_sha256(batches, slots),
        retry_policy_id=RETRY_POLICY_ID,
        retry_policy_sha256=RETRY_POLICY_SHA256,
        retry_limit=1,
        stopping_rule_id=STOPPING_RULE_ID,
        stopping_rule_sha256=STOPPING_RULE_SHA256,
        concurrency_contract="serialized-one-attempt-v1",
        execution_source=_source(),
        batches=batches,
        slots=slots,
    )


def _intent(manifest: CampaignManifest, slot: CampaignSlot) -> TaskAttemptIntent:
    identity = AttemptIdentity(
        campaign_id=manifest.campaign_id,
        campaign_manifest_sha256=manifest.sha256,
        batch_id=slot.batch_id,
        execution_plan_id=manifest.execution_plan_id,
        execution_plan_sha256=manifest.execution_plan_sha256,
        trial_id=slot.trial_id,
        suite_semver=manifest.suite_semver,
        suite_freeze_sha256=manifest.suite_freeze_sha256,
        task_id=slot.task_id,
        task_content_sha256=slot.task_content_sha256,
        arm=slot.arm,
        treatment_profile_id=slot.treatment_profile_id,
        treatment_profile_sha256=slot.treatment_profile_sha256,
        chain_track=slot.chain_track,
        chain_profile_id=slot.chain_profile_id,
        chain_profile_sha256=slot.chain_profile_sha256,
        requested_model=slot.requested_model,
        thinking_level=slot.thinking_level,
        model_variant_id=slot.model_variant_id,
        model_profile_id=slot.model_profile_id,
        model_profile_sha256=slot.model_profile_sha256,
        budget=slot.budget,
        trial_challenge_id=slot.trial_challenge_id,
        trial_challenge_sha256=slot.trial_challenge_sha256,
        run_params_derivation=slot.run_params_derivation,
        prompt_params_sha256="3" * 64,
        verifier_private_commitment_scheme=VERIFIER_PRIVATE_COMMITMENT_SCHEME,
        verifier_private_commitment_sha256="4" * 64,
        resource_equivalence_policy_id=slot.resource_equivalence_policy_id,
        resource_equivalence_policy_sha256=slot.resource_equivalence_policy_sha256,
        retry_policy_id=manifest.retry_policy_id,
        retry_policy_sha256=manifest.retry_policy_sha256,
        execution_source=manifest.execution_source,
    )
    return TaskAttemptIntent(
        attempt_id="attempt-" + "b" * 32,
        created_utc="2026-09-01T00:00:01Z",
        identity=identity,
    )


def test_campaign_round_trips_with_stable_schedule_and_digest():
    manifest = _manifest()
    loaded = CampaignManifest.from_dict(manifest.to_dict())
    assert loaded == manifest
    assert loaded.schema_version == CAMPAIGN_SCHEMA_VERSION
    assert [slot.slot_id for slot in loaded.ordered_slots] == [
        "slot-1",
        "slot-2",
        "slot-3",
        "slot-4",
    ]
    assert loaded.sha256 == manifest.sha256
    assert (
        RETRY_POLICY_SHA256
        == "04e149ec29671adf8bcf61e70b39f612bf18cc5043d2dc88ad7cbcc7919bb56c"
    )
    assert (
        STOPPING_RULE_SHA256
        == "768e9459edee96e2cdea5ba2f3fff9cfeb632cfe6ca5066f9efde10d57f6ac4e"
    )


def test_campaign_freeze_is_canonical_write_once_and_strict_on_read(tmp_path: Path):
    manifest = _manifest()
    draft = tmp_path / "draft.json"
    frozen = tmp_path / "campaign.json"
    draft.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

    assert freeze_campaign(draft, frozen) == manifest
    assert frozen.read_bytes() == canonical_json_bytes(manifest.to_dict())
    assert load_campaign(frozen) == manifest
    with pytest.raises(CampaignError, match="cannot be replaced"):
        freeze_campaign(draft, frozen)

    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    with pytest.raises(CampaignError, match="not canonical"):
        load_campaign(noncanonical)


def test_new_suite_manifest_cannot_use_the_legacy_full_document_freezer(tmp_path: Path):
    manifest = replace(_manifest(), suite_semver="4.0.0")
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    with pytest.raises(CampaignError, match="release-derived"):
        freeze_campaign(draft, tmp_path / "campaign.json")


@pytest.mark.parametrize(
    "mutation",
    (
        lambda manifest: replace(manifest, retry_limit=0),
        lambda manifest: replace(manifest, retry_limit=True),
        lambda manifest: replace(manifest, retry_policy_id="retry-anything-v1"),
        lambda manifest: replace(manifest, retry_policy_sha256="9" * 64),
        lambda manifest: replace(manifest, stopping_rule_id="score-aware-stop-v1"),
        lambda manifest: replace(manifest, stopping_rule_sha256="9" * 64),
        lambda manifest: replace(manifest, concurrency_contract="parallel-v1"),
        lambda manifest: replace(manifest, execution_plan_sha256="9" * 64),
        lambda manifest: replace(
            manifest,
            batches=(CampaignBatch("batch-a", ("slot-1", "slot-2", "slot-3")),),
        ),
        lambda manifest: replace(
            manifest,
            slots=(manifest.slots[0], manifest.slots[0], *manifest.slots[2:]),
        ),
    ),
)
def test_campaign_refuses_policy_schedule_and_identity_drift(mutation):
    with pytest.raises(CampaignError):
        mutation(_manifest())


def test_campaign_refuses_unmatched_nonadjacent_and_uncounterbalanced_pairs():
    manifest = _manifest()
    mismatched_slots = (
        manifest.slots[0],
        replace(manifest.slots[1], max_score=11),
        *manifest.slots[2:],
    )
    with pytest.raises(CampaignError, match="differ outside treatment"):
        replace(
            manifest,
            slots=mismatched_slots,
            execution_plan_sha256=execution_plan_sha256(manifest.batches, mismatched_slots),
        )

    same_treatment_digest = (
        manifest.slots[0],
        replace(
            manifest.slots[1],
            treatment_profile_sha256=manifest.slots[0].treatment_profile_sha256,
        ),
        *manifest.slots[2:],
    )
    with pytest.raises(CampaignError, match="distinct treatment"):
        replace(
            manifest,
            slots=same_treatment_digest,
            execution_plan_sha256=execution_plan_sha256(
                manifest.batches,
                same_treatment_digest,
            ),
        )

    nonadjacent = (
        manifest.slots[0],
        manifest.slots[2],
        manifest.slots[1],
        manifest.slots[3],
    )
    batch = (CampaignBatch("batch-a", tuple(slot.slot_id for slot in nonadjacent)),)
    with pytest.raises(CampaignError, match="adjacent"):
        replace(
            manifest,
            batches=batch,
            execution_plan_sha256=execution_plan_sha256(batch, manifest.slots),
        )

    wrong_order = (
        manifest.slots[1],
        manifest.slots[0],
        manifest.slots[2],
        manifest.slots[3],
    )
    batch = (CampaignBatch("batch-a", tuple(slot.slot_id for slot in wrong_order)),)
    with pytest.raises(CampaignError, match="counterbalanced"):
        replace(
            manifest,
            batches=batch,
            execution_plan_sha256=execution_plan_sha256(batch, manifest.slots),
        )


def test_campaign_slot_model_variant_and_attempt_binding_fail_closed():
    manifest = _manifest()
    slot = manifest.slots[0]
    intent = _intent(manifest, slot)
    validate_intent_for_slot(manifest, slot, intent)

    with pytest.raises(CampaignError, match="model variant"):
        replace(slot, model_variant_id="mv1-" + "0" * 64)
    with pytest.raises(CampaignError, match="campaign slot"):
        validate_intent_for_slot(
            manifest,
            slot,
            replace(intent, identity=replace(intent.identity, trial_id="trial-other")),
        )


def _attempt_reference(
    ordinal: int = 0,
    *,
    outcome: str | None = None,
) -> AttemptArtifactReference:
    return AttemptArtifactReference(
        attempt_id="attempt-" + ("a" if ordinal == 0 else "b") * 32,
        intent_sha256="1" * 64,
        preflight_requirements_sha256="2" * 64,
        journal_entry_sha256s=("3" * 64,),
        preflight_evidence_sha256="4" * 64,
        result_sha256="5" * 64,
        cleanup_receipt_sha256s=("6" * 64,),
        retry_ordinal=ordinal,
        outcome=outcome or ("pass" if ordinal == 0 else "infra_fail"),
    )


def test_accepted_resolution_and_exploratory_preview_are_distinct_schemas(tmp_path: Path):
    manifest = _manifest()
    original = _attempt_reference()
    resolved = ResolvedCampaignSlot("slot-1", original, None, original.attempt_id)
    resolution = AcceptedReportResolution(manifest.campaign_id, manifest.sha256, (resolved,))
    accepted_path = tmp_path / "accepted.json"
    publish_document(accepted_path, resolution.to_dict(), "accepted report resolution")
    assert load_report_resolution(accepted_path) == resolution
    with pytest.raises(CampaignError):
        load_exploratory_preview(accepted_path)

    summary = ExploratoryAttemptSummary(
        attempt_id=original.attempt_id,
        campaign_id=manifest.campaign_id,
        task_id="task-read-tip",
        arm="B",
        model_variant_id=VARIANT,
        retry_ordinal=0,
        state="complete",
        outcome="pass",
    )
    preview = ExploratoryPreview((summary,))
    preview_path = tmp_path / "preview.json"
    publish_document(preview_path, preview.to_dict(), "exploratory preview")
    assert load_exploratory_preview(preview_path) == preview
    with pytest.raises(CampaignError):
        load_report_resolution(preview_path)


def test_campaign_public_artifacts_reject_secret_shaped_values():
    manifest = _manifest()
    with pytest.raises(CampaignError, match="secret-shaped"):
        replace(manifest.slots[0], task_id="sk-secret-value").to_dict()

    with pytest.raises(CampaignError, match="secret-shaped"):
        publish_document(
            Path("unused-secret-artifact.json"),
            {"value": "sk-secret-value"},
            "public artifact",
        )


def test_report_resolution_must_bind_the_complete_manifest_and_valid_lineage():
    manifest = _manifest()
    references = tuple(
        ResolvedCampaignSlot(
            slot.slot_id,
            AttemptArtifactReference(
                attempt_id="attempt-" + f"{index:032x}",
                intent_sha256="1" * 64,
                preflight_requirements_sha256="2" * 64,
                journal_entry_sha256s=("3" * 64,),
                preflight_evidence_sha256="4" * 64,
                result_sha256="5" * 64,
                cleanup_receipt_sha256s=("6" * 64,),
                retry_ordinal=0,
                outcome="pass",
            ),
            None,
            "attempt-" + f"{index:032x}",
        )
        for index, slot in enumerate(manifest.ordered_slots, start=1)
    )
    resolution = AcceptedReportResolution(manifest.campaign_id, manifest.sha256, references)
    validate_report_resolution(manifest, resolution)

    with pytest.raises(CampaignError, match="bind"):
        validate_report_resolution(
            manifest,
            replace(resolution, campaign_manifest_sha256="9" * 64),
        )
    with pytest.raises(CampaignError, match="slot order"):
        validate_report_resolution(manifest, replace(resolution, slots=references[::-1]))

    original = _attempt_reference(outcome="infra_fail")
    terminal_infrastructure = ResolvedCampaignSlot(
        "slot-1", original, None, original.attempt_id
    )
    assert terminal_infrastructure.retry is None
    scored = _attempt_reference(outcome="pass")
    retry = _attempt_reference(1, outcome="pass")
    with pytest.raises(CampaignError, match="scored predecessor"):
        ResolvedCampaignSlot("slot-1", scored, retry, retry.attempt_id)
    retry = replace(retry, attempt_id=original.attempt_id)
    with pytest.raises(CampaignError, match="distinct attempt"):
        ResolvedCampaignSlot("slot-1", original, retry, retry.attempt_id)

    repeated_attempt = replace(
        references[1],
        original=replace(
            references[1].original,
            attempt_id=references[0].original.attempt_id,
        ),
        terminal_attempt_id=references[0].original.attempt_id,
    )
    with pytest.raises(CampaignError, match="reuse an attempt"):
        replace(resolution, slots=(references[0], repeated_attempt, *references[2:]))


def test_exploratory_preview_refuses_impossible_state_and_outcome():
    base = ExploratoryAttemptSummary(
        attempt_id="attempt-" + "a" * 32,
        campaign_id="campaign-" + "b" * 32,
        task_id="task-read-tip",
        arm="B",
        model_variant_id=VARIANT,
        retry_ordinal=0,
        state="complete",
        outcome="pass",
    )
    with pytest.raises(CampaignError, match="state"):
        replace(base, state="unknown")
    with pytest.raises(CampaignError, match="outcome"):
        replace(base, outcome="almost-pass")
    with pytest.raises(CampaignError, match="contradicts"):
        replace(base, state="active")
    assert replace(base, state="cleanup-pending").outcome == "pass"


def test_attempt_reference_requires_every_artifact_digest_once():
    reference = _attempt_reference()
    with pytest.raises(CampaignError, match="ordinal"):
        replace(reference, retry_ordinal=True)
    with pytest.raises(CampaignError, match="non-empty"):
        replace(reference, journal_entry_sha256s=())
    with pytest.raises(CampaignError, match="repeat"):
        replace(
            reference,
            cleanup_receipt_sha256s=(
                reference.cleanup_receipt_sha256s[0],
                reference.cleanup_receipt_sha256s[0],
            ),
        )


def test_canonical_loaders_use_one_opened_snapshot(tmp_path: Path, monkeypatch):
    path = tmp_path / "campaign.json"
    path.write_bytes(canonical_json_bytes(_manifest().to_dict()))

    def refuse_reopen(_path: Path) -> bytes:
        raise AssertionError("loader reopened a path after validating its descriptor")

    monkeypatch.setattr(Path, "read_bytes", refuse_reopen)
    assert load_campaign(path).campaign_id == _manifest().campaign_id
