"""Deterministic static ladder chart renderer (ADR-0011/0012).

Ports spikes/ladder-chart/render-ladder.js to production Python. Produces self-contained HTML with
inline SVG and a secondary leaderboard table. No external JS/CSS/CDN. Same dataset -> byte-identical
output.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from ckbbench.config import LADDER_ORDER
import math

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
        if b_pt and c_pt:
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
        f'<table class="leaderboard" data-chain="{_attr(chain)}">',
        "<thead><tr>"
        "<th>model</th><th>family</th>"
        "<th>C−B</th><th>CI</th><th>direction</th>"
        "<th>infra-fail %</th><th>violation %</th>"
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


def render_ladder_html(dataset: dict[str, Any]) -> str:
    """Build the full self-contained HTML page (deterministic)."""
    synthetic = bool(dataset.get("_SYNTHETIC"))
    chains = list(CHAINS)
    groups = "\n".join(
        render_chain_group(dataset, ch, visible=(i == 0))
        for i, ch in enumerate(chains)
    )

    # The toggle reads the chain from data-chain (no per-button inline JS string), so the chain
    # value never enters a JS-string context in an attribute.
    toggle_btns = "\n".join(
        f'<button class="chain-btn{" active" if i == 0 else ""}" '
        f'data-chain="{_attr(ch)}" onclick="showChain(this.getAttribute(\'data-chain\'))">'
        f"{_text(ch)}</button>"
        for i, ch in enumerate(chains)
    )

    leaderboard_sections = "\n".join(
        f'<div class="lb-section" data-chain="{_attr(ch)}" '
        f'style="display:{"block" if i == 0 else "none"}">'
        f"<h2>Leaderboard - {_text(ch)}</h2>"
        f"{render_leaderboard_table(dataset, ch)}</div>"
        for i, ch in enumerate(chains)
    )

    js = """
function showChain(chain){
  document.querySelectorAll('svg .chart').forEach(function(g){
    g.style.display = (g.getAttribute('data-chain')===chain)?'block':'none';
  });
  document.querySelectorAll('.chain-btn').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-chain')===chain);
  });
  document.querySelectorAll('.lb-section').forEach(function(s){
    s.style.display = (s.getAttribute('data-chain')===chain)?'block':'none';
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
    generated_at = _text(str(dataset.get("generated_at", "deterministic")))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CKB AI Bench - Condition Ladder{title_suffix}</title>
<style>
  body {{ font: 14px/1.45 system-ui, sans-serif; margin: 24px; color: #1a1a1a; background:#fff; }}
  .synthetic-banner {{
    background: #b00020; color: #fff; font-weight: 700; letter-spacing: .03em;
    padding: 8px 12px; border-radius: 6px; margin-bottom: 14px;
  }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  h2 {{ font-size: 15px; margin: 20px 0 8px; }}
  .sub {{ color: #555; margin: 0 0 14px; }}
  .toolbar {{ margin: 0 0 8px; }}
  .chain-btn {{
    font: inherit; padding: 5px 14px; margin-right: 6px; cursor: pointer;
    border: 1px solid #888; background: #f4f4f4; border-radius: 5px;
  }}
  .chain-btn.active {{ background: #1a1a1a; color: #fff; border-color: #1a1a1a; }}
  svg {{ border: 1px solid #e3e3e3; border-radius: 6px; background: #fff; }}
  .grid {{ stroke: #eee; stroke-width: 1; }}
  .tick {{ stroke: #f3f3f3; stroke-width: 1; }}
  .axis-label {{ fill: #555; font-size: 11px; }}
  .axis-label.y {{ text-anchor: end; }}
  .axis-label.x {{ text-anchor: middle; }}
  .axis-title {{ fill: #333; font-size: 12px; text-anchor: middle; }}
  .legend-title {{ fill: #222; font-size: 11px; font-weight: 700; }}
  .legend-row {{ fill: #222; font-size: 12px; }}
  .legend-cb {{ font-size: 10.5px; }}
  .legend-cb.dir-positive {{ fill: #0a7a2f; }}
  .legend-cb.dir-negative {{ fill: #b00020; }}
  .legend-cb.dir-flat {{ fill: #777; }}
  .legend-note {{ fill: #888; font-size: 10px; }}
  .note {{ color:#555; font-size:12px; margin-top:10px; max-width:680px; }}
  table.leaderboard {{ border-collapse: collapse; width: 100%; max-width: 680px; }}
  table.leaderboard th, table.leaderboard td {{
    border: 1px solid #ddd; padding: 6px 10px; text-align: left;
  }}
  table.leaderboard th {{ background: #f4f4f4; }}
  .dir-positive {{ color: #0a7a2f; }}
  .dir-negative {{ color: #b00020; }}
  .dir-flat {{ color: #777; }}
</style>
</head>
<body>
  {banner}
  <h1>CKB AI Bench - Condition Ladder</h1>
  <p class="sub">X = condition ladder A→B→C→D · Y = Pass@1 · one line per model, colored by family · CI band on every point · headline = <strong>C−B</strong> (MCP value on top of web research). DevNet and TestNet are <strong>separate scores</strong> (toggle below; never merged).</p>
  <div class="toolbar">chain: {toggle_btns}</div>
  <svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="condition ladder chart">
{groups}
  </svg>
  {leaderboard_sections}
  <p class="note">The bold <strong>B→C</strong> segment of each line is literally the MCP's marginal value over ordinary web research. A visible upward kick at C = MCP helps; a flat or downward B→C = it does not. This page renders whatever the data shows (positive, flat, or negative) without spin (ADR-0011).</p>
  <p class="note">Generated_at: {generated_at}.</p>
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