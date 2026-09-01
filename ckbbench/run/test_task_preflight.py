from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from ckbbench.run.model_profile import model_variant_id
from ckbbench.run.task_preflight import (
    MAX_CKB_AI_PREFLIGHT_REQUESTS,
    MAX_MODEL_EVIDENCE_AGE_SECONDS,
    MAX_PROVIDER_READINESS_REQUESTS,
    QUALIFICATION_KIND,
    READINESS_OPERATION,
    ChainIdentityObservation,
    CheckEvidence,
    CkbAiObservation,
    DependencyObservation,
    FundingObservation,
    FundingRequirement,
    OutputObservation,
    TaskPreflightError,
    TaskPreflightEvidence,
    TaskPreflightRequirements,
    ProviderObservation,
    SignerObservation,
    SourceObservation,
    allocate_preflight_id,
    run_task_preflight,
    validate_task_preflight_evidence,
)
from ckbbench.run.task_attempt import (
    VERIFIER_PRIVATE_COMMITMENT_SCHEME,
    AttemptIdentity,
    ExecutionSource,
    OwnershipJournalEntry,
    TaskAttemptIntent,
    TaskBudget,
    artifact_sha256,
)

ATTEMPT = "attempt-" + "a" * 32
EVIDENCE = "preflight-" + "b" * 32
CHECKED_UTC = "2026-09-01T00:10:00Z"
QUALIFIED_UTC = "2026-09-01T00:00:00Z"
MODEL = "provider/synthetic-model"
PROFILE_ID = "model-profile-synthetic-v1"
PROFILE_SHA = "1" * 64
CHAIN_ID = "ckb_testnet"
GENESIS = "0x" + "2" * 64
TIP = "0x" + "3" * 64
SIGNER_HANDLE = "signer-attempt-a"
SIGNER_ADDRESS = "ckt1qsyntheticaddress"
LEASE = "inputs-attempt-a"


def _identity(*, arm: str = "B", chain_track: str = "testnet") -> AttemptIdentity:
    return AttemptIdentity(
        campaign_id="campaign-synthetic-v1",
        campaign_manifest_sha256="4" * 64,
        batch_id="batch-synthetic-1",
        execution_plan_id="execution-plan-synthetic-v1",
        execution_plan_sha256="5" * 64,
        trial_id="trial-1",
        suite_semver="3.0.0",
        suite_freeze_sha256="6" * 64,
        task_id="read-tip",
        task_content_sha256="7" * 64,
        arm=arm,  # type: ignore[arg-type]
        treatment_profile_id="web-only-v1" if arm == "B" else "ckb-ai-v1",
        treatment_profile_sha256="8" * 64,
        chain_track=chain_track,  # type: ignore[arg-type]
        chain_profile_id="ckb-testnet-v1" if chain_track != "local-hermetic" else "local-v1",
        chain_profile_sha256="9" * 64,
        requested_model=MODEL,
        thinking_level="high",
        model_variant_id=model_variant_id(
            requested_model=MODEL,
            thinking_level="high",
            profile_id=PROFILE_ID,
            profile_sha256=PROFILE_SHA,
        ),
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
        prompt_params_sha256="c" * 64,
        verifier_private_commitment_scheme=VERIFIER_PRIVATE_COMMITMENT_SCHEME,
        verifier_private_commitment_sha256="d" * 64,
        resource_equivalence_policy_id="resource-equivalence-testnet-v1",
        resource_equivalence_policy_sha256="e" * 64,
        retry_policy_id="whole-task-infra-retry-v1",
        retry_policy_sha256="f" * 64,
        execution_source=ExecutionSource(
            repository_revision="1" * 40,
            source_tree_sha256="2" * 64,
            agent_image_digest="sha256:" + "3" * 64,
            verifier_image_digest="sha256:" + "4" * 64,
            toolchain_sha256="5" * 64,
        ),
    )


def _intent(*, arm: str = "B", chain_track: str = "testnet") -> TaskAttemptIntent:
    return TaskAttemptIntent(
        attempt_id=ATTEMPT,
        created_utc="2026-09-01T00:00:00Z",
        identity=_identity(arm=arm, chain_track=chain_track),
    )


