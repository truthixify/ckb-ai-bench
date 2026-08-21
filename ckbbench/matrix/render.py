"""Deterministic static ladder chart renderer (ADR-0011/0012).

Ports spikes/ladder-chart/render-ladder.js to production Python. Produces self-contained HTML with
inline SVG and a secondary leaderboard table. No external JS/CSS/CDN. Same dataset -> byte-identical
output.
"""

from __future__ import annotations

import math
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from ckbbench.config import LADDER_ORDER
from ckbbench.matrix.metrics import CHAINS, leaderboard_rows, line_series_for_chain


def _attr(value: Any) -> str:
    """Escape a value for an HTML/SVG double-quoted ATTRIBUTE context. html.escape defaults to
    quote=False, which leaves a literal '\"' that would break out of data-foo=\"...\" and allow
    injection (e.g. a model name containing a quote). quote=True is mandatory for attributes."""
    return escape(str(value), quote=True)


def _text(value: Any) -> str:
    """Escape a value for an HTML/SVG TEXT context."""
    return escape(str(value), quote=False)


ARMS = LADDER_ORDER
ARM_LABELS = {
    "A": "A · floor",
    "B": "B · web",
    "C": "C · MCP+web",
    "D": "D · MCP-only",
}

FAMILY_HUE = {"Anthropic": 24, "xAI": 210, "OpenAI": 145, "Other": 280}

W = 720
H = 440
M_TOP = 28
M_RIGHT = 220
M_BOTTOM = 52
M_LEFT = 56
PLOT_W = W - M_LEFT - M_RIGHT
PLOT_H = H - M_TOP - M_BOTTOM


def _x_of(arm_idx: int) -> float:
    return M_LEFT + (PLOT_W * arm_idx) / (len(ARMS) - 1)


def _y_of(score: float) -> float:
    return M_TOP + PLOT_H * (1.0 - score)


def _fmt_delta(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}"


def _fmt_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fmt_percentage_point_delta(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value * 100:.1f} pp"


def _fmt_count(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}"


