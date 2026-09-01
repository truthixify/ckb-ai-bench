"""Model-neutral whole-task infrastructure retry policy."""

from __future__ import annotations

from typing import Any

from ckbbench.run.task_attempt import TaskAttemptResult, artifact_sha256

RETRY_POLICY_ID = "whole-task-infrastructure-retry-v2"
RETRY_COOLDOWN_SECONDS = 30

_RETRYABLE_FAILURES = (
    ("agent", ("adapter-error", "interrupted")),
    ("ckb_ai", ("adapter-error", "ckb-ai-unready", "deadline-exceeded")),
    ("dependencies", ("adapter-error", "deadline-exceeded")),
    ("funding", ("adapter-error", "deadline-exceeded")),
    ("grading", ("adapter-error", "deadline-exceeded")),
    ("intent", ("interrupted",)),
    ("outputs", ("adapter-error", "deadline-exceeded", "output-not-fresh")),
    ("protocol", ("adapter-error", "deadline-exceeded")),
    ("provider", ("adapter-error", "deadline-exceeded", "provider-unready")),
    ("rpc", ("adapter-error", "deadline-exceeded", "rpc-unready")),
    ("setup", ("adapter-error", "deadline-exceeded", "interrupted")),
    ("signer", ("adapter-error", "deadline-exceeded", "signer-unready")),
    ("source", ("adapter-error", "deadline-exceeded")),
    ("stop", ("adapter-error", "deadline-exceeded")),
)

RETRY_POLICY: dict[str, Any] = {
    "cooldown_seconds": RETRY_COOLDOWN_SECONDS,
    "eligible_failures": [
        {"categories": list(categories), "stage": stage}
        for stage, categories in _RETRYABLE_FAILURES
    ],
    "eligible_predecessor_outcome": "infra_fail",
    "id": RETRY_POLICY_ID,
    "maximum_retries_per_slot": 1,
    "require_complete_predecessor_cleanup": True,
    "require_fresh_attempt_and_resources": True,
    "scored_predecessor_retryable": False,
}
RETRY_POLICY_SHA256 = artifact_sha256(RETRY_POLICY)

_RETRYABLE_FAILURE_PAIRS = frozenset(
    (stage, category)
    for stage, categories in _RETRYABLE_FAILURES
    for category in categories
)


def is_retryable_infrastructure_failure(result: TaskAttemptResult) -> bool:
    """Return whether one cleaned predecessor may receive the policy's sole fresh retry."""
    if not isinstance(result, TaskAttemptResult) or result.outcome != "infra_fail":
        return False
    return (result.failure_stage, result.failure_category) in _RETRYABLE_FAILURE_PAIRS
