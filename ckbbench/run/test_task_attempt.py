from __future__ import annotations

import json
import multiprocessing
from dataclasses import replace
from pathlib import Path

import pytest

from ckbbench.run.attempt_store import AttemptStore, AttemptStoreError
from ckbbench.run.model_profile import model_variant_id
from ckbbench.run.task_preflight import (
    QUALIFICATION_KIND,
    READINESS_OPERATION,
    CheckEvidence,
    TaskPreflightEvidence,
    TaskPreflightRequirements,
)
from ckbbench.run.result import RESULT_SCHEMA_VERSION as LEGACY_RESULT_SCHEMA_VERSION
from ckbbench.run.task_attempt import (
    CANONICAL_JSON_VERSION,
    CONCURRENCY_CONTRACT,
    VERIFIER_PRIVATE_COMMITMENT_SCHEME,
    AttemptIdentity,
    AttemptSchemaError,
    AttemptTimings,
    AttemptUsage,
    CleanupReceipt,
    ExecutionSource,
    OwnershipJournalEntry,
    PreflightBinding,
    ResourceDisposition,
    RetryReference,
    TaskAttemptIntent,
    TaskAttemptResult,
    TaskBudget,
    TaskGrade,
    artifact_sha256,
    allocate_attempt_id,
    allocate_receipt_id,
    canonical_json_bytes,
    validate_attempt_envelope,
    validate_journal,
    validate_receipt_chain,
    validate_result_binding,
    validate_retry_link,
    validate_retry_resource_freshness,
)

ATTEMPT_A = "attempt-" + "a" * 32
ATTEMPT_B = "attempt-" + "b" * 32
MODEL = "openai/synthetic-model"
PROFILE_ID = "model-profile-synthetic-v1"
PROFILE_SHA = "1" * 64


def _append_in_child(root: str, entry: OwnershipJournalEntry, queue: object) -> None:
    try:
        AttemptStore(root).append_journal(entry)
    except AttemptStoreError:
        queue.put("rejected")  # type: ignore[attr-defined]
    else:
        queue.put("written")  # type: ignore[attr-defined]


def _create_intent_in_child(root: str, intent: TaskAttemptIntent, queue: object) -> None:
    try:
        AttemptStore(root).create_intent(intent)
    except AttemptStoreError:
        queue.put("rejected")  # type: ignore[attr-defined]
    else:
        queue.put("written")  # type: ignore[attr-defined]


def _identity(
    *,
    prompt_sha: str = "2" * 64,
    private_sha: str = "3" * 64,
    arm: str = "B",
    thinking_level: str = "high",
) -> AttemptIdentity:
    variant = model_variant_id(
        requested_model=MODEL,
        thinking_level=thinking_level,
        profile_id=PROFILE_ID,
        profile_sha256=PROFILE_SHA,
    )
    return AttemptIdentity(
        campaign_id="campaign-synthetic-v1",
        campaign_manifest_sha256="4" * 64,
        batch_id="batch-synthetic-1",
        execution_plan_id="execution-plan-synthetic-v1",
        execution_plan_sha256="5" * 64,
        trial_id="trial-1",
        suite_semver="3.0.0",
        suite_freeze_sha256="6" * 64,
        task_id="task-01-read-tip",
        task_content_sha256="7" * 64,
        arm=arm,  # type: ignore[arg-type]
        treatment_profile_id="treatment-web-only-v1" if arm == "B" else "treatment-ckb-ai-v1",
        treatment_profile_sha256="8" * 64,
        chain_track="testnet",
        chain_profile_id="ckb-testnet-v1",
        chain_profile_sha256="9" * 64,
        requested_model=MODEL,
        thinking_level=thinking_level,
        model_variant_id=variant,
        model_profile_id=PROFILE_ID,
        model_profile_sha256=PROFILE_SHA,
        budget=TaskBudget(
            profile_id="task-budget-read-tip-v1",
            profile_sha256="a" * 64,
            step_limit=40,
            wall_time_limit_seconds=900,
            provider_call_limit=80,
            output_token_limit=None,
        ),
        trial_challenge_id="challenge-trial-1",
        trial_challenge_sha256="b" * 64,
        run_params_derivation="task-run-params-v1",
        prompt_params_sha256=prompt_sha,
        verifier_private_commitment_scheme=VERIFIER_PRIVATE_COMMITMENT_SCHEME,
        verifier_private_commitment_sha256=private_sha,
        resource_equivalence_policy_id="resource-equivalence-testnet-v1",
        resource_equivalence_policy_sha256="c" * 64,
        retry_policy_id="whole-task-infra-retry-v1",
        retry_policy_sha256="d" * 64,
        execution_source=ExecutionSource(
            repository_revision="e" * 40,
            source_tree_sha256="f" * 64,
            agent_image_digest="sha256:" + "1" * 64,
            verifier_image_digest="sha256:" + "2" * 64,
            toolchain_sha256="3" * 64,
        ),
    )


def _intent(
    *,
    attempt_id: str = ATTEMPT_A,
    identity: AttemptIdentity | None = None,
    created_utc: str = "2026-09-01T00:00:00Z",
    retry_ordinal: int = 0,
    retry: RetryReference | None = None,
) -> TaskAttemptIntent:
    return TaskAttemptIntent(
        attempt_id=attempt_id,
        created_utc=created_utc,
        identity=identity or _identity(),
        retry_ordinal=retry_ordinal,
        retry=retry,
    )


def _entry(
    intent: TaskAttemptIntent,
    *,
    sequence: int,
    created_utc: str,
    phase: str,
    action: str,
    previous: OwnershipJournalEntry | None,
    resource_kind: str = "workspace",
    resource_id: str = "ckbbench-workspace-a",
) -> OwnershipJournalEntry:
    return OwnershipJournalEntry(
        attempt_id=intent.attempt_id,
        intent_sha256=intent.sha256,
        sequence=sequence,
        created_utc=created_utc,
        phase=phase,
        action=action,
        resource_kind=resource_kind,
        resource_id=resource_id,
        details_sha256=None,
        previous_entry_sha256=None if previous is None else previous.sha256,
    )


def _journal(intent: TaskAttemptIntent) -> tuple[OwnershipJournalEntry, ...]:
    claim = _entry(
        intent,
        sequence=0,
        created_utc="2026-09-01T00:00:01Z",
        phase="reserve",
        action="claim",
        previous=None,
    )
    mutation = _entry(
        intent,
        sequence=1,
        created_utc="2026-09-01T00:00:02Z",
        phase="setup",
        action="mutation-intent",
        previous=claim,
    )
    acquired = _entry(
        intent,
        sequence=2,
        created_utc="2026-09-01T00:00:03Z",
        phase="setup",
        action="acquired",
        previous=mutation,
    )
    release_intent = _entry(
        intent,
        sequence=3,
        created_utc="2026-09-01T00:00:05Z",
        phase="teardown",
        action="release-intent",
        previous=acquired,
    )
    released = _entry(
        intent,
        sequence=4,
        created_utc="2026-09-01T00:00:06Z",
        phase="teardown",
        action="released",
        previous=release_intent,
    )
    return claim, mutation, acquired, release_intent, released


def _complete_usage() -> AttemptUsage:
    return AttemptUsage(
        token_usage_status="complete",
        cost_status="complete",
        provider_reported_cost_usd="0.0012",
        model_calls=1,
        provider_attempts=1,
        provider_responses=1,
        provider_retry_count=0,
        provider_retry_delay_seconds=0,
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        provider_failure_category=None,
        provider_response_model_counts=((MODEL, 1),),
    )


