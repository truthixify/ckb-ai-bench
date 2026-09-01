from __future__ import annotations

import fcntl
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ckbbench.run.attempt_store import AttemptStore
from ckbbench.run.model_profile import model_variant_id
from ckbbench.run.task_preflight import (
    QUALIFICATION_KIND,
    READINESS_OPERATION,
    CkbAiObservation,
    DependencyObservation,
    OutputObservation,
    TaskPreflightRequirements,
    ProviderObservation,
    SourceObservation,
    run_task_preflight,
)
from ckbbench.run.single_task import (
    AgentInfrastructureFailure,
    AgentObservation,
    SetupObservation,
    SingleTaskExecutionError,
    SingleTaskExecutorBusy,
    execute_single_task,
    recover_single_task,
)
from ckbbench.run.task_attempt import (
    VERIFIER_PRIVATE_COMMITMENT_SCHEME,
    AttemptIdentity,
    AttemptUsage,
    ExecutionSource,
    OwnershipJournalEntry,
    TaskAttemptIntent,
    TaskBudget,
    TaskGrade,
    artifact_sha256,
)
from ckbbench.suite.execution_contract import (
    BudgetCalibration,
    HarnessDeadlines,
    TaskBudgetProfile,
    TaskExecutionContract,
    TreatmentRequirement,
)

MODEL = "provider/synthetic-model"
PROFILE = "model-profile-synthetic-v1"
PROFILE_SHA = "1" * 64


class Clock:
    def __init__(self) -> None:
        self.second = 0
        self.tick = 0.0

    def utc(self) -> str:
        self.second += 1
        return f"2026-09-01T00:00:{self.second:02d}Z"

    def monotonic(self) -> float:
        self.tick += 0.25
        return self.tick


class ManualClock:
    def __init__(self) -> None:
        self.tick = 0.0

    def monotonic(self) -> float:
        return self.tick

    def advance(self, seconds: float) -> None:
        self.tick += seconds


