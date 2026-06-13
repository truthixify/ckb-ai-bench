"""Pure ladder metrics: Pass@1 aggregation and C-B headline delta (ADR-0011/0012).

Ports spikes/ladder-chart/ladder-metrics.js to production Python. No I/O. Pass@1 excludes
``infra_fail`` from the denominator (RECOMMENDATION §4); ``agent_fail`` and
``protocol_violation`` count as 0. Health rates for infra and protocol violations are
published separately, never folded into Pass@1.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from ckbbench.config import CHAIN_PROFILES

Direction = Literal["positive", "negative", "flat"]

# Model -> provider family for chart coloring (ADR-0011).
MODEL_FAMILIES: dict[str, str] = {
    "Sonnet": "Anthropic",
    "Opus": "Anthropic",
    "Fable": "Anthropic",
    "Grok-Build": "xAI",
    "Grok-Compose": "xAI",
    "GPT-5.5": "OpenAI",
}

CHAINS = CHAIN_PROFILES

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
    mean: float
    ci_low: float
    ci_high: float
    infra_fail_rate: float
    protocol_violation_rate: float


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
    """Resolve chart color family; unknown models bucket as 'Other'."""
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


def pass_at1_ci(*, successes: int, scored_runs: int) -> tuple[float, float, float]:
    """Deterministic Pass@1 mean and 95% Wilson CI.

    When ``scored_runs < 2``, the interval is widened honestly to reflect high uncertainty.
    """
    if scored_runs < 0 or successes < 0 or successes > scored_runs:
        raise ValueError(
            f"invalid Pass@1 inputs: successes={successes}, scored_runs={scored_runs} "
            "(require 0 <= successes <= scored_runs)"
        )
    if scored_runs <= 0:
        return 0.0, 0.0, 1.0

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


def build_dataset(
    results: list[dict[str, Any]],
    *,
    synthetic: bool = False,
    generated_at: str = "deterministic",
) -> dict[str, Any]:
    """Build the chart/leaderboard dataset from raw run rows."""
    cells = aggregate_results(results)
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


def line_series_for_chain(dataset: dict[str, Any], chain: str) -> list[dict[str, Any]]:
    """Per-chain, per-model line series for the ladder chart (one chain at a time)."""
    refuse_chain_merge(chain)
    if chain not in CHAINS:
        raise ValueError(
            f'unknown chain "{chain}"; chains are kept separate: {", ".join(CHAINS)}'
        )

    cells = [c for c in dataset["cells"] if c["chain"] == chain]
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
        headline = None
        if b and c:
            hd = headline_delta(b, c)
            headline = {
                "delta": hd.delta,
                "ci_low": hd.ci_low,
                "ci_high": hd.ci_high,
                "half_width": hd.half_width,
                "direction": hd.direction,
                "significant": hd.significant,
            }
        lines.append({**line, "headline": headline})
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
    rows.sort(
        key=lambda r: (
            -(r["headline"]["delta"] if r["headline"] else -999.0),
            r["model"],
        )
    )
    return rows