def _not_started_usage() -> AttemptUsage:
    return AttemptUsage(
        token_usage_status="not_started",
        cost_status="unavailable",
        provider_reported_cost_usd=None,
        model_calls=0,
        provider_attempts=0,
        provider_responses=0,
        provider_retry_count=0,
        provider_retry_delay_seconds=0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        provider_failure_category=None,
    )


def _result(
    intent: TaskAttemptIntent,
    journal: tuple[OwnershipJournalEntry, ...],
    *,
    infra_fail: bool = False,
) -> TaskAttemptResult:
    return TaskAttemptResult(
        attempt_id=intent.attempt_id,
        created_utc="2026-09-01T00:00:04Z",
        intent_sha256=intent.sha256,
        identity=intent.identity,
        pre_teardown_journal_sha256=journal[2 if not infra_fail else 0].sha256,
        preflight=PreflightBinding(
            evidence_id="preflight-evidence-a",
            evidence_sha256="4" * 64,
            status="failed" if infra_fail else "passed",
        ),
        outcome="infra_fail" if infra_fail else "pass",
        correctness_eligible=not infra_fail,
        grade=(
            TaskGrade(
                status="not_scored",
                verifier_score=0,
                score_awarded=0,
                max_score=10,
                reason="Preflight failed.",
                proof="",
            )
            if infra_fail
            else TaskGrade(
                status="passed",
                verifier_score=10,
                score_awarded=10,
                max_score=10,
                reason="Verifier passed.",
                proof="proof-sha256:" + "5" * 64,
            )
        ),
        usage=_not_started_usage() if infra_fail else _complete_usage(),
        timings=AttemptTimings(
            reservation_seconds=0.1,
            preflight_seconds=0.2,
            setup_seconds=0.3 if not infra_fail else 0.0,
            agent_seconds=1.0 if not infra_fail else 0.0,
            grading_seconds=0.4 if not infra_fail else 0.0,
        ),
        initial_resource_equivalence_sha256="6" * 64,
        agent_exit_status=None if infra_fail else "submitted",
        failure_stage="preflight" if infra_fail else None,
        failure_category="rpc-unavailable" if infra_fail else None,
    )


def _receipt(
    intent: TaskAttemptIntent,
    result: TaskAttemptResult,
    journal: tuple[OwnershipJournalEntry, ...],
) -> CleanupReceipt:
    return CleanupReceipt(
        receipt_id="receipt-" + "a" * 32,
        attempt_id=intent.attempt_id,
        created_utc="2026-09-01T00:00:07Z",
        sequence=0,
        kind="cleanup",
        status="complete",
        intent_sha256=intent.sha256,
        result_sha256=result.sha256,
        pre_teardown_journal_sha256=result.pre_teardown_journal_sha256,
        terminal_journal_sha256=journal[-1].sha256,
        prior_receipt_sha256=None,
        dispositions=(
            ResourceDisposition(
                resource_kind="workspace",
                resource_id="ckbbench-workspace-a",
                final_state="released",
                journal_entry_sha256=journal[-1].sha256,
            ),
        ),
    )


def _envelope(*, infra_fail: bool = False):
    intent = _intent()
    journal = _journal(intent)
    if infra_fail:
        release_intent = _entry(
            intent,
            sequence=1,
            created_utc="2026-09-01T00:00:05Z",
            phase="teardown",
            action="release-intent",
            previous=journal[0],
        )
        released = _entry(
            intent,
            sequence=2,
            created_utc="2026-09-01T00:00:06Z",
            phase="teardown",
            action="released",
            previous=release_intent,
        )
        journal = journal[0], release_intent, released
    result = _result(intent, journal, infra_fail=infra_fail)
    receipt = _receipt(intent, result, journal)
    return intent, journal, result, (receipt,)


def _persist(store: AttemptStore, *, infra_fail: bool = False):
    intent, requirements, journal, evidence, result, receipts = _store_envelope(
        infra_fail=infra_fail
    )
    store.create_intent(intent)
    store.write_preflight_requirements(intent.attempt_id, requirements)
    for entry in journal[:2]:
        store.append_journal(entry)
    store.write_preflight_evidence(intent.attempt_id, evidence)
    for entry in journal[2:6]:
        store.append_journal(entry)
    store.write_result(result)
    for entry in journal[6:]:
        store.append_journal(entry)
    store.append_receipt(receipts[0])
    return intent, journal, result, receipts


def _store_intent(
    *,
    attempt_id: str = ATTEMPT_A,
    identity: AttemptIdentity | None = None,
    created_utc: str = "2026-09-01T00:00:00Z",
    retry_ordinal: int = 0,
    retry: RetryReference | None = None,
) -> TaskAttemptIntent:
    local_identity = replace(
        _identity(),
        chain_track="local-hermetic",
        chain_profile_id="local-hermetic-v1",
    )
    return _intent(
        attempt_id=attempt_id,
        identity=identity or local_identity,
        created_utc=created_utc,
        retry_ordinal=retry_ordinal,
        retry=retry,
    )


def _store_requirements(intent: TaskAttemptIntent) -> TaskPreflightRequirements:
    claims = (
        ("runtime-name", f"runtime-{intent.attempt_id}"),
        ("workspace", f"workspace-{intent.attempt_id}"),
    )
    return TaskPreflightRequirements(
        requirements_id="store-fixture-v1",
        intent_sha256=intent.sha256,
        model_qualification_kind=QUALIFICATION_KIND,
        model_qualification_evidence_sha256="4" * 64,
        model_qualification_utc="2026-09-01T00:00:00Z",
        model_evidence_max_age_seconds=3600,
        provider_readiness_operation=READINESS_OPERATION,
        provider_readiness_request_limit=1,
        ckb_ai_surface_id="docs-only-v1",
        ckb_ai_surface_sha256="5" * 64,
        ckb_ai_server_version="1.7.0",
        ckb_ai_catalog_sha256="6" * 64,
        ckb_ai_request_limit=1,
        ckb_ai_claims_live_chain=False,
        expected_chain_id=None,
        expected_genesis_hash=None,
        signer_required=False,
        expected_signer_handle=None,
        expected_signer_address=None,
        signing_policy_id=None,
        signing_policy_sha256=None,
        funding=None,
        required_dependencies=(),
        required_resource_claims=claims,
        expected_output_resources=claims,
    )


def _store_journal(intent: TaskAttemptIntent) -> tuple[OwnershipJournalEntry, ...]:
    requirements = _store_requirements(intent)
    rows: list[OwnershipJournalEntry] = []
    actions = (
        *(("reserve", "claim", resource) for resource in requirements.required_resource_claims),
        *(("setup", "mutation-intent", resource) for resource in requirements.required_resource_claims),
        *(("setup", "acquired", resource) for resource in requirements.required_resource_claims),
        *(("teardown", "release-intent", resource) for resource in requirements.required_resource_claims),
        *(("teardown", "released", resource) for resource in requirements.required_resource_claims),
    )
    base_second = int(intent.created_utc[17:19])
    for sequence, (phase, action, resource) in enumerate(actions):
        previous = rows[-1] if rows else None
        rows.append(
            _entry(
                intent,
                sequence=sequence,
                created_utc=f"2026-09-01T00:00:{base_second + sequence + 1:02d}Z",
                phase=phase,
                action=action,
                previous=previous,
                resource_kind=resource[0],
                resource_id=resource[1],
            )
        )
    return tuple(rows)