def _claims(*, local: bool) -> tuple[tuple[str, str], ...]:
    claims = [
        ("runtime-name", "runtime-attempt-a"),
        ("workspace", "workspace-attempt-a"),
    ]
    if not local:
        claims.extend((("signer", SIGNER_HANDLE), ("spendable-input", LEASE)))
    return tuple(sorted(claims))


def _journal(
    intent: TaskAttemptIntent,
    *,
    claims: tuple[tuple[str, str], ...],
) -> tuple[OwnershipJournalEntry, ...]:
    entries: list[OwnershipJournalEntry] = []
    previous: OwnershipJournalEntry | None = None
    for sequence, (kind, resource_id) in enumerate(claims):
        entry = OwnershipJournalEntry(
            attempt_id=intent.attempt_id,
            intent_sha256=intent.sha256,
            sequence=sequence,
            created_utc=f"2026-09-01T00:00:{sequence + 1:02d}Z",
            phase="reserve",
            action="claim",
            resource_kind=kind,
            resource_id=resource_id,
            details_sha256=None,
            previous_entry_sha256=None if previous is None else previous.sha256,
        )
        entries.append(entry)
        previous = entry
    return tuple(entries)


def _requirements(
    intent: TaskAttemptIntent,
    *,
    local: bool = False,
) -> TaskPreflightRequirements:
    claims = _claims(local=local)
    return TaskPreflightRequirements(
        requirements_id="preflight-requirements-read-tip-v1",
        intent_sha256=intent.sha256,
        model_qualification_kind=QUALIFICATION_KIND,
        model_qualification_evidence_sha256="6" * 64,
        model_qualification_utc=QUALIFIED_UTC,
        model_evidence_max_age_seconds=3600,
        provider_readiness_operation=READINESS_OPERATION,
        provider_readiness_request_limit=1,
        ckb_ai_surface_id="ckb-ai-testnet-read-v1" if not local else "ckb-ai-docs-v1",
        ckb_ai_surface_sha256="7" * 64,
        ckb_ai_server_version="1.7.0",
        ckb_ai_catalog_sha256="8" * 64,
        ckb_ai_request_limit=3,
        ckb_ai_claims_live_chain=not local,
        expected_chain_id=None if local else CHAIN_ID,
        expected_genesis_hash=None if local else GENESIS,
        signer_required=not local,
        expected_signer_handle=None if local else SIGNER_HANDLE,
        expected_signer_address=None if local else SIGNER_ADDRESS,
        signing_policy_id=None if local else "read-tip-signer-v1",
        signing_policy_sha256=None if local else "9" * 64,
        funding=None if local else FundingRequirement(1000, 100, 200, 2, 4),
        required_dependencies=(("secp256k1-deployment", "a" * 64),),
        required_resource_claims=claims,
        expected_output_resources=tuple(
            claim for claim in claims if claim[0] in {"runtime-name", "workspace"}
        ),
    )


def _read_only_requirements(intent: TaskAttemptIntent) -> TaskPreflightRequirements:
    signed = _requirements(intent)
    claims = tuple(
        claim
        for claim in signed.required_resource_claims
        if claim[0] not in {"signer", "spendable-input"}
    )
    return replace(
        signed,
        requirements_id="preflight-requirements-read-only-tip-v1",
        signer_required=False,
        expected_signer_handle=None,
        expected_signer_address=None,
        signing_policy_id=None,
        signing_policy_sha256=None,
        funding=None,
        required_resource_claims=claims,
    )


def _chain() -> ChainIdentityObservation:
    return ChainIdentityObservation(
        chain_id=CHAIN_ID,
        genesis_hash=GENESIS,
        tip_number=123,
        tip_hash=TIP,
        request_count=2,
    )


