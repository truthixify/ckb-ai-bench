"""Pure ladder metrics: Pass@1 aggregation and C-B headline delta (ADR-0011/0012).

Ports spikes/ladder-chart/ladder-metrics.js to production Python. No I/O. Pass@1 excludes
``infra_fail`` from the denominator (RECOMMENDATION §4); ``agent_fail`` and
``protocol_violation`` count as 0. Health rates for infra and protocol violations are
published separately, never folded into Pass@1.
"""

from __future__ import annotations

import copy
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from ckbbench.config import CHAIN_PROFILES, LADDER_ORDER
from ckbbench.run.metrics import COMPLETE, INCOMPLETE, NOT_STARTED

Direction = Literal["positive", "negative", "flat"]

# Model to provider family for report grouping and provenance.
MODEL_FAMILIES: dict[str, str] = {
    "Sonnet": "Anthropic",
    "Opus": "Anthropic",
    "Fable": "Anthropic",
    "Grok-Build": "xAI",
    "Grok-Compose": "xAI",
    "GPT-5.5": "OpenAI",
    "gpt-5.6-sol": "OpenAI",
    "gpt-5.6-terra": "OpenAI",
    "gpt-5.6-luna": "OpenAI",
    "deepseek/deepseek-v4-flash-0731": "DeepSeek",
    "deepseek/deepseek-v4-pro-0813": "DeepSeek",
    "google/gemini-3.7-flash": "Google",
    "stealth/ox-alpha": "Ox",
}

CHAINS = CHAIN_PROFILES
COMPARED_ARMS = frozenset({"B", "C"})
HEADLINE_MIN_SCORED_RUNS_PER_ARM = 3
BUDGET_EXHAUSTED_EXIT_STATUSES = frozenset({"LimitsExceeded", "TimeExceeded"})
if not COMPARED_ARMS.issubset(LADDER_ORDER):  # pragma: no cover - static configuration guard
    raise RuntimeError("phase-one compared arms must exist in the configured ladder")

# Wilson 95% z-score (deterministic normal approximation at n>=2).
_Z95 = 1.959963984540054


@dataclass(frozen=True)
class CellAggregate:
    """Aggregated Pass@1 for one (suite, chain, arm, model) cell."""

    suite_semver: str
    model: str
    family: str
    chain: str
    arm: str
    runs: int
    scored_runs: int
    # Undefined when `scored_runs == 0`. An excluded denominator has no Pass@1, so numeric zero
    # would fabricate a flat comparison from aborted cells.
    mean: float | None
    ci_low: float | None
    ci_high: float | None
    infra_fail_rate: float
    protocol_violation_rate: float

    @property
    def has_correctness(self) -> bool:
        """Whether this cell contributes any scored correctness evidence."""
        return self.scored_runs > 0


@dataclass(frozen=True)
class HeadlineDelta:
    """C - B headline delta with propagated CI (ADR-0011, RECOMMENDATION §2)."""

    delta: float
    ci_low: float
    ci_high: float
    half_width: float
    direction: Direction
    significant: bool


def family_for_model(model: str) -> str:
    """Resolve report family; unknown models bucket as 'Other'."""
    return MODEL_FAMILIES.get(model, "Other")


def correctness_value(outcome: str) -> int | None:
    """Map one run outcome to a Pass@1 contribution (RECOMMENDATION §4).

    ``pass`` -> 1, ``agent_fail`` / ``protocol_violation`` -> 0,
    ``infra_fail`` -> excluded (``None``).
    """
    if outcome == "pass":
        return 1
    if outcome in ("agent_fail", "protocol_violation"):
        return 0
    if outcome == "infra_fail":
        return None
    raise ValueError(f"unknown outcome {outcome!r}")


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _round3(x: float) -> float:
    return round(x, 3)