def _store_evidence(
    intent: TaskAttemptIntent,
    requirements: TaskPreflightRequirements,
) -> TaskPreflightEvidence:
    checks = tuple(
        CheckEvidence(name, "passed", str(index + 1) * 64, request_count)
        for index, (name, request_count) in enumerate(
            (("source", 0), ("provider", 1), ("ckb_ai", 1), ("dependencies", 0), ("outputs", 0))
        )
    )
    return TaskPreflightEvidence(
        evidence_id="preflight-" + "a" * 32,
        attempt_id=intent.attempt_id,
        intent_sha256=intent.sha256,
        requirements_sha256=requirements.sha256,
        created_utc=f"2026-09-01T00:00:{int(intent.created_utc[17:19]) + 3:02d}Z",
        status="passed",
        failure_stage=None,
        failure_category=None,
        checks=checks,
        controller_request_count_status="exact",
        controller_request_count=2,
        direct_chain_identity_sha256=None,
        ckb_ai_chain_identity_sha256=None,
        signer_observation_sha256=None,
        funding_observation_sha256=None,
        required_capacity_shannons=None,
        spendable_capacity_shannons=None,
    )


def _store_envelope(*, infra_fail: bool = False):
    intent = _store_intent()
    requirements = _store_requirements(intent)
    journal = _store_journal(intent)
    evidence = _store_evidence(intent, requirements)
    result = replace(
        _result(intent, journal, infra_fail=infra_fail),
        created_utc="2026-09-01T00:00:07Z",
        pre_teardown_journal_sha256=journal[5].sha256,
        preflight=evidence.binding(),
    )
    latest = {
        (entry.resource_kind, entry.resource_id): entry
        for entry in journal
        if entry.action == "released"
    }
    receipt = CleanupReceipt(
        receipt_id="receipt-" + "a" * 32,
        attempt_id=intent.attempt_id,
        created_utc="2026-09-01T00:00:11Z",
        sequence=0,
        kind="cleanup",
        status="complete",
        intent_sha256=intent.sha256,
        result_sha256=result.sha256,
        pre_teardown_journal_sha256=result.pre_teardown_journal_sha256,
        terminal_journal_sha256=journal[-1].sha256,
        prior_receipt_sha256=None,
        dispositions=tuple(
            ResourceDisposition(kind, resource_id, "released", latest[(kind, resource_id)].sha256)
            for kind, resource_id in requirements.required_resource_claims
        ),
    )
    return intent, requirements, journal, evidence, result, (receipt,)


def _publish_store_prefix(
    store: AttemptStore,
    intent: TaskAttemptIntent,
    requirements: TaskPreflightRequirements,
    journal: tuple[OwnershipJournalEntry, ...],
    evidence: TaskPreflightEvidence,
) -> None:
    store.create_intent(intent)
    store.write_preflight_requirements(intent.attempt_id, requirements)
    for entry in journal[:2]:
        store.append_journal(entry)
    store.write_preflight_evidence(intent.attempt_id, evidence)
    for entry in journal[2:6]:
        store.append_journal(entry)


def _store_failed_cleanup(store: AttemptStore, *, append_receipt: bool = True):
    intent, requirements, planned, evidence, result, _receipts = _store_envelope()
    _publish_store_prefix(store, intent, requirements, planned, evidence)
    store.write_result(result)
    journal = planned[:6]
    runtime, workspace = requirements.required_resource_claims
    for resource, action in (
        (runtime, "release-intent"),
        (runtime, "cleanup-failed"),
        (workspace, "release-intent"),
        (workspace, "released"),
    ):
        row = _entry(
            intent,
            sequence=len(journal),
            created_utc=f"2026-09-01T00:00:{len(journal) + 1:02d}Z",
            phase="teardown",
            action=action,
            previous=journal[-1],
            resource_kind=resource[0],
            resource_id=resource[1],
        )
        if action == "cleanup-failed":
            row = replace(row, details_sha256="7" * 64)
        store.append_journal(row)
        journal = (*journal, row)
    latest = {
        (entry.resource_kind, entry.resource_id): entry
        for entry in journal
        if entry.action in {"cleanup-failed", "released"}
    }
    receipt = CleanupReceipt(
        receipt_id="receipt-" + "b" * 32,
        attempt_id=intent.attempt_id,
        created_utc="2026-09-01T00:00:11Z",
        sequence=0,
        kind="cleanup",
        status="incomplete",
        intent_sha256=intent.sha256,
        result_sha256=result.sha256,
        pre_teardown_journal_sha256=result.pre_teardown_journal_sha256,
        terminal_journal_sha256=journal[-1].sha256,
        prior_receipt_sha256=None,
        dispositions=(
            ResourceDisposition(*runtime, "failed", latest[runtime].sha256),
            ResourceDisposition(*workspace, "released", latest[workspace].sha256),
        ),
    )
    if append_receipt:
        store.append_receipt(receipt)
    return intent, requirements, journal, result, receipt


def test_complete_envelope_round_trips_with_stable_canonical_digests():
    intent, journal, result, receipts = _envelope()
    validate_attempt_envelope(intent, journal, result, receipts)

    assert TaskAttemptIntent.from_dict(intent.to_dict()) == intent
    assert OwnershipJournalEntry.from_dict(journal[0].to_dict()) == journal[0]
    assert TaskAttemptResult.from_dict(result.to_dict()) == result
    assert CleanupReceipt.from_dict(receipts[0].to_dict()) == receipts[0]
    assert canonical_json_bytes(intent.to_dict()).endswith(b"\n")
    assert artifact_sha256(intent.to_dict()) == intent.sha256
    assert CANONICAL_JSON_VERSION == "canonical-json-sha256-v1"
    assert intent.identity.execution_source.concurrency_contract == CONCURRENCY_CONTRACT


def test_scored_outcomes_refuse_contradictory_failure_metadata():
    intent, journal, result, _receipts = _envelope()
    with pytest.raises(AttemptSchemaError, match="failure metadata"):
        replace(result, failure_stage="grading", failure_category="verifier-failed")

    failed = replace(
        result,
        outcome="agent_fail",
        grade=TaskGrade("failed", 0, 0, 10, "Verifier failed.", ""),
        failure_stage="grading",
        failure_category="verifier-failed",
    )
    assert failed.outcome == "agent_fail"
    with pytest.raises(AttemptSchemaError, match="failure metadata"):
        replace(failed, failure_stage="agent")

    violated = replace(
        result,
        outcome="protocol_violation",
        grade=replace(result.grade, score_awarded=0),
        failure_stage="protocol",
        failure_category="treatment-violation",
    )
    assert violated.outcome == "protocol_violation"
    with pytest.raises(AttemptSchemaError, match="failure metadata"):
        replace(violated, failure_category="verifier-failed")


def test_unavailable_timings_are_explicit_and_cannot_enter_scored_results():
    unavailable = AttemptTimings(
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        measurement_status="unavailable",
    )
    assert AttemptTimings.from_dict(unavailable.to_dict()) == unavailable
    with pytest.raises(AttemptSchemaError, match="structural zero"):
        replace(unavailable, agent_seconds=1.0)

    _intent_row, _journal_rows, result, _receipts = _envelope()
    with pytest.raises(AttemptSchemaError, match="measured timings"):
        replace(result, timings=unavailable)


