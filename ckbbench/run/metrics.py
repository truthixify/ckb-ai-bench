"""Run-level metrics v1 (RECOMMENDATION §5 simplified).

Records only total wall-time and total tokens per run. Per-task attribution is a
documented deferred enhancement (ADR-0009): a single composed pass emits no per-task
complete signal, so phase-split and per-task token/time are unmeasurable in v1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RunMetrics:
    """Raw v1 metrics for one run."""

    total_wall_seconds: float
    total_tokens: int | None


def _usage_total_tokens(usage: dict[str, Any]) -> int | None:
    total = usage.get("total_tokens")
    if total is not None:
        return int(total)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        return None
    return int(prompt or 0) + int(completion or 0)


def collect_metrics_from_agent(agent: Any, *, wall_seconds: float) -> RunMetrics:
    """Collect v1 metrics from an agent after ``run()`` completes.

    Token sum is best-effort: walks assistant messages for litellm ``usage`` blocks.
    Returns ``total_tokens=None`` when usage is absent (fail soft, not crash).
    """
    tokens_sum = 0
    found_any = False
    messages = getattr(agent, "messages", None) or []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        extra = msg.get("extra")
        if not isinstance(extra, dict):
            continue
        response = extra.get("response")
        if not isinstance(response, dict):
            continue
        usage = response.get("usage")
        if not isinstance(usage, dict):
            continue
        part = _usage_total_tokens(usage)
        if part is None:
            continue
        tokens_sum += part
        found_any = True
    return RunMetrics(
        total_wall_seconds=wall_seconds,
        total_tokens=tokens_sum if found_any else None,
    )