"""Run-level metrics v1 with provider token provenance (ADR-0014, RECOMMENDATION §5 simplified).

Records total wall-time plus the run's provider usage. Per-task attribution is a documented deferred
enhancement (ADR-0009): a single composed pass emits no per-task complete signal, so phase-split and
per-task token/time are unmeasurable in v1.

Token totals come from the model's sanitized usage ledger, never from a walk over retained messages
and never by deriving a field the provider did not send. A run whose usage cannot be established
completely says so, rather than reporting a number that looks like a full billable total.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

TokenUsageStatus = Literal["not_started", "complete", "incomplete"]

NOT_STARTED: TokenUsageStatus = "not_started"
COMPLETE: TokenUsageStatus = "complete"
INCOMPLETE: TokenUsageStatus = "incomplete"
USAGE_STATUSES: frozenset[str] = frozenset({NOT_STARTED, COMPLETE, INCOMPLETE})


@dataclass(frozen=True)
class RunMetrics:
    """Raw v1 metrics for one run."""

    total_wall_seconds: float
    total_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model_calls: int = 0
    provider_attempts: int = 0
    provider_responses: int = 0
    token_usage_status: TokenUsageStatus = NOT_STARTED

    @property
    def efficiency_eligible(self) -> bool:
        """Only a complete observation may enter a token comparison."""
        return self.token_usage_status == COMPLETE


def _ledger_of(agent: Any) -> Any | None:
    return getattr(getattr(agent, "model", None), "usage_ledger", None)


def collect_metrics_from_agent(agent: Any, *, wall_seconds: float) -> RunMetrics:
    """Collect v1 metrics from an agent after ``run()`` returns or raises.

    An agent whose model keeps no ledger reports ``not_started``: absence of evidence is recorded as
    absence, never as a zero-token run.
    """
    ledger = _ledger_of(agent)
    if ledger is None:
        return RunMetrics(total_wall_seconds=wall_seconds)

    attempts = int(ledger.attempt_count)
    responses = int(ledger.response_count)
    calls = int(getattr(ledger, "turn_count", attempts))
    if attempts == 0 and responses == 0 and calls == 0:
        return RunMetrics(total_wall_seconds=wall_seconds)

    totals = ledger.totals()
    complete = bool(ledger.is_complete()) and calls == attempts == responses
    prompt, completion, total = totals if totals is not None else (None, None, None)
    return RunMetrics(
        total_wall_seconds=wall_seconds,
        total_tokens=total,
        prompt_tokens=prompt,
        completion_tokens=completion,
        model_calls=calls,
        provider_attempts=attempts,
        provider_responses=responses,
        token_usage_status=COMPLETE if complete else INCOMPLETE,
    )


def harness_error_count(agent: Any) -> int:
    """Failures this harness caused, kept out of the serialized provider health numbers."""
    ledger = _ledger_of(agent)
    return int(getattr(ledger, "internal_errors", 0) or 0) if ledger is not None else 0


def response_model_identity(agent: Any) -> str | None:
    """The one model identity every response reported, or None when absent or drifted."""
    ledger = _ledger_of(agent)
    if ledger is None:
        return None
    identities = ledger.response_models
    if len(identities) != 1:
        return None
    return next(iter(identities))