def test_allocated_artifact_ids_are_opaque_unique_and_filename_safe():
    attempts = {allocate_attempt_id() for _ in range(32)}
    receipts = {allocate_receipt_id() for _ in range(32)}
    assert len(attempts) == len(receipts) == 32
    assert all(len(value) == 40 and value.startswith("attempt-") for value in attempts)
    assert all(len(value) == 40 and value.startswith("receipt-") for value in receipts)


@pytest.mark.parametrize("thinking_level", ["provider-default", "unsupported"])
def test_non_numeric_thinking_states_are_first_class_identity(thinking_level: str):
    identity = _identity(thinking_level=thinking_level)
    assert AttemptIdentity.from_dict(identity.to_dict()) == identity
    assert identity.thinking_level == thinking_level


def test_legacy_result_schema_remains_unchanged():
    assert LEGACY_RESULT_SCHEMA_VERSION == "1.8.0"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("arm", "C"),
        ("trial_challenge_sha256", "0" * 64),
        ("prompt_params_sha256", "0" * 64),
        ("treatment_profile_sha256", "0" * 64),
    ],
)
def test_result_identity_drift_fails(field: str, value: object):
    intent, journal, result, _receipts = _envelope()
    changed = replace(result, identity=replace(result.identity, **{field: value}))
    with pytest.raises(AttemptSchemaError):
        validate_result_binding(intent, journal, changed)


def test_result_rejects_task_model_budget_source_and_intent_drift():
    intent, journal, result, _receipts = _envelope()
    changed_model = "openai/different-model"
    coherent_model_variant = model_variant_id(
        requested_model=changed_model,
        thinking_level=result.identity.thinking_level,
        profile_id=result.identity.model_profile_id,
        profile_sha256=result.identity.model_profile_sha256,
    )
    mutations = (
        replace(result.identity, task_id="task-02-different"),
        replace(
            result.identity,
            requested_model=changed_model,
            model_variant_id=coherent_model_variant,
        ),
        replace(
            result.identity,
            budget=replace(result.identity.budget, step_limit=41),
        ),
        replace(
            result.identity,
            execution_source=replace(
                result.identity.execution_source,
                source_tree_sha256="0" * 64,
            ),
        ),
    )
    for changed_identity in mutations:
        with pytest.raises(AttemptSchemaError, match="identity differs"):
            validate_result_binding(
                intent,
                journal,
                replace(result, identity=changed_identity),
            )
    with pytest.raises(AttemptSchemaError, match="does not bind"):
        validate_result_binding(
            intent,
            journal,
            replace(result, intent_sha256="0" * 64),
        )


def test_model_variant_identity_rejects_profile_or_variant_drift_at_construction():
    identity = _identity()
    with pytest.raises(AttemptSchemaError, match="model_variant_id"):
        replace(identity, model_variant_id="mv1-" + "0" * 64)
    with pytest.raises(AttemptSchemaError, match="model_variant_id"):
        replace(identity, model_profile_sha256="0" * 64)


def test_result_rejects_unknown_or_teardown_journal_prefix():
    intent, journal, result, _receipts = _envelope()
    with pytest.raises(AttemptSchemaError, match="unknown journal prefix"):
        validate_result_binding(
            intent,
            journal,
            replace(result, pre_teardown_journal_sha256="0" * 64),
        )
    with pytest.raises(AttemptSchemaError, match="teardown"):
        validate_result_binding(
            intent,
            journal,
            replace(result, pre_teardown_journal_sha256=journal[3].sha256),
        )
    with pytest.raises(AttemptSchemaError, match="every resource to be acquired"):
        validate_result_binding(
            intent,
            journal[:1],
            replace(result, pre_teardown_journal_sha256=journal[0].sha256),
        )


def test_journal_rejects_gap_fork_cross_attempt_and_backwards_chronology():
    intent, journal, _result_row, _receipts = _envelope()
    with pytest.raises(AttemptSchemaError, match="gapped"):
        validate_journal(intent, (journal[0], replace(journal[1], sequence=2)))
    with pytest.raises(AttemptSchemaError, match="gapped"):
        validate_journal(
            intent,
            (journal[0], replace(journal[1], previous_entry_sha256="0" * 64)),
        )
    with pytest.raises(AttemptSchemaError, match="bind"):
        validate_journal(intent, (replace(journal[0], attempt_id=ATTEMPT_B),))
    with pytest.raises(AttemptSchemaError, match="chronology"):
        validate_journal(
            intent,
            (journal[0], replace(journal[1], created_utc=intent.created_utc)),
        )


def test_journal_rejects_action_before_claim_and_action_after_final():
    intent, journal, _result_row, _receipts = _envelope()
    unclaimed = _entry(
        intent,
        sequence=1,
        created_utc="2026-09-01T00:00:02Z",
        phase="reserve",
        action="acquired",
        previous=journal[0],
        resource_id="ckbbench-unclaimed",
    )
    with pytest.raises(AttemptSchemaError, match="before its durable claim"):
        validate_journal(intent, (journal[0], unclaimed))
    extra = _entry(
        intent,
        sequence=5,
        created_utc="2026-09-01T00:00:07Z",
        phase="teardown",
        action="observed",
        previous=journal[-1],
    )
    with pytest.raises(AttemptSchemaError, match="after final disposition"):
        validate_journal(intent, (*journal, extra))


def test_journal_rejects_cleanup_actions_before_teardown_and_late_claims():
    intent = _intent()
    claim = _journal(intent)[0]
    with pytest.raises(AttemptSchemaError, match="cleanup action"):
        replace(claim, phase="setup", action="cleanup-failed")
    with pytest.raises(AttemptSchemaError, match="claim must precede"):
        replace(claim, phase="teardown")


def test_journal_must_begin_with_a_reserve_claim():
    intent = _intent()
    first = _entry(
        intent,
        sequence=0,
        created_utc="2026-09-01T00:00:01Z",
        phase="setup",
        action="claim",
        previous=None,
    )
    with pytest.raises(AttemptSchemaError, match="reserve claim"):
        validate_journal(intent, (first,))


def test_receipt_rejects_missing_resource_wrong_digest_and_wrong_result():
    intent, journal, result, receipts = _envelope()
    receipt = receipts[0]
    with pytest.raises(AttemptSchemaError, match="every owned resource"):
        validate_receipt_chain(intent, journal, result, (replace(receipt, dispositions=()),))
    wrong_disposition = replace(receipt.dispositions[0], journal_entry_sha256="0" * 64)
    with pytest.raises(AttemptSchemaError, match="contradicts"):
        validate_receipt_chain(
            intent,
            journal,
            result,
            (replace(receipt, dispositions=(wrong_disposition,)),),
        )
    with pytest.raises(AttemptSchemaError, match="does not bind its Task result"):
        validate_receipt_chain(
            intent,
            journal,
            result,
            (replace(receipt, result_sha256="0" * 64),),
        )


