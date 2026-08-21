"""Run-level metrics v1 with provider token provenance (ADR-0014, RECOMMENDATION §5 simplified).

Records total wall-time plus the run's provider usage. Per-task attribution is a documented deferred
enhancement (ADR-0009): a single composed pass emits no per-task complete signal, so phase-split and
per-task token/time are unmeasurable in v1.

Token totals come from the model's sanitized usage ledger, never from a walk over retained messages
and never by deriving a field the provider did not send. A run whose usage cannot be established
completely says so, rather than reporting a number that looks like a full billable total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TokenUsageStatus = Literal["not_started", "complete", "incomplete"]

# The ONE vocabulary for why an accepted provider attempt returned no usable response. Defined here
# so the runtime boundary and the result validator cannot drift apart, and deliberately closed: a
# category is chosen by `isinstance` at the provider boundary, never derived from an exception
# message, status text, response body, URL, or class name.
PROVIDER_FAILURE_CATEGORIES: tuple[str, ...] = (
    "authentication",
    "authorization",
    "rate_limit",
    "timeout",
    "connection",
    "server",
    "request",
    "protocol",
    "unsupported",
    "context_window",
    "other_provider",
    "multiple",
)
PROVIDER_FAILURE_CATEGORY_SET: frozenset[str] = frozenset(PROVIDER_FAILURE_CATEGORIES)
# Reserved for a cell whose failed attempts disagree; never produced by a single attempt.
MULTIPLE_CATEGORIES = "multiple"

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
    provider_retry_count: int = 0
    provider_retry_delay_seconds: int = 0
    history_compaction_count: int = 0
    history_dropped_groups: int = 0
    history_dropped_items: int = 0
    history_max_prepared_bytes: int = 0
    token_usage_status: TokenUsageStatus = NOT_STARTED
    # Why an unanswered attempt failed, as one fixed allowlisted token. `None` unless at least one
    # accepted provider attempt returned no usable response.
    provider_failure_category: str | None = None
    provider_failure_counts: dict[str, int] = field(default_factory=dict)

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
    # Read before anything else can raise so the category survives when `agent.run()` itself fails.
    failure_category = _failure_category_of(ledger)
    failure_counts = _failure_counts_of(ledger)

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
        provider_retry_count=int(getattr(ledger, "retry_count", 0) or 0),
        provider_retry_delay_seconds=int(
            getattr(ledger, "retry_delay_seconds", 0) or 0
        ),
        history_compaction_count=int(
            getattr(ledger, "history_compaction_count", 0) or 0
        ),
        history_dropped_groups=int(getattr(ledger, "history_dropped_groups", 0) or 0),
        history_dropped_items=int(getattr(ledger, "history_dropped_items", 0) or 0),
        history_max_prepared_bytes=int(
            getattr(ledger, "history_max_prepared_bytes", 0) or 0
        ),
        token_usage_status=COMPLETE if complete else INCOMPLETE,
        provider_failure_category=failure_category,
        provider_failure_counts=failure_counts,
    )


def _failure_category_of(ledger: Any) -> str | None:
    """The ledger's reduced failure category, accepted only if it is an allowlisted token.

    The type check precedes membership because `in` on an unhashable value raises TypeError; metric
    collection must reduce an arbitrary value to None, never fail the run.
    """
    value = getattr(ledger, "provider_failure_category", None)
    if isinstance(value, str) and value in PROVIDER_FAILURE_CATEGORY_SET:
        return value
    return None


def _failure_counts_of(ledger: Any) -> dict[str, int]:
    """A closed, positive category-count map from the sanitized ledger."""
    raw = getattr(ledger, "provider_failure_counts", {})
    if not isinstance(raw, dict):
        return {}
    return {
        key: value for key, value in sorted(raw.items())
        if key in PROVIDER_FAILURE_CATEGORY_SET - {MULTIPLE_CATEGORIES}
        and isinstance(value, int) and not isinstance(value, bool) and value > 0
    }


def harness_error_count(agent: Any) -> int:
    """Failures this harness caused, kept out of the serialized provider health numbers."""
    ledger = _ledger_of(agent)
    return int(getattr(ledger, "internal_errors", 0) or 0) if ledger is not None else 0


def correctness_evidence_complete(agent: Any) -> bool:
    """Whether every model turn ultimately received one response under one model identity.

    This is deliberately weaker than complete token usage. A bounded failed attempt followed by a
    usable response preserves correctness evidence while making the token denominator incomplete.
    """
    ledger = _ledger_of(agent)
    if ledger is None:
        return False
    check = getattr(ledger, "is_correctness_complete", None)
    if not callable(check):
        return False
    try:
        return check() is True
    except Exception:
        return False


def response_model_identity(agent: Any) -> str | None:
    """The one model identity every response reported, or None when absent or drifted."""
    ledger = _ledger_of(agent)
    if ledger is None:
        return None
    identities = ledger.response_models
    if len(identities) != 1:
        return None
    return next(iter(identities))