def _intent(suffix: str = "a", *, arm: str = "B") -> TaskAttemptIntent:
    profile_id = PROFILE
    identity = AttemptIdentity(
        campaign_id="campaign-single-task-v1",
        campaign_manifest_sha256="2" * 64,
        batch_id="batch-1",
        execution_plan_id="plan-v1",
        execution_plan_sha256="3" * 64,
        trial_id=f"trial-{suffix}",
        suite_semver="3.0.0",
        suite_freeze_sha256="4" * 64,
        task_id="task-read-tip",
        task_content_sha256="5" * 64,
        arm=arm,  # type: ignore[arg-type]
        treatment_profile_id="web-only-v1" if arm == "B" else "ckb-ai-v1",
        treatment_profile_sha256="6" * 64,
        chain_track="local-hermetic",
        chain_profile_id="local-hermetic-v1",
        chain_profile_sha256="7" * 64,
        requested_model=MODEL,
        thinking_level="high",
        model_variant_id=model_variant_id(
            requested_model=MODEL,
            thinking_level="high",
            profile_id=profile_id,
            profile_sha256=PROFILE_SHA,
        ),
        model_profile_id=profile_id,
        model_profile_sha256=PROFILE_SHA,
        budget=TaskBudget(
            profile_id="read-tip-budget-v1",
            profile_sha256="8" * 64,
            step_limit=40,
            wall_time_limit_seconds=900,
            provider_call_limit=80,
            output_token_limit=None,
        ),
        trial_challenge_id=f"challenge-{suffix}",
        trial_challenge_sha256="9" * 64,
        run_params_derivation="task-run-params-v1",
        prompt_params_sha256=("a" if suffix == "a" else "b") * 64,
        verifier_private_commitment_scheme=VERIFIER_PRIVATE_COMMITMENT_SCHEME,
        verifier_private_commitment_sha256=("c" if suffix == "a" else "d") * 64,
        resource_equivalence_policy_id="local-equivalence-v1",
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
    return TaskAttemptIntent(
        attempt_id="attempt-" + suffix * 32,
        created_utc="2026-09-01T00:00:00Z",
        identity=identity,
    )


def _requirements(intent: TaskAttemptIntent, suffix: str = "a") -> TaskPreflightRequirements:
    claims = (
        ("runtime-name", f"runtime-{suffix}"),
        ("workspace", f"workspace-{suffix}"),
    )
    return TaskPreflightRequirements(
        requirements_id=f"requirements-{suffix}-v1",
        intent_sha256=intent.sha256,
        model_qualification_kind=QUALIFICATION_KIND,
        model_qualification_evidence_sha256="6" * 64,
        model_qualification_utc="2026-09-01T00:00:00Z",
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


class Probe:
    def __init__(self, intent: TaskAttemptIntent, requirements: TaskPreflightRequirements) -> None:
        self.calls: list[str] = []
        self.timeouts: list[tuple[str, float | None]] = []
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
        value = getattr(self, f"{name}_value")
        if isinstance(value, Exception):
            raise value
        return value

    def source(self, *, timeout_seconds: float | None) -> SourceObservation:
        self.timeouts.append(("source", timeout_seconds))
        return self._read("source")

    def provider(self, *, timeout_seconds: float | None) -> ProviderObservation:
        self.timeouts.append(("provider", timeout_seconds))
        return self._read("provider")

    def ckb_ai(self, *, timeout_seconds: float | None) -> CkbAiObservation:
        self.timeouts.append(("ckb_ai", timeout_seconds))
        return self._read("ckb_ai")

    def dependencies(self, *, timeout_seconds: float | None) -> DependencyObservation:
        self.timeouts.append(("dependencies", timeout_seconds))
        return self._read("dependencies")

    def outputs(self, *, timeout_seconds: float | None) -> OutputObservation:
        self.timeouts.append(("outputs", timeout_seconds))
        return self._read("outputs")

    def rpc(self, *, timeout_seconds: float | None) -> Any:
        raise AssertionError("local attempt must not call RPC")

    def signer(self, *, timeout_seconds: float | None) -> Any:
        raise AssertionError("local attempt must not call signer")

    def funding(self, *, timeout_seconds: float | None) -> Any:
        raise AssertionError("local attempt must not call funding")


def _usage() -> AttemptUsage:
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
        provider_response_model_counts=((MODEL, 1),),
    )


class Backend:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.fail: str | None = None
        self.verifier_passed = True
        self.violation = False
        self.no_handle = False
        self.unsafe_usage = False
        self.unsafe_grade = False
        self.cleanup_value: object = "released"
        self.cleanup_failures: dict[tuple[str, str], int] = {}
        self.cleanup_crashes: dict[tuple[str, str], int] = {}
        self.handles: list[object] = []
        self.received_timeouts: list[tuple[str, float | None]] = []
        self.received_agent_limits: tuple[int, int, int | None, int | None] | None = None
        self.usage = _usage()
        self.exit_status = "submitted"
        self.clock: ManualClock | None = None
        self.elapsed_by_stage: dict[str, float] = {}

    def _elapse(self, stage: str) -> None:
        if self.clock is not None:
            self.clock.advance(self.elapsed_by_stage.get(stage, 0.0))

    def _fail(self, stage: str) -> None:
        if self.fail == stage:
            raise RuntimeError("sk-live-secret-must-not-survive")

    def setup(
        self,
        _intent: TaskAttemptIntent,
        _requirements: TaskPreflightRequirements,
        *,
        timeout_seconds: float | None,
    ) -> SetupObservation:
        self.events.append("setup")
        self.received_timeouts.append(("setup", timeout_seconds))
        self._elapse("setup")
        self._fail("setup")
        return SetupObservation("9" * 64)

    def start_agent(
        self,
        _intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> object:
        self.events.append("start")
        self.received_timeouts.append(("start", timeout_seconds))
        self._elapse("start")
        self._fail("start")
        if self.no_handle:
            return None  # type: ignore[return-value]
        handle = object()
        self.handles.append(handle)
        return handle

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
        self.received_agent_limits = (
            step_limit,
            wall_time_limit_seconds,
            provider_call_limit,
            output_token_limit,
        )
        self._elapse("run")
        self._fail("run")
        usage = self.usage
        if self.unsafe_usage:
            usage = replace(
                usage,
                provider_response_model_counts=(("sk-live-secret-value", 1),),
            )
        return AgentObservation(self.exit_status, usage)

    def stop_agent_checked(
        self,
        _agent: object,
        *,
        timeout_seconds: float | None,
    ) -> None:
        self.events.append("stop")
        self.received_timeouts.append(("stop", timeout_seconds))
        self._elapse("stop")
        self._fail("stop")

    def grade(
        self,
        _intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> TaskGrade:
        self.events.append("grade")
        self.received_timeouts.append(("grade", timeout_seconds))
        self._elapse("grade")
        self._fail("grade")
        if self.unsafe_grade:
            return TaskGrade("passed", 10, 10, 10, "Bearer private-value", "proof")
        if self.verifier_passed:
            return TaskGrade("passed", 10, 10, 10, "Verifier passed.", "proof")
        return TaskGrade("failed", 0, 0, 10, "Verifier failed.", "")

    def protocol_violated(
        self,
        _intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> bool:
        self.events.append("protocol")
        self.received_timeouts.append(("protocol", timeout_seconds))
        self._elapse("protocol")
        self._fail("protocol")
        return self.violation

    def cleanup_resource(
        self,
        _intent: TaskAttemptIntent,
        kind: str,
        resource_id: str,
        *,
        timeout_seconds: float | None,
    ) -> str:
        resource = kind, resource_id
        self.events.append(f"cleanup:{kind}:{resource_id}")
        self.received_timeouts.append((f"cleanup:{kind}", timeout_seconds))
        self._elapse(f"cleanup:{kind}")
        crashes = self.cleanup_crashes.get(resource, 0)
        if crashes:
            self.cleanup_crashes[resource] = crashes - 1
            raise SimulatedProcessDeath
        remaining = self.cleanup_failures.get(resource, 0)
        if remaining:
            self.cleanup_failures[resource] = remaining - 1
            raise RuntimeError("private cleanup error")
        return self.cleanup_value  # type: ignore[return-value]


class SimulatedProcessDeath(BaseException):
    pass


def _execute(tmp_path: Path, backend: Backend | None = None):
    intent = _intent()
    requirements = _requirements(intent)
    selected = backend or Backend()
    clock = Clock()
    envelope = execute_single_task(
        AttemptStore(tmp_path / "attempts"),
        intent,
        requirements,
        Probe(intent, requirements),
        selected,
        max_score=10,
        utc_now=clock.utc,
        monotonic=clock.monotonic,
    )
    return envelope, selected, requirements


def _execution_contract() -> TaskExecutionContract:
    return TaskExecutionContract(
        contract_id="local-read-tip-execution-v1",
        chain_track="local-hermetic",
        chain_profile_id="local-hermetic-v1",
        chain_profile_sha256="7" * 64,
        budget=TaskBudgetProfile(
            profile_id="read-tip-budget-v1",
            step_limit=40,
            wall_time_limit_seconds=900,
            provider_call_limit=80,
            output_token_limit=None,
        ),
        harness_deadlines=HarnessDeadlines(120, 120, 180, 120),
        treatment=TreatmentRequirement(
            requirement_id="local-docs-v1",
            claims_live_chain=False,
            required_tools=(),
            required_resource_prefixes=("ckb://docs/",),
        ),
        signer_required=False,
        signing_policy_id=None,
        funding=None,
        required_dependencies=(),
        required_resource_kinds=("runtime-name", "workspace"),
        expected_output_resource_kinds=("runtime-name", "workspace"),
        run_params_derivation="task-run-params-v1",
        resource_equivalence_policy_id="local-equivalence-v1",
        calibration=BudgetCalibration(
            status="owner-approved-exception",
            evidence_sha256s=("f" * 64,),
            observed_max_steps=None,
            observed_max_wall_seconds=None,
            observed_max_provider_calls=None,
        ),
    )


def _intent_for_contract(contract: TaskExecutionContract) -> TaskAttemptIntent:
    original = _intent()
    budget = original.identity.budget
    identity = replace(
        original.identity,
        suite_semver="4.0.0",
        chain_track=contract.chain_track,
        chain_profile_id=contract.chain_profile_id,
        chain_profile_sha256=contract.chain_profile_sha256,
        run_params_derivation=contract.run_params_derivation,
        resource_equivalence_policy_id=contract.resource_equivalence_policy_id,
        resource_equivalence_policy_sha256=contract.resource_equivalence_policy_sha256,
        budget=replace(
            budget,
            profile_id=contract.budget.profile_id,
            profile_sha256=contract.budget.sha256,
            step_limit=contract.budget.step_limit,
            wall_time_limit_seconds=contract.budget.wall_time_limit_seconds,
            provider_call_limit=contract.budget.provider_call_limit,
            output_token_limit=contract.budget.output_token_limit,
        ),
    )
    return replace(original, identity=identity)


def test_pass_runs_one_agent_stops_before_grading_and_cleans_every_claim(tmp_path: Path):
    envelope, backend, requirements = _execute(tmp_path)

    assert envelope.result.outcome == "pass"
    assert envelope.result.grade.score_awarded == 10
    assert envelope.result.usage.total_tokens == 12
    assert envelope.receipts[-1].status == "complete"
    assert len(backend.handles) == 1
    assert backend.events.index("stop") < backend.events.index("grade")
    assert [event for event in backend.events if event.startswith("cleanup:")] == [
        f"cleanup:{kind}:{resource_id}"
        for kind, resource_id in requirements.required_resource_claims
    ]
    assert [entry.action for entry in envelope.journal[:2]] == ["claim", "claim"]
    assert all(entry.phase == "teardown" for entry in envelope.journal[6:])


def test_release_bound_execution_passes_exact_limits_to_every_adapter(tmp_path: Path):
    contract = _execution_contract()
    intent = _intent_for_contract(contract)
    requirements = _requirements(intent)
    probe = Probe(intent, requirements)
    backend = Backend()
    clock = Clock()

    envelope = execute_single_task(
        AttemptStore(tmp_path / "attempts"),
        intent,
        requirements,
        probe,
        backend,
        max_score=10,
        execution_contract=contract,
        utc_now=clock.utc,
        monotonic=clock.monotonic,
    )

    assert envelope.result.outcome == "pass"
    assert backend.received_agent_limits == (40, 900, 80, None)
    assert [name for name, _timeout in probe.timeouts] == [
        "source",
        "provider",
        "ckb_ai",
        "dependencies",
        "outputs",
    ]
    assert all(timeout is not None and 0 < timeout <= 120 for _name, timeout in probe.timeouts)
    timeout_by_stage = dict(backend.received_timeouts)
    assert 0 < timeout_by_stage["setup"] <= 120
    assert 0 < timeout_by_stage["start"] <= 120
    assert 0 < timeout_by_stage["grade"] <= 180
    assert 0 < timeout_by_stage["protocol"] <= 180
    assert 0 < timeout_by_stage["stop"] <= 120
    assert 0 < timeout_by_stage["cleanup:runtime-name"] <= 120
    assert 0 < timeout_by_stage["cleanup:workspace"] <= 120


def test_release_bound_execution_refuses_budget_drift_before_reserving_resources(tmp_path: Path):
    contract = _execution_contract()
    exact = _intent_for_contract(contract)
    intent = replace(
        exact,
        identity=replace(
            exact.identity,
            budget=replace(exact.identity.budget, step_limit=41),
        ),
    )
    requirements = _requirements(intent)
    probe = Probe(intent, requirements)
    backend = Backend()
    store = AttemptStore(tmp_path / "attempts")

    with pytest.raises(SingleTaskExecutionError, match="budget differs"):
        execute_single_task(
            store,
            intent,
            requirements,
            probe,
            backend,
            max_score=10,
            execution_contract=contract,
        )

    assert not store.root.exists()
    assert probe.calls == []
    assert backend.events == []


@pytest.mark.parametrize(
    "field,value",
    (
        ("chain_profile_id", "other-local-profile-v1"),
        ("chain_profile_sha256", "0" * 64),
        ("run_params_derivation", "other-params-v1"),
        ("resource_equivalence_policy_id", "other-equivalence-v1"),
        ("resource_equivalence_policy_sha256", "1" * 64),
    ),
)
def test_release_bound_execution_refuses_identity_drift_before_storage(
    tmp_path: Path,
    field: str,
    value: str,
):
    contract = _execution_contract()
    intent = _intent_for_contract(contract)
    intent = replace(intent, identity=replace(intent.identity, **{field: value}))
    requirements = _requirements(intent)
    store = AttemptStore(tmp_path / "attempts")

    with pytest.raises(SingleTaskExecutionError, match="identity differs"):
        execute_single_task(
            store,
            intent,
            requirements,
            Probe(intent, requirements),
            Backend(),
            max_score=10,
            execution_contract=contract,
        )

    assert not store.root.exists()


def test_new_suite_execution_and_recovery_cannot_omit_or_replace_the_contract(tmp_path: Path):
    contract = _execution_contract()
    intent = _intent_for_contract(contract)
    requirements = _requirements(intent)
    store = AttemptStore(tmp_path / "attempts")

    with pytest.raises(SingleTaskExecutionError, match="requires its execution contract"):
        execute_single_task(
            store,
            intent,
            requirements,
            Probe(intent, requirements),
            Backend(),
            max_score=10,
        )
    assert not store.root.exists()

    clock = Clock()
    envelope = execute_single_task(
        store,
        intent,
        requirements,
        Probe(intent, requirements),
        Backend(),
        max_score=10,
        execution_contract=contract,
        utc_now=clock.utc,
        monotonic=clock.monotonic,
    )
    drifted = replace(
        contract,
        budget=replace(contract.budget, profile_id="drifted-budget-v1"),
    )
    with pytest.raises(SingleTaskExecutionError, match="budget differs"):
        recover_single_task(
            store,
            envelope.intent.attempt_id,
            requirements,
            Backend(),
            max_score=10,
            execution_contract=drifted,
        )


def test_usage_beyond_a_released_agent_ceiling_is_terminal_infrastructure_evidence(
    tmp_path: Path,
):
    contract = _execution_contract()
    intent = _intent_for_contract(contract)
    requirements = _requirements(intent)
    backend = Backend()
    backend.usage = replace(
        _usage(),
        model_calls=41,
        provider_attempts=41,
        provider_responses=41,
        provider_response_model_counts=((MODEL, 41),),
    )
    clock = Clock()

    envelope = execute_single_task(
        AttemptStore(tmp_path / "attempts"),
        intent,
        requirements,
        Probe(intent, requirements),
        backend,
        max_score=10,
        execution_contract=contract,
        utc_now=clock.utc,
        monotonic=clock.monotonic,
    )

    assert envelope.result.outcome == "infra_fail"
    assert (envelope.result.failure_stage, envelope.result.failure_category) == (
        "agent",
        "budget-contract-violation",
    )
    assert "grade" not in backend.events
    assert envelope.receipts[-1].status == "complete"


def test_setup_includes_agent_start_and_teardown_shares_one_deadline(tmp_path: Path):
    contract = _execution_contract()
    intent = _intent_for_contract(contract)
    requirements = _requirements(intent)
    backend = Backend()
    monotonic = ManualClock()
    backend.clock = monotonic
    backend.elapsed_by_stage = {
        "setup": 10,
        "start": 20,
        "run": 30,
        "stop": 5,
        "cleanup:runtime-name": 10,
    }
    utc = Clock()

    envelope = execute_single_task(
        AttemptStore(tmp_path / "attempts"),
        intent,
        requirements,
        Probe(intent, requirements),
        backend,
        max_score=10,
        execution_contract=contract,
        utc_now=utc.utc,
        monotonic=monotonic.monotonic,
    )

    assert envelope.result.outcome == "pass"
    assert envelope.result.timings.setup_seconds == 30
    assert envelope.result.timings.agent_seconds == 30
    timeout_by_stage = dict(backend.received_timeouts)
    assert timeout_by_stage["setup"] == 120
    assert timeout_by_stage["start"] == 110
    assert timeout_by_stage["stop"] == 120
    assert timeout_by_stage["cleanup:runtime-name"] == 115
    assert timeout_by_stage["cleanup:workspace"] == 105


def test_agent_start_exceeding_setup_deadline_is_stopped_and_not_run(tmp_path: Path):
    contract = _execution_contract()
    intent = _intent_for_contract(contract)
    requirements = _requirements(intent)
    backend = Backend()
    monotonic = ManualClock()
    backend.clock = monotonic
    backend.elapsed_by_stage = {"setup": 100, "start": 21}
    utc = Clock()

    envelope = execute_single_task(
        AttemptStore(tmp_path / "attempts"),
        intent,
        requirements,
        Probe(intent, requirements),
        backend,
        max_score=10,
        execution_contract=contract,
        utc_now=utc.utc,
        monotonic=monotonic.monotonic,
    )

    assert (envelope.result.outcome, envelope.result.failure_stage) == (
        "infra_fail",
        "setup",
    )
    assert envelope.result.failure_category == "deadline-exceeded"
    assert envelope.result.timings.setup_seconds == 121
    assert "stop" in backend.events
    assert "run" not in backend.events
    assert "grade" not in backend.events


@pytest.mark.parametrize("exit_status", ["LimitsExceeded", "TimeExceeded"])
def test_an_agent_stopped_at_its_released_limit_is_still_graded(
    tmp_path: Path,
    exit_status: str,
):
    contract = _execution_contract()
    intent = _intent_for_contract(contract)
    requirements = _requirements(intent)
    backend = Backend()
    backend.exit_status = exit_status
    backend.usage = replace(
        _usage(),
        model_calls=40,
        provider_attempts=40,
        provider_responses=40,
        provider_response_model_counts=((MODEL, 40),),
    )
    monotonic = ManualClock()
    backend.clock = monotonic
    if exit_status == "TimeExceeded":
        backend.elapsed_by_stage["run"] = 901
    utc = Clock()

    envelope = execute_single_task(
        AttemptStore(tmp_path / exit_status / "attempts"),
        intent,
        requirements,
        Probe(intent, requirements),
        backend,
        max_score=10,
        execution_contract=contract,
        utc_now=utc.utc,
        monotonic=monotonic.monotonic,
    )

    assert envelope.result.outcome == "pass"
    assert envelope.result.agent_exit_status == exit_status
    assert envelope.result.correctness_eligible
    assert "grade" in backend.events


def test_output_token_overrun_is_a_terminal_contract_violation(tmp_path: Path):
    base = _execution_contract()
    contract = replace(
        base,
        budget=replace(base.budget, output_token_limit=1),
    )
    intent = _intent_for_contract(contract)
    requirements = _requirements(intent)
    backend = Backend()
    utc = Clock()

    envelope = execute_single_task(
        AttemptStore(tmp_path / "attempts"),
        intent,
        requirements,
        Probe(intent, requirements),
        backend,
        max_score=10,
        execution_contract=contract,
        utc_now=utc.utc,
        monotonic=utc.monotonic,
    )

    assert (envelope.result.failure_stage, envelope.result.failure_category) == (
        "agent",
        "budget-contract-violation",
    )
    assert "grade" not in backend.events


def test_teardown_deadline_leaves_recoverable_cleanup_evidence(tmp_path: Path):
    contract = _execution_contract()
    intent = _intent_for_contract(contract)
    requirements = _requirements(intent)
    backend = Backend()
    monotonic = ManualClock()
    backend.clock = monotonic
    backend.elapsed_by_stage = {
        "stop": 119,
        "cleanup:runtime-name": 2,
    }
    utc = Clock()
    store = AttemptStore(tmp_path / "attempts")

    incomplete = execute_single_task(
        store,
        intent,
        requirements,
        Probe(intent, requirements),
        backend,
        max_score=10,
        execution_contract=contract,
        utc_now=utc.utc,
        monotonic=monotonic.monotonic,
    )

    assert incomplete.result.outcome == "pass"
    assert incomplete.receipts[-1].status == "incomplete"
    assert any(
        entry.details_sha256 == artifact_sha256(
            {"category": "deadline-exceeded", "stage": "cleanup"}
        )
        for entry in incomplete.journal
    )

    backend.elapsed_by_stage.clear()
    recovered = recover_single_task(
        store,
        intent.attempt_id,
        requirements,
        backend,
        max_score=10,
        execution_contract=contract,
        utc_now=utc.utc,
        monotonic=monotonic.monotonic,
    )
    assert [receipt.status for receipt in recovered.receipts] == ["incomplete", "complete"]


def test_verifier_failure_and_protocol_violation_remain_scored(tmp_path: Path):
    failed_backend = Backend()
    failed_backend.verifier_passed = False
    failed, _backend, _requirements_row = _execute(tmp_path / "failed", failed_backend)
    assert (failed.result.outcome, failed.result.grade.status) == ("agent_fail", "failed")
    assert failed.result.correctness_eligible

    violation_backend = Backend()
    violation_backend.violation = True
    violated, _backend, _requirements_row = _execute(tmp_path / "violated", violation_backend)
    assert violated.result.outcome == "protocol_violation"
    assert violated.result.grade.verifier_score == 10
    assert violated.result.grade.score_awarded == 0


def test_preflight_failure_skips_setup_agent_and_grading(tmp_path: Path):
    intent = _intent()
    requirements = _requirements(intent)
    probe = Probe(intent, requirements)
    probe.source_value = replace(probe.source_value, tracked_change_count=1)
    backend = Backend()
    clock = Clock()

    envelope = execute_single_task(
        AttemptStore(tmp_path / "attempts"),
        intent,
        requirements,
        probe,
        backend,
        max_score=10,
        utc_now=clock.utc,
        monotonic=clock.monotonic,
    )

    assert envelope.result.outcome == "infra_fail"
    assert envelope.result.usage.token_usage_status == "not_started"
    assert envelope.result.failure_stage == "source"
    assert not {"setup", "start", "run", "stop", "grade", "protocol"} & set(backend.events)
    assert envelope.receipts[-1].status == "complete"


@pytest.mark.parametrize(
    ("stage", "expected_failure_stage", "usage_status", "graded"),
    (
        ("setup", "setup", "not_started", False),
        ("start", "setup", "not_started", False),
        ("run", "agent", "unavailable", False),
        ("stop", "stop", "complete", False),
        ("grade", "grading", "complete", True),
        ("protocol", "protocol", "complete", True),
    ),
)
def test_adapter_failures_seal_infrastructure_evidence_and_cleanup(
    tmp_path: Path,
    stage: str,
    expected_failure_stage: str,
    usage_status: str,
    graded: bool,
):
    backend = Backend()
    backend.fail = stage

    envelope, backend, _requirements_row = _execute(tmp_path, backend)

    assert envelope.result.outcome == "infra_fail"
    assert envelope.result.failure_stage == expected_failure_stage
    assert envelope.result.failure_category == "adapter-error"
    assert envelope.result.usage.token_usage_status == usage_status
    assert ("grade" in backend.events) is graded
    assert envelope.receipts[-1].status == "complete"
    retained = b"".join(path.read_bytes() for path in (tmp_path / "attempts").rglob("*.json"))
    assert b"sk-live-secret-must-not-survive" not in retained


def test_agent_infrastructure_failure_retains_sanitized_usage_before_cleanup(
    tmp_path: Path,
):
    class FailingAgentBackend(Backend):
        def run_agent(self, agent: object, **limits) -> AgentObservation:
            observation = super().run_agent(agent, **limits)
            raise AgentInfrastructureFailure(observation)

    envelope, backend, _requirements_row = _execute(tmp_path, FailingAgentBackend())

    assert envelope.result.outcome == "infra_fail"
    assert envelope.result.failure_stage == "agent"
    assert envelope.result.failure_category == "adapter-error"
    assert envelope.result.usage == _usage()
    assert "stop" in backend.events
    assert "grade" not in backend.events
    assert envelope.receipts[-1].status == "complete"


def test_cleanup_failure_is_reconciled_without_rewriting_the_result(tmp_path: Path):
    backend = Backend()
    runtime = ("runtime-name", "runtime-a")
    backend.cleanup_failures[runtime] = 1
    envelope, backend, requirements = _execute(tmp_path, backend)
    original_result = envelope.result

    assert envelope.receipts[-1].status == "incomplete"
    recovery_clock = Clock()
    recovery_clock.second = 30
    recovered = recover_single_task(
        AttemptStore(tmp_path / "attempts"),
        envelope.intent.attempt_id,
        requirements,
        backend,
        max_score=10,
        utc_now=recovery_clock.utc,
    )

    assert recovered.result == original_result
    assert [receipt.status for receipt in recovered.receipts] == ["incomplete", "complete"]
    assert recovered.receipts[1].prior_receipt_sha256 == recovered.receipts[0].sha256


def test_recovery_after_result_publication_finishes_interrupted_cleanup(tmp_path: Path):
    backend = Backend()
    runtime = ("runtime-name", "runtime-a")
    backend.cleanup_crashes[runtime] = 1

    with pytest.raises(SimulatedProcessDeath):
        _execute(tmp_path, backend)

    store = AttemptStore(tmp_path / "attempts")
    state = store.load_state(_intent().attempt_id)
    assert state.result is not None
    assert state.receipts == ()
    assert state.journal[-1].action == "release-intent"
    original_result = state.result
    backend.events.clear()

    recovered = recover_single_task(
        store,
        state.intent.attempt_id,
        _requirements(state.intent),
        backend,
        max_score=10,
    )

    assert recovered.result == original_result
    assert recovered.receipts[-1].status == "complete"
    assert not {"setup", "start", "run", "stop", "grade", "protocol"} & set(backend.events)


def test_recovery_seals_an_interrupted_reconciliation_before_retrying_it(tmp_path: Path):
    backend = Backend()
    runtime = ("runtime-name", "runtime-a")
    backend.cleanup_failures[runtime] = 1
    envelope, backend, requirements = _execute(tmp_path, backend)
    store = AttemptStore(tmp_path / "attempts")
    clock = Clock()
    clock.second = 30
    state = store.load_state(envelope.intent.attempt_id)

    release_intent = OwnershipJournalEntry(
        attempt_id=state.intent.attempt_id,
        intent_sha256=state.intent.sha256,
        sequence=len(state.journal),
        created_utc=clock.utc(),
        phase="reconcile",
        action="release-intent",
        resource_kind=runtime[0],
        resource_id=runtime[1],
        details_sha256=None,
        previous_entry_sha256=state.journal[-1].sha256,
    )
    store.append_journal(release_intent)
    cleanup_failed = OwnershipJournalEntry(
        attempt_id=state.intent.attempt_id,
        intent_sha256=state.intent.sha256,
        sequence=len(state.journal) + 1,
        created_utc=clock.utc(),
        phase="reconcile",
        action="cleanup-failed",
        resource_kind=runtime[0],
        resource_id=runtime[1],
        details_sha256=artifact_sha256({"category": "adapter-error", "stage": "cleanup"}),
        previous_entry_sha256=release_intent.sha256,
    )
    store.append_journal(cleanup_failed)
    backend.events.clear()

    sealed = recover_single_task(
        store,
        state.intent.attempt_id,
        requirements,
        backend,
        max_score=10,
        utc_now=clock.utc,
    )

    assert sealed.receipts[-1].status == "incomplete"
    assert not [event for event in backend.events if event.startswith("cleanup:")]

    recovered = recover_single_task(
        store,
        state.intent.attempt_id,
        requirements,
        backend,
        max_score=10,
        utc_now=clock.utc,
    )

    assert recovered.receipts[-1].status == "complete"
    assert [event for event in backend.events if event.startswith("cleanup:")] == [
        "cleanup:runtime-name:runtime-a"
    ]
    assert not {"setup", "start", "run", "stop", "grade", "protocol"} & set(backend.events)


def test_malformed_adapter_values_fail_closed_without_public_secret_retention(tmp_path: Path):
    cases = (
        ("no-handle", {"no_handle": True}, "setup", "not_started"),
        ("unsafe-usage", {"unsafe_usage": True}, "agent", "unavailable"),
        ("unsafe-grade", {"unsafe_grade": True}, "grading", "complete"),
    )
    for name, settings, stage, usage_status in cases:
        backend = Backend()
        for field, value in settings.items():
            setattr(backend, field, value)
        envelope, _backend, _requirements_row = _execute(tmp_path / name, backend)
        assert envelope.result.outcome == "infra_fail"
        assert envelope.result.failure_stage == stage
        assert envelope.result.usage.token_usage_status == usage_status
        retained = b"".join(
            path.read_bytes() for path in (tmp_path / name / "attempts").rglob("*.json")
        )
        assert b"sk-live-secret-value" not in retained
        assert b"Bearer private-value" not in retained

    invalid_cleanup = Backend()
    invalid_cleanup.cleanup_value = []
    incomplete, _backend, _requirements_row = _execute(tmp_path / "cleanup", invalid_cleanup)
    assert incomplete.result.outcome == "pass"
    assert incomplete.receipts[-1].status == "incomplete"


def _manual_append(
    store: AttemptStore,
    intent: TaskAttemptIntent,
    rows: tuple[OwnershipJournalEntry, ...],
    resource: tuple[str, str],
    action: str,
    clock: Clock,
) -> tuple[OwnershipJournalEntry, ...]:
    previous = rows[-1] if rows else None
    row = OwnershipJournalEntry(
        attempt_id=intent.attempt_id,
        intent_sha256=intent.sha256,
        sequence=len(rows),
        created_utc=clock.utc(),
        phase="reserve" if action == "claim" else "setup",
        action=action,
        resource_kind=resource[0],
        resource_id=resource[1],
        details_sha256=None,
        previous_entry_sha256=None if previous is None else previous.sha256,
    )
    store.append_journal(row)
    return (*rows, row)


@pytest.mark.parametrize(
    ("boundary", "usage_status", "failure_stage"),
    (
        ("intent", "not_started", "intent"),
        ("partial-claims", "not_started", "intent"),
        ("preflight", "not_started", "setup"),
        ("partial-setup", "not_started", "setup"),
        ("setup-complete", "unavailable", "agent"),
    ),
)
def test_recovery_seals_each_interrupted_prefix_without_resuming_work(
    tmp_path: Path,
    boundary: str,
    usage_status: str,
    failure_stage: str,
):
    intent = _intent()
    requirements = _requirements(intent)
    probe = Probe(intent, requirements)
    backend = Backend()
    store = AttemptStore(tmp_path / "attempts")
    clock = Clock()
    store.create_intent(intent)
    rows: tuple[OwnershipJournalEntry, ...] = ()

    if boundary != "intent":
        store.write_preflight_requirements(intent.attempt_id, requirements)
        rows = _manual_append(store, intent, rows, requirements.required_resource_claims[0], "claim", clock)
    if boundary not in {"intent", "partial-claims"}:
        rows = _manual_append(store, intent, rows, requirements.required_resource_claims[1], "claim", clock)
        evidence = run_task_preflight(
            intent,
            rows,
            requirements,
            probe,
            checked_utc=clock.utc(),
        )
        store.write_preflight_evidence(intent.attempt_id, evidence)
    if boundary in {"partial-setup", "setup-complete"}:
        for resource in requirements.required_resource_claims:
            rows = _manual_append(store, intent, rows, resource, "mutation-intent", clock)
        if boundary == "setup-complete":
            for resource in requirements.required_resource_claims:
                rows = _manual_append(store, intent, rows, resource, "acquired", clock)

    recovered = recover_single_task(
        store,
        intent.attempt_id,
        requirements,
        backend,
        max_score=10,
        utc_now=clock.utc,
    )

    assert recovered.result.outcome == "infra_fail"
    assert recovered.result.usage.token_usage_status == usage_status
    assert recovered.result.timings.measurement_status == "unavailable"
    assert recovered.result.failure_stage == failure_stage
    assert not {"setup", "start", "run", "stop", "grade", "protocol"} & set(backend.events)
    assert recovered.receipts[-1].status == "complete"


def test_recovery_of_a_complete_attempt_is_idempotent(tmp_path: Path):
    envelope, backend, requirements = _execute(tmp_path)
    event_count = len(backend.events)
    recovered = recover_single_task(
        AttemptStore(tmp_path / "attempts"),
        envelope.intent.attempt_id,
        requirements,
        backend,
        max_score=10,
    )
    assert recovered == envelope
    assert len(backend.events) == event_count


def test_recovery_refuses_drift_even_after_an_attempt_is_complete(tmp_path: Path):
    envelope, backend, requirements = _execute(tmp_path)
    store = AttemptStore(tmp_path / "attempts")

    with pytest.raises(SingleTaskExecutionError, match="requirements differ"):
        recover_single_task(
            store,
            envelope.intent.attempt_id,
            replace(requirements, requirements_id="requirements-drifted-v1"),
            backend,
            max_score=10,
        )
    with pytest.raises(SingleTaskExecutionError, match="maximum score differs"):
        recover_single_task(
            store,
            envelope.intent.attempt_id,
            requirements,
            backend,
            max_score=20,
        )


def test_sequential_attempts_use_distinct_agents_and_resources(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    backend = Backend()
    clock = Clock()
    envelopes = []
    for suffix in ("a", "b"):
        intent = _intent(suffix)
        requirements = _requirements(intent, suffix)
        envelopes.append(
            execute_single_task(
                store,
                intent,
                requirements,
                Probe(intent, requirements),
                backend,
                max_score=10,
                utc_now=clock.utc,
                monotonic=clock.monotonic,
            )
        )

    assert len(backend.handles) == 2
    assert backend.handles[0] is not backend.handles[1]
    resources = [set(envelope.preflight_requirements.required_resource_claims) for envelope in envelopes]
    assert resources[0].isdisjoint(resources[1])


def test_execution_lock_refuses_concurrency_before_attempt_or_adapter_activity(tmp_path: Path):
    store = AttemptStore(tmp_path / "attempts")
    store.initialize()
    lock_path = store.root.parent / f".{store.root.name}.single-task.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    intent = _intent()
    requirements = _requirements(intent)
    probe = Probe(intent, requirements)
    backend = Backend()
    try:
        with pytest.raises(SingleTaskExecutorBusy):
            execute_single_task(
                store,
                intent,
                requirements,
                probe,
                backend,
                max_score=10,
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert not (store.root / intent.attempt_id).exists()
    assert probe.calls == []
    assert backend.events == []