def test_incomplete_cleanup_can_be_reconciled_without_rewriting_history():
    intent, journal, result, _receipts = _envelope()
    failed = replace(
        journal[3],
        action="cleanup-failed",
        details_sha256="7" * 64,
    )
    first_entries = (*journal[:3], failed)
    first = CleanupReceipt(
        receipt_id="receipt-" + "b" * 32,
        attempt_id=intent.attempt_id,
        created_utc="2026-09-01T00:00:06Z",
        sequence=0,
        kind="cleanup",
        status="incomplete",
        intent_sha256=intent.sha256,
        result_sha256=result.sha256,
        pre_teardown_journal_sha256=result.pre_teardown_journal_sha256,
        terminal_journal_sha256=failed.sha256,
        prior_receipt_sha256=None,
        dispositions=(
            ResourceDisposition(
                resource_kind="workspace",
                resource_id="ckbbench-workspace-a",
                final_state="failed",
                journal_entry_sha256=failed.sha256,
            ),
        ),
    )
    release_intent = _entry(
        intent,
        sequence=4,
        created_utc="2026-09-01T00:00:07Z",
        phase="reconcile",
        action="release-intent",
        previous=failed,
    )
    released = _entry(
        intent,
        sequence=5,
        created_utc="2026-09-01T00:00:08Z",
        phase="reconcile",
        action="released",
        previous=release_intent,
    )
    entries = (*first_entries, release_intent, released)
    second = CleanupReceipt(
        receipt_id="receipt-" + "c" * 32,
        attempt_id=intent.attempt_id,
        created_utc="2026-09-01T00:00:09Z",
        sequence=1,
        kind="reconciliation",
        status="complete",
        intent_sha256=intent.sha256,
        result_sha256=result.sha256,
        pre_teardown_journal_sha256=result.pre_teardown_journal_sha256,
        terminal_journal_sha256=released.sha256,
        prior_receipt_sha256=first.sha256,
        dispositions=(
            ResourceDisposition(
                resource_kind="workspace",
                resource_id="ckbbench-workspace-a",
                final_state="released",
                journal_entry_sha256=released.sha256,
            ),
        ),
    )
    validate_receipt_chain(intent, entries, result, (first, second))
    with pytest.raises(AttemptSchemaError, match="receipt IDs"):
        validate_receipt_chain(
            intent,
            entries,
            result,
            (first, replace(second, receipt_id=first.receipt_id)),
        )
    with pytest.raises(AttemptSchemaError, match="cleanup is not complete"):
        validate_receipt_chain(intent, first_entries, result, (first,))
    repeated = replace(
        first,
        receipt_id="receipt-" + "d" * 32,
        sequence=1,
        kind="reconciliation",
        prior_receipt_sha256=first.sha256,
    )
    with pytest.raises(AttemptSchemaError, match="new journal evidence"):
        validate_receipt_chain(
            intent,
            first_entries,
            result,
            (first, repeated),
            require_complete=False,
        )


def test_cleanup_failure_cannot_be_buried_inside_a_complete_receipt():
    intent, journal, result, _receipts = _envelope()
    failed = replace(journal[3], action="cleanup-failed", details_sha256="7" * 64)
    release_intent = _entry(
        intent,
        sequence=4,
        created_utc="2026-09-01T00:00:06Z",
        phase="teardown",
        action="release-intent",
        previous=failed,
    )
    released = _entry(
        intent,
        sequence=5,
        created_utc="2026-09-01T00:00:07Z",
        phase="teardown",
        action="released",
        previous=release_intent,
    )
    receipt = replace(
        _receipt(intent, result, journal),
        created_utc="2026-09-01T00:00:08Z",
        terminal_journal_sha256=released.sha256,
        dispositions=(
            ResourceDisposition(
                resource_kind="workspace",
                resource_id="ckbbench-workspace-a",
                final_state="released",
                journal_entry_sha256=released.sha256,
            ),
        ),
    )
    with pytest.raises(AttemptSchemaError, match="sealed before retrying"):
        validate_receipt_chain(
            intent,
            (*journal[:3], failed, release_intent, released),
            result,
            (receipt,),
        )
    with pytest.raises(AttemptSchemaError, match="sealed before retrying"):
        validate_receipt_chain(
            intent,
            (*journal[:3], failed, release_intent),
            result,
            (),
            require_complete=False,
        )


def test_usage_requires_exact_response_model_and_failure_accounting():
    usage = _complete_usage()
    with pytest.raises(AttemptSchemaError, match="every provider response"):
        replace(usage, provider_response_model_counts=())
    with pytest.raises(AttemptSchemaError, match="backed by classified failures"):
        replace(
            usage,
            token_usage_status="incomplete",
            provider_responses=0,
            provider_retry_count=1,
            provider_response_model_counts=(),
        )
    with pytest.raises(AttemptSchemaError, match="multiple failures"):
        replace(
            usage,
            token_usage_status="incomplete",
            provider_responses=0,
            provider_retry_count=1,
            provider_failure_category="multiple",
            provider_failure_counts=(("timeout", 1),),
            provider_response_model_counts=(),
        )
    with pytest.raises(AttemptSchemaError, match="logical model calls"):
        replace(usage, model_calls=0)


def test_scored_result_requires_agent_and_provider_evidence():
    intent, journal, result, _receipts = _envelope()
    with pytest.raises(AttemptSchemaError, match="agent and provider"):
        replace(result, agent_exit_status=None)
    with pytest.raises(AttemptSchemaError, match="agent and provider"):
        replace(result, usage=_not_started_usage())


def test_public_documents_reject_secret_shaped_values():
    intent = _intent(identity=replace(_identity(), campaign_id="sk-secret-value"))
    with pytest.raises(AttemptSchemaError, match="secret-shaped"):
        intent.to_dict()
    response_key = replace(
        _complete_usage(),
        provider_response_model_counts=(("SK-secret-value", 1),),
    )
    _intent_row, _journal_rows, result, _receipts = _envelope()
    with pytest.raises(AttemptSchemaError, match="secret-shaped"):
        replace(result, usage=response_key).to_dict()


def test_public_text_and_cost_fields_are_bounded():
    with pytest.raises(AttemptSchemaError, match="valid Unicode"):
        replace(
            TaskGrade("passed", 1, 1, 1, "ok", "proof"),
            proof="\ud800",
        )
    with pytest.raises(AttemptSchemaError, match="canonical"):
        replace(_complete_usage(), provider_reported_cost_usd="1" * 19)


def test_strict_readers_reject_extra_fields_and_boolean_sequences():
    intent = _intent()
    document = intent.to_dict()
    document["extra"] = True
    with pytest.raises(AttemptSchemaError, match="exactly"):
        TaskAttemptIntent.from_dict(document)
    with pytest.raises(AttemptSchemaError, match="non-negative integer"):
        replace(intent, retry_ordinal=False)
    with pytest.raises(AttemptSchemaError, match="sequence limit"):
        replace(_journal(intent)[0], sequence=1_000_000)


def test_malformed_enums_and_mutable_nested_records_fail_as_schema_errors():
    intent, _journal_rows, result, receipts = _envelope()
    malformed_result = result.to_dict()
    malformed_result["outcome"] = []
    with pytest.raises(AttemptSchemaError, match="outcome"):
        TaskAttemptResult.from_dict(malformed_result)
    with pytest.raises(AttemptSchemaError, match="immutable key/count pairs"):
        replace(_complete_usage(), provider_failure_counts=[])
    with pytest.raises(AttemptSchemaError, match="immutable typed records"):
        replace(receipts[0], dispositions=[])
    with pytest.raises(AttemptSchemaError, match="typed budget"):
        replace(intent.identity, budget={})


