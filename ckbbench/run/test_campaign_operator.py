from __future__ import annotations

import fcntl
import io
import json
import multiprocessing
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from ckbbench.run.attempt_store import (
    AttemptEnvelope,
    AttemptState,
    AttemptStore,
    AttemptStoreError,
)
from ckbbench.run.campaign import (
    STOPPING_RULE_ID,
    STOPPING_RULE_SHA256,
    CampaignBatch,
    CampaignManifest,
    CampaignSlot,
    execution_plan_sha256,
    load_report_resolution,
)
from ckbbench.run.campaign_operator import (
    CampaignOperator,
    CampaignOperatorBusy,
    CampaignOperatorError,
    CampaignProviderUnavailable,
    PreparedTaskAttempt,
    _campaign_lock,
    build_exploratory_preview,
    inspect_campaign,
    main,
    resolve_accepted_report,
    _require_private_runtime_outside_store,
    validate_report_resolution_evidence,
)
from ckbbench.run.task_preflight import (
    QUALIFICATION_KIND,
    READINESS_OPERATION,
    CkbAiObservation,
    DependencyObservation,
    OutputObservation,
    TaskPreflightRequirements,
    ProviderObservation,
    SourceObservation,
)
from ckbbench.run.single_task import AgentObservation, SetupObservation
from ckbbench.run.task_attempt import (
    VERIFIER_PRIVATE_COMMITMENT_SCHEME,
    AttemptIdentity,
    AttemptUsage,
    RetryReference,
    TaskAttemptIntent,
    TaskGrade,
    artifact_sha256,
    canonical_json_bytes,
)
from ckbbench.run.test_campaign import _manifest


def _hold_campaign_lock_in_child(path: str, ready: Any, release: Any) -> None:
    with _campaign_lock(Path(path)):
        ready.put("locked")
        release.get(timeout=10)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Probe:
    def __init__(
        self,
        intent: TaskAttemptIntent,
        requirements: TaskPreflightRequirements,
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
            model_profile_id=intent.identity.model_profile_id,
            model_profile_sha256=intent.identity.model_profile_sha256,
            qualification_kind=QUALIFICATION_KIND,
            qualification_evidence_sha256=requirements.model_qualification_evidence_sha256,
            qualification_utc=requirements.model_qualification_utc,
            operation=READINESS_OPERATION,
            authenticated=True,
            credential_present=True,
            ready=True,
            request_count=1,
            generation_request_count=0,
            body_sent=False,
            redirect_followed=False,
        )
        self.ckb_ai_value: Any = CkbAiObservation(
            surface_id=requirements.ckb_ai_surface_id,
            surface_sha256=requirements.ckb_ai_surface_sha256,
            server_version=requirements.ckb_ai_server_version,
            catalog_sha256=requirements.ckb_ai_catalog_sha256,
            ready=True,
            request_count=1,
            chain_identity=None,
        )
        self.dependencies_value: Any = DependencyObservation((), None, 0)
        self.outputs_value: Any = OutputObservation(
            requirements.expected_output_resources,
            True,
            0,
            0,
            1,
        )

    def _read(self, name: str) -> Any:
        self.calls.append(name)
        return getattr(self, f"{name}_value")

    def source(self, *, timeout_seconds: float | None) -> SourceObservation:
        return self._read("source")

    def provider(self, *, timeout_seconds: float | None) -> ProviderObservation:
        return self._read("provider")

    def ckb_ai(self, *, timeout_seconds: float | None) -> CkbAiObservation:
        return self._read("ckb_ai")

    def dependencies(self, *, timeout_seconds: float | None) -> DependencyObservation:
        return self._read("dependencies")

    def outputs(self, *, timeout_seconds: float | None) -> OutputObservation:
        return self._read("outputs")

    def rpc(self, *, timeout_seconds: float | None) -> Any:
        raise AssertionError("local campaign cannot call RPC")

    def signer(self, *, timeout_seconds: float | None) -> Any:
        raise AssertionError("local campaign cannot call signer")

    def funding(self, *, timeout_seconds: float | None) -> Any:
        raise AssertionError("local campaign cannot call funding")


def _usage(model: str) -> AttemptUsage:
    return AttemptUsage(
        token_usage_status="complete",
        cost_status="complete",
        provider_reported_cost_usd="0.01",
        model_calls=1,
        provider_attempts=1,
        provider_responses=1,
        provider_retry_count=0,
        provider_retry_delay_seconds=0,
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        provider_failure_category=None,
        provider_response_model_counts=((model, 1),),
    )