class FakeProbe:
    def __init__(
        self,
        intent: TaskAttemptIntent,
        requirements: TaskPreflightRequirements,
        *,
        local: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.source_value: Any = SourceObservation(
            execution_source=intent.identity.execution_source,
            staged_change_count=0,
            tracked_change_count=0,
            untracked_execution_input_count=0,
            untracked_execution_inputs_sha256=artifact_sha256({"execution_inputs": []}),
        )
        self.provider_value: Any = ProviderObservation(
            model_profile_id=PROFILE_ID,
            model_profile_sha256=PROFILE_SHA,
            qualification_kind=QUALIFICATION_KIND,
            qualification_evidence_sha256="6" * 64,
            qualification_utc=QUALIFIED_UTC,
            operation=READINESS_OPERATION,
            authenticated=True,
            credential_present=True,
            ready=True,
            request_count=1,
            generation_request_count=0,
            body_sent=False,
            redirect_followed=False,
        )
        chain = None if local else _chain()
        self.ckb_ai_value: Any = CkbAiObservation(
            surface_id=requirements.ckb_ai_surface_id,
            surface_sha256=requirements.ckb_ai_surface_sha256,
            server_version=requirements.ckb_ai_server_version,
            catalog_sha256=requirements.ckb_ai_catalog_sha256,
            ready=True,
            request_count=1 if local else 3,
            chain_identity=chain,
        )
        self.rpc_value: Any = _chain()
        self.signer_value: Any = SignerObservation(
            signer_handle=SIGNER_HANDLE,
            public_address=SIGNER_ADDRESS,
            signing_policy_id="read-tip-signer-v1",
            signing_policy_sha256="9" * 64,
            chain_identity_sha256=_chain().stable_identity_sha256,
            single_assignment=True,
            agent_accessible=False,
            check_count=3,
        )
        self.funding_value: Any = FundingObservation(
            signer_handle=SIGNER_HANDLE,
            lease_resource_id=LEASE,
            chain_identity_sha256=_chain().stable_identity_sha256,
            spendable_capacity_shannons=1400,
            cell_count=2,
            minimum_confirmations=4,
            cells_sha256="b" * 64,
            request_count=3,
        )
        self.dependencies_value: Any = DependencyObservation(
            dependencies=requirements.required_dependencies,
            chain_identity_sha256=None if local else _chain().stable_identity_sha256,
            request_count=1,
        )
        self.outputs_value: Any = OutputObservation(
            resources=requirements.expected_output_resources,
            fresh=True,
            symlink_count=0,
            foreign_owner_count=0,
            check_count=2,
        )

    def _get(self, name: str) -> Any:
        self.calls.append(name)
        value = getattr(self, f"{name}_value")
        if isinstance(value, Exception):
            raise value
        return value

    def source(self, *, timeout_seconds: float | None) -> SourceObservation:
        return self._get("source")

    def provider(self, *, timeout_seconds: float | None) -> ProviderObservation:
        return self._get("provider")

    def ckb_ai(self, *, timeout_seconds: float | None) -> CkbAiObservation:
        return self._get("ckb_ai")

    def rpc(self, *, timeout_seconds: float | None) -> ChainIdentityObservation:
        return self._get("rpc")

    def signer(self, *, timeout_seconds: float | None) -> SignerObservation:
        return self._get("signer")

    def funding(self, *, timeout_seconds: float | None) -> FundingObservation:
        return self._get("funding")

    def dependencies(self, *, timeout_seconds: float | None) -> DependencyObservation:
        return self._get("dependencies")

    def outputs(self, *, timeout_seconds: float | None) -> OutputObservation:
        return self._get("outputs")


def _fixture(
    *,
    local: bool = False,
    arm: str = "B",
) -> tuple[
    TaskAttemptIntent,
    tuple[OwnershipJournalEntry, ...],
    TaskPreflightRequirements,
    FakeProbe,
]:
    intent = _intent(arm=arm, chain_track="local-hermetic" if local else "testnet")
    requirements = _requirements(intent, local=local)
    journal = _journal(intent, claims=requirements.required_resource_claims)
    return intent, journal, requirements, FakeProbe(intent, requirements, local=local)


def _run(
    intent: TaskAttemptIntent,
    journal: tuple[OwnershipJournalEntry, ...],
    requirements: TaskPreflightRequirements,
    probe: FakeProbe,
    *,
    checked_utc: str = CHECKED_UTC,
) -> TaskPreflightEvidence:
    return run_task_preflight(
        intent,
        journal,
        requirements,
        probe,
        checked_utc=checked_utc,
        evidence_id=EVIDENCE,
    )


def test_a_complete_testnet_preflight_is_deterministic_and_round_trips():
    intent, journal, requirements, probe = _fixture()
    first = _run(intent, journal, requirements, probe)
    second_probe = FakeProbe(intent, requirements)
    second = _run(intent, journal, requirements, second_probe)

    assert first.status == "passed"
    assert first == second
    assert first.sha256 == second.sha256
    assert first.binding().evidence_sha256 == first.sha256
    assert first.controller_request_count_status == "exact"
    assert first.controller_request_count == 10
    assert first.required_capacity_shannons == 1300
    assert first.spendable_capacity_shannons == 1400
    assert [check.name for check in first.checks] == [
        "source", "provider", "ckb_ai", "rpc", "signer", "funding", "dependencies", "outputs",
    ]
    assert probe.calls == second_probe.calls == [
        "source", "provider", "ckb_ai", "rpc", "signer", "funding", "dependencies", "outputs",
    ]
    assert TaskPreflightRequirements.from_dict(requirements.to_dict()) == requirements
    assert TaskPreflightEvidence.from_dict(first.to_dict()) == first
    validate_task_preflight_evidence(intent, requirements, first)


def test_allocated_preflight_ids_are_opaque_and_unique():
    first = allocate_preflight_id()
    second = allocate_preflight_id()
    assert first != second
    assert len(first) == len("preflight-") + 32
    int(first.removeprefix("preflight-"), 16)


def test_a_local_preflight_never_calls_chain_signer_or_funding_adapters():
    intent, journal, requirements, probe = _fixture(local=True)
    probe.rpc_value = AssertionError("RPC must not run")
    probe.signer_value = AssertionError("signer must not run")
    probe.funding_value = AssertionError("funding must not run")

    evidence = _run(intent, journal, requirements, probe)

    assert evidence.status == "passed"
    assert probe.calls == ["source", "provider", "ckb_ai", "dependencies", "outputs"]
    assert evidence.direct_chain_identity_sha256 is None
    assert evidence.ckb_ai_chain_identity_sha256 is None
    assert evidence.signer_observation_sha256 is None
    assert evidence.funding_observation_sha256 is None
    assert evidence.required_capacity_shannons is None
    assert evidence.spendable_capacity_shannons is None


def test_a_read_only_testnet_preflight_checks_chain_without_signer_or_funding():
    intent = _intent(chain_track="testnet")
    requirements = _read_only_requirements(intent)
    journal = _journal(intent, claims=requirements.required_resource_claims)
    probe = FakeProbe(intent, requirements)
    probe.signer_value = AssertionError("read-only task must not inspect a signer")
    probe.funding_value = AssertionError("read-only task must not inspect funding")

    evidence = _run(intent, journal, requirements, probe)

    assert evidence.status == "passed"
    assert probe.calls == [
        "source", "provider", "ckb_ai", "rpc", "dependencies", "outputs",
    ]
    assert evidence.direct_chain_identity_sha256 is not None
    assert evidence.ckb_ai_chain_identity_sha256 is not None
    assert evidence.signer_observation_sha256 is None
    assert evidence.funding_observation_sha256 is None
    assert evidence.required_capacity_shannons is None
    assert evidence.spendable_capacity_shannons is None
    assert TaskPreflightEvidence.from_dict(evidence.to_dict()) == evidence


def test_read_only_testnet_requirements_refuse_partial_signer_material():
    intent = _intent(chain_track="testnet")
    requirements = _read_only_requirements(intent)

    with pytest.raises(TaskPreflightError, match="unsigned requirements"):
        replace(requirements, expected_signer_handle=SIGNER_HANDLE)
    with pytest.raises(TaskPreflightError, match="expected chain"):
        replace(
            requirements,
            expected_chain_id=None,
            expected_genesis_hash=None,
            signer_required=True,
            expected_signer_handle=SIGNER_HANDLE,
            expected_signer_address=SIGNER_ADDRESS,
            signing_policy_id="read-tip-signer-v1",
            signing_policy_sha256="9" * 64,
            funding=FundingRequirement(1000, 100, 200, 2, 4),
        )


def test_b_and_c_execute_the_same_readiness_sequence():
    sequences = []
    for arm in ("B", "C"):
        intent, journal, requirements, probe = _fixture(arm=arm)
        assert _run(intent, journal, requirements, probe).status == "passed"
        sequences.append(probe.calls)
    assert sequences[0] == sequences[1]


@pytest.mark.parametrize(
    ("field", "value", "category"),
    [
        ("staged_change_count", 1, "source-drift"),
        ("tracked_change_count", 1, "source-drift"),
        ("untracked_execution_input_count", 1, "source-drift"),
        ("untracked_execution_inputs_sha256", "0" * 64, "source-drift"),
    ],
)
def test_source_drift_stops_before_provider(field: str, value: Any, category: str):
    intent, journal, requirements, probe = _fixture()
    probe.source_value = replace(probe.source_value, **{field: value})

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.status, evidence.failure_stage, evidence.failure_category) == (
        "failed", "source", category,
    )
    assert probe.calls == ["source"]


