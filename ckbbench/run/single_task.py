"""Serialized supervisor for one isolated Task attempt."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Protocol

from ckbbench.run.attempt_store import AttemptEnvelope, AttemptState, AttemptStore
from ckbbench.run.task_preflight import (
    TaskPreflightEvidence,
    TaskPreflightProbe,
    TaskPreflightRequirements,
    allocate_preflight_id,
    run_task_preflight,
    validate_task_preflight_evidence,
)
from ckbbench.run.task_attempt import (
    AttemptSchemaError,
    AttemptOutcome,
    AttemptTimings,
    AttemptUsage,
    CleanupReceipt,
    OwnershipJournalEntry,
    ResourceDisposition,
    TaskAttemptIntent,
    TaskAttemptResult,
    TaskGrade,
    allocate_receipt_id,
    artifact_sha256,
    state_for_entries,
    validate_public_artifact_values,
    validate_journal,
)

_FINAL_ACTIONS = frozenset({"released", "retired", "permanent", "absent"})
_FINAL_STATE = {
    "released": "released",
    "retired": "retired",
    "permanent": "permanent",
    "absent": "absent",
}
_UNOBSERVED_EQUIVALENCE_SHA256 = artifact_sha256({"status": "not-observed"})
_CLEANUP_FAILURE_SHA256 = artifact_sha256({
    "category": "adapter-error",
    "stage": "cleanup",
})


class SingleTaskExecutionError(RuntimeError):
    """The immutable execution contract cannot be followed."""


class SingleTaskExecutorBusy(SingleTaskExecutionError):
    """Another accepted Task execution currently owns the serialized runner."""


@dataclass(frozen=True)
class SetupObservation:
    initial_resource_equivalence_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.initial_resource_equivalence_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.initial_resource_equivalence_sha256
        ):
            raise SingleTaskExecutionError("setup returned an invalid equivalence digest")


@dataclass(frozen=True)
class AgentObservation:
    exit_status: str
    usage: AttemptUsage

    def __post_init__(self) -> None:
        if (
            not isinstance(self.exit_status, str)
            or not self.exit_status
            or len(self.exit_status) > 200
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}", self.exit_status) is None
        ):
            raise SingleTaskExecutionError("agent returned an invalid exit status")
        if not isinstance(self.usage, AttemptUsage):
            raise SingleTaskExecutionError("agent returned untyped usage")
        validate_public_artifact_values(self.usage.to_dict())


class SingleTaskBackend(Protocol):
    """Private adapter boundary; implementations may retain handles but not raw data in evidence."""

    def setup(
        self,
        intent: TaskAttemptIntent,
        requirements: TaskPreflightRequirements,
    ) -> SetupObservation: ...

    def start_agent(self, intent: TaskAttemptIntent) -> object: ...

    def run_agent(self, agent: object) -> AgentObservation: ...

    def stop_agent_checked(self, agent: object) -> None: ...

    def grade(self, intent: TaskAttemptIntent) -> TaskGrade: ...

    def protocol_violated(self, intent: TaskAttemptIntent) -> bool: ...

    def cleanup_resource(
        self,
        intent: TaskAttemptIntent,
        resource_kind: str,
        resource_id: str,
    ) -> str: ...


UtcNow = Callable[[], str]
Monotonic = Callable[[], float]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _unavailable_usage() -> AttemptUsage:
    return replace(_not_started_usage(), token_usage_status="unavailable")


def _unscored(max_score: int, reason: str) -> TaskGrade:
    return TaskGrade(
        status="not_scored",
        verifier_score=0,
        score_awarded=0,
        max_score=max_score,
        reason=reason,
        proof="",
    )


def _duration(start: float, monotonic: Monotonic) -> float:
    elapsed = float(monotonic()) - start
    if elapsed < 0:
        raise SingleTaskExecutionError("monotonic clock moved backwards")
    return elapsed


@contextmanager
def _serialized(store: AttemptStore) -> Iterator[None]:
    store.initialize()
    lock_path = store.root.parent / f".{store.root.name}.single-task.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise SingleTaskExecutionError("cannot open the serialized execution lock") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SingleTaskExecutionError("serialized execution lock must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SingleTaskExecutorBusy("another Task attempt is already executing") from None
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _entry(
    intent: TaskAttemptIntent,
    journal: tuple[OwnershipJournalEntry, ...],
    *,
    utc_now: UtcNow,
    phase: str,
    action: str,
    resource: tuple[str, str],
    details_sha256: str | None = None,
) -> OwnershipJournalEntry:
    previous = journal[-1] if journal else None
    return OwnershipJournalEntry(
        attempt_id=intent.attempt_id,
        intent_sha256=intent.sha256,
        sequence=len(journal),
        created_utc=utc_now(),
        phase=phase,
        action=action,
        resource_kind=resource[0],
        resource_id=resource[1],
        details_sha256=details_sha256,
        previous_entry_sha256=None if previous is None else previous.sha256,
    )


def _append(
    store: AttemptStore,
    intent: TaskAttemptIntent,
    journal: tuple[OwnershipJournalEntry, ...],
    *,
    utc_now: UtcNow,
    phase: str,
    action: str,
    resource: tuple[str, str],
    details_sha256: str | None = None,
) -> tuple[OwnershipJournalEntry, ...]:
    row = _entry(
        intent,
        journal,
        utc_now=utc_now,
        phase=phase,
        action=action,
        resource=resource,
        details_sha256=details_sha256,
    )
    store.append_journal(row)
    return (*journal, row)


def _interrupted_preflight(
    intent: TaskAttemptIntent,
    requirements: TaskPreflightRequirements,
    *,
    created_utc: str,
) -> TaskPreflightEvidence:
    required_capacity = (
        None if requirements.funding is None else requirements.funding.required_capacity_shannons
    )
    evidence = TaskPreflightEvidence(
        evidence_id=allocate_preflight_id(),
        attempt_id=intent.attempt_id,
        intent_sha256=intent.sha256,
        requirements_sha256=requirements.sha256,
        created_utc=created_utc,
        status="failed",
        failure_stage="intent",
        failure_category="interrupted",
        checks=(),
        controller_request_count_status="exact",
        controller_request_count=0,
        direct_chain_identity_sha256=None,
        ckb_ai_chain_identity_sha256=None,
        signer_observation_sha256=None,
        funding_observation_sha256=None,
        required_capacity_shannons=required_capacity,
        spendable_capacity_shannons=None,
    )
    validate_task_preflight_evidence(intent, requirements, evidence)
    return evidence


def _result(
    *,
    intent: TaskAttemptIntent,
    evidence: TaskPreflightEvidence,
    journal: tuple[OwnershipJournalEntry, ...],
    max_score: int,
    utc_now: UtcNow,
    outcome: AttemptOutcome,
    grade: TaskGrade,
    usage: AttemptUsage,
    timings: AttemptTimings,
    equivalence_sha256: str,
    agent_exit_status: str | None,
    failure_stage: str | None,
    failure_category: str | None,
) -> TaskAttemptResult:
    if grade.max_score != max_score:
        raise SingleTaskExecutionError("Task grade does not match the selected maximum score")
    return TaskAttemptResult(
        attempt_id=intent.attempt_id,
        created_utc=utc_now(),
        intent_sha256=intent.sha256,
        identity=intent.identity,
        pre_teardown_journal_sha256=journal[-1].sha256,
        preflight=evidence.binding(),
        outcome=outcome,
        correctness_eligible=outcome != "infra_fail",
        grade=grade,
        usage=usage,
        timings=timings,
        initial_resource_equivalence_sha256=equivalence_sha256,
        agent_exit_status=agent_exit_status,
        failure_stage=failure_stage,
        failure_category=failure_category,
    )


def _infra_result(
    *,
    intent: TaskAttemptIntent,
    evidence: TaskPreflightEvidence,
    journal: tuple[OwnershipJournalEntry, ...],
    max_score: int,
    utc_now: UtcNow,
    usage: AttemptUsage,
    timings: AttemptTimings,
    equivalence_sha256: str,
    agent_exit_status: str | None,
    stage: str,
    category: str,
) -> TaskAttemptResult:
    return _result(
        intent=intent,
        evidence=evidence,
        journal=journal,
        max_score=max_score,
        utc_now=utc_now,
        outcome="infra_fail",
        grade=_unscored(max_score, "Infrastructure prevented a trustworthy grade."),
        usage=usage,
        timings=timings,
        equivalence_sha256=equivalence_sha256,
        agent_exit_status=agent_exit_status,
        failure_stage=stage,
        failure_category=category,
    )


def _cleanup(
    store: AttemptStore,
    state: AttemptState,
    backend: SingleTaskBackend,
    *,
    utc_now: UtcNow,
) -> AttemptEnvelope:
    if state.result is None:
        raise SingleTaskExecutionError("cleanup requires a sealed Task result")
    journal = state.journal
    prior = state.receipts[-1] if state.receipts else None
    phase = "teardown" if prior is None else "reconcile"
    latest = state_for_entries(journal)
    resources = tuple(sorted(validate_journal(state.intent, journal).resources))
    sealed_length = 0
    if prior is not None:
        sealed_length = next(
            index + 1
            for index, entry in enumerate(journal)
            if entry.sha256 == prior.terminal_journal_sha256
        )
    unsealed_failures = {
        (entry.resource_kind, entry.resource_id)
        for entry in journal[sealed_length:]
        if entry.action == "cleanup-failed"
    }

    for resource in resources:
        action, _digest = latest[resource]
        if action in _FINAL_ACTIONS:
            continue
        if action == "cleanup-failed" and resource in unsealed_failures:
            continue
        journal = _append(
            store,
            state.intent,
            journal,
            utc_now=utc_now,
            phase=phase,
            action="release-intent",
            resource=resource,
        )
        try:
            final_action = backend.cleanup_resource(state.intent, *resource)
        except Exception:
            final_action = None
        if isinstance(final_action, str) and final_action in _FINAL_ACTIONS:
            journal = _append(
                store,
                state.intent,
                journal,
                utc_now=utc_now,
                phase=phase,
                action=final_action,
                resource=resource,
            )
        else:
            journal = _append(
                store,
                state.intent,
                journal,
                utc_now=utc_now,
                phase=phase,
                action="cleanup-failed",
                resource=resource,
                details_sha256=_CLEANUP_FAILURE_SHA256,
            )
        latest = state_for_entries(journal)

    dispositions = []
    incomplete = False
    for resource in resources:
        action, digest = latest[resource]
        if action in _FINAL_ACTIONS:
            final_state = _FINAL_STATE[action]
        elif action == "cleanup-failed":
            final_state = "failed"
            incomplete = True
        else:
            raise SingleTaskExecutionError("cleanup left an unaccounted active resource")
        dispositions.append(ResourceDisposition(*resource, final_state, digest))

    receipt = CleanupReceipt(
        receipt_id=allocate_receipt_id(),
        attempt_id=state.intent.attempt_id,
        created_utc=utc_now(),
        sequence=len(state.receipts),
        kind="cleanup" if prior is None else "reconciliation",
        status="incomplete" if incomplete else "complete",
        intent_sha256=state.intent.sha256,
        result_sha256=state.result.sha256,
        pre_teardown_journal_sha256=state.result.pre_teardown_journal_sha256,
        terminal_journal_sha256=journal[-1].sha256,
        prior_receipt_sha256=None if prior is None else prior.sha256,
        dispositions=tuple(dispositions),
    )
    store.append_receipt(receipt)
    return store.load_envelope(state.intent.attempt_id, require_complete=False)


def execute_single_task(
    store: AttemptStore,
    intent: TaskAttemptIntent,
    requirements: TaskPreflightRequirements,
    preflight_probe: TaskPreflightProbe,
    backend: SingleTaskBackend,
    *,
    max_score: int,
    utc_now: UtcNow = _utc_now,
    monotonic: Monotonic = time.monotonic,
) -> AttemptEnvelope:
    """Execute one new attempt and return its sealed envelope, including cleanup evidence."""
    if isinstance(max_score, bool) or not isinstance(max_score, int) or max_score <= 0:
        raise SingleTaskExecutionError("max_score must be a positive integer")
    with _serialized(store):
        reservation_start = float(monotonic())
        store.create_intent(intent)
        store.write_preflight_requirements(intent.attempt_id, requirements)
        journal: tuple[OwnershipJournalEntry, ...] = ()
        for resource in requirements.required_resource_claims:
            journal = _append(
                store,
                intent,
                journal,
                utc_now=utc_now,
                phase="reserve",
                action="claim",
                resource=resource,
            )
        reservation_seconds = _duration(reservation_start, monotonic)

        preflight_start = float(monotonic())
        evidence = run_task_preflight(
            intent,
            journal,
            requirements,
            preflight_probe,
            checked_utc=utc_now(),
        )
        preflight_seconds = _duration(preflight_start, monotonic)
        store.write_preflight_evidence(intent.attempt_id, evidence)

        timings = AttemptTimings(
            reservation_seconds=reservation_seconds,
            preflight_seconds=preflight_seconds,
            setup_seconds=0.0,
            agent_seconds=0.0,
            grading_seconds=0.0,
        )
        equivalence = _UNOBSERVED_EQUIVALENCE_SHA256

        if evidence.status == "failed":
            result = _infra_result(
                intent=intent,
                evidence=evidence,
                journal=journal,
                max_score=max_score,
                utc_now=utc_now,
                usage=_not_started_usage(),
                timings=timings,
                equivalence_sha256=equivalence,
                agent_exit_status=None,
                stage=evidence.failure_stage or "preflight",
                category=evidence.failure_category or "adapter-error",
            )
            store.write_result(result)
            return _cleanup(store, store.load_state(intent.attempt_id), backend, utc_now=utc_now)

        setup_start = float(monotonic())
        for resource in requirements.required_resource_claims:
            journal = _append(
                store,
                intent,
                journal,
                utc_now=utc_now,
                phase="setup",
                action="mutation-intent",
                resource=resource,
            )
        try:
            setup = backend.setup(intent, requirements)
            if type(setup) is not SetupObservation:
                raise SingleTaskExecutionError("setup returned an untyped observation")
            equivalence = setup.initial_resource_equivalence_sha256
        except Exception:
            timings = replace(timings, setup_seconds=_duration(setup_start, monotonic))
            result = _infra_result(
                intent=intent,
                evidence=evidence,
                journal=journal,
                max_score=max_score,
                utc_now=utc_now,
                usage=_not_started_usage(),
                timings=timings,
                equivalence_sha256=equivalence,
                agent_exit_status=None,
                stage="setup",
                category="adapter-error",
            )
            store.write_result(result)
            return _cleanup(store, store.load_state(intent.attempt_id), backend, utc_now=utc_now)
        for resource in requirements.required_resource_claims:
            journal = _append(
                store,
                intent,
                journal,
                utc_now=utc_now,
                phase="setup",
                action="acquired",
                resource=resource,
            )
        timings = replace(timings, setup_seconds=_duration(setup_start, monotonic))

        agent_start = float(monotonic())
        agent: object | None = None
        observation: AgentObservation | None = None
        agent_failure: tuple[str, str] | None = None
        agent_run_started = False
        try:
            agent = backend.start_agent(intent)
            if agent is None:
                raise SingleTaskExecutionError("agent start returned no handle")
            agent_run_started = True
            observation = backend.run_agent(agent)
            if type(observation) is not AgentObservation:
                raise SingleTaskExecutionError("agent returned an untyped observation")
        except Exception:
            agent_failure = ("agent", "adapter-error")
        timings = replace(timings, agent_seconds=_duration(agent_start, monotonic))

        if agent is not None:
            try:
                backend.stop_agent_checked(agent)
            except Exception:
                agent_failure = ("stop", "adapter-error")

        if agent_failure is not None:
            result = _infra_result(
                intent=intent,
                evidence=evidence,
                journal=journal,
                max_score=max_score,
                utc_now=utc_now,
                usage=(
                    _unavailable_usage()
                    if observation is None and agent_run_started
                    else _not_started_usage()
                    if observation is None
                    else observation.usage
                ),
                timings=timings,
                equivalence_sha256=equivalence,
                agent_exit_status=None if observation is None else observation.exit_status,
                stage=agent_failure[0],
                category=agent_failure[1],
            )
            store.write_result(result)
            return _cleanup(store, store.load_state(intent.attempt_id), backend, utc_now=utc_now)

        if observation is None:
            raise SingleTaskExecutionError("agent observation is missing after successful execution")

        grading_start = float(monotonic())
        try:
            grade = backend.grade(intent)
            if type(grade) is not TaskGrade or grade.max_score != max_score:
                raise SingleTaskExecutionError("grader returned an invalid Task grade")
            if grade.status == "not_scored":
                raise SingleTaskExecutionError("grader returned no correctness observation")
            validate_public_artifact_values(grade.to_dict())
        except Exception:
            timings = replace(timings, grading_seconds=_duration(grading_start, monotonic))
            result = _infra_result(
                intent=intent,
                evidence=evidence,
                journal=journal,
                max_score=max_score,
                utc_now=utc_now,
                usage=observation.usage,
                timings=timings,
                equivalence_sha256=equivalence,
                agent_exit_status=observation.exit_status,
                stage="grading",
                category="adapter-error",
            )
            store.write_result(result)
            return _cleanup(store, store.load_state(intent.attempt_id), backend, utc_now=utc_now)

        try:
            violated = backend.protocol_violated(intent)
            if type(violated) is not bool:
                raise SingleTaskExecutionError("protocol check returned a non-boolean result")
        except Exception:
            timings = replace(timings, grading_seconds=_duration(grading_start, monotonic))
            result = _infra_result(
                intent=intent,
                evidence=evidence,
                journal=journal,
                max_score=max_score,
                utc_now=utc_now,
                usage=observation.usage,
                timings=timings,
                equivalence_sha256=equivalence,
                agent_exit_status=observation.exit_status,
                stage="protocol",
                category="adapter-error",
            )
            store.write_result(result)
            return _cleanup(store, store.load_state(intent.attempt_id), backend, utc_now=utc_now)

        timings = replace(timings, grading_seconds=_duration(grading_start, monotonic))
        outcome: AttemptOutcome
        if violated:
            grade = replace(grade, score_awarded=0)
            outcome = "protocol_violation"
            failure_stage = "protocol"
            failure_category = "treatment-violation"
        elif grade.status == "passed":
            outcome = "pass"
            failure_stage = failure_category = None
        else:
            outcome = "agent_fail"
            failure_stage = "grading"
            failure_category = "verifier-failed"
        result = _result(
            intent=intent,
            evidence=evidence,
            journal=journal,
            max_score=max_score,
            utc_now=utc_now,
            outcome=outcome,
            grade=grade,
            usage=observation.usage,
            timings=timings,
            equivalence_sha256=equivalence,
            agent_exit_status=observation.exit_status,
            failure_stage=failure_stage,
            failure_category=failure_category,
        )
        store.write_result(result)
        return _cleanup(store, store.load_state(intent.attempt_id), backend, utc_now=utc_now)


def recover_single_task(
    store: AttemptStore,
    attempt_id: str,
    requirements: TaskPreflightRequirements,
    backend: SingleTaskBackend,
    *,
    max_score: int,
    utc_now: UtcNow = _utc_now,
) -> AttemptEnvelope:
    """Seal and clean an interrupted attempt without running preflight, setup, agent, or grade."""
    if isinstance(max_score, bool) or not isinstance(max_score, int) or max_score <= 0:
        raise SingleTaskExecutionError("max_score must be a positive integer")
    with _serialized(store):
        state = store.load_state(attempt_id)
        if (
            state.preflight_requirements is not None
            and state.preflight_requirements != requirements
        ):
            raise SingleTaskExecutionError("recovery requirements differ from the stored plan")
        if state.result is not None and state.result.grade.max_score != max_score:
            raise SingleTaskExecutionError("recovery maximum score differs from the stored result")
        if state.receipts and state.receipts[-1].status == "complete":
            return store.load_envelope(attempt_id)
        if state.preflight_requirements is None:
            store.write_preflight_requirements(attempt_id, requirements)
            state = store.load_state(attempt_id)

        expected_claims = requirements.required_resource_claims
        reserve_entries = tuple(
            entry
            for entry in state.journal
            if entry.phase == "reserve" and entry.action == "claim"
        )
        observed_claims = tuple(
            (entry.resource_kind, entry.resource_id) for entry in reserve_entries
        )
        if observed_claims != expected_claims[: len(observed_claims)]:
            raise SingleTaskExecutionError("recovery found claims outside the immutable plan")
        if state.preflight_evidence is None and len(state.journal) != len(reserve_entries):
            raise SingleTaskExecutionError("activity exists without terminal preflight evidence")

        journal = state.journal
        for resource in expected_claims[len(observed_claims):]:
            journal = _append(
                store,
                state.intent,
                journal,
                utc_now=utc_now,
                phase="reserve",
                action="claim",
                resource=resource,
            )

        evidence = state.preflight_evidence
        if evidence is None:
            evidence = _interrupted_preflight(
                state.intent,
                requirements,
                created_utc=utc_now(),
            )
            store.write_preflight_evidence(attempt_id, evidence)

        result = state.result
        if result is None:
            usage = _not_started_usage()
            stage = evidence.failure_stage or "setup"
            category = evidence.failure_category or "interrupted"
            if evidence.status == "passed":
                latest = state_for_entries(journal)
                setup_complete = all(
                    latest[resource][0] == "acquired" for resource in expected_claims
                )
                if setup_complete:
                    usage = _unavailable_usage()
                    stage = "agent"
            timings = AttemptTimings(
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                measurement_status="unavailable",
            )
            result = _infra_result(
                intent=state.intent,
                evidence=evidence,
                journal=journal,
                max_score=max_score,
                utc_now=utc_now,
                usage=usage,
                timings=timings,
                equivalence_sha256=_UNOBSERVED_EQUIVALENCE_SHA256,
                agent_exit_status=None,
                stage=stage,
                category=category,
            )
            store.write_result(result)

        return _cleanup(store, store.load_state(attempt_id), backend, utc_now=utc_now)