def pass_at1_ci(
    *, successes: int, scored_runs: int
) -> tuple[float | None, float | None, float | None]:
    """Deterministic Pass@1 mean and 95% Wilson CI.

    With no scored run the statistic is UNDEFINED and all three values are ``None``. Returning
    ``(0.0, 0.0, 1.0)`` made an empty denominator look like a measured zero, which is exactly how a
    pair of infrastructure failures became a published "no difference" headline.

    When ``scored_runs < 2``, the interval is widened honestly to reflect high uncertainty.
    """
    if scored_runs < 0 or successes < 0 or successes > scored_runs:
        raise ValueError(
            f"invalid Pass@1 inputs: successes={successes}, scored_runs={scored_runs} "
            "(require 0 <= successes <= scored_runs)"
        )
    if scored_runs <= 0:
        return None, None, None

    mean = successes / scored_runs
    if scored_runs < 2:
        return _round3(mean), 0.0, 1.0

    n = scored_runs
    p = mean
    z2 = _Z95 * _Z95
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = (_Z95 / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    low = _clamp01(center - margin)
    high = _clamp01(center + margin)
    return _round3(mean), _round3(low), _round3(high)


def aggregate_cell(
    *,
    suite_semver: str,
    model: str,
    chain: str,
    arm: str,
    runs: list[dict[str, Any]],
) -> CellAggregate:
    """Aggregate Pass@1 and health rates for one matrix cell from its run rows."""
    total = len(runs)
    infra = sum(1 for r in runs if r["outcome"] == "infra_fail")
    protocol = sum(1 for r in runs if r["outcome"] == "protocol_violation")

    successes = 0
    scored = 0
    for r in runs:
        val = correctness_value(str(r["outcome"]))
        if val is None:
            continue
        scored += 1
        successes += val

    mean, ci_low, ci_high = pass_at1_ci(successes=successes, scored_runs=scored)
    infra_rate = infra / total if total else 0.0
    protocol_rate = protocol / total if total else 0.0

    return CellAggregate(
        suite_semver=suite_semver,
        model=model,
        family=family_for_model(model),
        chain=chain,
        arm=arm,
        runs=total,
        scored_runs=scored,
        mean=mean,
        ci_low=ci_low,
        ci_high=ci_high,
        infra_fail_rate=_round3(infra_rate),
        protocol_violation_rate=_round3(protocol_rate),
    )


def cell_group_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """Group runs by (suite, chain, arm, model)."""
    return (
        str(row["suite_semver"]),
        str(row["chain"]),
        str(row["arm"]),
        str(row["model"]),
    )


def aggregate_results(results: list[dict[str, Any]]) -> list[CellAggregate]:
    """Aggregate all validated run rows into per-cell Pass@1 summaries."""
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        buckets[cell_group_key(row)].append(row)

    cells: list[CellAggregate] = []
    for (suite, chain, arm, model), runs in sorted(buckets.items()):
        cells.append(
            aggregate_cell(
                suite_semver=suite,
                model=model,
                chain=chain,
                arm=arm,
                runs=runs,
            )
        )
    return cells


def _mean(values: list[float | int]) -> float | None:
    """Deterministic descriptive mean, undefined for an empty observation set."""
    if not values:
        return None
    return _round3(sum(values) / len(values))


def _score_fraction(row: dict[str, Any]) -> float:
    """Return one scored row's weighted task fraction without silently coercing bad data."""
    score = row.get("total_score")
    maximum = row.get("max_score")
    for field, value in (("total_score", score), ("max_score", maximum)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be numeric for phase-one reporting")
        if not math.isfinite(float(value)):
            raise ValueError(f"{field} must be finite for phase-one reporting")
    if float(maximum) <= 0 or float(score) < 0 or float(score) > float(maximum):
        raise ValueError(
            "phase-one reporting requires 0 <= total_score <= max_score and max_score > 0"
        )
    return _round3(float(score) / float(maximum))


def _task_pass_summaries(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate observed scored-task verdicts without inventing missing task outcomes."""
    buckets: dict[str, list[int]] = defaultdict(list)
    for row in runs:
        if correctness_value(str(row["outcome"])) is None:
            continue
        tasks = row.get("tasks", ())
        if not isinstance(tasks, (list, tuple)):
            raise ValueError("tasks must be a list for phase-one reporting")
        seen: set[str] = set()
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("each task outcome must be an object for phase-one reporting")
            scored = task.get("scored", True)
            if not isinstance(scored, bool):
                raise ValueError("each task outcome must carry a boolean scored value")
            if not scored:
                continue
            task_id = task.get("task_id")
            passed = task.get("passed")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("each scored task outcome needs a task_id")
            if task_id in seen:
                raise ValueError(f"duplicate task outcome {task_id!r} in one run")
            if not isinstance(passed, bool):
                raise ValueError(f"task outcome {task_id!r} must carry a boolean passed value")
            seen.add(task_id)
            buckets[task_id].append(int(passed))

    return [
        {
            "task_id": task_id,
            "passes": sum(values),
            "runs": len(values),
            "pass_rate": _mean(values),
            "pass_values": sorted(values),
        }
        for task_id, values in buckets.items()
    ]


def aggregate_phase_one_arms(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build per-arm weighted-score, usage and wall-time summaries from raw run rows.

    This second view prevents a composed 70/100 run from looking identical to 0/100 and publishes
    the efficiency values already retained in each row. Every ladder arm is summarised so the
    condition ladder can plot all four; only B and C are ever paired into a comparison.
    Infrastructure failures stay in health counts but cannot enter correctness or efficiency means.
    Token and wall-time efficiency use the same complete-usage scored rows.
    """
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        if str(row.get("arm")) in LADDER_ORDER:
            buckets[cell_group_key(row)].append(row)

    summaries: list[dict[str, Any]] = []
    for (suite, chain, arm, model), runs in sorted(buckets.items()):
        profile_paths = {
            (str(row.get("model_profile_id")), str(row.get("model_profile_sha256")))
            for row in runs
        }
        if len(profile_paths) != 1:
            raise ValueError("one model/arm summary cannot mix model profile versions")
        profile_id, profile_sha256 = next(iter(profile_paths))
        scored = [r for r in runs if correctness_value(str(r["outcome"])) is not None]
        score_values = sorted(_score_fraction(r) for r in scored)
        step_limit_runs = sum(
            1 for row in runs if row.get("agent_exit_status") == "LimitsExceeded"
        )
        wall_time_limit_runs = sum(
            1 for row in runs if row.get("agent_exit_status") == "TimeExceeded"
        )
        budget_exhausted_runs = sum(
            1
            for row in runs
            if row.get("agent_exit_status") in BUDGET_EXHAUSTED_EXIT_STATUSES
        )
        scored_seeds: list[int] = []
        for row in scored:
            seed = row.get("seed")
            if isinstance(seed, bool) or not isinstance(seed, int):
                raise ValueError("seed must be an integer for phase-one reporting")
            scored_seeds.append(seed)

        token_values: list[int] = []
        observed_token_values: list[int] = []
        observed_token_seeds: list[int] = []
        efficiency_seeds: list[int] = []
        wall_values: list[float] = []
        observed_wall_values: list[float] = []
        provider_attempts = 0
        provider_responses = 0
        incomplete_usage_runs = 0
        not_started_usage_runs = 0
        history_compaction_count = 0
        history_dropped_groups = 0
        history_dropped_items = 0
        history_max_prepared_bytes = 0
        for row in runs:
            metrics = row.get("metrics")
            if not isinstance(metrics, dict):
                continue
            status = metrics.get("token_usage_status")
            history_compaction_count += int(metrics.get("history_compaction_count", 0) or 0)
            history_dropped_groups += int(metrics.get("history_dropped_groups", 0) or 0)
            history_dropped_items += int(metrics.get("history_dropped_items", 0) or 0)
            history_max_prepared_bytes = max(
                history_max_prepared_bytes,
                int(metrics.get("history_max_prepared_bytes", 0) or 0),
            )
            if status == INCOMPLETE:
                incomplete_usage_runs += 1
            elif status == NOT_STARTED:
                not_started_usage_runs += 1
            if correctness_value(str(row["outcome"])) is None:
                continue

            provider_attempts += int(metrics.get("provider_attempts", 0) or 0)
            provider_responses += int(metrics.get("provider_responses", 0) or 0)

            seed = row.get("seed")
            if isinstance(seed, bool) or not isinstance(seed, int):
                raise ValueError("seed must be an integer for phase-one reporting")

            total_tokens = metrics.get("total_tokens")
            if (
                isinstance(total_tokens, int)
                and not isinstance(total_tokens, bool)
                and total_tokens >= 0
            ):
                observed_token_values.append(total_tokens)
                observed_token_seeds.append(seed)

            wall = metrics.get("total_wall_seconds")
            if (
                isinstance(wall, (int, float))
                and not isinstance(wall, bool)
                and math.isfinite(float(wall))
                and float(wall) >= 0
            ):
                observed_wall_values.append(float(wall))

            if (
                status == COMPLETE
                and isinstance(total_tokens, int)
                and not isinstance(total_tokens, bool)
                and total_tokens >= 0
            ):
                token_values.append(total_tokens)
                efficiency_seeds.append(seed)
                if (
                    isinstance(wall, (int, float))
                    and not isinstance(wall, bool)
                    and math.isfinite(float(wall))
                    and float(wall) >= 0
                ):
                    wall_values.append(float(wall))

        summaries.append(
            {
                "suite_semver": suite,
                "model": model,
                "model_profile_id": profile_id,
                "model_profile_sha256": profile_sha256,
                "family": family_for_model(model),
                "chain": chain,
                "arm": arm,
                "runs": len(runs),
                "scored_runs": len(scored),
                "scored_seed_values": sorted(scored_seeds),
                "suite_passes": sum(1 for r in scored if r["outcome"] == "pass"),
                "budget_exhausted_runs": budget_exhausted_runs,
                "step_limit_exhausted_runs": step_limit_runs,
                "wall_time_limit_exhausted_runs": wall_time_limit_runs,
                "infra_fail_rate": _round3(
                    sum(1 for r in runs if r["outcome"] == "infra_fail") / len(runs)
                ),
                "protocol_violation_rate": _round3(
                    sum(1 for r in runs if r["outcome"] == "protocol_violation") / len(runs)
                ),
                "weighted_score_mean": _mean(score_values),
                "weighted_score_values": score_values,
                "task_pass_rates": _task_pass_summaries(runs),
                "efficiency_runs": len(token_values),
                "efficiency_seed_values": sorted(efficiency_seeds),
                "total_tokens_mean": _mean(token_values),
                "total_tokens_values": sorted(token_values),
                "observed_token_runs": len(observed_token_values),
                "observed_token_seed_values": sorted(observed_token_seeds),
                "observed_total_tokens_mean": _mean(observed_token_values),
                "observed_total_tokens_sum": sum(observed_token_values),
                "observed_total_tokens_values": sorted(observed_token_values),
                "wall_time_runs": len(wall_values),
                "agent_wall_seconds_mean": _mean(wall_values),
                "agent_wall_seconds_values": sorted(wall_values),
                "observed_wall_time_runs": len(observed_wall_values),
                "observed_agent_wall_seconds_mean": _mean(observed_wall_values),
                "observed_agent_wall_seconds_values": sorted(observed_wall_values),
                "provider_attempts": provider_attempts,
                "provider_responses": provider_responses,
                "unanswered_provider_attempts": provider_attempts - provider_responses,
                "incomplete_usage_runs": incomplete_usage_runs,
                "not_started_usage_runs": not_started_usage_runs,
                "history_compaction_count": history_compaction_count,
                "history_dropped_groups": history_dropped_groups,
                "history_dropped_items": history_dropped_items,
                "history_max_prepared_bytes": history_max_prepared_bytes,
            }
        )
    return summaries


def _comparison_readiness(
    b: dict[str, Any] | None,
    c: dict[str, Any] | None,
) -> dict[str, Any]:
    """Decide whether a B/C difference may be promoted beyond raw descriptive evidence.

    The benchmark's declared publication design is three runs per cell with paired seeds. A sparse
    or completion-conditioned slice remains useful evidence, but it cannot become the chart or
    leaderboard headline. This gate is intentionally stricter than the arithmetic delta helper.
    """
    arms = {"B": b, "C": c}
    recorded = {arm: int(summary["runs"]) if summary else 0 for arm, summary in arms.items()}
    scored = {arm: int(summary["scored_runs"]) if summary else 0 for arm, summary in arms.items()}
    seeds = {
        arm: list(summary.get("scored_seed_values", ())) if summary else []
        for arm, summary in arms.items()
    }
    budget_exhausted = {
        arm: int(summary.get("budget_exhausted_runs", 0)) if summary else 0
        for arm, summary in arms.items()
    }
    step_limit_exhausted = {
        arm: int(summary.get("step_limit_exhausted_runs", 0)) if summary else 0
        for arm, summary in arms.items()
    }
    wall_time_limit_exhausted = {
        arm: int(summary.get("wall_time_limit_exhausted_runs", 0)) if summary else 0
        for arm, summary in arms.items()
    }

    reasons: list[str] = []
    if any(scored[arm] < HEADLINE_MIN_SCORED_RUNS_PER_ARM for arm in ("B", "C")):
        reasons.append("fewer_than_three_scored_runs_per_arm")
    if scored["B"] != scored["C"]:
        reasons.append("unbalanced_scored_runs")
    if seeds["B"] != seeds["C"]:
        reasons.append("unmatched_scored_seed_multiset")
    completion_conditioned = any(scored[arm] < recorded[arm] for arm in ("B", "C"))
    if completion_conditioned:
        reasons.append("completion_conditioned")
    return {
        "status": "headline_eligible" if not reasons else "provisional",
        "headline_eligible": not reasons,
        "minimum_scored_runs_per_arm": HEADLINE_MIN_SCORED_RUNS_PER_ARM,
        "completion_conditioned": completion_conditioned,
        "recorded_rows": recorded,
        "scored_runs": scored,
        "scored_seed_values": seeds,
        "budget_exhausted_runs": budget_exhausted,
        "step_limit_exhausted_runs": step_limit_exhausted,
        "wall_time_limit_exhausted_runs": wall_time_limit_exhausted,
        "reasons": reasons,
    }


def _descriptive_delta(c_value: Any, b_value: Any) -> float | None:
    """C minus B for two defined descriptive means; no value means no claim."""
    if not isinstance(c_value, (int, float)) or isinstance(c_value, bool):
        return None
    if not isinstance(b_value, (int, float)) or isinstance(b_value, bool):
        return None
    return _round3(float(c_value) - float(b_value))


def _efficiency_readiness(
    b: dict[str, Any] | None,
    c: dict[str, Any] | None,
    correctness_readiness: dict[str, Any],
) -> dict[str, Any]:
    """Require a complete, matched token observation for every scored correctness row."""
    arms = {"B": b, "C": c}
    scored = {arm: int(summary["scored_runs"]) if summary else 0 for arm, summary in arms.items()}
    usable = {
        arm: int(summary["efficiency_runs"]) if summary else 0
        for arm, summary in arms.items()
    }
    seeds = {
        arm: list(summary.get("efficiency_seed_values", ())) if summary else []
        for arm, summary in arms.items()
    }
    reasons: list[str] = []
    if correctness_readiness.get("headline_eligible") is not True:
        reasons.append("correctness_cohort_not_ready")
    if any(usable[arm] != scored[arm] for arm in ("B", "C")):
        reasons.append("incomplete_usage_in_scored_rows")
    if usable["B"] != usable["C"]:
        reasons.append("unbalanced_complete_usage_runs")
    if seeds["B"] != seeds["C"]:
        reasons.append("unmatched_complete_usage_seed_multiset")
    return {
        "status": "comparison_eligible" if not reasons else "ineligible",
        "comparison_eligible": not reasons,
        "scored_runs": scored,
        "complete_usage_runs": usable,
        "complete_usage_seed_values": seeds,
        "reasons": reasons,
    }


def _task_comparisons(
    b: dict[str, Any] | None,
    c: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Join task-level B/C observations by task ID, preserving one-sided evidence."""
    b_tasks = {row["task_id"]: row for row in (b or {}).get("task_pass_rates", ())}
    c_tasks = {row["task_id"]: row for row in (c or {}).get("task_pass_rates", ())}
    task_order = [*b_tasks, *(task_id for task_id in c_tasks if task_id not in b_tasks)]
    return [
        {
            "task_id": task_id,
            "B": b_tasks.get(task_id),
            "C": c_tasks.get(task_id),
            "pass_rate_delta": _descriptive_delta(
                c_tasks.get(task_id, {}).get("pass_rate"),
                b_tasks.get(task_id, {}).get("pass_rate"),
            ),
        }
        for task_id in task_order
    ]


def phase_one_comparisons(arm_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair B/C descriptive summaries by suite, model and chain without claiming paired inference."""
    grouped: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for summary in arm_summaries:
        key = (
            str(summary["suite_semver"]),
            str(summary["model"]),
            str(summary["chain"]),
        )
        grouped[key][str(summary["arm"])] = summary

    comparisons: list[dict[str, Any]] = []
    for (suite, model, chain), arms in sorted(grouped.items()):
        b = arms.get("B")
        c = arms.get("C")
        readiness = _comparison_readiness(b, c)
        efficiency_readiness = _efficiency_readiness(b, c, readiness)
        comparisons.append(
            {
                "suite_semver": suite,
                "model": model,
                "family": family_for_model(model),
                "chain": chain,
                "B": b,
                "C": c,
                "comparison_readiness": readiness,
                "efficiency_readiness": efficiency_readiness,
                "task_comparisons": _task_comparisons(b, c),
                "weighted_score_delta": _descriptive_delta(
                    c.get("weighted_score_mean") if c else None,
                    b.get("weighted_score_mean") if b else None,
                ),
                "total_tokens_delta": (
                    _descriptive_delta(
                        c.get("total_tokens_mean") if c else None,
                        b.get("total_tokens_mean") if b else None,
                    )
                    if efficiency_readiness["comparison_eligible"]
                    else None
                ),
                "observed_total_tokens_delta": _descriptive_delta(
                    c.get("observed_total_tokens_mean") if c else None,
                    b.get("observed_total_tokens_mean") if b else None,
                ),
                "agent_wall_seconds_delta": (
                    _descriptive_delta(
                        c.get("agent_wall_seconds_mean") if c else None,
                        b.get("agent_wall_seconds_mean") if b else None,
                    )
                    if efficiency_readiness["comparison_eligible"]
                    else None
                ),
                "observed_agent_wall_seconds_delta": _descriptive_delta(
                    c.get("observed_agent_wall_seconds_mean") if c else None,
                    b.get("observed_agent_wall_seconds_mean") if b else None,
                ),
            }
        )
    return comparisons


_RUN_EPOCH = re.compile(r"-(\d{10})$")

# Published per run. `proof` is deliberately absent: the report states outcomes, never the
# submitted artefact bodies.
_TASK_REPORT_FIELDS = ("task_id", "passed", "scored", "score", "score_awarded", "reason")

_ENVIRONMENT_FIELDS = (
    ("suite_freeze_hash", ("suite_freeze_hash",)),
    ("run_params_derivation", ("run_params_derivation",)),
    ("mcp_server_version", ("mcp_server_version",)),
    ("schema_version", ("schema_version",)),
    ("chain_id", ("devnet_state", "chain")),
    ("genesis_hash", ("devnet_state", "genesis_hash")),
    ("devnet_config_sha256", ("devnet_state", "config_sha256")),
    ("lifecycle_policy", ("devnet_state", "lifecycle_policy")),
    ("step_limit", ("agent_limits", "step_limit")),
    ("wall_time_limit_seconds", ("agent_limits", "wall_time_limit_seconds")),
)


def _dig(row: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = row
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def run_epoch(run_id: Any) -> int | None:
    """Return the canonical Unix start time encoded in a production run ID."""
    match = _RUN_EPOCH.search(str(run_id or ""))
    return int(match.group(1)) if match else None


def report_runs(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitized per-run rows for the report, in a deterministic order.

    ``build_dataset`` otherwise keeps only aggregates, so run-level views would have nothing to
    render. Submitted proofs are dropped here rather than at render time.
    """
    rows: list[dict[str, Any]] = []
    for row in results:
        tasks = row.get("tasks")
        rows.append({
            "run_id": row.get("run_id"),
            "epoch": run_epoch(row.get("run_id")),
            "suite_semver": row.get("suite_semver"),
            "model": row.get("model"),
            "model_response_id": row.get("model_response_id"),
            "model_profile_id": row.get("model_profile_id"),
            "model_profile_sha256": row.get("model_profile_sha256"),
            "chain": row.get("chain"),
            "arm": row.get("arm"),
            "seed": row.get("seed"),
            "outcome": row.get("outcome"),
            "agent_exit_status": row.get("agent_exit_status"),
            "total_score": row.get("total_score"),
            "max_score": row.get("max_score"),
            "schema_version": row.get("schema_version"),
            "mcp_surface_profile": row.get("mcp_surface_profile"),
            "mcp_server_version": row.get("mcp_server_version"),
            "suite_freeze_hash": row.get("suite_freeze_hash"),
            "devnet_state": copy.deepcopy(row.get("devnet_state")) or {},
            "agent_limits": copy.deepcopy(row.get("agent_limits")) or {},
            "metrics": copy.deepcopy(row.get("metrics")) or {},
            "tasks": [
                {field: task.get(field) for field in _TASK_REPORT_FIELDS}
                for task in tasks if isinstance(task, dict)
            ] if isinstance(tasks, list) else [],
        })
    rows.sort(key=lambda r: (r["epoch"] is None, r["epoch"] or 0, str(r["run_id"])))
    return rows


def report_environment(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Pinned identity shared by every row, or an explicit mixed marker per field.

    A report covering cohorts that disagree on a pinned value must say so rather than silently
    publishing one cohort's value as if it governed all rows.
    """
    environment: dict[str, Any] = {}
    for name, path in _ENVIRONMENT_FIELDS:
        seen = {_dig(row, path) for row in results}
        seen.discard(None)
        if len(seen) == 1:
            environment[name] = seen.pop()
        elif seen:
            environment[name] = "mixed"
        else:
            environment[name] = None
    return environment


def build_dataset(
    results: list[dict[str, Any]],
    *,
    synthetic: bool = False,
    generated_at: str = "timestamp unavailable",
    report_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the chart/leaderboard dataset from raw run rows."""
    cells = aggregate_results(results)
    arm_summaries = aggregate_phase_one_arms(results)
    suites = sorted({c.suite_semver for c in cells})
    models = sorted({c.model for c in cells})
    families = sorted({c.family for c in cells})

    cell_dicts = [
        {
            "suite_semver": c.suite_semver,
            "model": c.model,
            "family": c.family,
            "chain": c.chain,
            "arm": c.arm,
            "runs": c.runs,
            "scored_runs": c.scored_runs,
            "mean": c.mean,
            "ci_low": c.ci_low,
            "ci_high": c.ci_high,
            "infra_fail_rate": c.infra_fail_rate,
            "protocol_violation_rate": c.protocol_violation_rate,
        }
        for c in cells
    ]

    out: dict[str, Any] = {
        "generated_at": generated_at,
        "suites": suites,
        "models": models,
        "families": families,
        "chains": list(CHAINS),
        "cells": cell_dicts,
        "phase_one_arms": arm_summaries,
        "phase_one_comparisons": phase_one_comparisons(arm_summaries),
        "runs": report_runs(results),
        "environment": report_environment(results),
        "report_sources": copy.deepcopy(report_sources or []),
    }
    if synthetic:
        out["_SYNTHETIC"] = True
        out["_WARNING"] = (
            "SYNTHETIC FABRICATED DATA - NOT a real benchmark run. "
            "Do NOT cite as results."
        )
    return out


def half_width(cell: dict[str, float]) -> float:
    """Half-width of a [ci_low, ci_high] interval around mean."""
    mean = cell["mean"]
    return max(mean - cell["ci_low"], cell["ci_high"] - mean)


def headline_delta(cell_b: dict[str, float], cell_c: dict[str, float]) -> HeadlineDelta:
    """C - B headline delta with CI propagated in quadrature (ADR-0011)."""
    if not cell_b or not cell_c:
        raise ValueError("headline_delta needs both arm B and arm C cells")
    # Callers must screen undefined statistics out first; a headline over an empty denominator is
    # not a small error, it is a fabricated result.
    for label, cell in (("B", cell_b), ("C", cell_c)):
        for field in ("mean", "ci_low", "ci_high"):
            value = cell.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(
                    f"headline_delta needs numeric {field} for arm {label}; "
                    "a cell with no scored runs has no Pass@1"
                )
            if math.isnan(value) or math.isinf(value):
                raise ValueError(f"headline_delta needs a finite {field} for arm {label}")

    delta = cell_c["mean"] - cell_b["mean"]
    h_b = half_width(cell_b)
    h_c = half_width(cell_c)
    half_w = math.sqrt(h_b * h_b + h_c * h_c)
    ci_low = delta - half_w
    ci_high = delta + half_w

    if delta > 1e-9:
        direction: Direction = "positive"
    elif delta < -1e-9:
        direction = "negative"
    else:
        direction = "flat"

    significant = ci_low > 0 or ci_high < 0
    return HeadlineDelta(
        delta=delta,
        ci_low=ci_low,
        ci_high=ci_high,
        half_width=half_w,
        direction=direction,
        significant=significant,
    )


def refuse_chain_merge(chain: str) -> str:
    """Hard refusal: DevNet and TestNet never share an axis (ADR-0011)."""
    banned = {"all", "both", "merged", "pooled", "combined"}
    if str(chain).lower() in banned:
        raise ValueError(
            f"chains must stay separate (ADR-0011): refusing to merge {chain!r}. "
            f"Pick one of: {', '.join(CHAINS)}"
        )
    return chain


def _has_correctness(point: dict[str, Any] | None) -> bool:
    """Whether an arm point carries real scored evidence, not just an entry in the dataset.

    An arm with only excluded `infra_fail` rows still appears — its health rates are published — but
    it contributes no correctness geometry and cannot take part in a `C - B` headline.
    """
    if not point:
        return False
    if int(point.get("scored_runs", 0)) <= 0:
        return False
    return all(
        isinstance(point.get(f), (int, float)) and not isinstance(point.get(f), bool)
        and not math.isnan(point[f]) and not math.isinf(point[f])
        for f in ("mean", "ci_low", "ci_high")
    )


def line_series_for_chain(dataset: dict[str, Any], chain: str) -> list[dict[str, Any]]:
    """Per-chain, per-model line series for the ladder chart (one chain at a time)."""
    refuse_chain_merge(chain)
    if chain not in CHAINS:
        raise ValueError(
            f'unknown chain "{chain}"; chains are kept separate: {", ".join(CHAINS)}'
        )

    cells = [c for c in dataset["cells"] if c["chain"] == chain]
    readiness_by_model = {
        str(row["model"]): row.get("comparison_readiness", {})
        for row in dataset.get("phase_one_comparisons", ())
        if row.get("chain") == chain
    }
    by_model: dict[str, dict[str, Any]] = {}
    for c in cells:
        if c["model"] not in by_model:
            by_model[c["model"]] = {
                "model": c["model"],
                "family": c["family"],
                "points": {},
                # Health rates are PUBLISHED, never folded into Pass@1 (RECOMMENDATION 4). Carry
                # the worst (max) rate across the model's arms so the report can surface it.
                "infra_fail_rate": 0.0,
                "protocol_violation_rate": 0.0,
            }
        by_model[c["model"]]["points"][c["arm"]] = {
            "arm": c["arm"],
            "mean": c["mean"],
            "ci_low": c["ci_low"],
            "ci_high": c["ci_high"],
            # Renderers use this to decide whether any correctness geometry exists at all.
            "scored_runs": int(c.get("scored_runs", 0)),
        }
        by_model[c["model"]]["infra_fail_rate"] = max(
            by_model[c["model"]]["infra_fail_rate"], c.get("infra_fail_rate", 0.0)
        )
        by_model[c["model"]]["protocol_violation_rate"] = max(
            by_model[c["model"]]["protocol_violation_rate"], c.get("protocol_violation_rate", 0.0)
        )

    lines: list[dict[str, Any]] = []
    for model in sorted(by_model):
        line = by_model[model]
        b = line["points"].get("B")
        c = line["points"].get("C")
        readiness = readiness_by_model.get(model, {})
        headline = None
        if (
            readiness.get("headline_eligible") is True
            and _has_correctness(b)
            and _has_correctness(c)
        ):
            hd = headline_delta(b, c)
            headline = {
                "delta": hd.delta,
                "ci_low": hd.ci_low,
                "ci_high": hd.ci_high,
                "half_width": hd.half_width,
                "direction": hd.direction,
                "significant": hd.significant,
            }
        lines.append({**line, "headline": headline, "comparison_readiness": readiness})
    return lines


def leaderboard_rows(dataset: dict[str, Any], chain: str) -> list[dict[str, Any]]:
    """Secondary leaderboard: models sorted by C-B delta on one chain (ADR-0011)."""
    lines = line_series_for_chain(dataset, chain)
    rows: list[dict[str, Any]] = []
    for line in lines:
        h = line.get("headline")
        rows.append(
            {
                "model": line["model"],
                "family": line["family"],
                "headline": h,
                "points": line["points"],
                # Published health rates (RECOMMENDATION 4): shown beside the score, never folded in.
                "infra_fail_rate": line.get("infra_fail_rate", 0.0),
                "protocol_violation_rate": line.get("protocol_violation_rate", 0.0),
            }
        )
    # Rows WITH a headline keep the existing delta ordering; rows without one sort after them all,
    # then by model. A missing headline is not the smallest delta -- it is not a delta at all.
    rows.sort(
        key=lambda r: (
            0 if r["headline"] else 1,
            -(r["headline"]["delta"] if r["headline"] else 0.0),
            r["model"],
        )
    )
    return rows