def _retry_fixture():
    predecessor_intent, predecessor_journal, predecessor_result, predecessor_receipts = _envelope(
        infra_fail=True
    )
    retry_identity = replace(
        predecessor_intent.identity,
        prompt_params_sha256="7" * 64,
        verifier_private_commitment_sha256="8" * 64,
    )
    reference = RetryReference(
        predecessor_attempt_id=predecessor_intent.attempt_id,
        predecessor_intent_sha256=predecessor_intent.sha256,
        predecessor_result_sha256=predecessor_result.sha256,
        predecessor_cleanup_receipt_sha256=predecessor_receipts[-1].sha256,
    )
    retry_intent = _intent(
        attempt_id=ATTEMPT_B,
        identity=retry_identity,
        created_utc="2026-09-01T00:00:08Z",
        retry_ordinal=1,
        retry=reference,
    )
    return (
        retry_intent,
        predecessor_intent,
        predecessor_journal,
        predecessor_result,
        predecessor_receipts,
    )


def _store_retry_fixture():
    (
        predecessor_intent,
        _requirements,
        predecessor_journal,
        _evidence,
        predecessor_result,
        predecessor_receipts,
    ) = _store_envelope(infra_fail=True)
    retry_identity = replace(
        predecessor_intent.identity,
        prompt_params_sha256="7" * 64,
        verifier_private_commitment_sha256="8" * 64,
    )
    retry = _store_intent(
        attempt_id=ATTEMPT_B,
        identity=retry_identity,
        created_utc="2026-09-01T00:00:12Z",
        retry_ordinal=1,
        retry=RetryReference(
            predecessor_attempt_id=predecessor_intent.attempt_id,
            predecessor_intent_sha256=predecessor_intent.sha256,
            predecessor_result_sha256=predecessor_result.sha256,
            predecessor_cleanup_receipt_sha256=predecessor_receipts[-1].sha256,
        ),
    )
    return (
        retry,
        predecessor_intent,
        predecessor_journal,
        predecessor_result,
        predecessor_receipts,
    )


def test_first_infrastructure_retry_validates_against_complete_predecessor_envelope():
    args = _retry_fixture()
    validate_retry_link(*args)


def test_retry_resource_identities_must_be_fresh():
    retry, intent, journal, _result_row, _receipts = _retry_fixture()
    reused = replace(_journal(retry)[0], created_utc="2026-09-01T00:00:09Z")
    with pytest.raises(AttemptSchemaError, match="fresh resource"):
        validate_retry_resource_freshness(retry, (reused,), intent, journal)
    fresh = replace(reused, resource_id="ckbbench-workspace-retry")
    validate_retry_resource_freshness(retry, (fresh,), intent, journal)


def test_retry_rejects_scored_incomplete_cross_slot_and_stale_integrity_material():
    retry, intent, journal, result, receipts = _retry_fixture()
    scored_intent, scored_journal, scored, scored_receipts = _envelope()
    scored_retry = replace(
        retry,
        retry=RetryReference(
            predecessor_attempt_id=scored_intent.attempt_id,
            predecessor_intent_sha256=scored_intent.sha256,
            predecessor_result_sha256=scored.sha256,
            predecessor_cleanup_receipt_sha256=scored_receipts[-1].sha256,
        ),
    )
    with pytest.raises(AttemptSchemaError, match="infrastructure result"):
        validate_retry_link(
            scored_retry,
            scored_intent,
            scored_journal,
            scored,
            scored_receipts,
        )
    with pytest.raises(AttemptSchemaError, match="cleanup receipt"):
        validate_retry_link(retry, intent, journal, result, ())
    with pytest.raises(AttemptSchemaError, match="planned-slot identity"):
        validate_retry_link(
            replace(retry, identity=replace(retry.identity, arm="C")),
            intent,
            journal,
            result,
            receipts,
        )
    with pytest.raises(AttemptSchemaError, match="fresh prompt"):
        validate_retry_link(
            replace(
                retry,
                identity=replace(
                    retry.identity,
                    prompt_params_sha256=intent.identity.prompt_params_sha256,
                ),
            ),
            intent,
            journal,
            result,
            receipts,
        )
    with pytest.raises(AttemptSchemaError, match="fresh prompt"):
        validate_retry_link(
            replace(
                retry,
                identity=replace(
                    retry.identity,
                    verifier_private_commitment_sha256=(
                        intent.identity.verifier_private_commitment_sha256
                    ),
                ),
            ),
            intent,
            journal,
            result,
            receipts,
        )
    with pytest.raises(AttemptSchemaError, match="zero or one"):
        replace(retry, retry_ordinal=2)


def test_retry_rejects_invalid_predecessor_even_when_references_match():
    retry, intent, journal, result, receipts = _retry_fixture()
    broken = replace(journal[-1], previous_entry_sha256="0" * 64)
    with pytest.raises(AttemptSchemaError, match="gapped"):
        validate_retry_link(retry, intent, (*journal[:-1], broken), result, receipts)


