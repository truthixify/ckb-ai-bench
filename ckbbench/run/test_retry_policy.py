from dataclasses import replace

import pytest

from ckbbench.run.retry_policy import (
    RETRY_COOLDOWN_SECONDS,
    RETRY_POLICY,
    RETRY_POLICY_ID,
    RETRY_POLICY_SHA256,
    is_retryable_infrastructure_failure,
)
from ckbbench.run.task_attempt import artifact_sha256
from ckbbench.run.test_task_attempt import _intent, _journal, _result


def _infra_result(stage: str, category: str):
    intent = _intent()
    return replace(
        _result(intent, _journal(intent), infra_fail=True),
        failure_stage=stage,
        failure_category=category,
    )


def test_retry_policy_is_canonical_bounded_and_stably_identified():
    assert set(RETRY_POLICY) == {
        "cooldown_seconds",
        "eligible_failures",
        "eligible_predecessor_outcome",
        "id",
        "maximum_retries_per_slot",
        "require_complete_predecessor_cleanup",
        "require_fresh_attempt_and_resources",
        "scored_predecessor_retryable",
    }
    assert RETRY_POLICY["id"] == RETRY_POLICY_ID
    assert RETRY_POLICY["maximum_retries_per_slot"] == 1
    assert RETRY_POLICY["cooldown_seconds"] == RETRY_COOLDOWN_SECONDS == 30
    assert RETRY_POLICY_SHA256 == artifact_sha256(RETRY_POLICY)
    failures = RETRY_POLICY["eligible_failures"]
    assert [row["stage"] for row in failures] == sorted(row["stage"] for row in failures)
    assert all(row["categories"] == sorted(set(row["categories"])) for row in failures)


@pytest.mark.parametrize(
    ("stage", "category"),
    [
        (row["stage"], category)
        for row in RETRY_POLICY["eligible_failures"]
        for category in row["categories"]
    ],
)
def test_only_declared_infrastructure_failures_are_retryable(stage: str, category: str):
    assert is_retryable_infrastructure_failure(_infra_result(stage, category))


@pytest.mark.parametrize(
    ("stage", "category"),
    [
        ("source", "source-drift"),
        ("provider", "stale-model-evidence"),
        ("ckb_ai", "network-mismatch"),
        ("rpc", "network-mismatch"),
        ("funding", "funding-insufficient"),
        ("dependencies", "dependency-mismatch"),
        ("signer", "malformed-observation"),
        ("agent", "budget-exhausted"),
        ("grading", "verifier-failed"),
    ],
)
def test_configuration_scoring_and_budget_failures_are_terminal(stage: str, category: str):
    assert not is_retryable_infrastructure_failure(_infra_result(stage, category))


def test_scored_result_is_never_retryable():
    intent = _intent()
    assert not is_retryable_infrastructure_failure(_result(intent, _journal(intent)))