@pytest.mark.parametrize(
    ("field", "value", "category"),
    [
        ("model_profile_id", "another-profile", "provider-unready"),
        ("model_profile_sha256", "0" * 64, "provider-unready"),
        ("qualification_kind", "catalog-only-v1", "provider-unready"),
        ("qualification_evidence_sha256", "0" * 64, "provider-unready"),
        ("qualification_utc", "2026-08-01T00:00:00Z", "provider-unready"),
        ("operation", "generation-v1", "provider-unready"),
        ("authenticated", False, "provider-unready"),
        ("credential_present", False, "provider-unready"),
        ("ready", False, "provider-unready"),
        ("request_count", 0, "provider-unready"),
        ("request_count", 2, "provider-unready"),
        ("generation_request_count", 1, "provider-unready"),
        ("body_sent", True, "provider-unready"),
        ("redirect_followed", True, "provider-unready"),
    ],
)
def test_provider_drift_is_fail_closed(field: str, value: Any, category: str):
    intent, journal, requirements, probe = _fixture()
    probe.provider_value = replace(probe.provider_value, **{field: value})

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == ("provider", category)
    assert probe.calls == ["source", "provider"]


@pytest.mark.parametrize("qualification_utc", [
    "2026-08-01T00:00:00Z",
    "2026-09-01T00:10:01Z",
])
def test_matching_but_expired_or_future_qualification_is_stale(qualification_utc: str):
    intent, journal, requirements, probe = _fixture()
    requirements = replace(requirements, model_qualification_utc=qualification_utc)
    probe.provider_value = replace(probe.provider_value, qualification_utc=qualification_utc)

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == (
        "provider", "stale-model-evidence",
    )