def test_store_persists_and_loads_one_immutable_envelope(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    expected = _persist(store)
    loaded = store.load_envelope(ATTEMPT_A)
    assert loaded.intent == expected[0]
    assert loaded.journal == expected[1]
    assert loaded.result == expected[2]
    assert loaded.receipts == expected[3]
    assert loaded.preflight_requirements == _store_requirements(expected[0])
    assert (tmp_path / "attempts" / ATTEMPT_A / "intent.json").read_bytes() == canonical_json_bytes(
        expected[0].to_dict()
    )


def test_store_refuses_attempt_and_result_replacement(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent, requirements, journal, evidence, result, _receipts = _store_envelope()
    _publish_store_prefix(store, intent, requirements, journal, evidence)
    with pytest.raises(AttemptStoreError, match="already reserved"):
        store.create_intent(intent)
    store.write_result(result)
    with pytest.raises(AttemptStoreError, match="cannot be replaced"):
        store.write_result(result)


def test_store_refuses_teardown_before_result_publication(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent, requirements, journal, evidence, result, _receipts = _store_envelope()
    _publish_store_prefix(store, intent, requirements, journal, evidence)
    with pytest.raises(AttemptStoreError, match="before teardown"):
        store.append_journal(journal[6])
    store.write_result(result)


def test_store_rejects_post_result_setup_and_post_cleanup_journal_appends(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent, requirements, journal, evidence, result, _receipts = _store_envelope()
    _publish_store_prefix(store, intent, requirements, journal, evidence)
    store.write_result(result)
    setup = _entry(
        intent,
        sequence=6,
        created_utc="2026-09-01T00:00:07Z",
        phase="setup",
        action="observed",
        previous=journal[5],
        resource_kind=journal[5].resource_kind,
        resource_id=journal[5].resource_id,
    )
    with pytest.raises(AttemptStoreError, match="sealed result"):
        store.append_journal(setup)

    _persist(AttemptStore(tmp_path / "complete"))
    complete_store = AttemptStore(tmp_path / "complete")
    extra = _entry(
        intent,
        sequence=10,
        created_utc="2026-09-01T00:00:12Z",
        phase="reconcile",
        action="observed",
        previous=journal[-1],
        resource_kind=journal[-1].resource_kind,
        resource_id=journal[-1].resource_id,
    )
    with pytest.raises(AttemptStoreError, match="sealed result"):
        complete_store.append_journal(extra)


def test_store_refuses_symlinks_unexpected_files_and_noncanonical_bytes(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent = _store_intent()
    store.create_intent(intent)
    attempt_dir = tmp_path / "attempts" / ATTEMPT_A
    intent_path = attempt_dir / "intent.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(canonical_json_bytes(intent.to_dict()))
    intent_path.unlink()
    intent_path.symlink_to(outside)
    with pytest.raises(AttemptStoreError, match="non-symlink"):
        store.append_journal(_store_journal(intent)[0])

    second = AttemptStore(tmp_path / "unexpected")
    second.create_intent(intent)
    (tmp_path / "unexpected" / ATTEMPT_A / "noise").write_text("noise", encoding="ascii")
    with pytest.raises(AttemptStoreError, match="unexpected"):
        second.append_journal(_store_journal(intent)[0])

    third = AttemptStore(tmp_path / "noncanonical")
    third.create_intent(intent)
    path = tmp_path / "noncanonical" / ATTEMPT_A / "intent.json"
    path.write_text(json.dumps(intent.to_dict(), indent=2), encoding="ascii")
    with pytest.raises(AttemptStoreError, match="not canonical"):
        third.append_journal(_store_journal(intent)[0])


def test_store_rejects_missing_noncanonical_and_cross_bound_preflight_files(tmp_path: Path):
    missing = AttemptStore(tmp_path / "missing")
    _persist(missing)
    missing_path = tmp_path / "missing" / ATTEMPT_A / "preflight-requirements.json"
    missing_path.unlink()
    with pytest.raises(AttemptStoreError, match="missing its requirements"):
        missing.load_envelope(ATTEMPT_A)

    noncanonical = AttemptStore(tmp_path / "noncanonical-preflight")
    _persist(noncanonical)
    noncanonical_path = (
        tmp_path / "noncanonical-preflight" / ATTEMPT_A / "preflight-requirements.json"
    )
    document = json.loads(noncanonical_path.read_text(encoding="ascii"))
    noncanonical_path.write_text(json.dumps(document, indent=2), encoding="ascii")
    with pytest.raises(AttemptStoreError, match="not canonical"):
        noncanonical.load_envelope(ATTEMPT_A)

    cross_bound = AttemptStore(tmp_path / "cross-bound")
    intent, requirements, _journal_rows, _evidence, _result_row, _receipts = _store_envelope()
    _persist(cross_bound)
    cross_bound_path = (
        tmp_path / "cross-bound" / ATTEMPT_A / "preflight-requirements.json"
    )
    cross_bound_path.write_bytes(
        canonical_json_bytes(
            replace(requirements, requirements_id="store-fixture-v2").to_dict()
        )
    )
    with pytest.raises(AttemptStoreError, match="invalid"):
        cross_bound.load_envelope(intent.attempt_id)


def test_store_refuses_duplicate_json_keys_and_filename_digest_drift(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent = _store_intent()
    store.create_intent(intent)
    intent_path = tmp_path / "attempts" / ATTEMPT_A / "intent.json"
    payload = intent_path.read_text(encoding="ascii")
    intent_path.write_text(
        payload.replace('{"attempt_id":', '{"attempt_id":"x","attempt_id":'),
        encoding="ascii",
    )
    with pytest.raises(AttemptStoreError, match="duplicate JSON key"):
        store.append_journal(_store_journal(intent)[0])

    second = AttemptStore(tmp_path / "digest")
    second.create_intent(intent)
    second.write_preflight_requirements(intent.attempt_id, _store_requirements(intent))
    entry = _store_journal(intent)[0]
    path = second.append_journal(entry)
    path.rename(path.with_name("000000-" + "0" * 64 + ".json"))
    with pytest.raises(AttemptStoreError, match="filename does not bind"):
        second.append_journal(_store_journal(intent)[1])


def test_store_rejects_semantically_equivalent_non_schema_result_bytes(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    _persist(store)
    path = tmp_path / "attempts" / ATTEMPT_A / "result.json"
    payload = path.read_text(encoding="ascii")
    path.write_text(
        payload.replace('"agent_seconds":1.0', '"agent_seconds":1'),
        encoding="ascii",
    )
    with pytest.raises(AttemptStoreError, match="canonical schema representation"):
        store.load_envelope(ATTEMPT_A)


def test_store_refuses_to_publish_an_artifact_over_the_reader_limit(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent, requirements, journal, evidence, result, _receipts = _store_envelope()
    _publish_store_prefix(store, intent, requirements, journal, evidence)
    categories = tuple(
        (f"failure-{index:05d}-" + "x" * 180, 1)
        for index in range(6_000)
    )
    usage = replace(
        result.usage,
        token_usage_status="incomplete",
        model_calls=1,
        provider_attempts=len(categories),
        provider_responses=0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        provider_failure_category="multiple",
        provider_failure_counts=categories,
        provider_response_model_counts=(),
    )
    oversized = replace(result, usage=usage)
    assert len(canonical_json_bytes(oversized.to_dict())) > 1 << 20
    with pytest.raises(AttemptStoreError, match="size limit"):
        store.write_result(oversized)
    assert not (tmp_path / "attempts" / ATTEMPT_A / "result.json").exists()


def test_store_rejects_a_receipt_injected_before_result_publication(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent, requirements, journal, _evidence, _result_row, receipts = _store_envelope()
    store.create_intent(intent)
    store.write_preflight_requirements(intent.attempt_id, requirements)
    receipt = receipts[0]
    path = (
        tmp_path
        / "attempts"
        / ATTEMPT_A
        / "receipts"
        / f"000000-{receipt.sha256}.json"
    )
    path.write_bytes(canonical_json_bytes(receipt.to_dict()))
    with pytest.raises(AttemptStoreError, match="before the attempt result"):
        store.append_journal(journal[0])


def test_store_validates_retry_before_reserving_its_directory(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    predecessor = _persist(store, infra_fail=True)
    retry, _intent_row, _journal_rows, _result_row, _receipts = _store_retry_fixture()
    store.create_intent(retry)
    assert (tmp_path / "attempts" / ATTEMPT_B / "intent.json").is_file()

    bad_id = "attempt-" + "c" * 32
    invalid = replace(
        retry,
        attempt_id=bad_id,
        retry=replace(retry.retry, predecessor_result_sha256="0" * 64),  # type: ignore[arg-type]
    )
    with pytest.raises(AttemptStoreError, match="eligible predecessor"):
        store.create_intent(invalid)
    assert not (tmp_path / "attempts" / bad_id).exists()
    assert predecessor[2].outcome == "infra_fail"


def test_store_allows_only_one_retry_reservation_per_predecessor(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    _persist(store, infra_fail=True)
    retry, _intent_row, _journal_rows, _result_row, _receipts = _store_retry_fixture()
    store.create_intent(retry)
    sibling_id = "attempt-" + "c" * 32
    sibling = replace(
        retry,
        attempt_id=sibling_id,
        identity=replace(
            retry.identity,
            prompt_params_sha256="9" * 64,
            verifier_private_commitment_sha256="a" * 64,
        ),
    )
    with pytest.raises(AttemptStoreError, match="already has a reserved"):
        store.create_intent(sibling)
    assert not (tmp_path / "attempts" / sibling_id).exists()


def test_store_rejects_a_retry_claim_reusing_predecessor_resources(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    _persist(store, infra_fail=True)
    retry, _intent_row, _journal_rows, _result_row, _receipts = _store_retry_fixture()
    store.create_intent(retry)
    store.write_preflight_requirements(retry.attempt_id, _store_requirements(retry))
    predecessor_claim = _store_journal(_store_intent())[0]
    reused = replace(
        _store_journal(retry)[0],
        created_utc="2026-09-01T00:00:13Z",
        resource_kind=predecessor_claim.resource_kind,
        resource_id=predecessor_claim.resource_id,
    )
    with pytest.raises(AttemptStoreError, match="fresh resource"):
        store.append_journal(reused)
    store.append_journal(replace(reused, resource_id="ckbbench-workspace-retry"))


def test_store_revalidates_a_complete_retry_lineage_when_read(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    _persist(store, infra_fail=True)
    retry, _intent_row, _journal_rows, _result_row, _receipts = _store_retry_fixture()
    requirements = _store_requirements(retry)
    journal = _store_journal(retry)
    evidence = _store_evidence(retry, requirements)
    _publish_store_prefix(store, retry, requirements, journal, evidence)
    result = replace(
        _result(retry, journal),
        created_utc="2026-09-01T00:00:19Z",
        pre_teardown_journal_sha256=journal[5].sha256,
        preflight=evidence.binding(),
    )
    final_by_resource = {
        (entry.resource_kind, entry.resource_id): entry
        for entry in journal
        if entry.action == "released"
    }
    receipt = CleanupReceipt(
        receipt_id="receipt-" + "e" * 32,
        attempt_id=retry.attempt_id,
        created_utc="2026-09-01T00:00:23Z",
        sequence=0,
        kind="cleanup",
        status="complete",
        intent_sha256=retry.sha256,
        result_sha256=result.sha256,
        pre_teardown_journal_sha256=result.pre_teardown_journal_sha256,
        terminal_journal_sha256=journal[-1].sha256,
        prior_receipt_sha256=None,
        dispositions=tuple(
            ResourceDisposition(kind, resource_id, "released", final_by_resource[(kind, resource_id)].sha256)
            for kind, resource_id in requirements.required_resource_claims
        ),
    )
    store.write_result(result)
    for entry in journal[6:]:
        store.append_journal(entry)
    store.append_receipt(receipt)
    loaded = store.load_envelope(retry.attempt_id)
    assert loaded.intent == retry
    assert loaded.receipts == (receipt,)


def test_store_serializes_competing_retry_reservations(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    _persist(store, infra_fail=True)
    first, _intent_row, _journal_rows, _result_row, _receipts = _store_retry_fixture()
    second = replace(
        first,
        attempt_id="attempt-" + "c" * 32,
        identity=replace(
            first.identity,
            prompt_params_sha256="9" * 64,
            verifier_private_commitment_sha256="a" * 64,
        ),
    )
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_create_intent_in_child,
            args=(str(tmp_path / "attempts"), candidate, queue),
        )
        for candidate in (first, second)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=1) for _ in processes) == ["rejected", "written"]
    retry_intents = [
        path / "intent.json"
        for path in (tmp_path / "attempts").iterdir()
        if path.name != ATTEMPT_A
    ]
    assert len(retry_intents) == 1


def test_store_supports_multi_entry_reconciliation_without_rewriting_receipts(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent, requirements, journal, result, first = _store_failed_cleanup(store)
    runtime, workspace = requirements.required_resource_claims
    release_intent = _entry(
        intent,
        sequence=len(journal),
        created_utc="2026-09-01T00:00:12Z",
        phase="reconcile",
        action="release-intent",
        previous=journal[-1],
        resource_kind=runtime[0],
        resource_id=runtime[1],
    )
    store.append_journal(release_intent)
    assert store.load_envelope(ATTEMPT_A, require_complete=False).receipts == (first,)
    released = _entry(
        intent,
        sequence=len(journal) + 1,
        created_utc="2026-09-01T00:00:13Z",
        phase="reconcile",
        action="released",
        previous=release_intent,
        resource_kind=runtime[0],
        resource_id=runtime[1],
    )
    store.append_journal(released)
    workspace_terminal = next(
        entry
        for entry in reversed(journal)
        if (entry.resource_kind, entry.resource_id) == workspace and entry.action == "released"
    )
    second = CleanupReceipt(
        receipt_id="receipt-" + "c" * 32,
        attempt_id=intent.attempt_id,
        created_utc="2026-09-01T00:00:14Z",
        sequence=1,
        kind="reconciliation",
        status="complete",
        intent_sha256=intent.sha256,
        result_sha256=result.sha256,
        pre_teardown_journal_sha256=result.pre_teardown_journal_sha256,
        terminal_journal_sha256=released.sha256,
        prior_receipt_sha256=first.sha256,
        dispositions=(
            ResourceDisposition(*runtime, "released", released.sha256),
            ResourceDisposition(*workspace, "released", workspace_terminal.sha256),
        ),
    )
    store.append_receipt(second)
    assert store.load_envelope(ATTEMPT_A).receipts == (first, second)


def test_store_requires_a_receipt_before_retrying_failed_cleanup(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent, requirements, journal, _result, _receipt = _store_failed_cleanup(
        store, append_receipt=False
    )
    runtime = requirements.required_resource_claims[0]
    retry = _entry(
        intent,
        sequence=len(journal),
        created_utc="2026-09-01T00:00:11Z",
        phase="teardown",
        action="release-intent",
        previous=journal[-1],
        resource_kind=runtime[0],
        resource_id=runtime[1],
    )
    with pytest.raises(AttemptStoreError, match="sealed result"):
        store.append_journal(retry)


def test_store_requires_each_failed_reconciliation_to_be_receipted(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent, requirements, journal, _result, _first_receipt = _store_failed_cleanup(store)
    runtime = requirements.required_resource_claims[0]
    second_failure = replace(
        _entry(
            intent,
            sequence=len(journal),
            created_utc="2026-09-01T00:00:12Z",
            phase="reconcile",
            action="cleanup-failed",
            previous=journal[-1],
            resource_kind=runtime[0],
            resource_id=runtime[1],
        ),
        details_sha256="8" * 64,
    )
    store.append_journal(second_failure)
    retry = _entry(
        intent,
        sequence=len(journal) + 1,
        created_utc="2026-09-01T00:00:13Z",
        phase="reconcile",
        action="release-intent",
        previous=second_failure,
        resource_kind=runtime[0],
        resource_id=runtime[1],
    )
    with pytest.raises(AttemptStoreError, match="sealed result"):
        store.append_journal(retry)


def test_store_serializes_competing_process_appends(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    intent = _store_intent()
    requirements = _store_requirements(intent)
    journal = _store_journal(intent)
    store.create_intent(intent)
    store.write_preflight_requirements(intent.attempt_id, requirements)
    for claim in journal[:2]:
        store.append_journal(claim)
    store.write_preflight_evidence(intent.attempt_id, _store_evidence(intent, requirements))
    first = journal[2]
    second = replace(first, action="mutation-intent", details_sha256="9" * 64)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_append_in_child,
            args=(str(tmp_path / "attempts"), candidate, queue),
        )
        for candidate in (first, second)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=1) for _ in processes) == ["rejected", "written"]
    journal_files = list((tmp_path / "attempts" / ATTEMPT_A / "journal").iterdir())
    assert len(journal_files) == 3