class Backend:
    def __init__(self, outcome: str, max_score: int, model: str) -> None:
        self.outcome = outcome
        self.max_score = max_score
        self.model = model
        self.events: list[str] = []
        self.handle = object()

    def setup(
        self,
        _intent: TaskAttemptIntent,
        _requirements: TaskPreflightRequirements,
        *,
        timeout_seconds: float | None,
    ) -> SetupObservation:
        self.events.append("setup")
        return SetupObservation("9" * 64)

    def start_agent(
        self,
        _intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> object:
        self.events.append("start")
        return self.handle

    def run_agent(
        self,
        _agent: object,
        *,
        step_limit: int,
        wall_time_limit_seconds: int,
        provider_call_limit: int | None,
        output_token_limit: int | None,
    ) -> AgentObservation:
        self.events.append("run")
        if self.outcome == "infra_fail":
            raise RuntimeError("private provider failure")
        return AgentObservation("submitted", _usage(self.model))

    def stop_agent_checked(
        self,
        _agent: object,
        *,
        timeout_seconds: float | None,
    ) -> None:
        self.events.append("stop")

    def grade(
        self,
        _intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> TaskGrade:
        self.events.append("grade")
        if self.outcome == "agent_fail":
            return TaskGrade("failed", 0, 0, self.max_score, "Verifier failed.", "")
        return TaskGrade(
            "passed",
            self.max_score,
            self.max_score,
            self.max_score,
            "Verifier passed.",
            "proof",
        )

    def protocol_violated(
        self,
        _intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> bool:
        self.events.append("protocol")
        return False

    def cleanup_resource(
        self,
        _intent: TaskAttemptIntent,
        _kind: str,
        _resource_id: str,
        *,
        timeout_seconds: float | None,
    ) -> str:
        self.events.append("cleanup")
        return "invalid" if self.outcome == "cleanup-incomplete" else "released"


class Runtime:
    def __init__(self, outcomes: dict[tuple[str, int], str] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.counter = 0
        self.prepared: list[tuple[str, int]] = []
        self.backends: list[Backend] = []
        self.probes: list[Probe] = []
        self.requirements: dict[str, TaskPreflightRequirements] = {}

    def _intent(
        self,
        manifest: CampaignManifest,
        slot: CampaignSlot,
        predecessor: AttemptEnvelope | None,
    ) -> TaskAttemptIntent:
        self.counter += 1
        ordinal = 0 if predecessor is None else 1
        retry = None
        if predecessor is not None:
            retry = RetryReference(
                predecessor_attempt_id=predecessor.intent.attempt_id,
                predecessor_intent_sha256=predecessor.intent.sha256,
                predecessor_result_sha256=predecessor.result.sha256,
                predecessor_cleanup_receipt_sha256=predecessor.receipts[-1].sha256,
            )
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
            prompt_params_sha256=artifact_sha256({
                "counter": self.counter,
                "kind": "prompt-parameters",
            }),
            verifier_private_commitment_scheme=VERIFIER_PRIVATE_COMMITMENT_SCHEME,
            verifier_private_commitment_sha256=artifact_sha256({
                "counter": self.counter,
                "kind": "verifier-private-commitment",
            }),
            resource_equivalence_policy_id=slot.resource_equivalence_policy_id,
            resource_equivalence_policy_sha256=slot.resource_equivalence_policy_sha256,
            retry_policy_id=manifest.retry_policy_id,
            retry_policy_sha256=manifest.retry_policy_sha256,
            execution_source=manifest.execution_source,
        )
        return TaskAttemptIntent(
            attempt_id="attempt-" + f"{self.counter:032x}",
            created_utc=(
                _utc_now()
                if predecessor is None
                else predecessor.receipts[-1].created_utc
            ),
            identity=identity,
            retry_ordinal=ordinal,
            retry=retry,
        )

    def _requirements(self, intent: TaskAttemptIntent) -> TaskPreflightRequirements:
        claims = (
            ("runtime-name", f"runtime-{intent.attempt_id}"),
            ("workspace", f"workspace-{intent.attempt_id}"),
        )
        return TaskPreflightRequirements(
            requirements_id=f"requirements-{intent.attempt_id}",
            intent_sha256=intent.sha256,
            model_qualification_kind=QUALIFICATION_KIND,
            model_qualification_evidence_sha256="6" * 64,
            model_qualification_utc=_utc_now(),
            model_evidence_max_age_seconds=3600,
            provider_readiness_operation=READINESS_OPERATION,
            provider_readiness_request_limit=1,
            ckb_ai_surface_id="docs-only-v1",
            ckb_ai_surface_sha256="7" * 64,
            ckb_ai_server_version="1.7.0",
            ckb_ai_catalog_sha256="8" * 64,
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

    def prepare(
        self,
        manifest: CampaignManifest,
        slot: CampaignSlot,
        predecessor: AttemptEnvelope | None,
    ) -> PreparedTaskAttempt:
        ordinal = 0 if predecessor is None else 1
        self.prepared.append((slot.slot_id, ordinal))
        intent = self._intent(manifest, slot, predecessor)
        requirements = self._requirements(intent)
        backend = Backend(
            self.outcomes.get((slot.slot_id, ordinal), "pass"),
            slot.max_score,
            slot.requested_model,
        )
        self.backends.append(backend)
        self.requirements[intent.attempt_id] = requirements
        probe = Probe(intent, requirements)
        self.probes.append(probe)
        return PreparedTaskAttempt(
            intent,
            requirements,
            probe,
            backend,
            slot.max_score,
        )

    def prepare_recovery(
        self,
        _manifest: CampaignManifest,
        slot: CampaignSlot,
        state: AttemptState,
    ) -> tuple[TaskPreflightRequirements, Backend, int]:
        requirements = state.preflight_requirements or self._requirements(state.intent)
        backend = Backend("pass", slot.max_score, slot.requested_model)
        self.backends.append(backend)
        return requirements, backend, slot.max_score


def _operator(
    tmp_path: Path,
    runtime: Runtime | None = None,
    retry_wait: Callable[[float], None] | None = None,
):
    manifest = _manifest()
    store = AttemptStore(tmp_path / "attempts")
    selected = runtime or Runtime()
    selected_wait = retry_wait or (lambda _seconds: None)
    return (
        manifest,
        store,
        selected,
        CampaignOperator(
            manifest,
            store,
            selected,
            tmp_path / "coordination",
            retry_wait=selected_wait,
        ),
    )


def _outage_aware_manifest() -> CampaignManifest:
    return replace(
        _manifest(),
        stopping_rule_id=STOPPING_RULE_ID,
        stopping_rule_sha256=STOPPING_RULE_SHA256,
    )


def _outage_aware_operator(
    tmp_path: Path,
    runtime: Runtime,
    retry_wait: Callable[[float], None] | None = None,
):
    manifest = _outage_aware_manifest()
    store = AttemptStore(tmp_path / "attempts")
    return manifest, store, CampaignOperator(
        manifest,
        store,
        runtime,
        tmp_path / "coordination",
        retry_wait=retry_wait or (lambda _seconds: None),
    )


def _six_slot_manifest() -> CampaignManifest:
    base = _manifest()
    control = base.slots[0]
    treatment = base.slots[1]
    third_control = replace(
        control,
        slot_id="slot-5",
        trial_id="trial-3",
        task_id="task-signed-transfer",
        task_content_sha256="1" * 64,
        max_score=25,
        trial_challenge_id="challenge-3",
        trial_challenge_sha256="2" * 64,
    )
    third_treatment = replace(
        treatment,
        slot_id="slot-6",
        trial_id=third_control.trial_id,
        task_id=third_control.task_id,
        task_content_sha256=third_control.task_content_sha256,
        max_score=third_control.max_score,
        trial_challenge_id=third_control.trial_challenge_id,
        trial_challenge_sha256=third_control.trial_challenge_sha256,
    )
    slots = (*base.slots, third_control, third_treatment)
    batches = (CampaignBatch("batch-a", tuple(slot.slot_id for slot in slots)),)
    return replace(
        base,
        batches=batches,
        slots=slots,
        execution_plan_sha256=execution_plan_sha256(batches, slots),
    )


def test_single_task_runs_only_the_next_slot_and_cannot_rerun_or_skip(tmp_path: Path):
    manifest, store, runtime, operator = _operator(tmp_path)
    with pytest.raises(CampaignOperatorError, match="next unresolved"):
        operator.run_task("slot-2")

    envelope = operator.run_task("slot-1")
    assert envelope.result.outcome == "pass"
    assert runtime.prepared == [("slot-1", 0)]
    assert inspect_campaign(manifest, store).current.slot.slot_id == "slot-2"
    with pytest.raises(CampaignOperatorError, match="next unresolved"):
        operator.run_task("slot-1")


def test_batch_continues_scored_failure_and_runs_one_infrastructure_retry(tmp_path: Path):
    runtime = Runtime({
        ("slot-1", 0): "agent_fail",
        ("slot-2", 0): "infra_fail",
        ("slot-2", 1): "pass",
    })
    waits: list[float] = []
    manifest, store, runtime, operator = _operator(tmp_path, runtime, waits.append)

    envelopes = operator.run_batch("batch-a")

    assert [envelope.result.outcome for envelope in envelopes] == [
        "agent_fail",
        "infra_fail",
        "pass",
        "pass",
        "pass",
    ]
    assert runtime.prepared == [
        ("slot-1", 0),
        ("slot-2", 0),
        ("slot-2", 1),
        ("slot-3", 0),
        ("slot-4", 0),
    ]
    assert inspect_campaign(manifest, store).complete
    assert len({id(backend.handle) for backend in runtime.backends}) == 5
    assert waits == [30.0]


def test_outage_aware_batch_does_not_reserve_work_while_provider_is_unavailable(
    tmp_path: Path,
):
    class AvailabilityRuntime(Runtime):
        available = False

        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            if not self.available:
                assert isinstance(prepared.preflight_probe, Probe)
                prepared.preflight_probe.provider_value = replace(
                    prepared.preflight_probe.provider_value,
                    authenticated=False,
                    ready=False,
                )
            return prepared

    runtime = AvailabilityRuntime()
    manifest, store, operator = _outage_aware_operator(tmp_path, runtime)

    with pytest.raises(CampaignProviderUnavailable, match="paused before reserving"):
        operator.run_batch("batch-a")

    assert runtime.prepared == [("slot-1", 0)]
    assert runtime.probes[0].calls == ["source", "provider"]
    assert runtime.backends[0].events == []
    assert store.list_attempt_ids() == ()
    assert inspect_campaign(manifest, store).current.slot.slot_id == "slot-1"

    runtime.available = True
    completed = operator.run_batch("batch-a")
    assert len(completed) == 4
    assert inspect_campaign(manifest, store).complete


def test_outage_aware_gate_checks_source_before_contacting_the_provider(tmp_path: Path):
    class DirtySourceRuntime(Runtime):
        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            assert isinstance(prepared.preflight_probe, Probe)
            prepared.preflight_probe.source_value = replace(
                prepared.preflight_probe.source_value,
                tracked_change_count=1,
            )
            return prepared

    runtime = DirtySourceRuntime()
    _manifest_row, store, operator = _outage_aware_operator(tmp_path, runtime)

    with pytest.raises(CampaignOperatorError, match="readiness gate failed"):
        operator.run_batch("batch-a")

    assert runtime.probes[0].calls == ["source"]
    assert runtime.backends[0].events == []
    assert store.list_attempt_ids() == ()


def test_outage_aware_batch_pauses_before_using_its_infrastructure_retry(tmp_path: Path):
    runtime = Runtime({
        ("slot-1", 0): "infra_fail",
        ("slot-1", 1): "pass",
    })
    waits: list[float] = []
    manifest, store, operator = _outage_aware_operator(tmp_path, runtime, waits.append)

    first = operator.run_batch("batch-a")

    assert [row.result.outcome for row in first] == ["infra_fail"]
    assert inspect_campaign(manifest, store).current.status == "needs-retry"
    assert runtime.prepared == [("slot-1", 0)]
    assert waits == []

    remaining = operator.run_batch("batch-a")
    assert [row.result.outcome for row in remaining] == ["pass", "pass", "pass", "pass"]
    assert runtime.prepared == [
        ("slot-1", 0),
        ("slot-1", 1),
        ("slot-2", 0),
        ("slot-3", 0),
        ("slot-4", 0),
    ]
    assert waits == [30.0]
    assert inspect_campaign(manifest, store).complete


def test_provider_loss_between_gate_and_preflight_retains_one_attempt_and_pauses(
    tmp_path: Path,
):
    class ProviderDropsProbe(Probe):
        def provider(self, *, timeout_seconds: float | None) -> ProviderObservation:
            observed = super().provider(timeout_seconds=timeout_seconds)
            if self.calls.count("provider") == 2:
                return replace(observed, authenticated=False, ready=False)
            return observed

    class ProviderDropsRuntime(Runtime):
        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            if slot.slot_id == "slot-1" and predecessor is None:
                probe = ProviderDropsProbe(prepared.intent, prepared.requirements)
                self.probes[-1] = probe
                return replace(prepared, preflight_probe=probe)
            return prepared

    runtime = ProviderDropsRuntime()
    manifest, store, operator = _outage_aware_operator(tmp_path, runtime)

    completed = operator.run_batch("batch-a")

    assert len(completed) == 1
    assert completed[0].result.outcome == "infra_fail"
    assert (
        completed[0].result.failure_stage,
        completed[0].result.failure_category,
    ) == ("provider", "provider-unready")
    assert runtime.probes[0].calls == ["source", "provider", "source", "provider"]
    assert inspect_campaign(manifest, store).current.status == "needs-retry"
    assert len(store.list_attempt_ids()) == 1


def test_unavailable_retry_gate_preserves_the_only_retry_for_a_later_resume(tmp_path: Path):
    class RetryAvailabilityRuntime(Runtime):
        available = True

        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            if not self.available:
                assert isinstance(prepared.preflight_probe, Probe)
                prepared.preflight_probe.provider_value = replace(
                    prepared.preflight_probe.provider_value,
                    authenticated=False,
                    ready=False,
                )
            return prepared

    runtime = RetryAvailabilityRuntime({("slot-1", 0): "infra_fail"})
    waits: list[float] = []
    manifest, store, operator = _outage_aware_operator(tmp_path, runtime, waits.append)
    original = operator.run_batch("batch-a")[0]
    assert inspect_campaign(manifest, store).current.status == "needs-retry"

    runtime.available = False
    with pytest.raises(CampaignProviderUnavailable):
        operator.retry(original.intent.attempt_id)

    assert inspect_campaign(manifest, store).current.status == "needs-retry"
    assert len(store.list_attempt_ids()) == 1

    runtime.available = True
    retried = operator.retry(original.intent.attempt_id)
    assert retried.intent.retry_ordinal == 1
    assert retried.result.outcome == "pass"
    assert len(store.list_attempt_ids()) == 2
    assert waits == [30.0, 30.0]


def test_outage_aware_batch_stops_after_an_exhausted_retry(tmp_path: Path):
    runtime = Runtime({
        ("slot-1", 0): "infra_fail",
        ("slot-1", 1): "infra_fail",
    })
    manifest, store, operator = _outage_aware_operator(tmp_path, runtime)

    assert [row.result.outcome for row in operator.run_batch("batch-a")] == ["infra_fail"]
    assert [row.result.outcome for row in operator.run_batch("batch-a")] == ["infra_fail"]
    assert inspect_campaign(manifest, store).current.slot.slot_id == "slot-2"

    completed = operator.run_batch("batch-a")
    assert len(completed) == 3
    assert inspect_campaign(manifest, store).complete


def test_outage_aware_batch_still_continues_after_scored_failure(tmp_path: Path):
    runtime = Runtime({("slot-1", 0): "agent_fail"})
    manifest, store, operator = _outage_aware_operator(tmp_path, runtime)

    completed = operator.run_batch("batch-a")

    assert [row.result.outcome for row in completed] == [
        "agent_fail", "pass", "pass", "pass",
    ]
    assert inspect_campaign(manifest, store).complete


def test_outage_aware_gate_refuses_contract_drift_without_reserving_an_attempt(
    tmp_path: Path,
):
    class ContractDriftRuntime(Runtime):
        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            assert isinstance(prepared.preflight_probe, Probe)
            prepared.preflight_probe.provider_value = replace(
                prepared.preflight_probe.provider_value,
                body_sent=True,
                generation_request_count=1,
            )
            return prepared

    runtime = ContractDriftRuntime()
    _manifest_row, store, operator = _outage_aware_operator(tmp_path, runtime)

    with pytest.raises(CampaignOperatorError, match="readiness gate failed"):
        operator.run_batch("batch-a")

    assert runtime.probes[0].calls == ["source", "provider"]
    assert store.list_attempt_ids() == ()


def test_outage_aware_gate_requires_a_real_readiness_request(tmp_path: Path):
    class ZeroRequestRuntime(Runtime):
        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            assert isinstance(prepared.preflight_probe, Probe)
            prepared.preflight_probe.provider_value = replace(
                prepared.preflight_probe.provider_value,
                request_count=0,
            )
            return prepared

    runtime = ZeroRequestRuntime()
    _manifest_row, store, operator = _outage_aware_operator(tmp_path, runtime)

    with pytest.raises(CampaignOperatorError, match="readiness gate failed"):
        operator.run_batch("batch-a")

    assert runtime.probes[0].calls == ["source", "provider"]
    assert runtime.backends[0].events == []
    assert store.list_attempt_ids() == ()


def test_outage_aware_gate_refuses_a_relaxed_request_limit_before_observation(
    tmp_path: Path,
):
    class RelaxedLimitRuntime(Runtime):
        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            return replace(
                prepared,
                requirements=replace(
                    prepared.requirements,
                    provider_readiness_request_limit=2,
                ),
            )

    runtime = RelaxedLimitRuntime()
    _manifest_row, store, operator = _outage_aware_operator(tmp_path, runtime)

    with pytest.raises(CampaignOperatorError, match="readiness gate failed"):
        operator.run_batch("batch-a")

    assert runtime.probes[0].calls == []
    assert runtime.backends[0].events == []
    assert store.list_attempt_ids() == ()


def test_six_slot_campaign_survives_failures_retry_exhaustion_and_restart(
    tmp_path: Path,
):
    manifest = _six_slot_manifest()
    store = AttemptStore(tmp_path / "attempts")
    first_runtime = Runtime({
        ("slot-1", 0): "agent_fail",
        ("slot-2", 0): "infra_fail",
        ("slot-2", 1): "pass",
        ("slot-3", 0): "infra_fail",
        ("slot-3", 1): "infra_fail",
        ("slot-4", 0): "cleanup-incomplete",
    })
    waits: list[float] = []
    first = CampaignOperator(
        manifest,
        store,
        first_runtime,
        tmp_path / "coordination",
        retry_wait=waits.append,
    )

    before_restart = first.run_batch("batch-a")

    assert [row.result.outcome for row in before_restart] == [
        "agent_fail",
        "infra_fail",
        "pass",
        "infra_fail",
        "infra_fail",
        "pass",
    ]
    assert before_restart[-1].receipts[-1].status == "incomplete"
    assert waits == [30.0, 30.0]
    assert inspect_campaign(manifest, store).current.slot.slot_id == "slot-4"

    restarted_runtime = Runtime()
    restarted_runtime.counter = len(before_restart)
    restarted = CampaignOperator(
        manifest,
        store,
        restarted_runtime,
        tmp_path / "coordination",
        retry_wait=lambda _seconds: None,
    )
    recovered = restarted.recover(before_restart[-1].intent.attempt_id)
    remaining = restarted.run_batch("batch-a")

    assert recovered.result.sha256 == before_restart[-1].result.sha256
    assert recovered.receipts[-1].status == "complete"
    assert [row.intent.identity.task_id for row in remaining] == [
        "task-signed-transfer",
        "task-signed-transfer",
    ]
    assert all(row.result.outcome == "pass" for row in remaining)
    assert len({id(backend.handle) for backend in first_runtime.backends}) == 6
    assert len({id(backend.handle) for backend in restarted_runtime.backends}) == 3
    assert inspect_campaign(manifest, store).complete
    resolution = resolve_accepted_report(manifest, store)
    validate_report_resolution_evidence(manifest, resolution, store)
    assert len(resolution.slots) == 6
    assert resolution.slots[1].retry is not None
    assert resolution.slots[2].retry is not None


def test_non_retryable_infrastructure_failure_is_terminal_and_batch_continues(tmp_path: Path):
    class SourceDriftRuntime(Runtime):
        def prepare(
            self,
            manifest: CampaignManifest,
            slot: CampaignSlot,
            predecessor: AttemptEnvelope | None,
        ) -> PreparedTaskAttempt:
            prepared = super().prepare(manifest, slot, predecessor)
            if slot.slot_id == "slot-1" and predecessor is None:
                assert isinstance(prepared.preflight_probe, Probe)
                prepared.preflight_probe.source_value = replace(
                    prepared.preflight_probe.source_value,
                    tracked_change_count=1,
                )
            return prepared

    waits: list[float] = []
    runtime = SourceDriftRuntime()
    manifest, store, runtime, operator = _operator(tmp_path, runtime, waits.append)

    envelopes = operator.run_batch("batch-a")

    assert [envelope.result.outcome for envelope in envelopes] == [
        "infra_fail", "pass", "pass", "pass",
    ]
    assert (envelopes[0].result.failure_stage, envelopes[0].result.failure_category) == (
        "source", "source-drift",
    )
    assert runtime.prepared == [
        ("slot-1", 0), ("slot-2", 0), ("slot-3", 0), ("slot-4", 0),
    ]
    assert waits == []
    assert inspect_campaign(manifest, store).complete
    resolution = resolve_accepted_report(manifest, store)
    validate_report_resolution_evidence(manifest, resolution, store)
    assert resolution.slots[0].retry is None
    assert resolution.slots[0].original.outcome == "infra_fail"
    with pytest.raises(CampaignOperatorError, match="eligible"):
        operator.retry(envelopes[0].intent.attempt_id)


def test_incomplete_cleanup_pauses_batch_until_explicit_recovery(tmp_path: Path):
    runtime = Runtime({("slot-1", 0): "cleanup-incomplete"})
    manifest, store, runtime, operator = _operator(tmp_path, runtime)

    first = operator.run_batch("batch-a")
    assert len(first) == 1
    assert first[0].receipts[-1].status == "incomplete"
    assert inspect_campaign(manifest, store).current.status == "cleanup-incomplete"
    with pytest.raises(CampaignOperatorError, match="recovered"):
        operator.run_batch("batch-a")

    recovered = operator.recover(first[0].intent.attempt_id)
    assert recovered.result.sha256 == first[0].result.sha256
    assert recovered.receipts[-1].status == "complete"
    remainder = operator.run_batch("batch-a")
    assert len(remainder) == 3
    assert inspect_campaign(manifest, store).complete


def test_standalone_retry_requires_the_exact_current_infrastructure_attempt(tmp_path: Path):
    runtime = Runtime({("slot-1", 0): "infra_fail"})
    manifest, store, _runtime, operator = _operator(tmp_path, runtime)
    failed = operator.run_task("slot-1")
    assert inspect_campaign(manifest, store).current.status == "needs-retry"

    with pytest.raises(CampaignOperatorError, match="eligible"):
        operator.retry("attempt-" + "f" * 32)
    retried = operator.retry(failed.intent.attempt_id)
    assert retried.intent.retry_ordinal == 1
    assert retried.result.outcome == "pass"
    with pytest.raises(CampaignOperatorError, match="eligible"):
        operator.retry(failed.intent.attempt_id)


def test_progress_refuses_duplicate_and_out_of_order_attempt_evidence(tmp_path: Path):
    manifest, store, runtime, _operator_row = _operator(tmp_path / "duplicate")
    first = runtime.prepare(manifest, manifest.slots[0], None)
    from ckbbench.run.single_task import execute_single_task

    execute_single_task(
        store,
        first.intent,
        first.requirements,
        first.preflight_probe,
        first.backend,
        max_score=first.max_score,
    )
    duplicate = runtime.prepare(manifest, manifest.slots[0], None)
    execute_single_task(
        store,
        duplicate.intent,
        duplicate.requirements,
        duplicate.preflight_probe,
        duplicate.backend,
        max_score=duplicate.max_score,
    )
    with pytest.raises(CampaignOperatorError, match="duplicate"):
        inspect_campaign(manifest, store)

    manifest, store, runtime, _operator_row = _operator(tmp_path / "skip")
    later = runtime.prepare(manifest, manifest.slots[1], None)
    execute_single_task(
        store,
        later.intent,
        later.requirements,
        later.preflight_probe,
        later.backend,
        max_score=later.max_score,
    )
    with pytest.raises(CampaignOperatorError, match="skips ahead"):
        inspect_campaign(manifest, store)


def test_report_resolution_is_complete_deterministic_and_manual(tmp_path: Path):
    runtime = Runtime({("slot-2", 0): "infra_fail"})
    manifest, store, _runtime, operator = _operator(tmp_path, runtime)
    with pytest.raises(CampaignOperatorError, match="not complete"):
        resolve_accepted_report(manifest, store)

    operator.run_batch("batch-a")
    first = resolve_accepted_report(manifest, store)
    second = resolve_accepted_report(manifest, store)
    assert first == second
    assert [slot.slot_id for slot in first.slots] == [
        "slot-1",
        "slot-2",
        "slot-3",
        "slot-4",
    ]
    assert first.slots[1].retry is not None
    assert first.slots[1].terminal_attempt_id == first.slots[1].retry.attempt_id

    manifest_path = tmp_path / "manifest.json"
    output = tmp_path / "resolution.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    stdout = io.StringIO()
    assert main(
        [
            "report",
            "--manifest",
            str(manifest_path),
            "--attempt-root",
            str(store.root),
            "--output",
            str(output),
        ],
        stdout=stdout,
        coordination_root=tmp_path / "coordination",
    ) == 0
    assert load_report_resolution(output) == first
    assert "accepted report resolution" in stdout.getvalue()


def test_preview_is_explicitly_exploratory_and_handles_active_attempt(tmp_path: Path):
    manifest, store, runtime, _operator_row = _operator(tmp_path)
    prepared = runtime.prepare(manifest, manifest.slots[0], None)
    store.create_intent(prepared.intent)

    preview = build_exploratory_preview(store)
    assert not preview.accepted
    assert preview.kind == "exploratory"
    assert preview.attempts[0].state == "active"
    assert preview.attempts[0].outcome is None


def test_campaign_lock_refuses_concurrent_scheduling_before_runtime_activity(tmp_path: Path):
    _manifest_row, store, runtime, operator = _operator(tmp_path)
    store.initialize()
    coordination = tmp_path / "coordination"
    coordination.mkdir(mode=0o700)
    lock_path = coordination / ".accepted-execution.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(CampaignOperatorBusy):
            operator.run_task("slot-1")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert runtime.prepared == []
    assert store.list_attempt_ids() == ()


def test_one_coordination_lock_serializes_different_campaign_stores(tmp_path: Path):
    coordination = tmp_path / "coordination"
    first_manifest = _manifest()
    second_manifest = replace(first_manifest, campaign_id="campaign-" + "b" * 32)
    first_runtime = Runtime()
    second_runtime = Runtime()
    first = CampaignOperator(
        first_manifest,
        AttemptStore(tmp_path / "first-attempts"),
        first_runtime,
        coordination,
    )
    second = CampaignOperator(
        second_manifest,
        AttemptStore(tmp_path / "second-attempts"),
        second_runtime,
        coordination,
    )
    coordination.mkdir(mode=0o700)
    descriptor = os.open(coordination / ".accepted-execution.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(CampaignOperatorBusy):
            first.run_task("slot-1")
        with pytest.raises(CampaignOperatorBusy):
            second.run_task("slot-1")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert first_runtime.prepared == []
    assert second_runtime.prepared == []


def test_coordination_lock_refuses_shared_permissions_and_symlinks(tmp_path: Path):
    manifest = _manifest()
    runtime = Runtime()
    store = AttemptStore(tmp_path / "attempts")
    public = tmp_path / "public-coordination"
    public.mkdir(mode=0o700)
    public.chmod(0o755)
    operator = CampaignOperator(manifest, store, runtime, public)
    with pytest.raises(CampaignOperatorError, match="must be private"):
        operator.run_task("slot-1")

    private = tmp_path / "private-coordination"
    private.mkdir(mode=0o700)
    lock = private / ".accepted-execution.lock"
    lock.write_text("", encoding="ascii")
    lock.chmod(0o644)
    operator = CampaignOperator(manifest, store, runtime, private)
    with pytest.raises(CampaignOperatorError, match="scheduler lock must be private"):
        operator.run_task("slot-1")

    lock.unlink()
    lock.symlink_to(tmp_path / "outside-lock")
    with pytest.raises(CampaignOperatorError, match="cannot open"):
        operator.run_task("slot-1")
    assert runtime.prepared == []
    assert not store.root.exists()


def test_coordination_lock_excludes_another_process(tmp_path: Path):
    coordination = tmp_path / "coordination"
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Queue()
    process = context.Process(
        target=_hold_campaign_lock_in_child,
        args=(str(coordination), ready, release),
    )
    process.start()
    assert ready.get(timeout=10) == "locked"
    manifest, store, runtime, _operator_row = _operator(tmp_path / "campaign")
    operator = CampaignOperator(manifest, store, runtime, coordination)
    try:
        with pytest.raises(CampaignOperatorBusy):
            operator.run_task("slot-1")
    finally:
        release.put("release")
        process.join(timeout=10)
    assert process.exitcode == 0
    assert runtime.prepared == []


def test_restarted_operator_derives_progress_and_recovers_from_retained_state(tmp_path: Path):
    manifest, store, first_runtime, first = _operator(tmp_path)
    first.run_task("slot-1")

    restarted_runtime = Runtime()
    restarted_runtime.counter = 1
    restarted = CampaignOperator(manifest, store, restarted_runtime, tmp_path / "coordination")
    second = restarted.run_task("slot-2")
    assert second.intent.identity.task_id == manifest.slots[1].task_id
    assert inspect_campaign(manifest, store).current.slot.slot_id == "slot-3"

    recovery_root = tmp_path / "recovery"
    manifest, store, first_runtime, _operator_row = _operator(recovery_root)
    prepared = first_runtime.prepare(manifest, manifest.slots[0], None)
    store.create_intent(prepared.intent)
    recovered = CampaignOperator(
        manifest,
        store,
        Runtime(),
        recovery_root / "coordination",
    ).recover(prepared.intent.attempt_id)
    assert recovered.result.outcome == "infra_fail"
    assert recovered.receipts[-1].status == "complete"


def test_report_resolution_is_verified_against_retained_envelopes(tmp_path: Path):
    manifest, store, _runtime, operator = _operator(tmp_path)
    operator.run_batch("batch-a")
    resolution = resolve_accepted_report(manifest, store)
    validate_report_resolution_evidence(manifest, resolution, store)

    first = resolution.slots[0]
    altered_reference = replace(first.original, result_sha256="9" * 64)
    altered = replace(
        resolution,
        slots=(replace(first, original=altered_reference), *resolution.slots[1:]),
    )
    with pytest.raises(CampaignOperatorError, match="retained campaign evidence"):
        validate_report_resolution_evidence(manifest, altered, store)


def test_cli_fake_runtime_executes_and_reports_without_ambient_outputs(tmp_path: Path):
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    attempts = tmp_path / "attempts"
    coordination = tmp_path / "coordination"
    runtime = Runtime({("slot-2", 0): "infra_fail"})
    stdout = io.StringIO()
    stderr = io.StringIO()

    common = ["--manifest", str(manifest_path), "--attempt-root", str(attempts)]
    assert main(
        ["run-task", *common, "--slot", "slot-1"],
        runtime=runtime,
        retry_wait=lambda _seconds: None,
        stdout=stdout,
        stderr=stderr,
        coordination_root=coordination,
    ) == 0
    assert main(
        ["run-batch", *common, "--batch", "batch-a"],
        runtime=runtime,
        retry_wait=lambda _seconds: None,
        stdout=stdout,
        stderr=stderr,
        coordination_root=coordination,
    ) == 0
    output = tmp_path / "accepted.json"
    assert main(
        ["report", *common, "--output", str(output)],
        stdout=stdout,
        stderr=stderr,
        coordination_root=coordination,
    ) == 0
    resolution = load_report_resolution(output)
    validate_report_resolution_evidence(manifest, resolution, AttemptStore(attempts))
    assert stderr.getvalue() == ""


def test_cli_reports_provider_and_infrastructure_pauses_without_losing_progress(tmp_path: Path):
    class AvailabilityRuntime(Runtime):
        available = False

        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            if not self.available:
                assert isinstance(prepared.preflight_probe, Probe)
                prepared.preflight_probe.provider_value = replace(
                    prepared.preflight_probe.provider_value,
                    authenticated=False,
                    ready=False,
                )
            return prepared

    manifest = _outage_aware_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    store = AttemptStore(tmp_path / "attempts")
    common = [
        "run-batch",
        "--manifest", str(manifest_path),
        "--attempt-root", str(store.root),
        "--batch", "batch-a",
    ]
    runtime = AvailabilityRuntime()
    stderr = io.StringIO()

    assert main(
        common,
        runtime=runtime,
        stderr=stderr,
        coordination_root=tmp_path / "coordination",
    ) == 1
    assert stderr.getvalue().startswith("PAUSED: provider unavailable")
    assert store.list_attempt_ids() == ()

    runtime.available = True
    runtime.outcomes[("slot-1", 0)] = "infra_fail"
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert main(
        common,
        runtime=runtime,
        stdout=stdout,
        stderr=stderr,
        coordination_root=tmp_path / "coordination",
    ) == 1
    assert "\tinfra_fail\tcleanup=complete" in stdout.getvalue()
    assert stderr.getvalue().startswith("PAUSED: infrastructure failure retained")
    assert inspect_campaign(manifest, store).current.status == "needs-retry"

    stdout = io.StringIO()
    stderr = io.StringIO()
    assert main(
        common,
        runtime=runtime,
        retry_wait=lambda _seconds: None,
        stdout=stdout,
        stderr=stderr,
        coordination_root=tmp_path / "coordination",
    ) == 0
    assert "\tpass\tcleanup=complete" in stdout.getvalue()
    assert stderr.getvalue() == ""
    assert inspect_campaign(manifest, store).complete


def test_cli_reports_completed_work_before_a_between_attempt_provider_pause(tmp_path: Path):
    class MidBatchOutageRuntime(Runtime):
        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            if slot.slot_id == "slot-2":
                assert isinstance(prepared.preflight_probe, Probe)
                prepared.preflight_probe.provider_value = replace(
                    prepared.preflight_probe.provider_value,
                    ready=False,
                )
            return prepared

    manifest = _outage_aware_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    store = AttemptStore(tmp_path / "attempts")
    stdout = io.StringIO()
    stderr = io.StringIO()

    assert main(
        [
            "run-batch",
            "--manifest", str(manifest_path),
            "--attempt-root", str(store.root),
            "--batch", "batch-a",
        ],
        runtime=MidBatchOutageRuntime(),
        stdout=stdout,
        stderr=stderr,
        coordination_root=tmp_path / "coordination",
    ) == 1

    completed = tuple(store.load_envelope(attempt_id) for attempt_id in store.list_attempt_ids())
    assert len(completed) == 1
    assert completed[0].result.outcome == "pass"
    assert stdout.getvalue() == (
        f"{completed[0].intent.attempt_id}\tpass\tcleanup=complete\n"
    )
    assert stderr.getvalue().startswith("PAUSED: provider unavailable")
    assert inspect_campaign(manifest, store).current.slot.slot_id == "slot-2"


def test_cli_returns_failure_when_cleanup_pauses_execution(tmp_path: Path):
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert main(
        [
            "run-task",
            "--manifest",
            str(manifest_path),
            "--attempt-root",
            str(tmp_path / "attempts"),
            "--slot",
            "slot-1",
        ],
        runtime=Runtime({("slot-1", 0): "cleanup-incomplete"}),
        stdout=stdout,
        stderr=stderr,
        coordination_root=tmp_path / "coordination",
    ) == 1
    assert "cleanup=incomplete" in stdout.getvalue()
    assert "cleanup is incomplete" in stderr.getvalue()


def test_report_and_preview_outputs_cannot_pollute_attempt_store(tmp_path: Path):
    manifest, store, _runtime, operator = _operator(tmp_path)
    operator.run_batch("batch-a")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    stderr = io.StringIO()
    assert main(
        [
            "report",
            "--manifest",
            str(manifest_path),
            "--attempt-root",
            str(store.root),
            "--output",
            str(store.root / "report.json"),
        ],
        stderr=stderr,
        coordination_root=tmp_path / "coordination",
    ) == 1
    assert "outside the immutable attempt store" in stderr.getvalue()
    assert not (store.root / "report.json").exists()

    stderr = io.StringIO()
    assert main(
        [
            "preview",
            "--attempt-root",
            str(store.root),
            "--output",
            str(store.root / "preview.json"),
        ],
        stderr=stderr,
    ) == 1
    assert "outside the immutable attempt store" in stderr.getvalue()
    assert not (store.root / "preview.json").exists()


@pytest.mark.parametrize("private_relative", [".", "private", "private/nested"])
def test_private_runtime_material_cannot_overlap_the_attempt_store(
    tmp_path: Path,
    private_relative: str,
):
    store = AttemptStore(tmp_path / "attempts")
    private = store.root / private_relative

    with pytest.raises(CampaignOperatorError, match="must be separate"):
        _require_private_runtime_outside_store(private, store)


def test_attempt_store_cannot_be_nested_under_private_runtime_material(tmp_path: Path):
    store = AttemptStore(tmp_path / "private" / "attempts")

    with pytest.raises(CampaignOperatorError, match="must be separate"):
        _require_private_runtime_outside_store(tmp_path / "private", store)


def test_attempt_store_listing_is_sorted_and_refuses_foreign_entries(tmp_path: Path):
    manifest = _manifest()
    runtime = Runtime()
    store = AttemptStore(tmp_path / "attempts")
    second = runtime.prepare(manifest, manifest.slots[1], None)
    first = runtime.prepare(manifest, manifest.slots[0], None)
    store.create_intent(second.intent)
    store.create_intent(first.intent)
    assert store.list_attempt_ids() == tuple(sorted((second.intent.attempt_id, first.intent.attempt_id)))

    (store.root / "foreign.json").write_text("{}", encoding="ascii")
    with pytest.raises(AttemptStoreError, match="real directory"):
        store.list_attempt_ids()


def test_cli_listing_freeze_and_plan_do_not_construct_a_runtime(tmp_path: Path):
    runtime = Runtime()
    stdout = io.StringIO()
    stderr = io.StringIO()
    coordination = tmp_path / "coordination"
    assert main(
        ["tasks", "--suite", "suites/ckb-v1"],
        runtime=runtime,
        stdout=stdout,
        stderr=stderr,
        coordination_root=coordination,
    ) == 0
    assert "task-01-tip" in stdout.getvalue()

    manifest = _manifest()
    draft = tmp_path / "draft.json"
    frozen = tmp_path / "campaign.json"
    draft.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    assert main(
        ["freeze", "--draft", str(draft), "--output", str(frozen)],
        runtime=runtime,
        stdout=stdout,
        stderr=stderr,
        coordination_root=coordination,
    ) == 0
    assert main(
        ["plan", "--manifest", str(frozen)],
        runtime=runtime,
        stdout=stdout,
        stderr=stderr,
        coordination_root=coordination,
    ) == 0
    assert runtime.prepared == []
    assert not coordination.exists()
    assert stderr.getvalue() == ""


def test_cli_plan_and_live_refusal_are_offline_and_secret_safe(tmp_path: Path):
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert main(["plan", "--manifest", str(manifest_path)], stdout=stdout, stderr=stderr) == 0
    assert "slot-1" in stdout.getvalue()
    attempt_root = tmp_path / "attempts"
    assert main(
        [
            "run-task",
            "--manifest",
            str(manifest_path),
            "--attempt-root",
            str(attempt_root),
            "--slot",
            "slot-1",
        ],
        stdout=stdout,
        stderr=stderr,
    ) == 1
    assert "needs explicit live authorization" in stderr.getvalue()
    assert not attempt_root.exists()

    hostile = "sk-live-must-not-print"
    stderr = io.StringIO()
    assert main(["plan", hostile], stderr=stderr) == 1
    assert hostile not in stderr.getvalue()


def test_surface_capture_cli_requires_authorization_before_constructing_a_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[Path] = []

    def capture(output: Path | str):
        calls.append(Path(output))
        return (SimpleNamespace(profile_id="surface-a", sha256="1" * 64),)

    monkeypatch.setattr("ckbbench.run.surface_capture.capture_and_publish", capture)
    output = tmp_path / "surfaces"
    stderr = io.StringIO()

    assert main(
        ["capture-surfaces", "--output-dir", str(output)],
        stderr=stderr,
    ) == 1
    assert calls == []
    assert not output.exists()

    stdout = io.StringIO()
    assert main(
        [
            "capture-surfaces",
            "--output-dir",
            str(output),
            "--authorized-by-user",
        ],
        stdout=stdout,
        stderr=io.StringIO(),
    ) == 0
    assert calls == [output]
    assert "surface-a" in stdout.getvalue()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--authorized-by-user",), "model profile and private runtime root"),
        (
            (
                "--authorized-by-user",
                "--model-profile",
                "gpt-5.6-sol",
                "--private-runtime-root",
                "private-runtime",
            ),
            "requires CKBBENCH_DOCKER=1",
        ),
    ],
)
def test_live_cli_refuses_partial_runtime_inputs_before_attempt_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
    message: str,
):
    class Binding:
        @staticmethod
        def validate_manifest(_manifest_value):
            return None

    monkeypatch.delenv("CKBBENCH_DOCKER", raising=False)
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    attempt_root = tmp_path / "attempts"
    stderr = io.StringIO()

    assert main(
        [
            "run-task",
            "--manifest",
            str(manifest_path),
            "--attempt-root",
            str(attempt_root),
            "--slot",
            "slot-1",
            *extra,
        ],
        release_binding=Binding(),
        stderr=stderr,
    ) == 1
    assert message in stderr.getvalue()
    assert not attempt_root.exists()