@pytest.mark.parametrize(
    ("field", "value", "category"),
    [
        ("surface_id", "wrong-surface", "ckb-ai-unready"),
        ("surface_sha256", "0" * 64, "ckb-ai-unready"),
        ("server_version", "1.8.0", "ckb-ai-unready"),
        ("catalog_sha256", "0" * 64, "ckb-ai-unready"),
        ("ready", False, "ckb-ai-unready"),
        ("request_count", 4, "ckb-ai-unready"),
        ("chain_identity", None, "network-mismatch"),
    ],
)
def test_ckb_ai_drift_is_fail_closed(field: str, value: Any, category: str):
    intent, journal, requirements, probe = _fixture()
    probe.ckb_ai_value = replace(probe.ckb_ai_value, **{field: value})

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == ("ckb_ai", category)
    assert probe.calls == ["source", "provider", "ckb_ai"]


def test_ckb_ai_chain_identity_must_match_the_direct_network_contract():
    intent, journal, requirements, probe = _fixture()
    wrong_chain = replace(_chain(), genesis_hash="0x" + "0" * 64)
    probe.ckb_ai_value = replace(probe.ckb_ai_value, chain_identity=wrong_chain)

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == (
        "ckb_ai", "network-mismatch",
    )
    assert "rpc" not in probe.calls


def test_a_chain_neutral_surface_cannot_publish_a_live_chain_claim():
    intent, journal, requirements, probe = _fixture(local=True)
    probe.ckb_ai_value = replace(probe.ckb_ai_value, chain_identity=_chain(), request_count=3)

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == ("ckb_ai", "network-mismatch")
    assert probe.calls == ["source", "provider", "ckb_ai"]


