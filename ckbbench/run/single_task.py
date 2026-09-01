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
from ckbbench.suite.execution_contract import TaskExecutionContract

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
_CLEANUP_DEADLINE_SHA256 = artifact_sha256({
    "category": "deadline-exceeded",
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
        *,
        timeout_seconds: float | None,
    ) -> SetupObservation: ...

    def start_agent(
        self,
        intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> object: ...

    def run_agent(
        self,
        agent: object,
        *,
        step_limit: int,
        wall_time_limit_seconds: int,
        provider_call_limit: int | None,
        output_token_limit: int | None,
    ) -> AgentObservation: ...

    def stop_agent_checked(
        self,
        agent: object,
        *,
        timeout_seconds: float | None,
    ) -> None: ...

    def grade(
        self,
        intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> TaskGrade: ...

    def protocol_violated(
        self,
        intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> bool: ...

    def cleanup_resource(
        self,
        intent: TaskAttemptIntent,
        resource_kind: str,
        resource_id: str,
        *,
        timeout_seconds: float | None,
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


def _remaining(
    start: float,
    limit_seconds: int | None,
    monotonic: Monotonic,
) -> float | None:
    if limit_seconds is None:
        return None
    remaining = float(limit_seconds) - (float(monotonic()) - start)
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _validate_execution_contract(
    intent: TaskAttemptIntent,
    contract: TaskExecutionContract | None,
) -> None:
    if contract is None:
        if int(intent.identity.suite_semver.split(".", 1)[0]) >= 4:
            raise SingleTaskExecutionError("this suite requires its execution contract")
        return
    if type(contract) is not TaskExecutionContract:
        raise SingleTaskExecutionError("execution contract must be an immutable typed record")
    identity = intent.identity
    if (
        identity.chain_track,
        identity.chain_profile_id,
        identity.chain_profile_sha256,
        identity.run_params_derivation,
        identity.resource_equivalence_policy_id,
        identity.resource_equivalence_policy_sha256,
    ) != (
        contract.chain_track,
        contract.chain_profile_id,
        contract.chain_profile_sha256,
        contract.run_params_derivation,
        contract.resource_equivalence_policy_id,
        contract.resource_equivalence_policy_sha256,
    ):
        raise SingleTaskExecutionError("attempt identity differs from its execution contract")
    budget = identity.budget
    expected = contract.budget
    if (
        budget.profile_id,
        budget.profile_sha256,
        budget.step_limit,
        budget.wall_time_limit_seconds,
        budget.provider_call_limit,
        budget.output_token_limit,
    ) != (
        expected.profile_id,
        expected.sha256,
        expected.step_limit,
        expected.wall_time_limit_seconds,
        expected.provider_call_limit,
        expected.output_token_limit,
    ):
        raise SingleTaskExecutionError("attempt budget differs from its execution contract")


def _usage_within_budget(
    observation: AgentObservation,
    contract: TaskExecutionContract | None,
) -> bool:
    if contract is None:
        return True
    budget = contract.budget
    return (
        observation.usage.model_calls <= budget.step_limit
        and budget.provider_call_limit is not None
        and observation.usage.provider_attempts <= budget.provider_call_limit
        and (
            budget.output_token_limit is None
            or (
                observation.usage.completion_tokens is not None
                and observation.usage.completion_tokens <= budget.output_token_limit
            )
        )
    )


def _stop_agent(
    backend: SingleTaskBackend,
    agent: object,
    *,
    teardown_seconds: float | None,
    monotonic: Monotonic,
) -> tuple[tuple[str, str] | None, float | None]:
    started = float(monotonic())
    failure: tuple[str, str] | None = None
    try:
        backend.stop_agent_checked(agent, timeout_seconds=teardown_seconds)
    except Exception as exc:
        failure = (
            "stop",
            "deadline-exceeded" if isinstance(exc, TimeoutError) else "adapter-error",
        )
    elapsed = _duration(started, monotonic)
    remaining = (
        None
        if teardown_seconds is None
        else max(0.0, teardown_seconds - elapsed)
    )
    if failure is None and remaining is not None and remaining <= 0:
        failure = ("stop", "deadline-exceeded")
    return failure, remaining


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
    teardown_seconds: float | None = None,
    monotonic: Monotonic = time.monotonic,
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
    teardown_start = float(monotonic())

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
        details_sha256 = _CLEANUP_FAILURE_SHA256
        try:
            remaining = _remaining(teardown_start, teardown_seconds, monotonic)
            final_action = backend.cleanup_resource(
                state.intent,
                *resource,
                timeout_seconds=remaining,
            )
            if teardown_seconds is not None and _duration(teardown_start, monotonic) > teardown_seconds:
                final_action = None
                details_sha256 = _CLEANUP_DEADLINE_SHA256
        except TimeoutError:
            final_action = None
            details_sha256 = _CLEANUP_DEADLINE_SHA256
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
                details_sha256=details_sha256,
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
    execution_contract: TaskExecutionContract | None = None,
    utc_now: UtcNow = _utc_now,
    monotonic: Monotonic = time.monotonic,
) -> AttemptEnvelope:
    """Execute one new attempt and return its sealed envelope, including cleanup evidence."""
    if isinstance(max_score, bool) or not isinstance(max_score, int) or max_score <= 0:
        raise SingleTaskExecutionError("max_score must be a positive integer")
    _validate_execution_contract(intent, execution_contract)
    deadlines = None if execution_contract is None else execution_contract.harness_deadlines
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
            deadline_seconds=None if deadlines is None else deadlines.preflight_seconds,
            monotonic=monotonic,
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

        preflight_deadline_exceeded = (
            deadlines is not None and preflight_seconds > deadlines.preflight_seconds
        )

        if evidence.status == "failed" or preflight_deadline_exceeded:
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
                stage=(
                    evidence.failure_stage
                    if evidence.status == "failed"
                    else "preflight"
                ) or "preflight",
                category=(
                    evidence.failure_category
                    if evidence.status == "failed"
                    else "deadline-exceeded"
                ) or "adapter-error",
            )
            store.write_result(result)
            return _cleanup(
                store,
                store.load_state(intent.attempt_id),
                backend,
                utc_now=utc_now,
                teardown_seconds=None if deadlines is None else deadlines.teardown_seconds,
                monotonic=monotonic,
            )

        setup_start = float(monotonic())
        setup_seconds: float | None = None
        setup_failure_category = "adapter-error"
        agent: object | None = None
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
            setup = backend.setup(
                intent,
                requirements,
                timeout_seconds=_remaining(
                    setup_start,
                    None if deadlines is None else deadlines.setup_seconds,
                    monotonic,
                ),
            )
            if type(setup) is not SetupObservation:
                raise SingleTaskExecutionError("setup returned an untyped observation")
            equivalence = setup.initial_resource_equivalence_sha256
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
            agent = backend.start_agent(
                intent,
                timeout_seconds=_remaining(
                    setup_start,
                    None if deadlines is None else deadlines.setup_seconds,
                    monotonic,
                ),
            )
            if agent is None:
                raise SingleTaskExecutionError("agent start returned no handle")
            setup_seconds = _duration(setup_start, monotonic)
            if deadlines is not None and setup_seconds > deadlines.setup_seconds:
                raise TimeoutError
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                setup_failure_category = "deadline-exceeded"
            setup_seconds = (
                _duration(setup_start, monotonic)
                if setup_seconds is None
                else setup_seconds
            )
            timings = replace(timings, setup_seconds=setup_seconds)
            teardown_remaining = None if deadlines is None else float(deadlines.teardown_seconds)
            failure_stage = "setup"
            if agent is not None:
                stop_failure, teardown_remaining = _stop_agent(
                    backend,
                    agent,
                    teardown_seconds=teardown_remaining,
                    monotonic=monotonic,
                )
                if stop_failure is not None:
                    failure_stage, setup_failure_category = stop_failure
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
                stage=failure_stage,
                category=setup_failure_category,
            )
            store.write_result(result)
            return _cleanup(
                store,
                store.load_state(intent.attempt_id),
                backend,
                utc_now=utc_now,
                teardown_seconds=teardown_remaining,
                monotonic=monotonic,
            )
        if setup_seconds is None:
            raise SingleTaskExecutionError("setup timing is missing after successful execution")
        timings = replace(timings, setup_seconds=setup_seconds)

        agent_start = float(monotonic())
        observation: AgentObservation | None = None
        agent_failure: tuple[str, str] | None = None
        agent_run_started = True
        try:
            budget = intent.identity.budget
            observation = backend.run_agent(
                agent,
                step_limit=budget.step_limit,
                wall_time_limit_seconds=budget.wall_time_limit_seconds,
                provider_call_limit=budget.provider_call_limit,
                output_token_limit=budget.output_token_limit,
            )
            if type(observation) is not AgentObservation:
                raise SingleTaskExecutionError("agent returned an untyped observation")
        except Exception:
            agent_failure = ("agent", "adapter-error")
        agent_seconds = _duration(agent_start, monotonic)
        if observation is not None and agent_failure is None:
            if not _usage_within_budget(observation, execution_contract):
                agent_failure = ("agent", "budget-contract-violation")
            elif (
                execution_contract is not None
                and agent_seconds > execution_contract.budget.wall_time_limit_seconds
                and observation.exit_status != "TimeExceeded"
            ):
                agent_failure = ("agent", "budget-contract-violation")
        timings = replace(timings, agent_seconds=agent_seconds)

        stop_failure, teardown_remaining = _stop_agent(
            backend,
            agent,
            teardown_seconds=None if deadlines is None else float(deadlines.teardown_seconds),
            monotonic=monotonic,
        )
        if stop_failure is not None:
            agent_failure = stop_failure

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
            return _cleanup(
                store,
                store.load_state(intent.attempt_id),
                backend,
                utc_now=utc_now,
                teardown_seconds=teardown_remaining,
                monotonic=monotonic,
            )

        if observation is None:
            raise SingleTaskExecutionError("agent observation is missing after successful execution")

        grading_start = float(monotonic())
        grading_seconds: float | None = None
        try:
            grade = backend.grade(
                intent,
                timeout_seconds=_remaining(
                    grading_start,
                    None if deadlines is None else deadlines.grading_seconds,
                    monotonic,
                ),
            )
            if type(grade) is not TaskGrade or grade.max_score != max_score:
                raise SingleTaskExecutionError("grader returned an invalid Task grade")
            if grade.status == "not_scored":
                raise SingleTaskExecutionError("grader returned no correctness observation")
            validate_public_artifact_values(grade.to_dict())
        except Exception as exc:
            grading_seconds = _duration(grading_start, monotonic)
            timings = replace(timings, grading_seconds=grading_seconds)
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
                category=(
                    "deadline-exceeded" if isinstance(exc, TimeoutError) else "adapter-error"
                ),
            )
            store.write_result(result)
            return _cleanup(
                store,
                store.load_state(intent.attempt_id),
                backend,
                utc_now=utc_now,
                teardown_seconds=teardown_remaining,
                monotonic=monotonic,
            )

        try:
            violated = backend.protocol_violated(
                intent,
                timeout_seconds=_remaining(
                    grading_start,
                    None if deadlines is None else deadlines.grading_seconds,
                    monotonic,
                ),
            )
            if type(violated) is not bool:
                raise SingleTaskExecutionError("protocol check returned a non-boolean result")
        except Exception as exc:
            grading_seconds = _duration(grading_start, monotonic)
            timings = replace(timings, grading_seconds=grading_seconds)
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
                category=(
                    "deadline-exceeded" if isinstance(exc, TimeoutError) else "adapter-error"
                ),
            )
            store.write_result(result)
            return _cleanup(
                store,
                store.load_state(intent.attempt_id),
                backend,
                utc_now=utc_now,
                teardown_seconds=teardown_remaining,
                monotonic=monotonic,
            )

        grading_seconds = _duration(grading_start, monotonic)
        if deadlines is not None and grading_seconds > deadlines.grading_seconds:
            timings = replace(timings, grading_seconds=grading_seconds)
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
                category="deadline-exceeded",
            )
            store.write_result(result)
            return _cleanup(
                store,
                store.load_state(intent.attempt_id),
                backend,
                utc_now=utc_now,
                teardown_seconds=teardown_remaining,
                monotonic=monotonic,
            )

        timings = replace(timings, grading_seconds=grading_seconds)
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
        return _cleanup(
            store,
            store.load_state(intent.attempt_id),
            backend,
            utc_now=utc_now,
            teardown_seconds=teardown_remaining,
            monotonic=monotonic,
        )


def recover_single_task(
    store: AttemptStore,
    attempt_id: str,
    requirements: TaskPreflightRequirements,
    backend: SingleTaskBackend,
    *,
    max_score: int,
    execution_contract: TaskExecutionContract | None = None,
    utc_now: UtcNow = _utc_now,
    monotonic: Monotonic = time.monotonic,
) -> AttemptEnvelope:
    """Seal and clean an interrupted attempt without running preflight, setup, agent, or grade."""
    if isinstance(max_score, bool) or not isinstance(max_score, int) or max_score <= 0:
        raise SingleTaskExecutionError("max_score must be a positive integer")
    with _serialized(store):
        state = store.load_state(attempt_id)
        _validate_execution_contract(state.intent, execution_contract)
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

        return _cleanup(
            store,
            store.load_state(attempt_id),
            backend,
            utc_now=utc_now,
            teardown_seconds=(
                None
                if execution_contract is None
                else execution_contract.harness_deadlines.teardown_seconds
            ),
            monotonic=monotonic,
        )