def _fmt_count_delta(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{_fmt_count(value)}"


def _fmt_seconds(value: float) -> str:
    return f"{value:.2f} s"


def _fmt_seconds_delta(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f} s"


def _mean_with_raw(
    summary: dict[str, Any] | None,
    *,
    mean_field: str,
    values_field: str,
    formatter: Any,
) -> str:
    if not summary or not isinstance(summary.get(mean_field), (int, float)):
        return "n/a"
    values = list(summary.get(values_field, ()))
    raw = ", ".join(formatter(float(value)) for value in values)
    detail = f"n={len(values)}"
    if raw:
        detail += f"; raw: {raw}"
    return f"{formatter(float(summary[mean_field]))}<br/><span class=\"raw\">{detail}</span>"


def _model_color(family: str, idx_in_family: int, family_size: int) -> str:
    hue = FAMILY_HUE.get(family, 280)
    span = 24 if family_size > 1 else 0
    if family_size > 1:
        light = 38 + (idx_in_family / (family_size - 1)) * span
    else:
        light = 50
    return f"hsl({hue} 70% {light:.1f}%)"


def _assign_colors(lines: list[dict[str, Any]]) -> dict[str, str]:
    families: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        families.setdefault(line["family"], []).append(line)
    color_by_model: dict[str, str] = {}
    for fam in sorted(families):
        members = sorted(families[fam], key=lambda l: l["model"])
        for i, line in enumerate(members):
            color_by_model[line["model"]] = _model_color(fam, i, len(members))
    return color_by_model


def render_chain_group(
    dataset: dict[str, Any],
    chain: str,
    *,
    visible: bool,
) -> str:
    """Render one chain's SVG group (never co-plotted with another chain)."""
    lines = line_series_for_chain(dataset, chain)
    color_by_model = _assign_colors(lines)
    display = "block" if visible else "none"
    parts: list[str] = [
        f'<g class="chart" data-chain="{_attr(chain)}" style="display:{display}">',
    ]

    for gy in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = _y_of(gy)
        parts.append(
            f'<line class="grid" x1="{M_LEFT}" y1="{y:.1f}" '
            f'x2="{M_LEFT + PLOT_W:.1f}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis-label y" x="{M_LEFT - 8}" y="{y + 4:.1f}">'
            f"{gy:.2f}</text>"
        )

    for i, arm in enumerate(ARMS):
        x = _x_of(i)
        parts.append(
            f'<line class="tick" x1="{x:.1f}" y1="{M_TOP}" '
            f'x2="{x:.1f}" y2="{M_TOP + PLOT_H:.1f}"/>'
        )
        parts.append(
            f'<text class="axis-label x" x="{x:.1f}" y="{M_TOP + PLOT_H + 20:.1f}">'
            f"{_text(ARM_LABELS[arm])}</text>"
        )

    parts.append(
        f'<text class="axis-title" transform="translate(16 {M_TOP + PLOT_H / 2:.1f}) '
        f'rotate(-90)">Pass@1</text>'
    )

    for line in lines:
        color = color_by_model[line["model"]]
        pts = []
        for i, arm in enumerate(ARMS):
            p = line["points"].get(arm)
            # An arm with no scored run has an UNDEFINED Pass@1. It keeps its dataset entry and its
            # published health rates, but it gets no point, whisker, band vertex or segment end: a
            # circle at zero is a measurement claim this run cannot make.
            if not _plottable(p):
                continue
            pts.append(
                {
                    "i": i,
                    "arm": arm,
                    "x": _x_of(i),
                    "mean": p["mean"],
                    "low": p["ci_low"],
                    "high": p["ci_high"],
                }
            )

        safe = "".join(c if c.isalnum() else "-" for c in line["model"])

        if pts:
            upper = [f"{p['x']:.1f},{_y_of(p['high']):.1f}" for p in pts]
            lower = [f"{p['x']:.1f},{_y_of(p['low']):.1f}" for p in reversed(pts)]
            parts.append(
                f'<polygon class="ci-band" data-model="{_attr(line["model"])}" '
                f'points="{" ".join(upper + lower)}" fill="{color}" '
                f'fill-opacity="0.12" stroke="none"/>'
            )

            poly = " ".join(f"{p['x']:.1f},{_y_of(p['mean']):.1f}" for p in pts)
            parts.append(
                f'<polyline class="model-line" data-model="{_attr(line["model"])}" '
                f'data-family="{_attr(line["family"])}" points="{poly}" '
                f'fill="none" stroke="{color}" stroke-width="2.2"/>'
            )

        b_pt = next((p for p in pts if p["arm"] == "B"), None)
        c_pt = next((p for p in pts if p["arm"] == "C"), None)
        if (
            b_pt
            and c_pt
            and line.get("comparison_readiness", {}).get("headline_eligible") is True
        ):
            parts.append(
                f'<line class="bc-segment" data-model="{_attr(line["model"])}" '
                f'x1="{b_pt["x"]:.1f}" y1="{_y_of(b_pt["mean"]):.1f}" '
                f'x2="{c_pt["x"]:.1f}" y2="{_y_of(c_pt["mean"]):.1f}" '
                f'stroke="{color}" stroke-width="5" stroke-linecap="round" '
                f'stroke-opacity="0.55"/>'
            )

        for p in pts:
            parts.append(
                f'<line class="ci-whisker" data-model="{_attr(line["model"])}" '
                f'data-arm="{p["arm"]}" x1="{p["x"]:.1f}" '
                f'y1="{_y_of(p["high"]):.1f}" x2="{p["x"]:.1f}" '
                f'y2="{_y_of(p["low"]):.1f}" stroke="{color}" '
                f'stroke-width="1.3" stroke-opacity="0.7"/>'
            )
            parts.append(
                f'<circle class="pt pt-{safe}" data-model="{_attr(line["model"])}" '
                f'data-arm="{p["arm"]}" cx="{p["x"]:.1f}" '
                f'cy="{_y_of(p["mean"]):.1f}" r="3.2" fill="{color}"/>'
            )

    ly = M_TOP + 4
    parts.append(
        f'<text class="legend-title" x="{M_LEFT + PLOT_W + 16}" y="{ly}">'
        f"model · C−B (CI)</text>"
    )
    ly += 18
    for line in lines:
        color = color_by_model[line["model"]]
        h = line.get("headline")
        if h:
            badge = (
                f"{_fmt_delta(h['delta'])} [{_fmt_delta(h['ci_low'])},"
                f"{_fmt_delta(h['ci_high'])}] {h['direction']}"
            )
            if h["significant"]:
                badge += "*"
        else:
            badge = "n/a"
        parts.append(
            f'<rect class="legend-swatch" x="{M_LEFT + PLOT_W + 16}" '
            f'y="{ly - 9:.1f}" width="12" height="12" fill="{color}"/>'
        )
        cb_attr = f"{h['delta']:.3f}" if h else ""
        dir_attr = h["direction"] if h else ""
        parts.append(
            f'<text class="legend-row" data-model="{_attr(line["model"])}" '
            f'data-cb="{_attr(cb_attr)}" data-direction="{_attr(dir_attr)}" '
            f'x="{M_LEFT + PLOT_W + 34}" y="{ly:.1f}">'
            f"{_text(line['model'])}</text>"
        )
        ly += 15
        dir_class = _attr(f"dir-{h['direction']}") if h else "dir-na"
        parts.append(
            f'<text class="legend-cb {dir_class}" '
            f'x="{M_LEFT + PLOT_W + 34}" y="{ly:.1f}">'
            f"{_text(badge)}</text>"
        )
        ly += 19

    parts.append(
        f'<text class="legend-note" x="{M_LEFT + PLOT_W + 16}" y="{ly + 4:.1f}">'
        f"* CI excludes 0</text>"
    )
    parts.append("</g>")
    return "\n".join(parts)


def _plottable(point: dict[str, Any] | None) -> bool:
    """Whether an arm has scored correctness that may be drawn.

    Mirrors the dataset-level rule: no scored run means no Pass@1, so nothing to plot.
    """
    if not point or int(point.get("scored_runs", 0)) <= 0:
        return False
    return all(
        isinstance(point.get(f), (int, float)) and not isinstance(point.get(f), bool)
        and not math.isnan(point[f]) and not math.isinf(point[f])
        for f in ("mean", "ci_low", "ci_high")
    )


def render_leaderboard_table(dataset: dict[str, Any], chain: str) -> str:
    """Secondary leaderboard table for one chain (ADR-0011)."""
    rows = leaderboard_rows(dataset, chain)
    parts = [
        f'<table class="leaderboard" data-chain="{_attr(chain)}" '
        f'aria-label="{_attr(_chain_label(chain))} run health">',
        "<thead><tr>"
        '<th scope="col">model</th><th scope="col">family</th>'
        '<th scope="col">C−B</th><th scope="col">CI</th><th scope="col">direction</th>'
        '<th scope="col">infra-fail %</th><th scope="col">violation %</th>'
        "</tr></thead><tbody>",
    ]
    for row in rows:
        h = row.get("headline")
        if h:
            delta = _fmt_delta(h["delta"])
            ci = f"[{_fmt_delta(h['ci_low'])}, {_fmt_delta(h['ci_high'])}]"
            direction = h["direction"]
            sig = "*" if h["significant"] else ""
        else:
            delta = ci = direction = "n/a"
            sig = ""
        # Health rates are PUBLISHED beside the score (RECOMMENDATION 4), never folded into Pass@1.
        infra_pct = f"{row.get('infra_fail_rate', 0.0) * 100:.0f}%"
        viol_pct = f"{row.get('protocol_violation_rate', 0.0) * 100:.0f}%"
        dir_cls = _attr(f"dir-{direction}")
        parts.append(
            "<tr>"
            f"<td>{_text(row['model'])}</td>"
            f"<td>{_text(row['family'])}</td>"
            f'<td class="{dir_cls}">{_text(delta)}{sig}</td>'
            f"<td>{_text(ci)}</td>"
            f'<td class="{dir_cls}">{_text(direction)}</td>'
            f"<td>{_text(infra_pct)}</td>"
            f"<td>{_text(viol_pct)}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _phase_one_rows(dataset: dict[str, Any], chain: str) -> list[dict[str, Any]]:
    return [
        row for row in dataset.get("phase_one_comparisons", ())
        if row.get("chain") == chain
    ]


def render_phase_one_effectiveness_table(dataset: dict[str, Any], chain: str) -> str:
    """Weighted task scores beside suite-perfect Pass@1 and infrastructure health."""
    parts = [
        f'<table class="phase-summary effectiveness" '
        f'aria-label="{_attr(_chain_label(chain))} phase-one effectiveness">',
        '<thead><tr><th scope="col">model</th><th scope="col">scored B / C</th>'
        '<th scope="col">suite passes B / C</th><th scope="col">weighted B</th>'
        '<th scope="col">weighted C</th><th scope="col">weighted C−B</th>'
        '<th scope="col">infra-fail B / C</th><th scope="col">violations B / C</th>'
        '<th scope="col">comparison basis</th>'
        "</tr></thead><tbody>",
    ]
    for row in _phase_one_rows(dataset, chain):
        b = row.get("B")
        c = row.get("C")
        scored = (
            f"{int(b['scored_runs'])}/{int(b['runs'])}" if b else "n/a"
        ) + " / " + (
            f"{int(c['scored_runs'])}/{int(c['runs'])}" if c else "n/a"
        )
        suite_passes = _suite_pass_count(b) + " / " + _suite_pass_count(c)
        weighted_b = _mean_with_raw(
            b, mean_field="weighted_score_mean", values_field="weighted_score_values",
            formatter=_fmt_percent,
        )
        weighted_c = _mean_with_raw(
            c, mean_field="weighted_score_mean", values_field="weighted_score_values",
            formatter=_fmt_percent,
        )
        delta = row.get("weighted_score_delta")
        delta_text = (
            _fmt_percentage_point_delta(float(delta))
            if isinstance(delta, (int, float)) and not isinstance(delta, bool)
            else "n/a"
        )
        infra = (
            _fmt_percent(float(b["infra_fail_rate"])) if b else "n/a"
        ) + " / " + (
            _fmt_percent(float(c["infra_fail_rate"])) if c else "n/a"
        )
        violations = (
            _fmt_percent(float(b["protocol_violation_rate"])) if b else "n/a"
        ) + " / " + (
            _fmt_percent(float(c["protocol_violation_rate"])) if c else "n/a"
        )
        basis = _comparison_basis(row)
        parts.append(
            "<tr>"
            f"<td>{_text(row['model'])}</td><td>{_text(scored)}</td>"
            f"<td>{_text(suite_passes)}</td>"
            f"<td>{weighted_b}</td><td>{weighted_c}</td>"
            f"<td>{_text(delta_text)}</td><td>{_text(infra)}</td>"
            f"<td>{_text(violations)}</td><td>{_text(basis)}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _suite_pass_count(summary: dict[str, Any] | None) -> str:
    if not summary:
        return "n/a"
    scored_runs = int(summary["scored_runs"])
    if scored_runs == 0:
        return "n/a (0/0)"
    return f"{int(summary['suite_passes'])}/{scored_runs}"


def _task_rate(summary: dict[str, Any] | None) -> str:
    if not summary:
        return "n/a"
    return (
        f"{int(summary['passes'])}/{int(summary['runs'])} "
        f"({_fmt_percent(float(summary['pass_rate']))})"
    )


def render_phase_one_task_table(dataset: dict[str, Any], chain: str) -> str:
    """Per-task scored pass counts and descriptive C-minus-B differences."""
    parts = [
        f'<table class="phase-summary task-rates" '
        f'aria-label="{_attr(_chain_label(chain))} phase-one task outcomes">',
        '<thead><tr><th scope="col">model</th><th scope="col">task</th>'
        '<th scope="col">B passes</th><th scope="col">C passes</th>'
        '<th scope="col">pass-rate C−B</th></tr></thead><tbody>',
    ]
    for row in _phase_one_rows(dataset, chain):
        for task in row.get("task_comparisons", ()):
            delta = task.get("pass_rate_delta")
            delta_text = (
                _fmt_percentage_point_delta(float(delta))
                if isinstance(delta, (int, float)) and not isinstance(delta, bool)
                else "n/a"
            )
            parts.append(
                "<tr>"
                f"<td>{_text(row['model'])}</td><td>{_text(task['task_id'])}</td>"
                f"<td>{_text(_task_rate(task.get('B')))}</td>"
                f"<td>{_text(_task_rate(task.get('C')))}</td>"
                f"<td>{_text(delta_text)}</td>"
                "</tr>"
            )
    parts.append("</tbody></table>")
    return "\n".join(parts)


def render_phase_one_efficiency_table(dataset: dict[str, Any], chain: str) -> str:
    """Token and wall means over the same complete-usage rows; gaps stay visible but excluded."""
    parts = [
        f'<table class="phase-summary efficiency" '
        f'aria-label="{_attr(_chain_label(chain))} phase-one efficiency">',
        '<thead><tr><th scope="col">model</th><th scope="col">usage n B / C</th>'
        '<th scope="col">tokens B</th><th scope="col">tokens C</th>'
        '<th scope="col">tokens C−B</th><th scope="col">agent wall B</th>'
        '<th scope="col">agent wall C</th><th scope="col">wall C−B</th>'
        '<th scope="col">usage gaps B / C</th><th scope="col">efficiency basis</th>'
        '</tr></thead><tbody>',
    ]
    for row in _phase_one_rows(dataset, chain):
        b = row.get("B")
        c = row.get("C")
        usage_n = (
            str(int(b["efficiency_runs"])) if b else "n/a"
        ) + " / " + (
            str(int(c["efficiency_runs"])) if c else "n/a"
        )
        tokens_b = _mean_with_raw(
            b, mean_field="total_tokens_mean", values_field="total_tokens_values",
            formatter=_fmt_count,
        )
        tokens_c = _mean_with_raw(
            c, mean_field="total_tokens_mean", values_field="total_tokens_values",
            formatter=_fmt_count,
        )
        token_delta = row.get("total_tokens_delta")
        token_delta_text = (
            _fmt_count_delta(float(token_delta))
            if isinstance(token_delta, (int, float)) and not isinstance(token_delta, bool)
            else "n/a"
        )
        wall_b = _mean_with_raw(
            b, mean_field="agent_wall_seconds_mean",
            values_field="agent_wall_seconds_values", formatter=_fmt_seconds,
        )
        wall_c = _mean_with_raw(
            c, mean_field="agent_wall_seconds_mean",
            values_field="agent_wall_seconds_values", formatter=_fmt_seconds,
        )
        wall_delta = row.get("agent_wall_seconds_delta")
        wall_delta_text = (
            _fmt_seconds_delta(float(wall_delta))
            if isinstance(wall_delta, (int, float)) and not isinstance(wall_delta, bool)
            else "n/a"
        )
        usage_gaps = _usage_gaps(b) + " / " + _usage_gaps(c)
        token_basis = _efficiency_basis(row)
        parts.append(
            "<tr>"
            f"<td>{_text(row['model'])}</td><td>{_text(usage_n)}</td>"
            f"<td>{tokens_b}</td><td>{tokens_c}</td><td>{_text(token_delta_text)}</td>"
            f"<td>{wall_b}</td><td>{wall_c}</td><td>{_text(wall_delta_text)}</td>"
            f"<td>{_text(usage_gaps)}</td><td>{_text(token_basis)}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _usage_gaps(summary: dict[str, Any] | None) -> str:
    if not summary:
        return "n/a"
    return (
        f"{int(summary['incomplete_usage_runs'])} incomplete, "
        f"{int(summary['not_started_usage_runs'])} not started"
    )


def _efficiency_basis(row: dict[str, Any]) -> str:
    readiness = row.get("efficiency_readiness", {})
    if readiness.get("comparison_eligible") is True:
        return "eligible; complete usage for matched scored seeds"
    reasons = set(readiness.get("reasons", ()))
    labels = []
    if "incomplete_usage_in_scored_rows" in reasons:
        labels.append("scored rows with incomplete usage")
    if "unbalanced_complete_usage_runs" in reasons:
        labels.append("unbalanced complete-usage runs")
    if "unmatched_complete_usage_seed_multiset" in reasons:
        labels.append("unmatched complete-usage seeds")
    if "correctness_cohort_not_ready" in reasons:
        labels.append("correctness cohort not ready")
    return "ineligible" + ("; " + "; ".join(labels) if labels else "")


def _chain_label(chain: str) -> str:
    return {"devnet": "DevNet", "testnet": "TestNet"}.get(chain, chain)


def _chain_has_data(dataset: dict[str, Any], chain: str) -> bool:
    return any(
        row.get("chain") == chain and int(row.get("runs", 0)) > 0
        for row in dataset.get("cells", ())
    )


def _overview_mean(summary: dict[str, Any] | None, field: str, formatter: Any) -> str:
    if not summary or not isinstance(summary.get(field), (int, float)):
        return "n/a"
    return formatter(float(summary[field]))


def _comparison_basis(row: dict[str, Any]) -> str:
    readiness = row.get("comparison_readiness", {})
    if readiness.get("headline_eligible") is True:
        return "headline-eligible; balanced paired seeds"
    reasons = set(readiness.get("reasons", ()))
    labels = []
    if "completion_conditioned" in reasons:
        labels.append("completion-conditioned")
    if "fewer_than_three_scored_runs_per_arm" in reasons:
        labels.append("fewer than 3 scored runs per arm")
    if "unbalanced_scored_runs" in reasons:
        labels.append("unbalanced scored runs")
    if "unmatched_scored_seed_multiset" in reasons:
        labels.append("unmatched scored seeds")
    return "provisional" + ("; " + "; ".join(labels) if labels else "")


def _comparison_note(row: dict[str, Any]) -> str:
    readiness = row.get("comparison_readiness", {})
    recorded = readiness.get("recorded_rows", {})
    scored = readiness.get("scored_runs", {})
    b_recorded, c_recorded = int(recorded.get("B", 0)), int(recorded.get("C", 0))
    b_scored, c_scored = int(scored.get("B", 0)), int(scored.get("C", 0))
    b_excluded, c_excluded = b_recorded - b_scored, c_recorded - c_scored
    minimum = int(readiness.get("minimum_scored_runs_per_arm", 3))

    if readiness.get("completion_conditioned") is True:
        title = "Survivorship warning."
        excluded = (
            f" Excluded infrastructure failures: B {b_excluded}, C {c_excluded}. Conditioning "
            "the means on which runs completed may bias every displayed C−B value."
        )
    else:
        title = "Preliminary comparison."
        excluded = ""

    if readiness.get("headline_eligible") is True:
        requirement = (
            " The evidence meets the report's descriptive headline floor, but causal inference "
            "still requires comparable, predeclared trials."
        )
        note_class = "evidence-note-ready"
    else:
        requirement = (
            f" The lead verdict stays inconclusive until each arm has at least {minimum} scored "
            "runs, equal scored counts and matching seed sets, with no excluded run."
        )
        note_class = "evidence-note-warning"

    text = (
        f"B's score uses {b_scored} of {b_recorded} recorded rows; C's uses "
        f"{c_scored} of {c_recorded}.{excluded}{requirement}"
    )
    return (
        f'<div class="evidence-note {note_class}" role="note">'
        f"<strong>{_text(title)}</strong> {_text(text)}</div>"
    )


def _signal_for_delta(delta: Any, readiness: dict[str, Any]) -> tuple[str, str]:
    if readiness.get("headline_eligible") is not True:
        return "Inconclusive", "neutral"
    if not isinstance(delta, (int, float)) or isinstance(delta, bool):
        return "Evidence incomplete", "neutral"
    if delta > 0:
        return "Observed positive difference", "positive"
    if delta < 0:
        return "Observed negative difference", "negative"
    return "No observed difference", "neutral"


def _display_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%b %d, %Y · %H:%M UTC").replace(" 0", " ")


def render_phase_one_overview(dataset: dict[str, Any], chain: str) -> str:
    """Lead with the observed treatment result before the detailed tables."""
    parts: list[str] = []
    for index, row in enumerate(_phase_one_rows(dataset, chain)):
        b = row.get("B")
        c = row.get("C")
        weighted_delta = row.get("weighted_score_delta")
        readiness = row.get("comparison_readiness", {})
        signal, signal_class = _signal_for_delta(weighted_delta, readiness)
        delta_text = (
            _fmt_percentage_point_delta(float(weighted_delta))
            if isinstance(weighted_delta, (int, float)) and not isinstance(weighted_delta, bool)
            else "n/a"
        )
        token_delta = row.get("total_tokens_delta")
        token_text = (
            _fmt_count_delta(float(token_delta))
            if isinstance(token_delta, (int, float)) and not isinstance(token_delta, bool)
            else "n/a"
        )
        wall_delta = row.get("agent_wall_seconds_delta")
        wall_text = (
            _fmt_seconds_delta(float(wall_delta))
            if isinstance(wall_delta, (int, float)) and not isinstance(wall_delta, bool)
            else "n/a"
        )
        b_weighted = _overview_mean(b, "weighted_score_mean", _fmt_percent)
        c_weighted = _overview_mean(c, "weighted_score_mean", _fmt_percent)
        b_infra = _overview_mean(b, "infra_fail_rate", _fmt_percent)
        c_infra = _overview_mean(c, "infra_fail_rate", _fmt_percent)
        heading_id = f"phase-result-heading-{index}"
        parts.append(
            f'<section class="result-panel" aria-labelledby="{heading_id}">'
            '<div class="result-panel-head">'
            '<div><p class="eyebrow">Phase one evidence</p>'
            f'<h2 id="{heading_id}">{_text(row["model"])}</h2></div>'
            f'<span class="signal signal-{_attr(signal_class)}">{_text(signal)}</span>'
            '</div>'
            '<p class="result-context">Arm B is web research without CKB AI. Arm C adds the '
            'fixed CKB AI documentation surface. Values below are descriptive differences of arm '
            'means, not paired inference.</p>'
            f'{_comparison_note(row)}'
            '<div class="metric-strip">'
            '<div class="metric"><span>Observed weighted score C−B</span>'
            f'<strong class="metric-{_attr(signal_class)}">{_text(delta_text)}</strong>'
            f'<small>B {_text(b_weighted)} · C {_text(c_weighted)} · scored runs only</small></div>'
            '<div class="metric"><span>Complete tokens C−B</span>'
            f'<strong>{_text(token_text)}</strong><small>Lower is more efficient; matched complete '
            'usage only</small></div>'
            '<div class="metric"><span>Agent time C−B</span>'
            f'<strong>{_text(wall_text)}</strong><small>Lower is faster</small></div>'
            '<div class="metric"><span>Infrastructure failures</span>'
            f'<strong>{_text(b_infra)} / {_text(c_infra)}</strong><small>B / C, all runs</small></div>'
            '</div></section>'
        )
    return "\n".join(parts)


def render_empty_chain(chain: str) -> str:
    label = _chain_label(chain)
    return (
        '<section class="empty-state" role="status">'
        '<div class="empty-state-mark" aria-hidden="true">0</div>'
        f'<div><p class="eyebrow">{_text(label)} evidence</p>'
        f'<h2>No {_text(label)} runs yet</h2>'
        '<p>This view stays empty until result rows are recorded for this chain. DevNet evidence '
        'is never copied, merged or inferred across the chain boundary.</p></div></section>'
    )


def render_ladder_html(dataset: dict[str, Any]) -> str:
    """Build the full self-contained HTML page (deterministic)."""
    synthetic = bool(dataset.get("_SYNTHETIC"))
    chains = list(CHAINS)
    active_chain = next((ch for ch in chains if _chain_has_data(dataset, ch)), chains[0])

    # The toggle reads the chain from data-chain (no per-button inline JS string), so the chain
    # value never enters a JS-string context in an attribute.
    toggle_btns = "\n".join(
        f'<button class="chain-btn{" active" if ch == active_chain else ""}" '
        f'data-chain="{_attr(ch)}" aria-controls="chain-view-{_attr(ch)}" '
        f'aria-pressed="{str(ch == active_chain).lower()}" '
        f'onclick="showChain(this.getAttribute(\'data-chain\'))">'
        f'<span>{_text(_chain_label(ch))}</span>'
        f'<small>{"Results" if _chain_has_data(dataset, ch) else "No data"}</small></button>'
        for ch in chains
    )

    chain_views: list[str] = []
    for ch in chains:
        hidden = "" if ch == active_chain else " hidden"
        label = _chain_label(ch)
        if not _chain_has_data(dataset, ch):
            content = render_empty_chain(ch)
        else:
            content = (
                f'{render_phase_one_overview(dataset, ch)}'
                '<section class="report-section">'
                '<div class="section-heading"><div><p class="eyebrow">Correctness</p>'
                f'<h2>Effectiveness · {_text(label)}</h2></div>'
                '<p>Suite-perfect outcomes and weighted task scores, with excluded infrastructure '
                'runs kept visible.</p></div>'
                f'<div class="table-wrap">{render_phase_one_effectiveness_table(dataset, ch)}</div>'
                '</section>'
                '<section class="report-section">'
                '<div class="section-heading"><div><p class="eyebrow">Task detail</p>'
                '<h2>Where the outcomes changed</h2></div>'
                '<p>Observed pass counts preserve every scored denominator.</p></div>'
                f'<div class="table-wrap">{render_phase_one_task_table(dataset, ch)}</div>'
                '</section>'
                '<section class="report-section">'
                '<div class="section-heading"><div><p class="eyebrow">Efficiency</p>'
                '<h2>Tokens and agent time</h2></div>'
                '<p>Only complete provider usage enters token means. Missing usage remains '
                'visible, and a token delta requires complete usage for every matched scored '
                'row.</p></div>'
                f'<div class="table-wrap">{render_phase_one_efficiency_table(dataset, ch)}</div>'
                '</section>'
                '<section class="report-section">'
                '<div class="section-heading"><div><p class="eyebrow">Condition ladder</p>'
                '<h2>Pass@1 by treatment arm</h2></div>'
                '<p>Confidence intervals are shown for scored runs. Missing denominators are not '
                'plotted as zero.</p></div>'
                '<div class="chart-frame"><div class="chart-scroll">'
                f'<svg class="chart-svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
                f'role="img" aria-label="{_attr(label)} condition ladder chart">'
                f'<title>{_text(label)} condition ladder chart</title>'
                f'{render_chain_group(dataset, ch, visible=True)}</svg></div></div>'
                '</section>'
                '<section class="report-section">'
                '<div class="section-heading"><div><p class="eyebrow">Reliability</p>'
                f'<h2>Run health · {_text(label)}</h2></div>'
                '<p>Infrastructure and protocol failures are reported beside correctness, never '
                'folded into it.</p></div>'
                f'<div class="table-wrap">{render_leaderboard_table(dataset, ch)}</div>'
                '</section>'
            )
        chain_views.append(
            f'<div class="chain-view" id="chain-view-{_attr(ch)}" '
            f'data-chain="{_attr(ch)}"{hidden}>{content}</div>'
        )

    js = """
function showChain(chain){
  document.querySelectorAll('.chain-view').forEach(function(view){
    view.hidden = view.getAttribute('data-chain') !== chain;
  });
  document.querySelectorAll('.chain-btn').forEach(function(b){
    var selected = b.getAttribute('data-chain') === chain;
    b.classList.toggle('active', selected);
    b.setAttribute('aria-pressed', selected ? 'true' : 'false');
  });
}
"""

    banner = ""
    if synthetic:
        banner = (
            '<div class="synthetic-banner">SYNTHETIC DATA - fabricated, '
            "NOT a real benchmark result. Do not cite.</div>"
        )

    title_suffix = " (SYNTHETIC)" if synthetic else ""
    results_through = str(dataset.get("generated_at", "timestamp unavailable"))
    suites = ", ".join(str(item) for item in dataset.get("suites", ())) or "n/a"
    models = len(dataset.get("models", ()))
    run_count = sum(int(row.get("runs", 0)) for row in dataset.get("cells", ()))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="color-scheme" content="light dark"/>
<title>CKB AI Bench - Benchmark Results{title_suffix}</title>
<style>
  :root {{
    color-scheme: light dark;
    --canvas: #f4f7f5;
    --surface: #ffffff;
    --surface-subtle: #f8faf9;
    --ink: #171c19;
    --muted: #59635d;
    --faint: #78827c;
    --border: #d9e0dc;
    --border-strong: #bcc8c0;
    --accent: #087a50;
    --accent-strong: #05633f;
    --accent-soft: #e3f4ec;
    --positive: #087a50;
    --negative: #b42318;
    --negative-soft: #fce8e6;
    --warning: #8a6100;
    --warning-soft: #fff7df;
    --focus: #1685d1;
    --chart-grid: #e7ece9;
    --chart-tick: #f0f3f1;
  }}
  * {{ box-sizing: border-box; letter-spacing: 0; }}
  html {{ background: var(--canvas); }}
  body {{
    margin: 0; color: var(--ink); background: var(--canvas);
    font: 14px/1.55 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  [hidden] {{ display: none !important; }}
  .page-shell {{ width: min(100% - 40px, 1200px); margin: 0 auto; padding: 24px 0 72px; }}
  .site-header {{
    display: flex; align-items: center; justify-content: space-between; gap: 20px;
    padding: 0 0 20px; border-bottom: 1px solid var(--border);
  }}
  .brand {{ display: flex; align-items: center; gap: 10px; font-weight: 750; }}
  .brand-mark {{
    width: 18px; height: 18px; border: 5px solid var(--ink); border-right-color: var(--accent);
    transform: rotate(45deg);
  }}
  .report-status {{ color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }}
  .hero {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 32px; padding: 48px 0 32px; }}
  .eyebrow {{ margin: 0 0 6px; color: var(--accent-strong); font-size: 12px; font-weight: 750; }}
  h1 {{ margin: 0; max-width: 760px; font-size: 38px; line-height: 1.08; font-weight: 760; }}
  h2 {{ margin: 0; font-size: 20px; line-height: 1.25; font-weight: 720; }}
  .hero-copy {{ max-width: 68ch; margin: 16px 0 0; color: var(--muted); font-size: 16px; }}
  .hero-meta {{
    display: grid; align-content: end; min-width: 220px; margin: 0;
    border-top: 1px solid var(--border-strong);
  }}
  .hero-meta div {{ display: flex; justify-content: space-between; gap: 20px; padding: 9px 0; border-bottom: 1px solid var(--border); }}
  .hero-meta dt {{ color: var(--muted); }}
  .hero-meta dd {{ margin: 0; font-weight: 680; font-variant-numeric: tabular-nums; text-align: right; }}
  .synthetic-banner {{
    background: var(--negative); color: #fff; font-weight: 750;
    padding: 10px 14px; border-radius: 6px; margin-bottom: 16px;
  }}
  .toolbar {{
    display: flex; align-items: center; justify-content: space-between; gap: 20px;
    padding: 16px 0 24px; border-top: 1px solid var(--border);
  }}
  .toolbar-label {{ color: var(--muted); font-size: 13px; }}
  .segmented {{ display: inline-flex; padding: 3px; background: var(--surface); border: 1px solid var(--border); border-radius: 7px; }}
  .chain-btn {{
    min-width: 112px; min-height: 42px; display: flex; align-items: center; justify-content: space-between;
    gap: 12px; padding: 6px 10px; cursor: pointer; color: var(--muted);
    font: inherit; font-weight: 680; background: transparent; border: 1px solid transparent;
    border-radius: 5px; transition: background-color 100ms ease-out, color 100ms ease-out,
      border-color 100ms ease-out;
  }}
  .chain-btn small {{ color: var(--faint); font-size: 10px; font-weight: 620; }}
  .chain-btn:hover {{ background: var(--surface-subtle); color: var(--ink); }}
  .chain-btn.active {{ background: var(--ink); color: var(--surface); border-color: var(--ink); }}
  .chain-btn.active small {{ color: var(--surface); opacity: .72; }}
  .chain-btn:focus-visible {{ outline: 3px solid var(--focus); outline-offset: 3px; }}
  .result-panel {{
    margin: 0 0 44px; background: var(--surface); border: 1px solid var(--border-strong);
    border-radius: 8px; overflow: hidden;
  }}
  .result-panel-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 22px 24px; }}
  .result-context {{ max-width: 76ch; margin: -8px 24px 22px; color: var(--muted); }}
  .signal {{ padding: 7px 10px; border-radius: 4px; font-size: 12px; font-weight: 750; white-space: nowrap; }}
  .signal-positive {{ color: var(--positive); background: var(--accent-soft); }}
  .signal-negative {{ color: var(--negative); background: var(--negative-soft); }}
  .signal-neutral {{ color: var(--muted); background: var(--surface-subtle); border: 1px solid var(--border); }}
  .evidence-note {{ margin: 0; padding: 14px 24px; border-top: 1px solid var(--border); font-size: 12px; }}
  .evidence-note-warning {{ color: var(--warning); background: var(--warning-soft); }}
  .evidence-note-ready {{ color: var(--accent-strong); background: var(--accent-soft); }}
  .metric-strip {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 1px solid var(--border); }}
  .metric {{ min-width: 0; padding: 18px 20px; border-right: 1px solid var(--border); }}
  .metric:last-child {{ border-right: 0; }}
  .metric span, .metric small {{ display: block; color: var(--muted); font-size: 11px; }}
  .metric strong {{ display: block; margin: 5px 0 3px; font: 720 22px/1.15 ui-monospace, SFMono-Regular, Consolas, monospace; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }}
  .metric .metric-positive {{ color: var(--positive); }}
  .metric .metric-negative {{ color: var(--negative); }}
  .report-section {{ padding: 32px 0 8px; border-top: 1px solid var(--border); }}
  .section-heading {{ display: flex; align-items: end; justify-content: space-between; gap: 28px; margin: 0 0 16px; }}
  .section-heading > p {{ max-width: 58ch; margin: 0; color: var(--muted); font-size: 13px; text-align: right; }}
  .chart-frame, .table-wrap {{ overflow: hidden; background: var(--surface); border: 1px solid var(--border); border-radius: 7px; }}
  .chart-scroll, .table-wrap {{ overflow-x: auto; overscroll-behavior-inline: contain; }}
  .chart-svg {{ display: block; width: 100%; min-width: 720px; height: auto; background: var(--surface); }}
  .grid {{ stroke: var(--chart-grid); stroke-width: 1; }}
  .tick {{ stroke: var(--chart-tick); stroke-width: 1; }}
  .axis-label {{ fill: var(--muted); font-size: 11px; }}
  .axis-label.y {{ text-anchor: end; }}
  .axis-label.x {{ text-anchor: middle; }}
  .axis-title {{ fill: var(--ink); font-size: 12px; text-anchor: middle; }}
  .legend-title {{ fill: var(--ink); font-size: 11px; font-weight: 750; }}
  .legend-row {{ fill: var(--ink); font-size: 12px; }}
  .legend-cb {{ font-size: 10.5px; }}
  .legend-cb.dir-positive {{ fill: var(--positive); }}
  .legend-cb.dir-negative {{ fill: var(--negative); }}
  .legend-cb.dir-flat {{ fill: var(--muted); }}
  .legend-note {{ fill: var(--faint); font-size: 10px; }}
  table.leaderboard, table.phase-summary {{ border-collapse: collapse; width: 100%; min-width: 780px; font-variant-numeric: tabular-nums; }}
  table.leaderboard th, table.leaderboard td,
  table.phase-summary th, table.phase-summary td {{
    border-bottom: 1px solid var(--border); padding: 11px 13px; text-align: left; vertical-align: top;
  }}
  table.leaderboard tr:last-child td, table.phase-summary tr:last-child td {{ border-bottom: 0; }}
  table.leaderboard th, table.phase-summary th {{ color: var(--muted); background: var(--surface-subtle); font-size: 11px; font-weight: 750; white-space: nowrap; }}
  table.leaderboard tbody tr:hover, table.phase-summary tbody tr:hover {{ background: var(--surface-subtle); }}
  table.phase-summary {{ font-size: 12px; }}
  table.phase-summary .raw {{ color: var(--faint); font-size: 10px; white-space: nowrap; }}
  table.effectiveness th:not(:first-child), table.effectiveness td:not(:first-child),
  table.efficiency th:not(:first-child), table.efficiency td:not(:first-child),
  table.task-rates th:nth-child(n+3), table.task-rates td:nth-child(n+3),
  table.leaderboard th:nth-child(n+3), table.leaderboard td:nth-child(n+3) {{ text-align: right; }}
  .dir-positive {{ color: var(--positive); }}
  .dir-negative {{ color: var(--negative); }}
  .dir-flat {{ color: var(--muted); }}
  .empty-state {{
    display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 24px; align-items: center;
    margin-top: 16px; padding: 42px 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  }}
  .empty-state-mark {{
    display: grid; place-items: center; width: 72px; height: 72px; border: 1px solid var(--border-strong);
    border-radius: 50%; color: var(--faint); font: 720 24px/1 ui-monospace, monospace;
  }}
  .empty-state p:last-child {{ max-width: 62ch; margin: 8px 0 0; color: var(--muted); }}
  .method-note {{ max-width: 80ch; margin: 40px 0 0; padding-top: 20px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--border); }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --canvas: #101311; --surface: #181c19; --surface-subtle: #1e2420; --ink: #eef3ef;
      --muted: #acb7b0; --faint: #8d9991; --border: #303832; --border-strong: #465149;
      --accent: #45c995; --accent-strong: #70d9af; --accent-soft: #173b2d;
      --positive: #70d9af; --negative: #ff8d84; --negative-soft: #472522;
      --warning: #f2cc75; --warning-soft: #372e17; --focus: #67b7f1;
      --chart-grid: #2b332e; --chart-tick: #242a26;
    }}
    .chain-btn.active {{ color: #101311; background: var(--ink); border-color: var(--ink); }}
    .chain-btn.active small {{ color: #101311; }}
  }}
  @media (max-width: 760px) {{
    .page-shell {{ width: min(100% - 28px, 1200px); padding-top: 16px; }}
    .site-header, .hero, .toolbar, .section-heading, .result-panel-head {{ align-items: stretch; }}
    .site-header, .toolbar, .section-heading, .result-panel-head {{ flex-direction: column; }}
    .hero {{ grid-template-columns: 1fr; gap: 24px; padding: 34px 0 24px; }}
    h1 {{ font-size: 30px; }}
    .hero-meta {{ min-width: 0; }}
    .segmented {{ display: grid; grid-template-columns: 1fr 1fr; width: 100%; }}
    .chain-btn {{ min-width: 0; }}
    .metric-strip {{ grid-template-columns: 1fr 1fr; }}
    .metric:nth-child(2) {{ border-right: 0; }}
    .metric:nth-child(-n+2) {{ border-bottom: 1px solid var(--border); }}
    .section-heading > p {{ text-align: left; }}
    .signal {{ align-self: flex-start; white-space: normal; }}
  }}
  @media (max-width: 460px) {{
    .metric-strip {{ grid-template-columns: 1fr; }}
    .metric {{ border-right: 0; border-bottom: 1px solid var(--border); }}
    .metric:last-child {{ border-bottom: 0; }}
    .empty-state {{ grid-template-columns: 1fr; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .chain-btn {{ transition: none; }}
  }}
  @media print {{
    :root {{ color-scheme: light; }}
    .page-shell {{ width: 100%; padding: 0; }}
    .toolbar {{ display: none; }}
    .chain-view[hidden] {{ display: block !important; break-before: page; }}
    .chart-scroll, .table-wrap {{ overflow: visible; }}
    .chart-svg, table.leaderboard, table.phase-summary {{ min-width: 0; }}
  }}
</style>
</head>
<body>
  <main class="page-shell">
    {banner}
    <div class="site-header">
      <div class="brand"><span class="brand-mark" aria-hidden="true"></span>CKB AI Bench</div>
      <div class="report-status">Phase one · static reproducible report</div>
    </div>
    <header class="hero">
      <div>
        <p class="eyebrow">Benchmark report</p>
        <h1>Does CKB AI improve CKB development?</h1>
        <p class="hero-copy">The same model attempts a frozen suite with and without the CKB AI
        documentation surface. Correctness, token usage, agent time and infrastructure health stay
        visible together.</p>
      </div>
      <dl class="hero-meta">
        <div><dt>Suite</dt><dd>{_text(suites)}</dd></div>
        <div><dt>Models</dt><dd>{models}</dd></div>
        <div><dt>Recorded runs</dt><dd>{run_count}</dd></div>
        <div><dt>Results through</dt><dd><time datetime="{_attr(results_through)}">{_text(_display_timestamp(results_through))}</time></dd></div>
      </dl>
    </header>
    <div class="toolbar" aria-label="Report chain selector">
      <span class="toolbar-label">Chain results are reported separately and never merged.</span>
      <div class="segmented">{toggle_btns}</div>
    </div>
    {''.join(chain_views)}
    <p class="method-note">A bold B→C chart segment and leaderboard delta appear only after both
    arms have at least three scored runs, equal scored counts, matching seed sets and no excluded
    infrastructure run. Raw completion-conditioned values remain visible in the detailed tables,
    but they are provisional rather than a verdict. Token differences additionally require complete
    usage for every matched scored row. Causal interpretation still requires comparable,
    predeclared trials.</p>
  </main>
  <script>{js}</script>
</body>
</html>
"""


def write_site(output_dir: Path | str, dataset: dict[str, Any]) -> Path:
    """Write deterministic ladder HTML to ``site/index.html``."""
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "index.html"
    html = render_ladder_html(dataset)
    path.write_text(html, encoding="utf-8")
    return path