@pytest.mark.parametrize(
    ("field", "value", "category"),
    [
        ("chain_id", "ckb_mainnet", "network-mismatch"),
        ("genesis_hash", "0x" + "0" * 64, "network-mismatch"),
        ("request_count", 5, "rpc-unready"),
    ],
)
def test_rpc_drift_is_fail_closed(field: str, value: Any, category: str):
    intent, journal, requirements, probe = _fixture()
    probe.rpc_value = replace(probe.rpc_value, **{field: value})

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == ("rpc", category)
    assert probe.calls[-1] == "rpc"
    assert "signer" not in probe.calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signer_handle", "another-signer"),
        ("public_address", "ckt1qanother"),
        ("signing_policy_id", "wrong-policy"),
        ("signing_policy_sha256", "0" * 64),
        ("chain_identity_sha256", "0" * 64),
        ("single_assignment", False),
        ("agent_accessible", True),
        ("check_count", 5),
    ],
)
def test_signer_drift_is_fail_closed(field: str, value: Any):
    intent, journal, requirements, probe = _fixture()
    probe.signer_value = replace(probe.signer_value, **{field: value})

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == ("signer", "signer-unready")
    assert probe.calls[-1] == "signer"
    assert "funding" not in probe.calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signer_handle", "another-signer"),
        ("lease_resource_id", "another-lease"),
        ("chain_identity_sha256", "0" * 64),
        ("spendable_capacity_shannons", 1299),
        ("cell_count", 1),
        ("minimum_confirmations", 3),
        ("request_count", 9),
    ],
)
def test_funding_drift_returns_valid_failure_evidence(field: str, value: Any):
    intent, journal, requirements, probe = _fixture()
    probe.funding_value = replace(probe.funding_value, **{field: value})

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == (
        "funding", "funding-insufficient",
    )
    assert evidence.required_capacity_shannons == 1300
    assert evidence.spendable_capacity_shannons is None
    assert TaskPreflightEvidence.from_dict(evidence.to_dict()) == evidence
    assert probe.calls[-1] == "funding"
    assert "dependencies" not in probe.calls


@pytest.mark.parametrize(
    ("target", "field", "value", "stage", "category"),
    [
        (
            "dependencies", "dependencies", (("wrong", "0" * 64),),
            "dependencies", "dependency-mismatch",
        ),
        ("dependencies", "chain_identity_sha256", "0" * 64, "dependencies", "dependency-mismatch"),
        ("dependencies", "request_count", 3, "dependencies", "dependency-mismatch"),
        ("outputs", "fresh", False, "outputs", "output-not-fresh"),
        ("outputs", "symlink_count", 1, "outputs", "output-not-fresh"),
        ("outputs", "foreign_owner_count", 1, "outputs", "output-not-fresh"),
        ("outputs", "check_count", 5, "outputs", "output-not-fresh"),
    ],
)
def test_dependency_and_output_drift_is_fail_closed(
    target: str,
    field: str,
    value: Any,
    stage: str,
    category: str,
):
    intent, journal, requirements, probe = _fixture()
    current = getattr(probe, f"{target}_value")
    setattr(probe, f"{target}_value", replace(current, **{field: value}))

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == (stage, category)
    assert probe.calls[-1] == stage


@pytest.mark.parametrize("stage", [
    "source", "provider", "ckb_ai", "rpc", "signer", "funding", "dependencies", "outputs",
])
def test_adapter_exceptions_are_sanitized_and_stop_the_sequence(stage: str):
    intent, journal, requirements, probe = _fixture()
    setattr(probe, f"{stage}_value", RuntimeError("Bearer sk-live-do-not-log raw body"))

    evidence = _run(intent, journal, requirements, probe)
    rendered = json.dumps(evidence.to_dict(), sort_keys=True)

    assert (evidence.failure_stage, evidence.failure_category) == (stage, "adapter-error")
    assert probe.calls[-1] == stage
    assert "do-not-log" not in rendered
    assert evidence.controller_request_count_status == "unknown"
    assert evidence.controller_request_count is None
    assert evidence.checks[-1].observation_sha256 is None
    assert TaskPreflightEvidence.from_dict(evidence.to_dict()) == evidence


