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

MODEL_LINE_COLORS = (
    "#ffcc66",
    "#c59cff",
    "#ff8a65",
    "#ff7eb6",
    "#91a7ff",
    "#53e0c1",
    "#ff6f73",
    "#e5ed69",
)
MODEL_LINE_PATTERNS = (
    None,
    "8 5",
    "2 4",
    "12 4 2 4",
    "5 3",
    "1 3",
    "10 3 2 3",
    "14 5",
)
MODEL_TONE_COUNT = len(MODEL_LINE_COLORS)

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


def _assign_colors(dataset: dict[str, Any]) -> dict[str, str]:
    return {
        model: MODEL_LINE_COLORS[tone]
        for model, tone in _model_tones(dataset).items()
    }


def render_chain_group(
    dataset: dict[str, Any],
    chain: str,
    *,
    visible: bool,
) -> str:
    """Render one chain's SVG group (never co-plotted with another chain)."""
    lines = line_series_for_chain(dataset, chain)
    color_by_model = _assign_colors(dataset)
    tone_by_model = _model_tones(dataset)
    selected_model = _preferred_model(dataset, [line["model"] for line in lines])
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
        tone = tone_by_model[line["model"]]
        pattern = MODEL_LINE_PATTERNS[tone]
        dash_attr = f' stroke-dasharray="{pattern}"' if pattern else ""
        hidden = "" if line["model"] == selected_model else ' style="display:none"'
        parts.append(
            f'<g class="plot-model" data-model="{_attr(line["model"])}"{hidden}>'
        )
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
                f'fill="none" stroke="{color}" stroke-width="2.8"{dash_attr}/>'
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
                f'stroke-opacity="0.7"{dash_attr}/>'
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
        parts.append("</g>")

    ly = M_TOP + 4
    parts.append(
        f'<text class="legend-title" x="{M_LEFT + PLOT_W + 16}" y="{ly}">'
        f"model · C−B (CI)</text>"
    )
    ly += 18
    legend_y = ly
    for line in lines:
        color = color_by_model[line["model"]]
        tone = tone_by_model[line["model"]]
        pattern = MODEL_LINE_PATTERNS[tone]
        dash_attr = f' stroke-dasharray="{pattern}"' if pattern else ""
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
        hidden = "" if line["model"] == selected_model else ' style="display:none"'
        parts.append(
            f'<g class="legend-model" data-model="{_attr(line["model"])}"{hidden}>'
        )
        parts.append(
            f'<line class="legend-swatch" x1="{M_LEFT + PLOT_W + 16}" '
            f'y1="{legend_y - 4:.1f}" x2="{M_LEFT + PLOT_W + 28}" '
            f'y2="{legend_y - 4:.1f}" '
            f'stroke="{color}" stroke-width="4" stroke-linecap="round"{dash_attr}/>'
        )
        cb_attr = f"{h['delta']:.3f}" if h else ""
        dir_attr = h["direction"] if h else ""
        parts.append(
            f'<text class="legend-row" data-model="{_attr(line["model"])}" '
            f'data-cb="{_attr(cb_attr)}" data-direction="{_attr(dir_attr)}" '
            f'x="{M_LEFT + PLOT_W + 34}" y="{legend_y:.1f}">'
            f"{_text(line['model'])}</text>"
        )
        dir_class = _attr(f"dir-{h['direction']}") if h else "dir-na"
        parts.append(
            f'<text class="legend-cb {dir_class}" '
            f'x="{M_LEFT + PLOT_W + 34}" y="{legend_y + 15:.1f}">'
            f"{_text(badge)}</text>"
        )
        parts.append("</g>")

    parts.append(
        f'<text class="legend-note" x="{M_LEFT + PLOT_W + 16}" '
        f'y="{legend_y + 38:.1f}">'
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
            f'<tr data-model="{_attr(row["model"])}">'
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


def _profile_id(row: dict[str, Any]) -> str:
    for arm in ("B", "C"):
        summary = row.get(arm)
        if isinstance(summary, dict) and summary.get("model_profile_id"):
            return str(summary["model_profile_id"])
    return "n/a"


def _arm_pair(row: dict[str, Any], field: str, formatter: Any = str) -> str:
    values = []
    for arm in ("B", "C"):
        summary = row.get(arm)
        value = summary.get(field) if isinstance(summary, dict) else None
        values.append(formatter(value) if value is not None else "n/a")
    return " / ".join(values)


def render_model_comparison_table(dataset: dict[str, Any], chain: str) -> str:
    """One compact cross-model view before the detailed per-model evidence."""
    parts = [
        f'<table class="phase-summary model-comparison" '
        f'aria-label="{_attr(_chain_label(chain))} model comparison">',
        '<thead><tr><th scope="col">model</th><th scope="col">status</th>'
        '<th scope="col">scored B / C</th><th scope="col">weighted B / C</th>'
        '<th scope="col">weighted C−B</th><th scope="col">tokens C−B</th>'
        '<th scope="col">history compactions B / C</th><th scope="col">profile</th>'
        '</tr></thead><tbody>',
    ]
    for row in _phase_one_rows(dataset, chain):
        readiness = row.get("comparison_readiness", {})
        status, status_class = _signal_for_delta(row.get("weighted_score_delta"), readiness)
        weighted_delta = row.get("weighted_score_delta")
        weighted_delta_text = (
            _fmt_percentage_point_delta(float(weighted_delta))
            if isinstance(weighted_delta, (int, float)) and not isinstance(weighted_delta, bool)
            else "n/a"
        )
        token_delta = row.get("total_tokens_delta")
        token_delta_text = (
            _fmt_count_delta(float(token_delta))
            if isinstance(token_delta, (int, float)) and not isinstance(token_delta, bool)
            else "n/a"
        )
        scored = _arm_pair(row, "scored_runs", lambda value: str(int(value)))
        weighted = _arm_pair(
            row, "weighted_score_mean", lambda value: _fmt_percent(float(value))
        )
        compactions = _arm_pair(
            row, "history_compaction_count", lambda value: str(int(value))
        )
        parts.append(
            f'<tr data-model="{_attr(row["model"])}">'
            f'<td><strong>{_text(row["model"])}</strong></td>'
            f'<td><span class="status-text status-{_attr(status_class)}">{_text(status)}</span></td>'
            f'<td>{_text(scored)}</td><td>{_text(weighted)}</td>'
            f'<td>{_text(weighted_delta_text)}</td><td>{_text(token_delta_text)}</td>'
            f'<td>{_text(compactions)}</td><td>{_text(_profile_id(row))}</td>'
            '</tr>'
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


CHART_METRICS = (
    ("weighted", "Weighted score"),
    ("suite", "Suite pass rate"),
    ("tokens", "Tokens"),
    ("wall", "Agent time"),
)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _chart_value(summary: dict[str, Any] | None, metric: str) -> float | None:
    if not summary:
        return None
    if metric == "weighted":
        return _finite_number(summary.get("weighted_score_mean"))
    if metric == "suite":
        scored = int(summary.get("scored_runs", 0))
        return int(summary.get("suite_passes", 0)) / scored if scored > 0 else None
    if metric == "tokens":
        return _finite_number(summary.get("total_tokens_mean"))
    if metric == "wall":
        return _finite_number(summary.get("agent_wall_seconds_mean"))
    raise ValueError(f"unknown comparison-chart metric {metric!r}")


def _chart_value_label(summary: dict[str, Any] | None, metric: str) -> str:
    value = _chart_value(summary, metric)
    if value is None:
        return "n/a"
    if metric == "weighted":
        return _fmt_percent(value)
    if metric == "suite":
        assert summary is not None
        return f"{int(summary['suite_passes'])}/{int(summary['scored_runs'])} · {_fmt_percent(value)}"
    if metric == "tokens":
        return _fmt_count(value)
    if metric == "wall":
        return _fmt_seconds(value)
    raise ValueError(f"unknown comparison-chart metric {metric!r}")


def _chart_delta(row: dict[str, Any], metric: str) -> float | None:
    if metric == "weighted":
        return _finite_number(row.get("weighted_score_delta"))
    if metric == "suite":
        b_value = _chart_value(row.get("B"), metric)
        c_value = _chart_value(row.get("C"), metric)
        return c_value - b_value if b_value is not None and c_value is not None else None
    field = "total_tokens_delta" if metric == "tokens" else "agent_wall_seconds_delta"
    return _finite_number(row.get(field))


def _chart_delta_label(row: dict[str, Any], metric: str) -> str:
    value = _chart_delta(row, metric)
    if value is None:
        return "n/a"
    if metric in {"weighted", "suite"}:
        return _fmt_percentage_point_delta(value)
    if metric == "tokens":
        return _fmt_count_delta(value)
    return _fmt_seconds_delta(value)


def _chart_metric_state(row: dict[str, Any], metric: str) -> tuple[str, str]:
    readiness_field = (
        "comparison_readiness" if metric in {"weighted", "suite"}
        else "efficiency_readiness"
    )
    eligible_field = (
        "headline_eligible" if readiness_field == "comparison_readiness"
        else "comparison_eligible"
    )
    if row.get(readiness_field, {}).get(eligible_field) is not True:
        return "provisional", "Provisional evidence"
    delta = _chart_delta(row, metric)
    if delta is None or delta == 0:
        return "neutral", "Eligible comparison"
    better = delta > 0 if metric in {"weighted", "suite"} else delta < 0
    return ("positive" if better else "negative"), "Eligible comparison"


def _chart_data_number(value: float | None) -> str:
    return "" if value is None else f"{value:.12g}"


def _model_tones(dataset: dict[str, Any]) -> dict[str, int]:
    models = sorted(
        {str(model) for model in dataset.get("models", ())}
        | {
            str(row["model"])
            for row in dataset.get("phase_one_comparisons", ())
            if isinstance(row.get("model"), str)
        }
    )
    return {model: index % MODEL_TONE_COUNT for index, model in enumerate(models)}


def _preferred_model(
    dataset: dict[str, Any], available_models: list[str] | None = None
) -> str:
    models = [str(model) for model in dataset.get("models", ())]
    if available_models is not None:
        available = set(available_models)
        models = [model for model in models if model in available]
    if not models:
        return ""
    for source in reversed(dataset.get("report_sources", ())):
        model = source.get("model")
        if source.get("schema_adapter") is None and model in models:
            return str(model)
    return models[0]


def render_ladder_model_select(dataset: dict[str, Any], chain: str) -> str:
    models = [line["model"] for line in line_series_for_chain(dataset, chain)]
    selected_model = _preferred_model(dataset, models)
    tones = _model_tones(dataset)
    options = "".join(
        f'<option value="{_attr(model)}" data-model-tone="{tones[model]}"'
        f'{" selected" if model == selected_model else ""}>{_text(model)}</option>'
        for model in models
    )
    selected_tone = tones.get(selected_model, 0)
    control_id = f"ladder-model-{chain}"
    return (
        '<div class="ladder-model-control">'
        f'<label for="{_attr(control_id)}">Model series</label>'
        f'<select id="{_attr(control_id)}" '
        f'class="ladder-model-select model-tone-{selected_tone}" '
        f'aria-controls="ladder-chart-{_attr(chain)}" '
        f'onchange="showLadderModel(this)">{options}</select></div>'
    )


def render_phase_one_comparison_chart(dataset: dict[str, Any], chain: str) -> str:
    """Interactive B/C comparison without pooling model identities or denominators."""
    rows: list[str] = []
    model_tones = _model_tones(dataset)
    for row in _phase_one_rows(dataset, chain):
        attributes: list[str] = []
        for metric, _label in CHART_METRICS:
            state, status = _chart_metric_state(row, metric)
            attributes.extend(
                [
                    f'data-{metric}-b="{_attr(_chart_data_number(_chart_value(row.get("B"), metric)))}"',
                    f'data-{metric}-c="{_attr(_chart_data_number(_chart_value(row.get("C"), metric)))}"',
                    f'data-{metric}-b-label="{_attr(_chart_value_label(row.get("B"), metric))}"',
                    f'data-{metric}-c-label="{_attr(_chart_value_label(row.get("C"), metric))}"',
                    f'data-{metric}-delta="{_attr(_chart_delta_label(row, metric))}"',
                    f'data-{metric}-state="{_attr(state)}"',
                    f'data-{metric}-status="{_attr(status)}"',
                ]
            )
        weighted_b = _chart_value(row.get("B"), "weighted")
        weighted_c = _chart_value(row.get("C"), "weighted")
        b_width = 0.0 if weighted_b is None else weighted_b * 100
        c_width = 0.0 if weighted_c is None else weighted_c * 100
        weighted_state, weighted_status = _chart_metric_state(row, "weighted")
        model_tone = model_tones[str(row["model"])]
        rows.append(
            f'<article class="comparison-row model-tone-{model_tone}" '
            f'data-model="{_attr(row["model"])}" '
            f'{" ".join(attributes)}>'
            '<div class="comparison-row-head">'
            f'<strong class="chart-model-label">{_text(row["model"])}</strong>'
            '<div class="comparison-result">'
            f'<span class="chart-status chart-status-{_attr(weighted_state)}" '
            f'data-role="chart-status">{_text(weighted_status)}</span>'
            f'<strong class="chart-delta chart-delta-{_attr(weighted_state)}" '
            f'data-role="chart-delta">{_text(_chart_delta_label(row, "weighted"))}</strong>'
            '</div></div>'
            '<div class="comparison-bars">'
            f'{_render_comparison_bar("B", row["model"], _chart_value_label(row.get("B"), "weighted"), b_width)}'
            f'{_render_comparison_bar("C", row["model"], _chart_value_label(row.get("C"), "weighted"), c_width)}'
            '</div></article>'
        )

    metric_buttons = "".join(
        f'<button class="chart-metric-btn{" active" if metric == "weighted" else ""}" '
        f'data-chart-metric="{metric}" aria-pressed="{str(metric == "weighted").lower()}" '
        f'onclick="showChartMetric(this)">{_text(label)}</button>'
        for metric, label in CHART_METRICS
    )
    return (
        f'<section class="report-section comparison-visual" data-chain-chart="{_attr(chain)}">'
        '<div class="section-heading comparison-heading"><div><p class="eyebrow">Treatment signal</p>'
        f'<h2>B vs C by model · {_text(_chain_label(chain))}</h2></div>'
        '<p>Switch metrics without merging models. Hover or focus a bar for its exact retained value.</p></div>'
        '<div class="chart-tool" data-active-metric="weighted">'
        '<div class="chart-tool-head">'
        f'<div class="chart-metric-segmented" aria-label="Comparison metric">{metric_buttons}</div>'
        '<div class="arm-legend" aria-label="Treatment legend">'
        '<span><i class="legend-dot legend-dot-b"></i>B · web only</span>'
        '<span><i class="legend-dot legend-dot-c"></i>C · CKB AI + web</span></div></div>'
        '<div class="comparison-axis" aria-hidden="true"><span>0</span>'
        '<strong data-role="chart-metric-title">Weighted task score</strong>'
        '<span data-role="chart-scale-max">100%</span></div>'
        f'<div class="comparison-chart" data-chart-chain="{_attr(chain)}">{"".join(rows)}</div>'
        '<p class="chart-caption" data-role="chart-caption">Weighted points awarded across the '
        'frozen task suite. Higher is better; C−B is descriptive.</p>'
        '</div></section>'
    )


def _render_comparison_bar(arm: str, model: str, label: str, width: float) -> str:
    arm_key = arm.lower()
    tooltip = f"{model} · arm {arm} · {label}"
    value = "n/a" if label == "n/a" else label
    return (
        f'<div class="comparison-bar-row" data-arm="{arm}">'
        f'<span class="arm-code arm-code-{arm_key}">{arm}</span>'
        '<div class="bar-lane">'
        f'<span class="comparison-bar comparison-bar-{arm_key}" data-bar-arm="{arm}" '
        f'role="img" tabindex="0" aria-label="{_attr(tooltip)}" '
        f'data-tooltip="{_attr(tooltip)}" style="--bar-size:{width:.3f}%"></span></div>'
        f'<strong class="bar-value bar-value-{arm_key}" '
        f'data-value-arm="{arm}">{_text(value)}</strong></div>'
    )


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
            f'<tr data-model="{_attr(row["model"])}">'
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
                f'<tr data-model="{_attr(row["model"])}">'
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
            f'<tr data-model="{_attr(row["model"])}">'
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
            f'<section class="result-panel" data-model="{_attr(row["model"])}" '
            f'data-signal="{_attr(signal_class)}" '
            f'aria-labelledby="{heading_id}">'
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


def render_report_sources(dataset: dict[str, Any]) -> str:
    """Publish the pinned profile and adapter provenance behind a combined report."""
    sources = list(dataset.get("report_sources", ()))
    if not sources:
        return ""
    rows = []
    for source in sources:
        adapter = source.get("schema_adapter") or "native current schema"
        digest = str(source.get("profile_sha256", ""))
        digest_label = f"{digest[:12]}…{digest[-8:]}" if len(digest) == 64 else "n/a"
        rows.append(
            f'<tr data-model="{_attr(source.get("model", ""))}">'
            f'<td>{_text(source.get("model", "n/a"))}</td>'
            f'<td>{_text(source.get("profile_id", "n/a"))}</td>'
            f'<td><code>{_text(digest_label)}</code></td>'
            f'<td>{_text(source.get("rows", "n/a"))}</td>'
            f'<td>{_text(adapter)}</td></tr>'
        )
    return (
        '<section class="report-section provenance">'
        '<div class="section-heading"><div><p class="eyebrow">Provenance</p>'
        '<h2>Pinned evidence sources</h2></div>'
        '<p>Historical rows remain byte-unchanged. Explicit in-memory adapters only make their '
        'older schema reportable beside current results.</p></div>'
        '<div class="table-wrap"><table class="phase-summary source-table" '
        'aria-label="Pinned report evidence sources"><thead><tr>'
        '<th scope="col">model</th><th scope="col">profile</th>'
        '<th scope="col">profile digest</th><th scope="col">rows</th>'
        '<th scope="col">result schema path</th></tr></thead><tbody>'
        + "\n".join(rows)
        + '</tbody></table></div></section>'
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
                f'{render_phase_one_comparison_chart(dataset, ch)}'
                f'{render_phase_one_overview(dataset, ch)}'
                '<section class="report-section model-analysis">'
                '<div class="section-heading"><div><p class="eyebrow">Across models</p>'
                f'<h2>Model comparison · {_text(label)}</h2></div>'
                '<p>Each model keeps its own pinned profile, denominators and treatment pair. '
                'No score or token total is pooled across models.</p></div>'
                f'<div class="table-wrap">{render_model_comparison_table(dataset, ch)}</div>'
                '</section>'
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
                '<section class="report-section ladder-section">'
                '<div class="section-heading ladder-heading"><div><p class="eyebrow">Condition ladder</p>'
                '<h2>Pass@1 by treatment arm</h2>'
                '<p class="section-note">Confidence intervals are shown for scored runs. Missing '
                'denominators are not plotted as zero.</p></div>'
                f'{render_ladder_model_select(dataset, ch)}</div>'
                '<div class="chart-frame"><div class="chart-scroll">'
                f'<svg id="ladder-chart-{_attr(ch)}" class="chart-svg" '
                f'viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
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
  var active = document.querySelector('.chain-view:not([hidden]) .comparison-chart');
  if(active){ refreshComparisonChart(active); }
}
function showLadderModel(select){
  var section = select.closest('.ladder-section');
  var model = select.value;
  section.querySelectorAll('.plot-model, .legend-model').forEach(function(group){
    group.style.display = group.getAttribute('data-model') === model ? '' : 'none';
  });
  var option = select.options[select.selectedIndex];
  select.className = 'ladder-model-select model-tone-' + option.getAttribute('data-model-tone');
}
var chartMetrics = {
  weighted: {
    title: 'Weighted task score', fixedMax: 1, maxLabel: '100%',
    caption: 'Weighted points awarded across the frozen task suite. Higher is better; C−B is descriptive.'
  },
  suite: {
    title: 'Suite pass rate', fixedMax: 1, maxLabel: '100%',
    caption: 'Share of scored runs that passed the entire suite. A zero remains visible rather than being softened by partial task credit.'
  },
  tokens: {
    title: 'Mean complete tokens', fixedMax: null, maxLabel: null,
    caption: 'Mean provider-reported tokens from scored rows with complete usage. Lower is better; unmatched cohorts show no C−B claim.'
  },
  wall: {
    title: 'Mean agent time', fixedMax: null, maxLabel: null,
    caption: 'Mean agent wall time from the same complete-usage scored rows used for token comparison. Lower is better.'
  }
};
function chartNumber(row, metric, arm){
  var raw = row.getAttribute('data-' + metric + '-' + arm.toLowerCase());
  if(raw === null || raw === ''){ return null; }
  var value = Number(raw);
  return Number.isFinite(value) ? value : null;
}
function compactNumber(value){
  if(value >= 1000000){ return (value / 1000000).toFixed(value >= 10000000 ? 0 : 1) + 'M'; }
  if(value >= 1000){ return (value / 1000).toFixed(value >= 100000 ? 0 : 1) + 'k'; }
  return value.toFixed(value >= 100 ? 0 : 1);
}
function refreshComparisonChart(chart){
  var tool = chart.closest('.chart-tool');
  var metric = tool.getAttribute('data-active-metric') || 'weighted';
  var config = chartMetrics[metric];
  var rows = Array.from(chart.querySelectorAll('.comparison-row')).filter(function(row){
    return row.style.display !== 'none';
  });
  var values = [];
  rows.forEach(function(row){
    ['B', 'C'].forEach(function(arm){
      var value = chartNumber(row, metric, arm);
      if(value !== null){ values.push(value); }
    });
  });
  var maximum = config.fixedMax === null ? Math.max.apply(null, values.concat([1])) : config.fixedMax;
  var scaleMax = tool.querySelector('[data-role="chart-scale-max"]');
  scaleMax.textContent = config.maxLabel || compactNumber(maximum);
  tool.querySelector('[data-role="chart-metric-title"]').textContent = config.title;
  tool.querySelector('[data-role="chart-caption"]').textContent = config.caption;
  rows.forEach(function(row){
    var model = row.getAttribute('data-model');
    ['B', 'C'].forEach(function(arm){
      var value = chartNumber(row, metric, arm);
      var label = row.getAttribute('data-' + metric + '-' + arm.toLowerCase() + '-label') || 'n/a';
      var bar = row.querySelector('[data-bar-arm="' + arm + '"]');
      var valueNode = row.querySelector('[data-value-arm="' + arm + '"]');
      var size = value === null ? 0 : Math.max(0, Math.min(100, (value / maximum) * 100));
      bar.style.setProperty('--bar-size', size.toFixed(3) + '%');
      bar.classList.toggle('is-zero', value === 0);
      bar.hidden = value === null;
      valueNode.textContent = label;
      var tooltip = model + ' · arm ' + arm + ' · ' + config.title + ': ' + label;
      bar.setAttribute('data-tooltip', tooltip);
      bar.setAttribute('aria-label', tooltip);
    });
    var state = row.getAttribute('data-' + metric + '-state') || 'provisional';
    var status = row.getAttribute('data-' + metric + '-status') || 'Provisional evidence';
    var statusNode = row.querySelector('[data-role="chart-status"]');
    var deltaNode = row.querySelector('[data-role="chart-delta"]');
    statusNode.className = 'chart-status chart-status-' + state;
    statusNode.textContent = status;
    deltaNode.className = 'chart-delta chart-delta-' + state;
    deltaNode.textContent = row.getAttribute('data-' + metric + '-delta') || 'n/a';
  });
}
function showChartMetric(button){
  var tool = button.closest('.chart-tool');
  var metric = button.getAttribute('data-chart-metric');
  tool.setAttribute('data-active-metric', metric);
  tool.querySelectorAll('.chart-metric-btn').forEach(function(candidate){
    var selected = candidate === button;
    candidate.classList.toggle('active', selected);
    candidate.setAttribute('aria-pressed', selected ? 'true' : 'false');
  });
  refreshComparisonChart(tool.querySelector('.comparison-chart'));
}
document.querySelectorAll('.ladder-model-select').forEach(showLadderModel);
document.querySelectorAll('.comparison-chart').forEach(refreshComparisonChart);
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
<meta name="color-scheme" content="dark"/>
<title>CKB AI Bench - Benchmark Results{title_suffix}</title>
<style>
  :root {{
    color-scheme: dark;
    --canvas: #070a08;
    --surface: #0e1310;
    --surface-raised: #141b16;
    --surface-subtle: #111713;
    --ink: #f0f5f1;
    --muted: #9ca89f;
    --faint: #707d73;
    --border: #253029;
    --border-strong: #3a493f;
    --accent: #a8ff60;
    --accent-strong: #bcff84;
    --accent-soft: #172610;
    --baseline: #52d5ff;
    --baseline-soft: #0d242b;
    --positive: #a8ff60;
    --negative: #ff707a;
    --negative-soft: #2f171a;
    --warning: #ffcc66;
    --warning-soft: #2b2414;
    --model-amber: #ffcc66;
    --model-amber-soft: #1b170e;
    --model-violet: #c59cff;
    --model-violet-soft: #181221;
    --model-coral: #ff8a65;
    --model-coral-soft: #1d110d;
    --model-rose: #ff7eb6;
    --model-rose-soft: #1c1016;
    --model-indigo: #91a7ff;
    --model-indigo-soft: #111622;
    --model-teal: #53e0c1;
    --model-teal-soft: #0d1b18;
    --model-red: #ff6f73;
    --model-red-soft: #1d1012;
    --model-yellow: #e5ed69;
    --model-yellow-soft: #191b0d;
    --focus: #52d5ff;
    --chart-grid: #29332c;
    --chart-tick: #1b231e;
  }}
  * {{ box-sizing: border-box; letter-spacing: 0; }}
  html {{ background: var(--canvas); }}
  body {{
    margin: 0; color: var(--ink); background: var(--canvas);
    font: 14px/1.55 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  ::selection {{ color: #071008; background: var(--accent); }}
  [hidden] {{ display: none !important; }}
  .page-shell {{ width: min(100% - 48px, 1280px); margin: 0 auto; padding: 20px 0 80px; }}
  .site-header {{
    display: flex; align-items: center; justify-content: space-between; gap: 20px;
    min-height: 48px; padding: 0 0 16px; border-bottom: 1px solid var(--border);
  }}
  .brand {{ display: flex; align-items: center; gap: 11px; font-weight: 760; font-size: 15px; }}
  .brand-mark {{
    position: relative; width: 19px; height: 19px; border: 4px solid var(--accent);
    border-right-color: var(--baseline); transform: rotate(45deg);
  }}
  .report-status {{ display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }}
  .report-status::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }}
  .hero {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(240px, 320px); gap: 64px; padding: 64px 0 42px; }}
  .eyebrow {{ margin: 0 0 7px; color: var(--accent-strong); font-size: 11px; font-weight: 760; text-transform: uppercase; }}
  h1 {{ margin: 0; max-width: 820px; font-size: 48px; line-height: 1.03; font-weight: 760; }}
  h2 {{ margin: 0; font-size: 20px; line-height: 1.25; font-weight: 720; }}
  .hero-copy {{ max-width: 64ch; margin: 20px 0 0; color: var(--muted); font-size: 16px; }}
  .hero-meta {{
    display: grid; align-content: end; min-width: 220px; margin: 0;
    border-top: 1px solid var(--accent);
  }}
  .hero-meta div {{ display: flex; justify-content: space-between; gap: 20px; min-width: 0; padding: 10px 0; border-bottom: 1px solid var(--border); }}
  .hero-meta dt {{ min-width: 0; color: var(--muted); }}
  .hero-meta dd {{ min-width: 0; margin: 0; font-weight: 680; font-variant-numeric: tabular-nums; text-align: right; overflow-wrap: anywhere; }}
  .synthetic-banner {{
    background: var(--negative); color: #fff; font-weight: 750;
    padding: 10px 14px; border-radius: 6px; margin-bottom: 16px;
  }}
  .toolbar {{
    display: flex; align-items: flex-end; justify-content: flex-end; gap: 16px 24px;
    flex-wrap: wrap;
    position: sticky; top: 0; z-index: 20; padding: 14px 0 18px;
    background: var(--canvas); border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
  }}
  .filter-group {{ display: grid; gap: 6px; min-width: 0; }}
  .toolbar-label {{ color: var(--muted); font-size: 11px; font-weight: 720; }}
  .segmented {{
    display: inline-flex; max-width: 100%; padding: 3px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 7px;
  }}
  .chain-btn {{
    min-width: 112px; min-height: 42px; display: flex; align-items: center; justify-content: space-between;
    gap: 12px; padding: 6px 10px; cursor: pointer; color: var(--muted);
    font: inherit; font-weight: 680; background: transparent; border: 1px solid transparent;
    border-radius: 5px; transition: background-color 100ms ease-out, color 100ms ease-out,
      border-color 100ms ease-out, transform 100ms ease-out;
  }}
  .chain-btn small {{ color: var(--faint); font-size: 10px; font-weight: 620; }}
  .chain-btn:hover {{ background: var(--surface-subtle); color: var(--ink); }}
  .chain-btn:active {{ transform: translateY(1px); }}
  .chain-btn.active {{
    background: var(--accent); color: #071008; border-color: var(--accent);
  }}
  .chain-btn.active small {{ color: #071008; opacity: .72; }}
  .chain-btn:focus-visible {{
    outline: 3px solid var(--focus); outline-offset: 3px;
  }}
  .result-panel {{
    margin: 24px 0 44px; background: var(--surface); border: 1px solid var(--border-strong);
    border-radius: 8px; overflow: hidden;
  }}
  .result-panel[data-signal="positive"] {{ border-top-color: var(--positive); }}
  .result-panel[data-signal="negative"] {{ border-top-color: var(--negative); }}
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
  .comparison-visual {{ padding-top: 28px; border-top: 0; }}
  .chart-tool {{ background: var(--surface); border: 1px solid var(--border-strong); border-radius: 8px; }}
  .chart-tool-head {{
    display: flex; align-items: center; justify-content: space-between; gap: 16px 24px;
    padding: 14px 16px; border-bottom: 1px solid var(--border);
  }}
  .chart-metric-segmented {{
    display: inline-flex; max-width: 100%; padding: 3px; overflow-x: auto;
    background: var(--canvas); border: 1px solid var(--border); border-radius: 6px;
    overscroll-behavior-inline: contain;
  }}
  .chart-metric-btn {{
    min-height: 34px; padding: 6px 10px; color: var(--muted); background: transparent;
    border: 1px solid transparent; border-radius: 4px; cursor: pointer; white-space: nowrap;
    font: inherit; font-size: 12px; font-weight: 680;
    transition: color 120ms cubic-bezier(.23,1,.32,1), background-color 120ms cubic-bezier(.23,1,.32,1), border-color 120ms cubic-bezier(.23,1,.32,1), transform 100ms cubic-bezier(.23,1,.32,1);
  }}
  .chart-metric-btn:hover {{ color: var(--ink); background: var(--surface-raised); }}
  .chart-metric-btn:active {{ transform: translateY(1px); }}
  .chart-metric-btn.active {{ color: var(--accent); background: var(--accent-soft); border-color: #395728; }}
  .chart-metric-btn:focus-visible, .comparison-bar:focus-visible {{
    outline: 3px solid var(--focus); outline-offset: 3px;
  }}
  .arm-legend {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px 16px; color: var(--muted); font-size: 11px; }}
  .arm-legend span {{ display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }}
  .legend-dot {{ width: 8px; height: 8px; border-radius: 2px; }}
  .legend-dot-b {{ background: var(--baseline); }}
  .legend-dot-c {{ background: var(--accent); }}
  .comparison-axis {{
    display: grid; grid-template-columns: 1fr auto 1fr; gap: 16px; align-items: center;
    padding: 13px 24px 9px; color: var(--faint); font: 11px/1.2 ui-monospace, SFMono-Regular, Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }}
  .comparison-axis strong {{ color: var(--muted); font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-weight: 700; }}
  .comparison-axis span:last-child {{ text-align: right; }}
  .comparison-chart {{ border-top: 1px solid var(--border); }}
  .model-tone-0 {{ --model-accent: var(--model-amber); --model-surface: var(--model-amber-soft); }}
  .model-tone-1 {{ --model-accent: var(--model-violet); --model-surface: var(--model-violet-soft); }}
  .model-tone-2 {{ --model-accent: var(--model-coral); --model-surface: var(--model-coral-soft); }}
  .model-tone-3 {{ --model-accent: var(--model-rose); --model-surface: var(--model-rose-soft); }}
  .model-tone-4 {{ --model-accent: var(--model-indigo); --model-surface: var(--model-indigo-soft); }}
  .model-tone-5 {{ --model-accent: var(--model-teal); --model-surface: var(--model-teal-soft); }}
  .model-tone-6 {{ --model-accent: var(--model-red); --model-surface: var(--model-red-soft); }}
  .model-tone-7 {{ --model-accent: var(--model-yellow); --model-surface: var(--model-yellow-soft); }}
  .comparison-row {{
    position: relative; padding: 20px 24px 22px; background: var(--model-surface);
    border-bottom: 1px solid var(--border); box-shadow: inset 4px 0 0 var(--model-accent);
  }}
  .comparison-row:last-child {{ border-bottom: 0; }}
  .comparison-row-head {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 14px; }}
  .chart-model-label {{
    display: inline-flex; align-items: center; gap: 10px; max-width: min(100%, 560px);
    min-height: 40px; color: var(--model-accent); text-align: left; overflow-wrap: anywhere;
    font: 720 15px/1.3 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .chart-model-label::before {{
    content: ""; flex: 0 0 auto; width: 10px; height: 10px; background: var(--model-accent);
    border-radius: 3px;
  }}
  .comparison-result {{ display: flex; align-items: center; justify-content: flex-end; gap: 10px; min-width: 0; }}
  .chart-status {{ color: var(--muted); font-size: 10px; white-space: nowrap; }}
  .chart-status-provisional {{ color: var(--warning); }}
  .chart-delta {{
    min-width: 72px; color: var(--muted); text-align: right;
    font: 720 13px/1 ui-monospace, SFMono-Regular, Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }}
  .chart-delta-positive {{ color: var(--positive); }}
  .chart-delta-negative {{ color: var(--negative); }}
  .chart-delta-provisional {{ color: var(--warning); }}
  .comparison-bars {{ display: grid; gap: 9px; }}
  .comparison-bar-row {{ display: grid; grid-template-columns: 24px minmax(120px, 1fr) minmax(92px, auto); gap: 11px; align-items: center; }}
  .arm-code {{
    display: grid; place-items: center; width: 22px; height: 22px; border: 1px solid var(--border);
    border-radius: 4px; color: var(--muted); font: 760 10px/1 ui-monospace, monospace;
  }}
  .arm-code-b {{ color: var(--baseline); border-color: #245a69; background: var(--baseline-soft); }}
  .arm-code-c {{ color: var(--accent); border-color: #395728; background: var(--accent-soft); }}
  .bar-lane {{
    position: relative; height: 18px; background: var(--canvas);
    border: 1px solid var(--border-strong); border-radius: 3px;
  }}
  .comparison-bar {{
    position: absolute; inset: -1px auto -1px -1px; width: var(--bar-size); min-width: 5px;
    border-radius: 3px; cursor: crosshair;
    transition: width 240ms cubic-bezier(.23,1,.32,1), filter 120ms cubic-bezier(.23,1,.32,1);
  }}
  .comparison-bar-b {{ background: var(--baseline); }}
  .comparison-bar-c {{ background: var(--accent); }}
  .comparison-bar::before {{
    content: ""; position: absolute; top: 50%; right: -4px; width: 8px; height: 22px;
    background: inherit; border: 2px solid var(--model-surface); border-radius: 3px;
    transform: translateY(-50%);
  }}
  .comparison-bar:hover, .comparison-bar:focus-visible {{ filter: brightness(1.18); z-index: 4; }}
  .comparison-bar::after {{
    content: attr(data-tooltip); position: absolute; left: 0; bottom: calc(100% + 10px);
    width: max-content; max-width: min(320px, 72vw); padding: 7px 9px; opacity: 0;
    color: var(--ink); background: #1b241e; border: 1px solid var(--border-strong);
    border-radius: 4px; pointer-events: none; transform: translateY(3px);
    font: 11px/1.35 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    transition: opacity 120ms cubic-bezier(.23,1,.32,1), transform 120ms cubic-bezier(.23,1,.32,1);
  }}
  .comparison-bar:hover::after, .comparison-bar:focus::after {{ opacity: 1; transform: translateY(0); }}
  .bar-value {{
    color: var(--ink); text-align: right; white-space: nowrap;
    font: 760 13px/1 ui-monospace, SFMono-Regular, Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }}
  .bar-value-b {{ color: var(--baseline); }}
  .bar-value-c {{ color: var(--accent); }}
  .chart-caption {{ margin: 0; padding: 13px 16px; color: var(--muted); border-top: 1px solid var(--border); font-size: 11px; }}
  .report-section {{ padding: 32px 0 8px; border-top: 1px solid var(--border); }}
  .section-heading {{ display: flex; align-items: end; justify-content: space-between; gap: 28px; margin: 0 0 16px; }}
  .section-heading > p {{ max-width: 58ch; margin: 0; color: var(--muted); font-size: 13px; text-align: right; }}
  .section-note {{ max-width: 62ch; margin: 8px 0 0; color: var(--muted); font-size: 12px; }}
  .ladder-heading {{ align-items: end; }}
  .ladder-model-control {{ display: grid; gap: 6px; width: min(100%, 280px); min-width: 240px; }}
  .ladder-model-control label {{ color: var(--muted); font-size: 11px; font-weight: 720; }}
  .ladder-model-select {{
    width: 100%; min-height: 42px; padding: 8px 12px; color: var(--model-accent);
    background: var(--model-surface); border: 1px solid var(--model-accent); border-radius: 6px;
    cursor: pointer; font: 680 13px/1.3 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .ladder-model-select:hover {{ border-color: var(--ink); }}
  .ladder-model-select:focus-visible {{ outline: 3px solid var(--focus); outline-offset: 3px; }}
  .ladder-model-select option {{ color: var(--ink); background: var(--surface); }}
  .chart-frame, .table-wrap {{ overflow: hidden; background: var(--surface); border: 1px solid var(--border-strong); border-radius: 7px; }}
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
  table.leaderboard th, table.phase-summary th {{ color: var(--muted); background: var(--surface-raised); font-size: 11px; font-weight: 750; white-space: nowrap; }}
  table.leaderboard tbody tr, table.phase-summary tbody tr {{ transition: background-color 120ms cubic-bezier(.23,1,.32,1); }}
  table.leaderboard tbody tr:hover, table.phase-summary tbody tr:hover {{ background: var(--surface-raised); }}
  table.phase-summary {{ font-size: 12px; }}
  table.phase-summary .raw {{ color: var(--faint); font-size: 10px; white-space: nowrap; }}
  .status-text {{ font-weight: 720; }}
  .status-positive {{ color: var(--positive); }}
  .status-negative {{ color: var(--negative); }}
  .status-neutral {{ color: var(--muted); }}
  .source-table code {{ color: var(--muted); font-size: 11px; white-space: nowrap; }}
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
  @media (max-width: 760px) {{
    .page-shell {{ width: min(100% - 28px, 1280px); padding-top: 14px; }}
    .site-header, .hero, .toolbar, .section-heading, .result-panel-head {{ align-items: stretch; }}
    .site-header, .toolbar, .section-heading, .result-panel-head {{ flex-direction: column; }}
    .hero {{ grid-template-columns: 1fr; gap: 24px; padding: 34px 0 24px; }}
    h1 {{ font-size: 30px; }}
    .hero-meta {{ min-width: 0; }}
    .filter-group {{ width: 100%; }}
    .chain-segmented {{ display: grid; grid-template-columns: 1fr 1fr; width: 100%; }}
    .chain-btn {{ min-width: 0; }}
    .ladder-model-control {{ width: 100%; min-width: 0; }}
    .metric-strip {{ grid-template-columns: 1fr 1fr; }}
    .metric:nth-child(2) {{ border-right: 0; }}
    .metric:nth-child(-n+2) {{ border-bottom: 1px solid var(--border); }}
    .section-heading > p {{ text-align: left; }}
    .signal {{ align-self: flex-start; white-space: normal; }}
    .chart-tool-head {{ align-items: stretch; flex-direction: column; }}
    .chart-metric-segmented {{ width: 100%; }}
    .chart-metric-btn {{ flex: 1 0 auto; }}
    .arm-legend {{ justify-content: flex-start; }}
    .comparison-row {{ padding: 18px 16px 20px; }}
    .comparison-axis {{ padding-inline: 16px; }}
  }}
  @media (max-width: 460px) {{
    .metric-strip {{ grid-template-columns: 1fr; }}
    .metric {{ border-right: 0; border-bottom: 1px solid var(--border); }}
    .metric:last-child {{ border-bottom: 0; }}
    .empty-state {{ grid-template-columns: 1fr; }}
    .comparison-row-head {{ align-items: flex-start; flex-direction: column; }}
    .comparison-result {{ width: 100%; justify-content: space-between; }}
    .chart-metric-segmented {{ display: grid; grid-template-columns: 1fr 1fr; overflow: visible; }}
    .chart-metric-btn {{ min-width: 0; white-space: normal; }}
    .comparison-bar-row {{ grid-template-columns: 24px minmax(84px, 1fr) minmax(74px, auto); gap: 8px; }}
    .bar-value {{ font-size: 11px; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .chain-btn, .chart-metric-btn, .comparison-bar,
    table.leaderboard tbody tr, table.phase-summary tbody tr {{ transition: none; }}
  }}
  @media print {{
    :root {{
      color-scheme: light; --canvas: #ffffff; --surface: #ffffff; --surface-raised: #f4f6f4;
      --surface-subtle: #f7f9f7; --ink: #111713; --muted: #4e5a52; --faint: #6c786f;
      --border: #d9dfda; --border-strong: #b9c3bc; --accent: #087a50;
      --accent-strong: #05633f; --accent-soft: #e3f4ec; --baseline: #087ca5;
      --baseline-soft: #e4f5fb; --positive: #087a50; --negative: #b42318;
      --negative-soft: #fce8e6; --warning: #8a6100; --warning-soft: #fff7df;
      --model-amber: #805500; --model-amber-soft: #fff8e8;
      --model-violet: #6842a3; --model-violet-soft: #f5efff;
      --model-coral: #9a3e27; --model-coral-soft: #fff1ec;
      --model-rose: #9a3567; --model-rose-soft: #fff0f6;
      --model-indigo: #4057a8; --model-indigo-soft: #eef1ff;
      --model-teal: #08745e; --model-teal-soft: #e8f8f4;
      --model-red: #a12631; --model-red-soft: #ffedef;
      --model-yellow: #626900; --model-yellow-soft: #fafbdc;
      --focus: #1685d1; --chart-grid: #e7ece9; --chart-tick: #f0f3f1;
    }}
    .page-shell {{ width: 100%; padding: 0; }}
    .toolbar {{ display: none; }}
    .ladder-model-control {{ display: none; }}
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
    <div class="toolbar" aria-label="Report filters">
      <div class="filter-group">
        <span class="toolbar-label">Chain, reported separately and never merged</span>
        <div class="segmented chain-segmented" aria-label="Chain filter">{toggle_btns}</div>
      </div>
    </div>
    {''.join(chain_views)}
    {render_report_sources(dataset)}
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