def test_adapter_timeout_is_typed_and_stops_the_sequence():
    intent, journal, requirements, probe = _fixture()
    probe.provider_value = TimeoutError("Bearer sk-live-do-not-log raw body")

    evidence = run_task_preflight(
        intent,
        journal,
        requirements,
        probe,
        checked_utc=CHECKED_UTC,
        evidence_id=EVIDENCE,
        deadline_seconds=120,
    )

    assert (evidence.failure_stage, evidence.failure_category) == (
        "provider",
        "deadline-exceeded",
    )
    assert probe.calls == ["source", "provider"]
    assert "do-not-log" not in json.dumps(evidence.to_dict())


def test_a_wrong_adapter_type_is_sanitized_as_malformed():
    intent, journal, requirements, probe = _fixture()
    probe.provider_value = {"Authorization": "Bearer sk-live-do-not-log"}

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == (
        "provider", "malformed-observation",
    )
    assert "do-not-log" not in json.dumps(evidence.to_dict())


def test_an_observation_subclass_cannot_override_canonical_serialization():
    class HostileProviderObservation(ProviderObservation):
        def to_dict(self) -> dict[str, Any]:
            return {"raw": "Bearer sk-live-do-not-log"}

    intent, journal, requirements, probe = _fixture()
    probe.provider_value = HostileProviderObservation(**probe.provider_value.__dict__)

    evidence = _run(intent, journal, requirements, probe)

    assert (evidence.failure_stage, evidence.failure_category) == (
        "provider", "malformed-observation",
    )
    assert "do-not-log" not in json.dumps(evidence.to_dict())


def test_invalid_intent_or_reservation_never_calls_an_adapter():
    intent, journal, requirements, probe = _fixture()
    wrong_requirements = replace(requirements, intent_sha256="0" * 64)
    evidence = _run(intent, journal, wrong_requirements, probe)
    assert (evidence.failure_stage, evidence.failure_category, probe.calls) == (
        "intent", "invalid-intent", [],
    )

    intent, journal, requirements, probe = _fixture()
    evidence = _run(intent, journal[:-1], requirements, probe)
    assert (evidence.failure_stage, evidence.failure_category, probe.calls) == (
        "intent", "reservation-mismatch", [],
    )

    local_intent = _intent(chain_track="local-hermetic")
    on_chain_requirements = replace(requirements, intent_sha256=local_intent.sha256)
    on_chain_journal = _journal(
        local_intent,
        claims=on_chain_requirements.required_resource_claims,
    )
    probe = FakeProbe(local_intent, on_chain_requirements)
    evidence = _run(local_intent, on_chain_journal, on_chain_requirements, probe)
    assert (evidence.failure_stage, evidence.failure_category, probe.calls) == (
        "intent", "invalid-intent", [],
    )


def test_invalid_evidence_id_is_rejected_before_any_adapter_runs():
    intent, journal, requirements, probe = _fixture()
    with pytest.raises(TaskPreflightError):
        run_task_preflight(
            intent,
            journal,
            requirements,
            probe,
            checked_utc=CHECKED_UTC,
            evidence_id="unsafe-output-name",
        )
    assert probe.calls == []


def test_preflight_time_cannot_precede_the_reservation_journal():
    intent, journal, requirements, probe = _fixture()
    evidence = _run(
        intent, journal, requirements, probe, checked_utc="2026-09-01T00:00:01Z"
    )
    assert (evidence.failure_stage, evidence.failure_category, probe.calls) == (
        "intent", "invalid-intent", [],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_evidence_max_age_seconds", MAX_MODEL_EVIDENCE_AGE_SECONDS + 1),
        ("provider_readiness_request_limit", MAX_PROVIDER_READINESS_REQUESTS + 1),
        ("ckb_ai_request_limit", MAX_CKB_AI_PREFLIGHT_REQUESTS + 1),
        ("model_qualification_kind", "catalog-only-v1"),
        ("provider_readiness_operation", "generation-v1"),
        ("ckb_ai_surface_id", "https://surface.example/private"),
    ],
)
def test_requirements_refuse_unreviewed_or_effectively_unbounded_policies(field: str, value: Any):
    intent, _, requirements, _ = _fixture()
    with pytest.raises(TaskPreflightError):
        replace(requirements, **{field: value})
    assert intent.sha256 == requirements.intent_sha256


def test_requirements_refuse_missing_ownership_and_zero_funding():
    _, _, requirements, _ = _fixture()
    with pytest.raises(TaskPreflightError):
        replace(
            requirements,
            required_resource_claims=tuple(
                claim for claim in requirements.required_resource_claims if claim[0] != "signer"
            ),
        )
    with pytest.raises(TaskPreflightError):
        replace(requirements, funding=FundingRequirement(0, 0, 0, 0, 0))
    with pytest.raises(TaskPreflightError):
        replace(requirements, funding=FundingRequirement(1000, 100, 200, 2, 0))


def test_requirements_bound_dependency_and_resource_collection_sizes():
    _, _, requirements, _ = _fixture()
    too_many_dependencies = tuple(
        (f"dependency-{index}", f"{index:064x}") for index in range(257)
    )
    with pytest.raises(TaskPreflightError):
        replace(requirements, required_dependencies=too_many_dependencies)


def test_nested_ckb_ai_chain_calls_cannot_exceed_the_surface_total():
    with pytest.raises(TaskPreflightError):
        CkbAiObservation(
            surface_id="surface-v1",
            surface_sha256="1" * 64,
            server_version="1.0.0",
            catalog_sha256="2" * 64,
            ready=True,
            request_count=1,
            chain_identity=_chain(),
        )


def test_evidence_reader_rejects_sequence_status_count_and_binding_forgery():
    intent, journal, requirements, probe = _fixture()
    evidence = _run(intent, journal, requirements, probe)

    bad_documents = []
    document = evidence.to_dict()
    document["checks"] = list(reversed(document["checks"]))
    bad_documents.append(document)

    document = evidence.to_dict()
    document["checks"] = document["checks"][:-1]
    bad_documents.append(document)

    document = evidence.to_dict()
    document["controller_request_count"] += 1
    bad_documents.append(document)

    document = evidence.to_dict()
    document["direct_chain_identity_sha256"] = None
    bad_documents.append(document)

    document = evidence.to_dict()
    document["signer_observation_sha256"] = None
    bad_documents.append(document)

    document = evidence.to_dict()
    document["spendable_capacity_shannons"] = None
    bad_documents.append(document)

    for forged in bad_documents:
        with pytest.raises(TaskPreflightError):
            TaskPreflightEvidence.from_dict(forged)


def test_cross_document_validation_refuses_rebound_evidence():
    intent, journal, requirements, probe = _fixture()
    evidence = _run(intent, journal, requirements, probe)

    with pytest.raises(TaskPreflightError):
        validate_task_preflight_evidence(
            intent,
            replace(requirements, requirements_id="another-requirements-v1"),
            evidence,
        )
    local_intent, _, local_requirements, _ = _fixture(local=True)
    with pytest.raises(TaskPreflightError):
        validate_task_preflight_evidence(local_intent, local_requirements, evidence)


def test_cross_document_validation_requires_ckb_ai_chain_evidence_when_declared():
    intent, journal, requirements, probe = _fixture()
    evidence = _run(intent, journal, requirements, probe)
    forged = replace(evidence, ckb_ai_chain_identity_sha256=None)
    with pytest.raises(TaskPreflightError):
        validate_task_preflight_evidence(intent, requirements, forged)


def test_a_failed_record_must_end_at_its_declared_failed_check():
    intent, journal, requirements, probe = _fixture()
    probe.provider_value = replace(probe.provider_value, ready=False)
    evidence = _run(intent, journal, requirements, probe)

    with pytest.raises(TaskPreflightError):
        replace(evidence, checks=evidence.checks[:-1])
    with pytest.raises(TaskPreflightError):
        replace(evidence, failure_stage="ckb_ai")
    with pytest.raises(TaskPreflightError):
        replace(evidence, controller_request_count_status="unknown", controller_request_count=None)
    with pytest.raises(TaskPreflightError):
        replace(evidence, failure_category="funding-insufficient")
    with pytest.raises(TaskPreflightError):
        replace(
            evidence,
            failure_category="adapter-error",
            controller_request_count_status="unknown",
            controller_request_count=None,
        )


def test_failed_check_evidence_and_request_count_are_both_known_or_both_unknown():
    with pytest.raises(TaskPreflightError):
        CheckEvidence("provider", "failed", "1" * 64, None)
    with pytest.raises(TaskPreflightError):
        CheckEvidence("provider", "failed", None, 1)
