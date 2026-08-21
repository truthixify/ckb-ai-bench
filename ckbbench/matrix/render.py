"""Static evidence report renderer (ADR-0012).

Renders one self-contained HTML document from a validated dataset. Every value is present in the
markup: the inline script only switches which view is shown and re-orders rows that are already
rendered, so the report stays complete and readable with scripting disabled.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ckbbench.config import LADDER_ORDER


def _attr(value: Any) -> str:
    """Escape a value for use inside a double-quoted HTML attribute."""
    return html.escape(str(value), quote=True)


def _text(value: Any) -> str:
    """Escape a value for use as HTML text."""
    return html.escape(str(value), quote=False)


ARMS = LADDER_ORDER

TONE = {
    "pos": "#1d6b4f",
    "neg": "#9a3324",
    "flat": "#5c636b",
    "incon": "#8a5a10",
    "infra": "#6b3f5f",
    "ink": "#17191c",
    "mute": "#5c636b",
    "faint": "#8d949b",
}

ARM_META = {
    "A": {
        "label": "no CKB AI, no web",
        "long": "CKB AI off, ordinary web research prohibited by prompt",
        "marker": "○",
        "surface": "off",
        "meaning": "Innate model ability, used as a floor.",
    },
    "B": {
        "label": "web only",
        "long": "CKB AI off, ordinary web research allowed",
        "marker": "○",
        "surface": "off",
        "meaning": "Baseline value of ordinary web research.",
    },
    "C": {
        "label": "CKB AI plus web",
        "long": "docs-only-v1 plus ordinary web research",
        "marker": "■",
        "surface": "docs-only-v1",
        "meaning": (
            "Documentation value on top of web research. Phase-one headline comparison against B."
        ),
    },
    "D": {
        "label": "CKB AI, no web",
        "long": "docs-only-v1, ordinary web research prohibited by prompt",
        "marker": "■",
        "surface": "docs-only-v1",
        "meaning": "Curated documentation without ordinary web research, a diagnostic slice.",
    },
}

ARM_LABELS = {arm: f"{arm}: {meta['label']}" for arm, meta in ARM_META.items()}

# Reader-facing descriptions of the frozen suite. The suite files carry ids, weights and verifier
# wiring; this is the prose that makes the same tasks legible in a report.
TASK_COPY = {
    "task-01-tip": {
        "name": "Chain tip readout",
        "category": "RPC read",
        "kind": "Control",
        "objective": (
            "Read the current run-bound CKB tip and report the block hash for that exact height."
        ),
        "fresh": "The DevNet instance is fresh per cell, so the tip height is unique to the run.",
        "proof": "Reported height plus block hash.",
        "verify": (
            "Direct CKB RPC confirms the height is fresh enough and that the hash matches that "
            "height."
        ),
    },
    "task-04-send-tx": {
        "name": "Signed transfer commit",
        "category": "Transaction",
        "kind": "Direct product evidence",
        "objective": (
            "Construct, sign and commit a transaction with one exact recipient output and a "
            "run-specific amount."
        ),
        "fresh": "Recipient address and capacity amount are generated per run.",
        "proof": "Committed transaction hash.",
        "verify": (
            "Direct CKB RPC confirms existence, freshness, recipient, amount and output structure."
        ),
    },
    "task-05-hashlock": {
        "name": "Password lock contract",
        "category": "Contract engineering",
        "kind": "Documentation-assisted engineering",
        "objective": "Implement and build a CKB password-lock contract from source.",
        "fresh": "A per-run preimage and expected lock argument.",
        "proof": "Built contract binary plus source tree.",
        "verify": (
            "A hidden Rust ckb-testtool suite rebuilds the contract and runs its cases in an "
            "isolated verifier container."
        ),
    },
    "task-06-sudt-script": {
        "name": "Simple UDT script identity",
        "category": "Canonical identity",
        "kind": "Direct product evidence",
        "objective": "Identify the canonical Simple UDT script code hash and hash type.",
        "fresh": (
            "Nothing — the canonical values are fixed, which makes this a documentation lookup."
        ),
        "proof": "Reported code hash and hash type.",
        "verify": "The submitted identity is compared with the fixed canonical values.",
    },
    "task-08-type-id-data-cell": {
        "name": "Type-ID data cell deploy",
        "category": "Deployment",
        "kind": "Documentation-assisted engineering",
        "objective": (
            "Deploy a fresh data cell carrying an exact payload under a canonical Type-ID type "
            "script."
        ),
        "fresh": "Cell payload bytes are generated per run.",
        "proof": "Out point of the committed cell.",
        "verify": (
            "Direct CKB RPC derives the Type-ID independently and verifies the committed cell and "
            "its script hash."
        ),
    },
}

_HIDDEN_VERIFIER_TASKS = frozenset({"task-05-hashlock"})

SANS = "'IBM Plex Sans',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif"
MONO = "'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SERIF = "Newsreader,'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif"

OUTCOME_STYLE = {
    "pass": {"tone": TONE["pos"], "glyph": "●", "label": "Pass"},
    "agent_fail": {"tone": "#3d444c", "glyph": "○", "label": "Agent fail"},
    "infra_fail": {"tone": TONE["infra"], "glyph": "▲", "label": "Infra fail"},
    "protocol_violation": {"tone": TONE["neg"], "glyph": "■", "label": "Protocol violation"},
}

SCORED_OUTCOMES = frozenset({"pass", "agent_fail"})


def _outcome_style(outcome: Any) -> dict[str, str]:
    return OUTCOME_STYLE.get(
        str(outcome), {"tone": TONE["flat"], "glyph": "—", "label": str(outcome)}
    )


def _task_name(task_id: Any) -> str:
    entry = TASK_COPY.get(str(task_id))
    return entry["name"] if entry else str(task_id)


# --- formatting ------------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """Return a finite float, or None for missing and non-finite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _fmt1(value: Any, *, dash: str = "—") -> str:
    number = _num(value)
    return dash if number is None else f"{number:.1f}"


def _fmt3(value: Any, *, dash: str = "—") -> str:
    number = _num(value)
    return dash if number is None else f"{number:.3f}"


def _fmt_int(value: Any, *, dash: str = "—") -> str:
    number = _num(value)
    return dash if number is None else f"{round(number):,}"


def _fmt_signed(value: Any, digits: int = 1, *, dash: str = "—") -> str:
    number = _num(value)
    if number is None:
        return dash
    body = f"{number:.{digits}f}"
    if body.startswith("-"):
        return body
    return ("+" if number > 0 else "±") + body


def _fmt_pct(value: Any, *, dash: str = "—") -> str:
    number = _num(value)
    return dash if number is None else f"{number * 100:.1f}%"


def _short(value: Any, keep: int = 10) -> str:
    text = str(value or "").removeprefix("sha256:").removeprefix("0x")
    if len(text) <= keep + 4:
        return text
    return f"{text[:keep]}…{text[-4:]}"


def _display_timestamp(value: str) -> str:
    """Render the dataset's data vintage without inventing precision it does not have."""
    text = str(value)
    if len(text) >= 17 and text.endswith("Z") and "T" in text:
        return f"{text[:10]} {text[11:16]} UTC"
    return text


# --- derivation ------------------------------------------------------------------------------


def _chain_label(chain: str) -> str:
    return {"devnet": "DevNet", "testnet": "TestNet"}.get(chain, chain)


def _runs_for(dataset: dict[str, Any], chain: str) -> list[dict[str, Any]]:
    return [r for r in dataset.get("runs", []) if str(r.get("chain")) == chain]


def _comparisons_for(dataset: dict[str, Any], chain: str) -> list[dict[str, Any]]:
    rows = [
        row for row in dataset.get("phase_one_comparisons", [])
        if str(row.get("chain")) == chain
    ]
    return sorted(rows, key=lambda row: str(row.get("model")))


def _cells_for(dataset: dict[str, Any], chain: str, model: str) -> dict[str, dict[str, Any]]:
    return {
        str(cell.get("arm")): cell
        for cell in dataset.get("cells", [])
        if str(cell.get("chain")) == chain and str(cell.get("model")) == model
    }


def _chain_has_data(dataset: dict[str, Any], chain: str) -> bool:
    return bool(_comparisons_for(dataset, chain)) or bool(_runs_for(dataset, chain))


def _scored(run: dict[str, Any]) -> bool:
    return str(run.get("outcome")) in SCORED_OUTCOMES


def _run_score(run: dict[str, Any]) -> float | None:
    return _num(run.get("total_score")) if _scored(run) else None


def _arm_health(runs: list[dict[str, Any]], model: str, arm: str) -> dict[str, Any]:
    """Recorded-row counts for one model/arm, including rows excluded from correctness means."""
    recorded = [r for r in runs if str(r.get("model")) == model and str(r.get("arm")) == arm]
    metrics = [r.get("metrics") or {} for r in recorded]
    return {
        "recorded": len(recorded),
        "infra": sum(1 for r in recorded if str(r.get("outcome")) == "infra_fail"),
        "protocol": sum(1 for r in recorded if str(r.get("outcome")) == "protocol_violation"),
        "retries": sum(int(_num(m.get("provider_retry_count")) or 0) for m in metrics),
        "compaction": sum(int(_num(m.get("history_compaction_count")) or 0) for m in metrics),
        "rows": recorded,
    }


def _readiness(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("comparison_readiness") or {}


def _efficiency_readiness(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("efficiency_readiness") or {}


def _headline_eligible(row: dict[str, Any]) -> bool:
    return bool(_readiness(row).get("headline_eligible"))


def _efficiency_eligible(row: dict[str, Any]) -> bool:
    return bool(_efficiency_readiness(row).get("comparison_eligible"))


def _status_for(row: dict[str, Any]) -> tuple[str, str, str]:
    """Lead status for one model.

    This is a statement about correctness only, so it turns on the correctness readiness gate
    alone. An ineligible usage cohort suppresses the token and time deltas, but it never
    downgrades a correctness verdict the comparison gate already allows.
    """
    b, c = row.get("B") or {}, row.get("C") or {}
    delta = _num(row.get("weighted_score_delta"))
    if not b.get("scored_runs") or not c.get("scored_runs"):
        return "Evidence incomplete", TONE["incon"], "○"
    if not _headline_eligible(row):
        return "Inconclusive", TONE["incon"], "◑"
    if delta is None:
        return "Evidence incomplete", TONE["incon"], "○"
    if delta > 0.005:
        return "Observed positive difference", TONE["pos"], "▲"
    if delta < -0.005:
        return "Observed negative difference", TONE["neg"], "▼"
    return "No observed difference", TONE["flat"], "▬"


METRICS = (
    {
        "key": "weighted",
        "label": "Weighted score",
        "unit": "points of 100",
        "better": "higher",
        "dir": 1,
        "axis": 100.0,
        "delta_unit": "points",
        "field": "weighted_score_mean",
        "delta_field": "weighted_score_delta",
        "n_field": "scored_runs",
        "digits": 1,
    },
    {
        "key": "suite",
        "label": "Suite Pass@1",
        "unit": "% of scored runs",
        "better": "higher",
        "dir": 1,
        "axis": 100.0,
        "delta_unit": "percentage points",
        "field": None,
        "delta_field": None,
        "n_field": "scored_runs",
        "digits": 1,
    },
    {
        "key": "tokens",
        "label": "Complete tokens",
        "unit": "tokens per run",
        "better": "lower",
        "dir": -1,
        "axis": None,
        "delta_unit": "tokens",
        "field": "total_tokens_mean",
        "delta_field": "total_tokens_delta",
        "n_field": "efficiency_runs",
        "digits": 0,
    },
    {
        "key": "wall",
        "label": "Agent time",
        "unit": "seconds per run",
        "better": "lower",
        "dir": -1,
        "axis": None,
        "delta_unit": "seconds",
        "field": "agent_wall_seconds_mean",
        "delta_field": "agent_wall_seconds_delta",
        "n_field": "wall_time_runs",
        "digits": 1,
    },
)

METRIC_BY_KEY = {metric["key"]: metric for metric in METRICS}
EFFICIENCY_METRICS = frozenset({"tokens", "wall"})


def _suite_pass_pct(summary: dict[str, Any] | None) -> float | None:
    if not summary:
        return None
    scored = _num(summary.get("scored_runs"))
    passes = _num(summary.get("suite_passes"))
    if not scored or passes is None:
        return None
    return (passes / scored) * 100.0


def _metric_value(summary: dict[str, Any] | None, key: str) -> float | None:
    if not summary:
        return None
    if key == "suite":
        return _suite_pass_pct(summary)
    if key == "weighted":
        mean = _num(summary.get("weighted_score_mean"))
        return None if mean is None else mean * 100.0
    return _num(summary.get(METRIC_BY_KEY[key]["field"]))


def _metric_delta(row: dict[str, Any], key: str) -> float | None:
    """Take the delta the dataset published.

    Token and wall deltas are withheld upstream when the usage cohort is ineligible, so they must
    never be recomputed from the arm means here.
    """
    if key == "weighted":
        delta = _num(row.get("weighted_score_delta"))
        return None if delta is None else delta * 100.0
    if key == "suite":
        b = _metric_value(row.get("B"), key)
        c = _metric_value(row.get("C"), key)
        return None if b is None or c is None else c - b
    return _num(row.get(METRIC_BY_KEY[key]["delta_field"]))


def _metric_label(key: str, value: float | None) -> str:
    if value is None:
        return "no data"
    if key == "weighted":
        return f"{value:.1f} / 100"
    if key == "suite":
        return f"{value:.1f}%"
    if key == "tokens":
        return _fmt_int(value)
    return f"{value:.1f}s"


def _metric_n(summary: dict[str, Any] | None, key: str) -> int:
    if not summary:
        return 0
    return int(_num(summary.get(METRIC_BY_KEY[key]["n_field"])) or 0)


def _metric_eligible(row: dict[str, Any], key: str) -> bool:
    return _efficiency_eligible(row) if key in EFFICIENCY_METRICS else _headline_eligible(row)


# --- page chrome -----------------------------------------------------------------------------

STYLE = """*{box-sizing:border-box}
body{margin:0;background:#f6f4ef;color:#17191c;font-family:__SANS__;font-size:14px;line-height:1.5;
-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
a{color:#17505a;text-decoration:none}
a:hover{color:#0c383f;text-decoration:underline;text-underline-offset:2px}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;
font-variant-numeric:tabular-nums}
select{font:inherit;color:inherit;background:#fdfcfa;border:1px solid rgba(23,25,28,.28);
padding:7px 9px;min-height:40px;border-radius:2px}
summary{cursor:pointer;list-style:none}
summary::-webkit-details-marker{display:none}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
caption{caption-side:top;text-align:left;font-size:11.5px;color:#6f767f;padding:0 0 8px}
th,td{padding:9px 12px;border-bottom:1px solid rgba(23,25,28,.10);vertical-align:top;
font-size:13px;line-height:1.45;text-align:left}
th{font:600 10.5px/1.3 __SANS__;letter-spacing:.08em;text-transform:uppercase;color:#5c636b;
white-space:nowrap;border-bottom:1px solid rgba(23,25,28,.32);background:#f6f4ef}
thead th{position:sticky;top:0;z-index:2}
th[data-num],td[data-num]{text-align:right}
tbody tr:hover{background:rgba(23,25,28,.028)}
a:focus-visible,button:focus-visible,select:focus-visible,summary:focus-visible,
details:focus-visible,[tabindex]:focus-visible{outline:2px solid #17505a;outline-offset:2px}
button[aria-pressed="true"]{background:#17191c;color:#fdfcfa;border-color:#17191c}
[data-active="1"]{color:#17191c;font-weight:600}
[data-arm="B"] [data-bar]{background:repeating-linear-gradient(135deg,#5b6873,#5b6873 2px,
rgba(0,0,0,0) 2px,rgba(0,0,0,0) 5px);border-right:2px solid #39434c}
[data-arm="C"] [data-bar]{background:#17505a;border-right:2px solid #0c383f}
[data-arm="A"] [data-bar]{background:repeating-linear-gradient(135deg,#9aa1a8,#9aa1a8 1px,
rgba(0,0,0,0) 1px,rgba(0,0,0,0) 4px);border-right:2px solid #6f767f}
[data-arm="D"] [data-bar]{background:repeating-linear-gradient(135deg,#2c7a86,#2c7a86 3px,
#bfd6da 3px,#bfd6da 6px);border-right:2px solid #17505a}
[data-r="spine"]{display:grid;grid-template-columns:62px minmax(0,1fr)}
[data-r="two"]{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:44px}
[data-r="split"]{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:32px}
[data-r="onlysm"]{display:none}
[data-r="scroll"]{overflow-x:auto;-webkit-overflow-scrolling:touch}
[data-nowrap]{white-space:nowrap}
.js [data-view]{display:none}
.js [data-view].is-active{display:block}
.js [data-chain]{display:none}
.js [data-chain].chain-on{display:block}
.js [data-ladder]{display:none}
.js [data-ladder].ladder-on{display:grid}
@media(max-width:1180px){[data-r="hidemd"]{display:none!important}}
@media(max-width:1100px){[data-r="split"]{grid-template-columns:minmax(0,1fr)!important;gap:26px!important}[data-r="split"]>*+*{border-left:0!important;padding-left:0!important;border-top:1px solid rgba(23,25,28,.14);padding-top:20px}}
@media(max-width:920px){
[data-r="spine"]{grid-template-columns:22px minmax(0,1fr)}
[data-r="two"],[data-r="split"]{grid-template-columns:minmax(0,1fr);gap:26px}
[data-r="hidesm"]{display:none!important}
[data-r="onlysm"]{display:block}
[data-r="pad"]{padding-left:16px!important;padding-right:16px!important}
[data-r="body"]{padding-left:14px!important}
[data-r="two"]>*+*,[data-r="split"]>*+*{border-left:0!important;padding-left:0!important;
border-top:1px solid rgba(23,25,28,.14);padding-top:18px}
}
@media(max-width:620px){
[data-r="lane"]{display:block!important}
[data-r="lane"]>*{margin-bottom:6px}
[data-r="lane"]>*:last-child{text-align:left!important}
}
@media(prefers-reduced-motion:reduce){*{animation-duration:.001ms!important;
transition-duration:.001ms!important}}
[data-ladder-hit]:hover{background:rgba(23,25,28,.045)}
@keyframes drawin{from{stroke-dashoffset:320}to{stroke-dashoffset:0}}
@keyframes washin{from{opacity:0}to{opacity:1}}
@keyframes popin{0%{transform:translateY(-50%) scale(0);opacity:0}
60%{transform:translateY(-50%) scale(1.35);opacity:1}
100%{transform:translateY(-50%) scale(1);opacity:1}}
@keyframes slidein{from{opacity:0;transform:translate(-4px,-50%)}
to{opacity:1;transform:translateY(-50%)}}
@media print{
body{background:#fff}
*{animation:none!important}
[data-r="noprint"]{display:none!important}
[data-view]{display:block!important}
thead th{position:static}
[data-arm="C"] [data-bar]{background:#17505a!important;-webkit-print-color-adjust:exact;
print-color-adjust:exact}
}""".replace("__SANS__", SANS)


DOT_MARK = (
    '<span style="display:grid;grid-template-columns:repeat(3,4px);'
    'grid-template-rows:repeat(3,4px);gap:1.5px" aria-hidden="true">'
    '<i style="background:#17191c"></i><i style="background:#17191c"></i>'
    '<i style="background:#c9c4b8"></i><i style="background:#17505a"></i>'
    '<i style="background:#17191c"></i><i style="background:#17191c"></i>'
    '<i style="background:#17191c"></i><i style="background:#c9c4b8"></i>'
    '<i style="background:#17505a"></i></span>'
)

_RULE = "1px solid rgba(23,25,28,.14)"
_RULE_STRONG = "1px solid rgba(23,25,28,.32)"

H1 = (
    f'font-family:{SERIF};font-weight:500;font-size:clamp(32px,4.4vw,52px);line-height:1.06;'
    'letter-spacing:-.018em'
)
H1_PAGE = (
    f'font-family:{SERIF};font-weight:500;font-size:38px;line-height:1.1;letter-spacing:-.015em'
)
H2_SERIF = f'font-family:{SERIF};font-weight:500;font-size:24px;letter-spacing:-.01em'
H2_SMALL = f'font-family:{SERIF};font-weight:500;font-size:23px'
EYEBROW = (
    f'font:600 10.5px/1 {SANS};letter-spacing:.1em;text-transform:uppercase;color:#5c636b'
)
LEDE = f'font-family:{SERIF};font-size:17px;line-height:1.55;color:#3d444c;text-wrap:pretty'


def _spine(body: str, *, first: bool = False, terminal: bool = False) -> str:
    """One spine section: the rule, its node, and the indented body."""
    node_top = "41px" if first else "29px"
    if terminal:
        rule = (
            '<div style="position:absolute;right:0;top:0;height:32px;width:1px;'
            'background:rgba(23,25,28,.14)"></div>'
        )
        node = (
            f'<div style="position:absolute;right:-4.5px;top:{node_top};width:9px;height:9px;'
            'border:1px solid #17191c;background:#f6f4ef"></div>'
        )
    else:
        rule_top = "44px" if first else "0"
        rule = (
            f'<div style="position:absolute;right:0;top:{rule_top};bottom:0;width:1px;'
            'background:rgba(23,25,28,.14)"></div>'
        )
        node = (
            f'<div style="position:absolute;right:-3.5px;top:{node_top};width:7px;height:7px;'
            'background:#17191c"></div>'
        )
    border = "" if first else f'border-top:{_RULE}'
    pad = "38px 0 44px 30px" if first else "30px 0 42px 30px"
    return (
        f'<section data-r="spine" style="{border}">'
        f'<div style="position:relative">{rule}{node}</div>'
        f'<div data-r="body" style="padding:{pad};min-width:0">{body}</div>'
        "</section>"
    )


def _table(caption: str, head: str, body: str) -> str:
    return (
        f'<div data-r="scroll" style="border-top:{_RULE_STRONG}"><table>'
        f"<caption>{caption}</caption><thead><tr>{head}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _row_header(content: str, *, mono: bool = False, size: str = "13px") -> str:
    font = (
        f'font:400 {size}/1.45 {MONO}' if mono else f"font-weight:500;font-size:{size}"
    )
    return (
        f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;{font};'
        f'color:#17191c;border-bottom:1px solid rgba(23,25,28,.10)">{content}</th>'
    )


def _note(text: str, colour: str = "#17505a", *, width: str = "56em") -> str:
    return (
        f'<p style="margin:14px 0 0;font-size:12.5px;color:#3d444c;border-left:2px solid {colour};'
        f'padding-left:11px;max-width:{width}">{text}</p>'
    )


def _callout(title: str, body: str, accent: str = "#8a5a10", *, width: str = "60em") -> str:
    return (
        f'<div style="border:1px solid rgba(23,25,28,.28);border-left:3px solid {accent};'
        f'background:#fdfcfa;padding:24px 26px;max-width:{width}">'
        f'<h3 style="margin:0 0 8px;font:600 15px/1.3 {SANS}">{title}</h3>{body}</div>'
    )


def _on(condition: bool, css_class: str) -> str:
    """Emit a class attribute only when the element starts out selected."""
    return f' class="{css_class}"' if condition else ""


def _nav_link(route: str, label: str, *, active: bool) -> str:
    target = "" if route == "overview" else route
    current = ' aria-current="page"' if active else ""
    return (
        f'<a href="#/{target}" data-nav="{route}" data-active="{"1" if active else "0"}"{current} '
        'style="display:flex;align-items:center;padding:0 11px;font-size:12.5px;color:#5c636b;'
        'text-decoration:none;white-space:nowrap;border-bottom:2px solid rgba(0,0,0,0)">'
        f"{_text(label)}</a>"
    )


NAV = (
    ("overview", "Overview"),
    ("models", "Models"),
    ("tasks", "Tasks"),
    ("runs", "Runs"),
    ("methodology", "Methodology"),
    ("provenance", "Provenance"),
)


def render_header(dataset: dict[str, Any]) -> str:
    """Sticky brand bar, section nav and the pinned suite/vintage strip."""
    env = dataset.get("environment") or {}
    suite = ", ".join(dataset.get("suites") or []) or "—"
    vintage = str(dataset.get("generated_at", ""))
    items = "".join(
        _nav_link(route, label, active=route == "overview")
        for route, label in NAV
    )
    return (
        '<header data-r="noprint" style="position:sticky;top:0;z-index:20;background:#f6f4ef;'
        'border-bottom:1px solid rgba(23,25,28,.28)">'
        '<div data-r="pad" style="max-width:1320px;margin:0 auto;padding:0 34px;display:flex;'
        'align-items:stretch;justify-content:space-between;gap:24px;min-height:58px">'
        '<div style="display:flex;align-items:center;gap:14px;flex:none">'
        '<a href="#/" data-nav="overview" style="display:flex;align-items:center;gap:9px;'
        f'color:#17191c;text-decoration:none">{DOT_MARK}'
        f'<span style="font:600 14.5px/1 {SANS};letter-spacing:-.01em">CKB AI Bench</span></a>'
        "</div>"
        '<nav aria-label="Report sections" style="display:flex;align-items:stretch;gap:2px;'
        'min-width:0;flex:1 1 auto;overflow-x:auto;scrollbar-width:none">'
        f"{items}</nav>"
        '<div data-r="hidemd" style="display:flex;align-items:center;gap:18px;flex:none;'
        f'font:400 11px/1.35 {MONO};color:#5c636b">'
        f'<span>suite <span style="color:#17191c">{_text(suite)}</span></span>'
        '<span style="width:1px;height:20px;background:rgba(23,25,28,.18)"></span>'
        '<span title="Newest canonical run timestamp, not a rebuild clock">Results through '
        f'<span style="color:#17191c">{_text(vintage[:10])}</span></span></div>'
        "</div></header>"
    )


def render_meta_strip(dataset: dict[str, Any]) -> str:
    """Chain toggle plus the recorded/scored/excluded counts the whole report is bound to."""
    suite = ", ".join(dataset.get("suites") or []) or "—"
    vintage = str(dataset.get("generated_at", ""))
    chains = "".join(
        f'<button type="button" data-chain-set="{_attr(chain)}" '
        f'aria-pressed="{"true" if index == 0 else "false"}" '
        'style="padding:0 14px;min-height:40px;font-size:12.5px;letter-spacing:.02em;'
        'border-right:1px solid rgba(23,25,28,.18)">'
        f"{_text(_chain_label(chain))}</button>"
        for index, chain in enumerate(dataset.get("chains") or [])
    )
    counts = "".join(
        f'<span data-chain="{_attr(chain)}"{_on(index == 0, "chain-on")}>'
        '<span style="display:flex;flex-wrap:wrap;gap:6px 22px;font-size:12px;color:#5c636b">'
        f'<span>Suite <span style="font-family:{MONO};color:#17191c">{_text(suite)}</span>'
        " · frozen</span>"
        f"<span>{len({str(r.get('model')) for r in _runs_for(dataset, chain)})}"
        " model identities</span>"
        f"<span>{len(_runs_for(dataset, chain))} recorded runs</span>"
        f"<span>{sum(1 for r in _runs_for(dataset, chain) if _scored(r))} scored runs</span>"
        f'<span style="color:#8a5a10">'
        f"{_excluded_label(_runs_for(dataset, chain))}</span>"
        f'<span>Results through <span style="font-family:{MONO};color:#17191c">'
        f"{_text(vintage[:10])}</span></span></span></span>"
        for index, chain in enumerate(dataset.get("chains") or [])
    )
    return (
        '<div data-r="noprint" style="display:flex;flex-wrap:wrap;align-items:center;'
        'gap:12px 26px;padding:13px 0;border-bottom:1px solid rgba(23,25,28,.14)">'
        '<div role="group" aria-label="Chain" style="display:flex;'
        'border:1px solid rgba(23,25,28,.28);border-radius:2px;overflow:hidden">'
        f"{chains}</div>{counts}"
        '<div style="margin-left:auto;display:flex;align-items:center;gap:8px;font-size:11.5px;'
        'color:#5c636b">'
        '<span aria-hidden="true" style="width:6px;height:6px;background:#1d6b4f;'
        'border-radius:50%"></span>'
        "<span>Artifact-backed · rebuilds byte-identical</span></div></div>"
    )


def _excluded_label(runs: list[dict[str, Any]]) -> str:
    excluded = sum(1 for r in runs if str(r.get("outcome")) == "infra_fail")
    return f"{excluded} excluded (infrastructure)" if excluded else ""


def render_footer(dataset: dict[str, Any]) -> str:
    env = dataset.get("environment") or {}
    suite = ", ".join(dataset.get("suites") or []) or "—"
    return (
        '<footer style="border-top:1px solid rgba(23,25,28,.28);margin-top:8px;'
        'padding:26px 0 44px;display:flex;flex-wrap:wrap;gap:18px 40px;'
        'justify-content:space-between;align-items:flex-start">'
        '<div style="font-size:12px;color:#5c636b;max-width:34em">'
        '<p style="margin:0 0 7px;color:#17191c;font-weight:500">'
        "CKB AI Bench — public evidence report</p>"
        '<p style="margin:0">Generated from validated flat JSON. Same inputs and renderer '
        "revision produce byte-identical output. The visible date is canonical run evidence, "
        "not a rebuild clock.</p></div>"
        '<div style="display:flex;gap:34px;flex-wrap:wrap;font-size:12.5px">'
        '<div style="display:flex;flex-direction:column;gap:6px">'
        '<a href="#/methodology" data-nav="methodology">Methodology</a>'
        '<a href="#/provenance" data-nav="provenance">Provenance</a></div>'
        '<div style="display:flex;flex-direction:column;gap:6px">'
        '<a href="#/runs" data-nav="runs">Run explorer</a>'
        f'<a href="#/tasks" data-nav="tasks">Frozen suite {_text(suite)}</a></div>'
        f'<div style="display:flex;flex-direction:column;gap:6px;font-family:{MONO};'
        'font-size:11px;color:#5c636b">'
        f'<span>freeze {_text(_short(env.get("suite_freeze_hash"), 8))}</span>'
        f'<span>schema {_text(env.get("schema_version") or "—")}</span></div>'
        "</div></footer>"
    )


# --- readiness copy --------------------------------------------------------------------------

CORRECTNESS_CHECKS = ("fewer_than_three_scored_runs_per_arm", "unbalanced_scored_runs",
                      "unmatched_scored_seed_multiset", "completion_conditioned")
EFFICIENCY_CHECKS = ("incomplete_usage_in_scored_rows", "unbalanced_complete_usage_runs",
                     "unmatched_complete_usage_seed_multiset")


def _check_text(code: str, row: dict[str, Any]) -> str:
    readiness = _readiness(row)
    efficiency = _efficiency_readiness(row)
    scored = readiness.get("scored_runs") or {}
    recorded = readiness.get("recorded_rows") or {}
    seeds = readiness.get("scored_seed_values") or {}
    usable = efficiency.get("complete_usage_runs") or {}
    minimum = readiness.get("minimum_scored_runs_per_arm", 3)
    b_seeds = ", ".join(str(s) for s in seeds.get("B", [])) or "none"
    c_seeds = ", ".join(str(s) for s in seeds.get("C", [])) or "none"
    return {
        "fewer_than_three_scored_runs_per_arm":
            f"At least {minimum} scored runs per arm — have B {scored.get('B', 0)}, "
            f"C {scored.get('C', 0)}.",
        "unbalanced_scored_runs":
            f"Equal scored counts per arm — B {scored.get('B', 0)} against "
            f"C {scored.get('C', 0)}.",
        "unmatched_scored_seed_multiset":
            f"Matching scored seed sets — B [{b_seeds}] against C [{c_seeds}].",
        "completion_conditioned":
            f"Every recorded row scored — B {scored.get('B', 0)} of {recorded.get('B', 0)}, "
            f"C {scored.get('C', 0)} of {recorded.get('C', 0)}.",
        "incomplete_usage_in_scored_rows":
            f"Complete token usage on every scored row — B {usable.get('B', 0)} of "
            f"{scored.get('B', 0)}, C {usable.get('C', 0)} of {scored.get('C', 0)}.",
        "unbalanced_complete_usage_runs":
            f"Equal complete-usage counts — B {usable.get('B', 0)} against "
            f"C {usable.get('C', 0)}.",
        "unmatched_complete_usage_seed_multiset":
            "Matching complete-usage seed sets across both arms.",
    }.get(code, code)


def _verdict_sentence(row: dict[str, Any]) -> str:
    b, c = row.get("B") or {}, row.get("C") or {}
    readiness = _readiness(row)
    b_scored, c_scored = int(b.get("scored_runs") or 0), int(c.get("scored_runs") or 0)
    if not b_scored or not c_scored:
        return (
            "Only one arm has scored evidence, so no C minus B difference exists for this model."
        )
    b_weighted = _metric_value(b, "weighted")
    c_weighted = _metric_value(c, "weighted")
    if not _headline_eligible(row):
        excluded_b = int(readiness.get("recorded_rows", {}).get("B", 0)) - b_scored
        excluded_c = int(readiness.get("recorded_rows", {}).get("C", 0)) - c_scored
        return (
            f"Among completed scored runs, B scored {_fmt1(b_weighted)} and C scored "
            f"{_fmt1(c_weighted)} of 100. That difference is completion-conditioned: "
            f"{excluded_b} of {int(readiness.get('recorded_rows', {}).get('B', 0))} recorded B "
            f"rows and {excluded_c} of "
            f"{int(readiness.get('recorded_rows', {}).get('C', 0))} recorded C rows were "
            f"excluded, and the scored samples are {b_scored} against {c_scored}."
        )
    seeds = ", ".join(str(s) for s in readiness.get("scored_seed_values", {}).get("B", []))
    delta = _num(row.get("weighted_score_delta"))
    suite_c = _suite_pass_pct(c)
    suite_b = _suite_pass_pct(b)
    suite_note = (
        f"Suite Pass@1 is unchanged at {_fmt1(suite_c)}% in both arms."
        if suite_b is not None and suite_c is not None and abs(suite_b - suite_c) < 0.05
        else f"Suite Pass@1 moved from {_fmt1(suite_b)}% to {_fmt1(suite_c)}%."
    )
    return (
        f"Across {b_scored} scored runs per arm on matched seeds {seeds}, C scored "
        f"{_fmt1(c_weighted)} against B at {_fmt1(b_weighted)} of 100 — a descriptive difference "
        f"of {_fmt_signed(None if delta is None else delta * 100)} points weighted. {suite_note}"
    )


# --- overview stations -----------------------------------------------------------------------


def _station_hero(dataset: dict[str, Any], chain: str) -> str:
    env = dataset.get("environment") or {}
    suite = ", ".join(dataset.get("suites") or []) or "—"
    identity = [
        ("Suite", f"{suite} · frozen"),
        ("Freeze hash", _short(env.get("suite_freeze_hash"), 12)),
        ("Chain", f"{env.get('chain_id') or chain} · {env.get('lifecycle_policy') or '—'}"),
        ("MCP surface", f"docs-only-v1 · server {env.get('mcp_server_version') or '—'}"),
        ("Results through", _display_timestamp(str(dataset.get("generated_at", "")))),
        ("Result schema", str(env.get("schema_version") or "—")),
    ]
    rows = "".join(
        '<dt style="color:#5c636b;white-space:nowrap">' + _text(k) + "</dt>"
        f'<dd style="margin:0;font-family:{MONO};font-size:11.5px;color:#17191c;'
        f'overflow-wrap:anywhere">{_text(v)}</dd>'
        for k, v in identity
    )
    return (
        '<div data-r="two"><div>'
        f'<p style="margin:0 0 16px;font:500 10.5px/1 {MONO};letter-spacing:.14em;'
        'text-transform:uppercase;color:#8a5a10">Phase one · '
        f"{_text(_chain_label(chain))} only</p>"
        f'<h1 style="margin:0 0 18px;{H1};max-width:15em;text-wrap:pretty">'
        "Does CKB AI improve CKB development?</h1>"
        f'<p style="margin:0;font-family:{SERIF};font-size:18.5px;line-height:1.52;color:#3d444c;'
        'max-width:34em;text-wrap:pretty">The same model runs the same frozen suite twice under '
        "matched budgets and seeds — once with ordinary web research only, once with the pinned "
        f'<span style="font-family:{MONO};font-size:15.5px;color:#17191c">docs-only-v1</span> '
        "documentation surface added. The measured quantity is arm C minus arm B, per model, "
        "never pooled.</p></div>"
        f'<div style="border-left:{_RULE};padding-left:26px;align-self:start">'
        f'<h2 style="margin:0 0 12px;{EYEBROW}">Report identity</h2>'
        '<dl style="margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);'
        f'gap:7px 16px;font-size:12.5px">{rows}</dl></div></div>'
    )


def _station_evidence_status(dataset: dict[str, Any], chain: str) -> str:
    rows = _comparisons_for(dataset, chain)
    header = (
        f'<h2 style="margin:0 0 5px;{H2_SERIF}">Evidence status</h2>'
        '<p style="margin:0 0 22px;font-size:13px;color:#5c636b;max-width:52em">One statement per '
        "model identity. Nothing here is pooled across models, chains, or suites.</p>"
    )
    if not rows:
        return header + _callout(
            f"No {_chain_label(chain)} runs yet",
            '<p style="margin:0 0 12px;font-size:13.5px;color:#3d444c;max-width:48em">Phase one '
            "is DevNet-only. No benchmark row has been recorded against "
            f"{_text(_chain_label(chain))}, and DevNet evidence is never copied, scaled, or "
            "inferred across the chain boundary — so there is nothing to plot here rather than a "
            "chart to fill.</p>"
            '<p style="margin:0;font-size:13px"><a href="#/methodology" data-nav="methodology">'
            "Why phase one is DevNet-only →</a></p>",
        )
    cards = []
    for row in rows:
        status, tone, glyph = _status_for(row)
        model = str(row.get("model"))
        readiness = _readiness(row)
        efficiency = _efficiency_readiness(row)
        failed = set(readiness.get("reasons") or []) | set(efficiency.get("reasons") or [])
        checks = "".join(
            '<li style="display:grid;grid-template-columns:14px minmax(0,1fr);gap:8px;'
            'font-size:12px;line-height:1.45;color:#3d444c">'
            f'<span aria-hidden="true" style="color:'
            f'{TONE["incon"] if code in failed else TONE["pos"]};font-size:11px">'
            f'{"✕" if code in failed else "✓"}</span>'
            f"<span>{_text(_check_text(code, row))}</span></li>"
            for code in CORRECTNESS_CHECKS + EFFICIENCY_CHECKS
        )
        eligible = _headline_eligible(row)
        b_scored = int((row.get("B") or {}).get("scored_runs") or 0)
        c_scored = int((row.get("C") or {}).get("scored_runs") or 0)
        basis = (
            "Headline-eligible descriptive difference. Not a claim of statistical power or "
            "universal causality."
            if eligible
            else ("Unavailable — one arm has no scored rows."
                  if not b_scored or not c_scored
                  else "Provisional, completion-conditioned. Arithmetic below is shown as detail, "
                       "not as a verdict.")
        )
        recorded = sum(
            1 for r in _runs_for(dataset, chain) if str(r.get("model")) == model
        )
        profile = str((row.get("B") or row.get("C") or {}).get("model_profile_id") or "—")
        cards.append(
            f'<div style="border-top:{_RULE};padding:20px 0">'
            '<div data-r="split" style="gap:28px"><div>'
            '<div style="display:flex;align-items:center;gap:9px;margin-bottom:9px">'
            f'<span aria-hidden="true" style="font-size:13px;line-height:1;color:{tone}">'
            f"{glyph}</span>"
            f'<span style="font:600 16.5px/1.2 {SANS};color:{tone}">{_text(status)}</span></div>'
            '<p style="margin:0 0 12px;font-size:13.5px;line-height:1.55;color:#3d444c;'
            f'max-width:40em;text-wrap:pretty">{_text(_verdict_sentence(row))}</p>'
            '<div style="display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:#5c636b">'
            f'<span>Model <span style="font-family:{MONO};color:#17191c">{_text(model)}</span>'
            "</span>"
            f'<span>Profile <span style="font-family:{MONO};color:#17191c">{_text(profile)}'
            "</span></span>"
            f'<span><a href="#/runs" data-nav="runs">Trace to {recorded} runs →</a></span>'
            "</div></div>"
            f'<div style="border-left:{_RULE};padding-left:22px">'
            f'<h4 style="margin:0 0 9px;font:600 10px/1 {SANS};letter-spacing:.1em;'
            'text-transform:uppercase;color:#5c636b">Comparison basis</h4>'
            '<p style="margin:0 0 10px;font-size:12.5px;line-height:1.5;color:#17191c">'
            f"{_text(basis)}</p>"
            '<ul style="margin:0;padding:0;list-style:none;display:flex;flex-direction:column;'
            f'gap:5px">{checks}</ul></div></div></div>'
        )
    return header + '<div style="display:flex;flex-direction:column;gap:0">' + "".join(cards) \
        + "</div>"


def _axis_max(rows: list[dict[str, Any]], metric: dict[str, Any]) -> float:
    if metric["axis"]:
        return float(metric["axis"])
    values = [
        value
        for row in rows
        for arm in ("B", "C")
        if (value := _metric_value(row.get(arm), metric["key"])) is not None
    ]
    largest = max(values) if values else 0.0
    return largest * 1.18 if largest > 0 else 1.0


def _tick_label(metric_key: str, value: float) -> str:
    if metric_key == "tokens":
        return f"{round(value / 1000)}k" if value >= 1000 else str(round(value))
    if metric_key == "wall":
        return f"{round(value)}s"
    return str(round(value))


def _usage_cohort(summary: dict[str, Any] | None) -> str:
    if not summary:
        return "no scored rows"
    scored = int(summary.get("scored_runs") or 0)
    complete = int(summary.get("efficiency_runs") or 0)
    if not scored:
        return "no scored rows"
    tail = "" if complete == scored else " — efficiency ineligible"
    return f"complete usage on {complete} of {scored} scored rows{tail}"


def _comparison_figure(row: dict[str, Any], metric: dict[str, Any], axis: float) -> str:
    """One model's paired B/C lanes for one metric, with its exact values as a table."""
    key = metric["key"]
    model = str(row.get("model"))
    lanes = []
    for arm in ("B", "C"):
        summary = row.get(arm)
        value = _metric_value(summary, key)
        pct = 0.0 if value is None else max(0.6, min(100.0, (value / axis) * 100.0))
        lanes.append(
            f'<div data-arm="{arm}" data-r="lane" style="display:grid;'
            'grid-template-columns:154px minmax(0,1fr) 122px;gap:14px;align-items:center">'
            '<div style="display:flex;align-items:center;gap:8px;min-width:0">'
            f'<span aria-hidden="true" style="font-size:10px;color:#17191c">'
            f'{ARM_META[arm]["marker"]}</span>'
            '<span style="font-size:12.5px;font-weight:500;white-space:nowrap;overflow:hidden;'
            f'text-overflow:ellipsis">{_text(ARM_LABELS[arm])}</span></div>'
            '<div style="position:relative;height:22px;background:#eae7e0;'
            'border:1px solid rgba(23,25,28,.12)">'
            f'<div data-bar style="position:absolute;left:0;top:0;bottom:0;width:{pct:.2f}%;'
            'transition:width .42s cubic-bezier(.2,.7,.2,1)"></div>'
            '<span style="position:absolute;left:0;top:0;bottom:0;width:1px;'
            'background:rgba(23,25,28,.14)"></span></div>'
            '<div style="text-align:right;font-size:13px;white-space:nowrap">'
            f'<span style="font-weight:600">{_text(_metric_label(key, value))}</span>'
            f'<span style="color:#5c636b;font-size:11.5px"> n={_metric_n(summary, key)}</span>'
            "</div></div>"
        )
    ticks = "".join(
        f'<span style="position:absolute;left:{frac * 100:.0f}%;top:0;'
        f'transform:translateX(-50%);font:400 10px/1 {MONO};color:#8d949b">'
        f"{_text(_tick_label(key, axis * frac))}</span>"
        for frac in (0, 0.25, 0.5, 0.75, 1)
    )
    delta = _metric_delta(row, key)
    eligible = _metric_eligible(row, key)
    good = 0.0 if delta is None else delta * metric["dir"]
    tone = TONE["incon"] if not eligible else (
        TONE["pos"] if good > 0.5 else TONE["neg"] if good < -0.5 else TONE["flat"]
    )
    b_value = _metric_value(row.get("B"), key)
    pct_part = (
        f" ({_fmt_signed((delta / b_value) * 100, 1)}%)"
        if delta is not None and b_value else ""
    )
    delta_text = (
        ("withheld — usage cohort ineligible" if key in EFFICIENCY_METRICS
         else "no difference available") if delta is None
        else f"{_fmt_signed(delta, metric['digits'])} {metric['delta_unit']}{pct_part}"
    )
    body_rows = "".join(
        "<tr>"
        + _row_header(_text(ARM_LABELS[arm]), size="13px").replace(
            "font-weight:500", "font-weight:600"
        )
        + f'<td style="font-family:{MONO};font-size:12px">'
        f'{_text(ARM_META[arm]["surface"])}</td>'
        f'<td data-num style="font-weight:600">'
        f'{_text(_metric_label(key, _metric_value(row.get(arm), key)))}</td>'
        f"<td data-num>{_metric_n(row.get(arm), key)}</td>"
        f'<td style="font-size:12px;color:#3d444c">{_text(_usage_cohort(row.get(arm)))}</td></tr>'
        for arm in ("B", "C")
    )
    better = "Higher is better." if metric["better"] == "higher" else "Lower is better."
    suite = str(row.get("suite_semver") or "")
    return (
        f'<figure data-metric="{key}" style="margin:0;border-top:{_RULE};padding:20px 0 18px">'
        '<figcaption style="display:flex;flex-wrap:wrap;align-items:baseline;'
        'justify-content:space-between;gap:14px;margin-bottom:16px">'
        '<span style="display:flex;flex-wrap:wrap;align-items:baseline;gap:10px">'
        f'<span style="font:500 15px/1.2 {MONO};color:#17191c">{_text(model)}</span>'
        f'<span style="font-size:12px;color:#5c636b">{_text(metric["label"])} · '
        f'{_text(_chain_label(str(row.get("chain"))))} · suite {_text(suite)} · '
        f'{_text(metric["unit"])}</span></span>'
        '<span style="display:flex;flex-wrap:wrap;align-items:baseline;gap:10px">'
        f'<span style="font:600 15px/1.2 {SANS};color:{tone}">'
        f'{"C − B " if eligible else "Provisional C − B "}{_text(delta_text)}</span>'
        '<span style="font-size:11.5px;color:#5c636b">'
        f'{"headline-eligible" if eligible else "not headline-eligible"}</span></span>'
        "</figcaption>"
        '<div style="display:flex;flex-direction:column;gap:7px">'
        + "".join(lanes)
        + '<div data-r="lane" style="display:grid;'
        'grid-template-columns:154px minmax(0,1fr) 122px;gap:14px"><span></span>'
        f'<div style="position:relative;height:16px">{ticks}</div><span></span></div></div>'
        '<details style="margin-top:12px;border-top:1px solid rgba(23,25,28,.10);'
        'padding-top:10px">'
        '<summary style="font-size:12px;color:#17505a">Exact values as a table</summary>'
        '<div data-r="scroll" style="margin-top:10px"><table>'
        f'<caption>{_text(metric["label"])} for {_text(model)}, '
        f'{_text(_chain_label(str(row.get("chain"))))}, suite {_text(suite)}. {better}</caption>'
        '<thead><tr><th scope="col">Arm</th><th scope="col">Surface</th>'
        '<th scope="col" data-num>Value</th><th scope="col" data-num>Scored n</th>'
        '<th scope="col">Usage cohort</th></tr></thead>'
        f"<tbody>{body_rows}</tbody></table></div></details></figure>"
    )


def _station_comparison(dataset: dict[str, Any], chain: str) -> str:
    rows = _comparisons_for(dataset, chain)
    if not rows:
        return ""
    buttons = "".join(
        f'<button type="button" data-metric-set="{metric["key"]}" '
        f'aria-pressed="{"true" if index == 0 else "false"}" '
        'style="padding:0 13px;min-height:40px;font-size:12.5px;'
        'border-right:1px solid rgba(23,25,28,.18)">'
        f'{_text(metric["label"])}</button>'
        for index, metric in enumerate(METRICS)
    )
    notes = "".join(
        f'<p data-metric="{metric["key"]}" aria-live="polite" style="margin:0 0 24px;'
        'font-size:12.5px;color:#3d444c;border-left:2px solid #17505a;padding-left:11px">'
        f'{_text(metric["label"])} — '
        f'{"higher is better" if metric["better"] == "higher" else "lower is better"}. '
        f'Measured in {_text(metric["unit"])} on a truthful zero-to-'
        f'{"100" if metric["axis"] else "maximum"} scale; every value is printed at rest, and no '
        "series is overlaid on another.</p>"
        for metric in METRICS
    )
    figures = "".join(
        _comparison_figure(row, metric, _axis_max(rows, metric))
        for metric in METRICS
        for row in rows
    )
    return (
        '<div style="display:flex;flex-wrap:wrap;align-items:flex-end;'
        'justify-content:space-between;gap:18px;margin-bottom:6px"><div>'
        f'<h2 style="margin:0 0 5px;{H2_SERIF}">B versus C</h2>'
        '<p style="margin:0;font-size:13px;color:#5c636b;max-width:46em">One row per arm, one '
        "block per model. Arms are never overlaid and models are never averaged together.</p>"
        "</div>"
        '<div role="group" aria-label="Metric" data-r="noprint" style="display:flex;'
        'flex-wrap:wrap;border:1px solid rgba(23,25,28,.28);border-radius:2px;overflow:hidden">'
        f"{buttons}</div></div>{notes}"
        f'<div style="display:flex;flex-direction:column;gap:2px">{figures}</div>'
    )


def _readiness_label(row: dict[str, Any]) -> tuple[str, str, str]:
    if _headline_eligible(row):
        return "Headline-eligible", TONE["pos"], "●"
    status, tone, _ = _status_for(row)
    return status, tone, "◑"


def _station_model_comparison(dataset: dict[str, Any], chain: str) -> str:
    rows = _comparisons_for(dataset, chain)
    if not rows:
        return ""
    body = []
    for row in rows:
        b, c = row.get("B") or {}, row.get("C") or {}
        delta = _num(row.get("weighted_score_delta"))
        readiness, r_tone, r_glyph = _readiness_label(row)
        tone = TONE["incon"] if not _headline_eligible(row) else (
            TONE["pos"] if (delta or 0) > 0 else TONE["neg"] if (delta or 0) < 0 else TONE["flat"]
        )
        model = str(row.get("model"))
        body.append(
            "<tr>"
            f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
            f'font:500 13px/1.4 {MONO};color:#17191c;'
            'border-bottom:1px solid rgba(23,25,28,.10)">'
            f'<a href="#/models" data-nav="models">{_text(model)}</a>'
            f'<span style="display:block;font-family:{SANS};font-size:11.5px;font-weight:400;'
            f'color:#5c636b">{_text(row.get("family") or "")}</span></th>'
            f'<td style="font-family:{MONO};font-size:11.5px">'
            f'{_text((b or c).get("model_profile_id") or "—")}'
            f'<span style="display:block;color:#5c636b">'
            f'{_text(_short((b or c).get("model_profile_sha256"), 8))}</span></td>'
            f'<td data-num>{int(b.get("runs") or 0)} / {int(c.get("runs") or 0)}</td>'
            f'<td data-num>{int(b.get("scored_runs") or 0)} / '
            f'{int(c.get("scored_runs") or 0)}</td>'
            f'<td data-num>{_text(_metric_label("weighted", _metric_value(b, "weighted")))}</td>'
            f'<td data-num>{_text(_metric_label("weighted", _metric_value(c, "weighted")))}</td>'
            f'<td data-num style="font-weight:600;color:{tone}">'
            f'{_text(_fmt_signed(None if delta is None else delta * 100))}</td>'
            f'<td><span style="display-inline-flex;font-size:12px;color:{r_tone}">'
            f'<span aria-hidden="true">{r_glyph}</span> {_text(readiness)}</span></td></tr>'
        )
    head = (
        '<th scope="col">Model</th><th scope="col">Profile</th>'
        '<th scope="col" data-num>Rec. B/C</th><th scope="col" data-num>Scored B/C</th>'
        '<th scope="col" data-num>Weighted B</th><th scope="col" data-num>Weighted C</th>'
        '<th scope="col" data-num>C − B</th><th scope="col">Readiness</th>'
    )
    suite = ", ".join(dataset.get("suites") or [])
    return (
        f'<h2 style="margin:0 0 5px;{H2_SERIF}">Model comparison</h2>'
        '<p style="margin:0 0 18px;font-size:13px;color:#5c636b;max-width:52em">Every row keeps '
        "its own denominators and readiness. There is deliberately no combined ranking column — "
        "evidence eligibility differs between these models.</p>"
        + _table(
            f"Weighted score by arm, {_text(_chain_label(chain))}, suite {_text(suite)}. "
            "Recorded rows include infrastructure failures; scored rows do not.",
            head,
            "".join(body),
        )
        + '<p style="margin:14px 0 0;font-size:12px;color:#5c636b">'
        '<a href="#/models" data-nav="models">Full model comparison, including efficiency and '
        "reliability columns →</a></p>"
    )


def _station_task_table(dataset: dict[str, Any], chain: str) -> str:
    rows = _comparisons_for(dataset, chain)
    if not rows:
        return ""
    columns = [(row, arm) for row in rows for arm in ("B", "C")]
    head = (
        '<th scope="col">Task</th><th scope="col" data-num>Weight</th>'
        + "".join(
            f'<th scope="col" data-num>{_text(str(row.get("model")).split("/")[-1])} {arm}</th>'
            for row, arm in columns
        )
    )
    task_ids = [str(t.get("task_id")) for t in (rows[0].get("task_comparisons") or [])]
    body = []
    for task_id in task_ids:
        cells = []
        for row, arm in columns:
            comparison = next(
                (t for t in row.get("task_comparisons") or []
                 if str(t.get("task_id")) == task_id),
                {},
            )
            side = comparison.get(arm) or {}
            other = comparison.get("C" if arm == "B" else "B") or {}
            runs = int(side.get("runs") or 0)
            passes = int(side.get("passes") or 0)
            rate = _num(side.get("pass_rate"))
            other_rate = _num(other.get("pass_rate"))
            differs = rate is not None and other_rate is not None \
                and abs(rate - other_rate) > 0.001
            tone = TONE["faint"] if not runs else (
                (TONE["pos"] if rate > other_rate else TONE["neg"]) if differs else TONE["ink"]
            )
            cells.append(
                f'<td data-num style="color:{tone}">'
                f'<span style="font-weight:{"600" if differs else "400"}">'
                f'{f"{passes}/{runs}" if runs else "—"}</span>'
                '<span style="display:block;font-size:10.5px;color:#8d949b">'
                f'{f"{round(rate * 100)}%" if runs and rate is not None else "no rows"}'
                "</span></td>"
            )
        body.append(
            "<tr>"
            + _row_header(
                f'<a href="#/tasks" data-nav="tasks">{_text(_task_name(task_id))}</a>'
                f'<span style="display:block;font:400 11px/1.4 {MONO};color:#5c636b">'
                f"{_text(task_id)}</span>",
                size="13px",
            )
            + f'<td data-num style="color:#3d444c">{_task_weight(dataset, task_id)}</td>'
            + "".join(cells)
            + "</tr>"
        )
    totals = "".join(
        f'<td data-num style="font-weight:600">'
        f'{_text(_fmt1(_metric_value(row.get(arm), "weighted")))}</td>'
        for row, arm in columns
    )
    body.append(
        '<tr style="background:rgba(23,25,28,.03)">'
        '<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
        'font-weight:600;font-size:13px;color:#17191c">Points available · weighted mean by arm'
        '</th><td data-num style="font-weight:600">100</td>' + totals + "</tr>"
    )
    suite = ", ".join(dataset.get("suites") or [])
    return (
        f'<h2 style="margin:0 0 5px;{H2_SERIF}">Where B and C differ, task by task</h2>'
        '<p style="margin:0 0 18px;font-size:13px;color:#5c636b;max-width:52em">Pass counts over '
        "scored runs, not rates without denominators. A run passes the suite only when all five "
        "scored tasks pass, so a nonzero weighted score is much weaker evidence than Suite "
        "Pass@1.</p>"
        + _table(
            f"Task pass counts by model and arm. {_text(_chain_label(chain))}, suite "
            f"{_text(suite)}, scored runs only.",
            head,
            "".join(body),
        )
        + _note(
            "Suite Pass@1 is stricter than a nonzero weighted score: a run passes only when every "
            "scored task passes, so a run can carry most of the points and still fail the suite.",
            "#8a5a10",
        )
    )


def _task_weight(dataset: dict[str, Any], task_id: str) -> str:
    for run in dataset.get("runs", []):
        for task in run.get("tasks") or []:
            if str(task.get("task_id")) == task_id:
                score = _num(task.get("score"))
                if score is not None:
                    return _fmt_int(score)
    return "—"


def _station_efficiency_reliability(dataset: dict[str, Any], chain: str) -> str:
    rows = _comparisons_for(dataset, chain)
    if not rows:
        return ""
    runs = _runs_for(dataset, chain)
    eff_body, health_body = [], []
    infra_total = 0
    for row in rows:
        model = str(row.get("model"))
        for arm in ("B", "C"):
            summary = row.get(arm) or {}
            health = _arm_health(runs, model, arm)
            infra_total += health["infra"]
            label = f"{model} · {arm}"
            tokens = _metric_value(summary, "tokens")
            wall = _metric_value(summary, "wall")
            complete = int(summary.get("efficiency_runs") or 0)
            scored = int(summary.get("scored_runs") or 0)
            all_complete = bool(scored) and complete == scored
            eff_body.append(
                "<tr>"
                f'<th scope="row" data-arm="{arm}" style="background:none;text-transform:none;'
                'letter-spacing:0;font-weight:500;font-size:12.5px;color:#17191c;'
                'border-bottom:1px solid rgba(23,25,28,.10)">'
                f'<span aria-hidden="true" style="font-size:9px;margin-right:6px">'
                f'{ARM_META[arm]["marker"]}</span>{_text(label)}</th>'
                f'<td data-num>{"ineligible" if tokens is None else _fmt_int(tokens)}</td>'
                f'<td data-num>{"ineligible" if wall is None else _fmt1(wall) + "s"}</td>'
                f'<td style="font-size:11.5px;color:'
                f'{TONE["pos"] if all_complete else TONE["incon"]}">'
                f'{"no scored rows" if not scored else f"{complete} of {scored} scored rows complete"}'
                "</td></tr>"
            )
            recorded = health["recorded"]
            pct = round((health["infra"] / recorded) * 100) if recorded else 0
            health_body.append(
                "<tr>"
                f'<th scope="row" data-arm="{arm}" style="background:none;text-transform:none;'
                'letter-spacing:0;font-weight:500;font-size:12.5px;color:#17191c;'
                'border-bottom:1px solid rgba(23,25,28,.10)">'
                f'<span aria-hidden="true" style="font-size:9px;margin-right:6px">'
                f'{ARM_META[arm]["marker"]}</span>{_text(label)}</th>'
                f"<td data-num>{recorded}</td>"
                f'<td data-num style="color:'
                f'{TONE["infra"] if health["infra"] else TONE["flat"]};'
                f'font-weight:{"600" if health["infra"] else "400"}">'
                f'{health["infra"]} ({pct}%)</td>'
                f'<td data-num>{health["protocol"]}</td>'
                f'<td data-num>{health["retries"]}</td></tr>'
            )
    efficiency = (
        f'<div style="min-width:0"><h2 style="margin:0 0 5px;{H2_SERIF}">Efficiency</h2>'
        '<p style="margin:0 0 16px;font-size:13px;color:#5c636b">Token and time means use only '
        "rows where every provider attempt returned valid usage under one model identity.</p>"
        + _table(
            "Complete tokens and agent time, eligible rows only.",
            '<th scope="col">Model / arm</th><th scope="col" data-num>Tokens</th>'
            '<th scope="col" data-num>Agent time</th><th scope="col">Usage cohort</th>',
            "".join(eff_body),
        )
        + '<p style="margin:12px 0 0;font-size:12px;color:#5c636b">Lower is better for both '
        "columns. Differences are reported as C minus B, so a negative value means the "
        "documentation surface cost less.</p></div>"
    )
    reliability = (
        f'<div style="min-width:0"><h2 style="margin:0 0 5px;{H2_SERIF}">Reliability</h2>'
        '<p style="margin:0 0 16px;font-size:13px;color:#5c636b">Infrastructure failures stay in '
        "recorded counts and keep their own route. They are never scored as zero.</p>"
        + _table(
            "Recorded rows by outcome. Excluded rows are missing from correctness means but "
            "present here.",
            '<th scope="col">Model / arm</th><th scope="col" data-num>Recorded</th>'
            '<th scope="col" data-num>Infra fail</th><th scope="col" data-num>Protocol</th>'
            '<th scope="col" data-num>Retries</th>',
            "".join(health_body),
        )
        + '<p style="margin:12px 0 0;font-size:12px;color:#5c636b">'
        f'<a href="#/runs" data-nav="runs">Open the {infra_total} infrastructure-failed rows in '
        "the run explorer →</a></p></div>"
    )
    return f'<div data-r="split" style="gap:38px">{efficiency}{reliability}</div>'


ARM_X = {"A": 12.5, "B": 37.5, "C": 62.5, "D": 87.5}
ARM_CONDITION = {
    "A": "no CKB AI · no web",
    "B": "no CKB AI · web",
    "C": "CKB AI · web",
    "D": "CKB AI · no web",
}
ARM_ROLE = {"A": "floor", "B": "baseline", "C": "treatment", "D": "diagnostic"}
LADDER_GRID = (100, 75, 50, 25, 0)
LADDER_PLOT_HEIGHT = "210px"


def _arm_summaries_for(
    dataset: dict[str, Any], chain: str, model: str
) -> dict[str, dict[str, Any]]:
    return {
        str(summary.get("arm")): summary
        for summary in dataset.get("phase_one_arms", [])
        if str(summary.get("chain")) == chain and str(summary.get("model")) == model
    }


def _ladder_points(summaries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One plotted point per arm, in fixed semantic order. Absent arms carry no coordinate.

    The whisker is the observed spread of scored weighted scores, not an inferential interval, so
    it is only drawn where more than one scored run exists.
    """
    points = []
    for arm in ARMS:
        summary = summaries.get(arm) or {}
        mean = _num(summary.get("weighted_score_mean"))
        scored = int(summary.get("scored_runs") or 0)
        observed = [
            v * 100.0 for value in summary.get("weighted_score_values") or []
            if (v := _num(value)) is not None
        ]
        value = None if mean is None else mean * 100.0
        has_spread = value is not None and scored > 1 and len(observed) > 1
        high = max(observed) if has_spread else 0.0
        low = min(observed) if has_spread else 0.0
        points.append({
            "arm": arm,
            "x": ARM_X[arm],
            "value": value,
            "top": 100.0 if value is None else 100.0 - value,
            "scored": scored,
            "has_ci": has_spread,
            "spread_low": low,
            "spread_high": high,
            "ci_top": 100.0 - high if has_spread else 0.0,
            "ci_height": high - low if has_spread else 0.0,
        })
    return points


def _ladder_segments(points: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Polyline and closed-area coordinates for each contiguous run of recorded arms.

    A gap breaks the line rather than interpolating across an arm that was never run.
    """
    segments: list[tuple[str, str]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current
        if len(current) > 1:
            pts = " ".join(f'{p["x"]:.1f},{p["top"]:.2f}' for p in current)
            area = f'{pts} {current[-1]["x"]:.1f},100 {current[0]["x"]:.1f},100'
            segments.append((pts, area))
        current = []

    for point in points:
        if point["value"] is None:
            flush()
        else:
            current.append(point)
    flush()
    return segments


def _ladder_figure(model: str, chain: str, suite: str, points: list[dict[str, Any]],
                   gradient_id: str) -> str:
    """The plotted ladder: gridded plot area, area wash, connecting line and per-arm markers."""
    axis_labels = "".join(
        f'<span style="position:absolute;right:8px;top:{100 - value}%;'
        f'transform:translateY(-50%);font:400 9.5px/1 {MONO};color:#8d949b">{value}</span>'
        for value in LADDER_GRID
    )
    grid_rules = "".join(
        f'<span style="position:absolute;left:0;right:0;top:{100 - value}%;height:1px;'
        'background:rgba(23,25,28,.07)"></span>'
        for value in LADDER_GRID
    )
    shapes = "".join(
        f'<polygon points="{area}" fill="url(#{gradient_id})" '
        'style="animation:washin .7s ease-out both"></polygon>'
        f'<polyline points="{pts}" fill="none" stroke="#17505a" stroke-width="1.5" '
        'stroke-linejoin="round" vector-effect="non-scaling-stroke" stroke-dasharray="320" '
        'style="animation:drawin .85s cubic-bezier(.32,.72,.28,1) both"></polyline>'
        for pts, area in _ladder_segments(points)
    )
    markers = []
    for point in points:
        arm, top = point["arm"], point["top"]
        parts = [
            '<span data-ladder-hit style="position:absolute;left:-13%;width:26%;top:0;'
            'bottom:0;cursor:default"></span>'
        ]
        if point["has_ci"]:
            parts.append(
                f'<span style="position:absolute;top:{point["ci_top"]:.2f}%;'
                f'height:{point["ci_height"]:.2f}%;left:-0.5px;width:1px;'
                'background:rgba(23,25,28,.32)"></span>'
            )
        if point["value"] is None:
            parts.append(
                '<span style="position:absolute;top:0;bottom:0;left:-0.5px;width:1px;'
                'background:repeating-linear-gradient(to bottom,rgba(23,25,28,.22),'
                'rgba(23,25,28,.22) 2px,rgba(0,0,0,0) 2px,rgba(0,0,0,0) 5px)"></span>'
                '<span style="position:absolute;top:50%;left:7px;transform:translateY(-50%);'
                'font-size:10.5px;color:#6f767f;white-space:nowrap;'
                'animation:slidein .5s ease-out both">no runs recorded</span>'
            )
        elif arm in ("A", "B"):
            parts.append(
                f'<span style="position:absolute;top:{top:.2f}%;left:-4.5px;width:9px;height:9px;'
                'border:1.5px solid #39434c;background:#fdfcfa;border-radius:50%;'
                'transform:translateY(-50%);'
                'animation:popin .5s cubic-bezier(.3,1.4,.5,1) both"></span>'
            )
        else:
            parts.append(
                f'<span style="position:absolute;top:{top:.2f}%;left:-4px;width:8px;height:8px;'
                'background:#17505a;transform:translateY(-50%);'
                'animation:popin .5s cubic-bezier(.3,1.4,.5,1) .1s both"></span>'
            )
        if point["value"] is not None:
            side = "right:10px" if arm == "D" else "left:10px"
            parts.append(
                f'<span style="position:absolute;top:{top:.2f}%;{side};'
                f'transform:translateY(-50%);font:600 11.5px/1 {SANS};color:#17191c;'
                'white-space:nowrap;animation:slidein .55s ease-out .2s both">'
                f'{_text(_fmt1(point["value"]))}</span>'
            )
        markers.append(
            f'<div style="position:absolute;left:{point["x"]}%;top:0;bottom:0;width:0">'
            + "".join(parts) + "</div>"
        )
    axis_cells = []
    for point in points:
        arm = point["arm"]
        count = f'n={point["scored"]}' if point["scored"] else "no runs"
        axis_cells.append(
            '<div style="padding:0 4px;text-align:center;min-width:0">'
            '<span style="display:block;width:1px;height:5px;background:rgba(23,25,28,.3);'
            'margin:0 auto 6px"></span>'
            f'<span style="display:block;font:600 13px/1.1 {SANS}">{arm}</span>'
            '<span style="display:block;font-size:10px;line-height:1.35;color:#3d444c;'
            f'margin-top:3px;text-wrap:balance">{_text(ARM_CONDITION[arm])}</span>'
            f'<span style="display:block;font:400 9.5px/1.3 {MONO};color:#8d949b;margin-top:3px">'
            f'{_text(ARM_ROLE[arm])} · {count}</span></div>'
        )
    axis = "".join(axis_cells)
    return (
        '<figure style="margin:0;min-width:0">'
        '<figcaption style="font-size:12px;color:#3d444c;margin-bottom:14px">'
        f"Weighted score by condition — {_text(model)}, {_text(_chain_label(chain))}, suite "
        f"{_text(suite)}. Points of 100, higher is better.</figcaption>"
        '<div style="display:grid;grid-template-columns:34px minmax(0,1fr)">'
        f'<div style="position:relative;height:{LADDER_PLOT_HEIGHT}">{axis_labels}</div>'
        f'<div style="position:relative;height:{LADDER_PLOT_HEIGHT};background:#fdfcfa;'
        'border-left:1px solid rgba(23,25,28,.30);'
        f'border-bottom:1px solid rgba(23,25,28,.30)">{grid_rules}'
        '<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true" '
        'focusable="false" style="position:absolute;left:0;top:0;width:100%;height:100%;'
        'overflow:visible">'
        f'<defs><linearGradient id="{gradient_id}" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#17505a" stop-opacity="0.26"></stop>'
        '<stop offset="1" stop-color="#17505a" stop-opacity="0.05"></stop>'
        f"</linearGradient></defs>{shapes}</svg>"
        + "".join(markers)
        + "</div><span></span>"
        '<div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr))">'
        f"{axis}</div></div>"
        '<p style="margin:14px 0 0;font-size:12px;line-height:1.6;color:#3d444c;'
        'border-left:2px solid #17505a;padding-left:11px;max-width:44em">Reading the ladder: '
        '<span style="color:#17191c;font-weight:500">B → C is the headline comparison</span> — '
        "the same conditions plus the pinned documentation surface. A is the innate-ability floor "
        "and D is a diagnostic slice, so a rise from A to B is web research working, not CKB AI. "
        "Open circles are arms with CKB AI off, filled squares are arms with it on; the vertical "
        "whisker is the observed spread across scored seeds, drawn only where more than one "
        "scored run exists.</p></figure>"
    )


def _station_ladder(dataset: dict[str, Any], chain: str) -> str:
    """Condition ladder in the design's plotted form, on the project's per-arm Pass@1 metric."""
    models = sorted({
        str(cell.get("model")) for cell in dataset.get("cells", [])
        if str(cell.get("chain")) == chain
    })
    if not models:
        return ""
    options = "".join(
        f'<option value="{_attr(model)}">{_text(model)}</option>' for model in models
    )
    suite = ", ".join(dataset.get("suites") or [])
    blocks = []
    for index, model in enumerate(models):
        points = _ladder_points(_arm_summaries_for(dataset, chain, model))
        body_rows = []
        for point in points:
            arm = point["arm"]
            value = "no runs" if point["value"] is None else _fmt1(point["value"]) + " / 100"
            if point["has_ci"]:
                interval = f'{_fmt1(point["spread_low"])} – {_fmt1(point["spread_high"])}'
            else:
                interval = "not defined at this n"
            body_rows.append(
                "<tr>"
                + _row_header(arm, size="13px").replace(
                    "font-weight:500", "font-weight:600"
                )
                + '<td style="font-size:12px;color:#3d444c">'
                + f'{_text(ARM_META[arm]["long"])}</td>'
                + f"<td data-num>{value}</td>"
                + f'<td data-num>{point["scored"]}</td>'
                + '<td style="font-size:12px;color:#5c636b">'
                + f"{interval}</td></tr>"
            )
        table_rows = "".join(body_rows)
        blocks.append(
            f'<div data-ladder="{_attr(model)}"{_on(index == 0, "ladder-on")}'
            ' data-r="split" style="gap:34px;margin-top:20px;align-items:start">'
            + _ladder_figure(model, chain, suite, points, f"ladderWash-{chain}-{index}")
            + _table(
                f"Condition ladder values for {_text(model)}. Arms with no recorded runs are "
                "absent, not zero.",
                '<th scope="col">Arm</th><th scope="col">Condition</th>'
                '<th scope="col" data-num>Weighted mean</th>'
                '<th scope="col" data-num>Scored n</th>'
                '<th scope="col">Observed spread</th>',
                table_rows,
            ).replace('<div data-r="scroll"', '<div data-r="scroll" style="min-width:0"', 1)
            + "</div>"
        )
    return (
        '<div style="display:flex;flex-wrap:wrap;align-items:flex-end;'
        'justify-content:space-between;gap:16px;margin-bottom:6px"><div>'
        f'<h2 style="margin:0 0 5px;{H2_SERIF}">Condition ladder</h2>'
        '<p style="margin:0;font-size:13px;color:#5c636b;max-width:44em">One model at a time, '
        "arms in fixed semantic order. Arms without runs are stated as absent, never plotted at "
        "zero.</p></div>"
        '<label style="display:flex;flex-direction:column;gap:5px;font-size:11px;color:#5c636b" '
        'data-r="noprint">'
        '<span style="font-weight:600;letter-spacing:.08em;text-transform:uppercase">'
        "Model plotted</span>"
        f'<select data-ladder-select>{options}</select></label></div>'
        + "".join(blocks)
    )



def _cohort_label(source: dict[str, Any]) -> str:
    cohort = source.get("cohort")
    return f"cohort {cohort}" if cohort else "results directory"


def _station_sources(dataset: dict[str, Any], chain: str) -> str:
    rows = _comparisons_for(dataset, chain)
    runs = _runs_for(dataset, chain)
    if not rows:
        return ""
    sources = {
        str(source.get("profile_sha256")): source
        for source in dataset.get("report_sources") or []
    }
    body = []
    for row in rows:
        model = str(row.get("model"))
        summary = row.get("B") or row.get("C") or {}
        sha = str(summary.get("model_profile_sha256") or "")
        source = sources.get(sha) or {}
        model_runs = [r for r in runs if str(r.get("model")) == model]
        schemas = sorted({str(r.get("schema_version")) for r in model_runs})
        adapter = source.get("schema_adapter")
        body.append(
            "<tr>"
            f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
            f'font:500 12.5px/1.4 {MONO};color:#17191c;'
            'border-bottom:1px solid rgba(23,25,28,.10)">'
            f'{_text(model)}<span style="display:block;font-family:{SANS};font-size:11px;'
            f'font-weight:400;color:#5c636b">'
            f'{_text(summary.get("model_profile_id") or "—")}</span></th>'
            f'<td><span style="display:inline-flex;align-items:center;gap:7px;'
            f'font-family:{MONO};font-size:11.5px;border:1px solid rgba(23,25,28,.18);'
            f'padding:4px 7px;border-radius:2px" title="{_attr(sha)}">'
            f"{_text(_short(sha, 12))}</span></td>"
            f"<td data-num>{len(model_runs)}</td>"
            f'<td style="font-size:11.5px;color:#3d444c">result schema '
            f'{_text(", ".join(schemas) or "—")}'
            f'<span style="display:block;color:#8a5a10">'
            f'{_text(f"adapter {adapter}" if adapter else "native current schema")}</span></td>'
            f'<td style="font-family:{MONO};font-size:11px;color:#3d444c;'
            'overflow-wrap:anywhere">'
            f'{_text(_cohort_label(source))}'
            "</td></tr>"
        )
    return (
        f'<h2 style="margin:0 0 5px;{H2_SERIF}">Pinned evidence sources</h2>'
        '<p style="margin:0 0 18px;font-size:13px;color:#5c636b;max-width:52em">Every number '
        "above resolves to one of these cohorts. Historical rows are never mutated; where an "
        "older schema is read, the adapter is named.</p>"
        + _table(
            "Source cohorts for this report. Profile digests are shortened on screen; the full "
            "value is the element title.",
            '<th scope="col">Model</th><th scope="col">Profile digest</th>'
            '<th scope="col" data-num>Rows</th><th scope="col">Schema</th>'
            '<th scope="col">Cohort</th>',
            "".join(body),
        )
        + '<p style="margin:14px 0 0;font-size:12px;color:#5c636b">'
        '<a href="#/provenance" data-nav="provenance">Full evidence registry, images, toolchains '
        "and pinned identity →</a></p>"
    )


def render_overview(dataset: dict[str, Any], chain: str) -> str:
    """The overview view for one chain, as one continuous spine."""
    sections = [body for body in (
        _station_hero(dataset, chain),
        _station_evidence_status(dataset, chain),
        _station_comparison(dataset, chain),
        _station_model_comparison(dataset, chain),
        _station_task_table(dataset, chain),
        _station_efficiency_reliability(dataset, chain),
        _station_ladder(dataset, chain),
        _station_sources(dataset, chain),
    ) if body]
    last = len(sections) - 1
    return "<main>" + "".join(
        _spine(body, first=index == 0, terminal=index == last)
        for index, body in enumerate(sections)
    ) + "</main>"


# --- secondary views -------------------------------------------------------------------------


def render_models_view(dataset: dict[str, Any], chain: str) -> str:
    """Authoritative per-model table: every primary metric with its own denominators."""
    rows = _comparisons_for(dataset, chain)
    runs = _runs_for(dataset, chain)
    if not rows:
        return ""
    body = []
    for row in rows:
        model = str(row.get("model"))
        b, c = row.get("B") or {}, row.get("C") or {}
        health_b = _arm_health(runs, model, "B")
        health_c = _arm_health(runs, model, "C")
        delta_w = _num(row.get("weighted_score_delta"))
        delta_tok = _metric_delta(row, "tokens")
        delta_wall = _metric_delta(row, "wall")
        readiness, r_tone, _ = _readiness_label(row)
        eligible, eff_eligible = _headline_eligible(row), _efficiency_eligible(row)

        def tone_for(value: float | None, better_low: bool, ok: bool) -> str:
            if not ok or value is None:
                return TONE["incon"]
            good = -value if better_low else value
            return TONE["pos"] if good > 0 else TONE["neg"] if good < 0 else TONE["flat"]

        infra = health_b["infra"] + health_c["infra"]
        recorded = health_b["recorded"] + health_c["recorded"]
        body.append(
            "<tr>"
            f'<th scope="row" data-nowrap style="background:none;text-transform:none;'
            f'letter-spacing:0;font:500 13px/1.4 {MONO};color:#17191c;'
            'border-bottom:1px solid rgba(23,25,28,.10)">'
            f'{_text(model)}<span style="display:block;font-family:{SANS};font-size:11px;'
            f'font-weight:400;color:#5c636b">{_text(row.get("family") or "")}</span></th>'
            f'<td data-nowrap style="font-family:{MONO};font-size:11.5px">'
            f'{_text((b or c).get("model_profile_id") or "—")}'
            f'<span style="display:block;color:#5c636b">'
            f'{_text(_short((b or c).get("model_profile_sha256"), 8))}</span></td>'
            f'<td data-num data-nowrap>{health_b["recorded"]} / {health_c["recorded"]}</td>'
            f'<td data-num data-nowrap>{int(b.get("scored_runs") or 0)} / '
            f'{int(c.get("scored_runs") or 0)}</td>'
            f'<td data-num data-nowrap>'
            f'{_text(_metric_label("weighted", _metric_value(b, "weighted")))}</td>'
            f'<td data-num data-nowrap>'
            f'{_text(_metric_label("weighted", _metric_value(c, "weighted")))}</td>'
            f'<td data-num data-nowrap style="font-weight:600;'
            f'color:{tone_for(None if delta_w is None else delta_w * 100, False, eligible)}">'
            f'{_text(_fmt_signed(None if delta_w is None else delta_w * 100))}</td>'
            f'<td data-num data-nowrap>{int(b.get("suite_passes") or 0)} / '
            f'{int(b.get("scored_runs") or 0)}</td>'
            f'<td data-num data-nowrap>{int(c.get("suite_passes") or 0)} / '
            f'{int(c.get("scored_runs") or 0)}</td>'
            f'<td data-num data-nowrap>'
            f'{_ineligible_or(_metric_value(b, "tokens"), _fmt_int)}</td>'
            f'<td data-num data-nowrap>'
            f'{_ineligible_or(_metric_value(c, "tokens"), _fmt_int)}</td>'
            f'<td data-num data-nowrap style="font-weight:600;'
            f'color:{tone_for(delta_tok, True, eff_eligible)}">'
            f'{_text(_fmt_signed(delta_tok, 0))}</td>'
            f'<td data-num data-nowrap>'
            f'{_ineligible_or(_metric_value(b, "wall"), lambda v: _fmt1(v) + "s")}</td>'
            f'<td data-num data-nowrap>'
            f'{_ineligible_or(_metric_value(c, "wall"), lambda v: _fmt1(v) + "s")}</td>'
            f'<td data-num data-nowrap style="font-weight:600;'
            f'color:{tone_for(delta_wall, True, eff_eligible)}">'
            f'{_text("—" if delta_wall is None else _fmt_signed(delta_wall, 1) + "s")}</td>'
            f'<td data-num data-nowrap style="color:'
            f'{TONE["infra"] if infra else TONE["flat"]}">{infra} of {recorded}</td>'
            f'<td data-num>{health_b["protocol"] + health_c["protocol"]}</td>'
            f'<td data-num>{health_b["compaction"] + health_c["compaction"]}</td>'
            f'<td data-nowrap style="font-size:12px;color:{r_tone}">{_text(readiness)}</td></tr>'
        )
    head = (
        '<th scope="col">Model</th><th scope="col">Profile</th>'
        '<th scope="col" data-num>Rec. B/C</th><th scope="col" data-num>Scored B/C</th>'
        '<th scope="col" data-num>Weighted B</th><th scope="col" data-num>Weighted C</th>'
        '<th scope="col" data-num>C − B</th>'
        '<th scope="col" data-num>Suite pass B</th><th scope="col" data-num>Suite pass C</th>'
        '<th scope="col" data-num>Tokens B</th><th scope="col" data-num>Tokens C</th>'
        '<th scope="col" data-num>Δ tokens</th>'
        '<th scope="col" data-num>Time B</th><th scope="col" data-num>Time C</th>'
        '<th scope="col" data-num>Δ time</th>'
        '<th scope="col" data-num>Infra</th><th scope="col" data-num>Protocol</th>'
        '<th scope="col" data-num>Compaction</th><th scope="col">Readiness</th>'
    )
    suite = ", ".join(dataset.get("suites") or [])
    body_html = (
        f'<h1 style="margin:0 0 12px;{H1_PAGE}">Model comparison</h1>'
        f'<p style="margin:0 0 30px;{LEDE};max-width:40em">Each model keeps its own denominators, '
        "its own profile and its own readiness. There is no combined ranking here, because "
        "evidence eligibility differs between these identities — a single ordering would imply "
        "one pooled profile that does not exist.</p>"
        + _table(
            f"Authoritative model comparison. {_text(_chain_label(chain))}, suite "
            f"{_text(suite)}. B is web research only; C adds the docs-only-v1 surface.",
            head,
            "".join(body),
        )
        + '<p style="margin:16px 0 0;font-size:12.5px;color:#5c636b;max-width:56em">Δ columns are '
        "C minus B. For weighted score and suite pass, higher is better; for tokens and agent "
        f'time, lower is better. A value of <span style="font-family:{MONO}">ineligible</span> '
        "means the usage cohort for that arm was incomplete, not that the run was fast or free."
        "</p>"
    )
    return f'<main data-r="spine">{_spine_body(body_html)}</main>'


def _ineligible_or(value: float | None, formatter: Any) -> str:
    return "ineligible" if value is None else _text(formatter(value))


def _spine_body(body: str, *, pad: str = "38px 0 60px 30px") -> str:
    """Spine rail plus indented body, for views that are one continuous page."""
    return (
        '<div style="position:relative">'
        '<div style="position:absolute;right:0;top:44px;bottom:0;width:1px;'
        'background:rgba(23,25,28,.14)"></div>'
        '<div style="position:absolute;right:-3.5px;top:41px;width:7px;height:7px;'
        'background:#17191c"></div></div>'
        f'<div data-r="body" style="padding:{pad};min-width:0">{body}</div>'
    )


def render_tasks_view(dataset: dict[str, Any], chain: str) -> str:
    """The frozen suite, one article per task, with its per-arm pass counts."""
    rows = _comparisons_for(dataset, chain)
    task_ids = [
        str(t.get("task_id")) for t in ((rows[0].get("task_comparisons") if rows else []) or [])
    ]
    if not task_ids:
        task_ids = [
            str(task.get("task_id"))
            for run in dataset.get("runs", []) for task in run.get("tasks") or []
        ]
        task_ids = list(dict.fromkeys(task_ids))
    articles = []
    for task_id in task_ids:
        copy = TASK_COPY.get(task_id, {})
        passes = "   ".join(
            f'{str(row.get("model")).split("/")[-1]} '
            + " · ".join(
                f"{arm} " + _task_pass_pair(row, arm, task_id) for arm in ("B", "C")
            )
            for row in rows
        )
        articles.append(
            f'<article style="border-bottom:{_RULE};padding:22px 0">'
            '<div data-r="two" style="gap:34px"><div style="min-width:0">'
            '<div style="display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;'
            'margin-bottom:8px">'
            f'<h2 style="margin:0;font:600 17px/1.25 {SANS}">'
            f'<span style="color:#17191c">{_text(copy.get("name", task_id))}</span></h2>'
            f'<span style="font:400 11.5px/1 {MONO};color:#5c636b">{_text(task_id)}</span>'
            '<span style="font-size:11px;color:#5c636b;border:1px solid rgba(23,25,28,.18);'
            f'padding:2px 7px;border-radius:2px">{_text(copy.get("category", "—"))}</span></div>'
            '<p style="margin:0 0 10px;font-size:13.5px;line-height:1.6;color:#3d444c;'
            f'max-width:44em;text-wrap:pretty">{_text(copy.get("objective", ""))}</p>'
            '<p style="margin:0;font-size:12.5px;line-height:1.55;color:#5c636b;max-width:44em">'
            '<span style="color:#17191c;font-weight:500">Verified by</span> '
            f'{_text(copy.get("verify", ""))}</p></div>'
            f'<div style="border-left:{_RULE};padding-left:22px;min-width:0">'
            '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:10px">'
            f'<span style="font:600 30px/1 {SANS};letter-spacing:-.02em">'
            f"{_task_weight(dataset, task_id)}</span>"
            '<span style="font-size:11.5px;color:#5c636b">of 100 points</span></div>'
            f'<p style="margin:0 0 8px;font-size:11.5px;color:#5c636b">'
            f'{_text(copy.get("kind", ""))}</p>'
            f'<p style="margin:0;font:400 11px/1.6 {MONO};color:#3d444c;white-space:pre-wrap">'
            f"{_text(passes)}</p></div></div></article>"
        )
    suite = ", ".join(dataset.get("suites") or [])
    body = (
        f'<h1 style="margin:0 0 12px;{H1_PAGE}">Frozen task suite {_text(suite)}</h1>'
        f'<p style="margin:0 0 8px;{LEDE};max-width:40em">Five scored tasks, 100 points in total, '
        "unchanged since the freeze. Two of them are ordinary CKB operations, two are engineering "
        "tasks where documentation should matter most, and one is a lookup control.</p>"
        '<p style="margin:0 0 30px;font-size:13px;color:#5c636b;max-width:44em">A run passes the '
        'suite only when all five scored tasks pass, so <span style="color:#17191c;'
        'font-weight:500">Suite Pass@1 is strictly harder than a nonzero weighted score</span>.'
        "</p>"
        f'<div style="border-top:{_RULE_STRONG}">' + "".join(articles)
        + '<div style="display:flex;justify-content:space-between;align-items:baseline;'
        f'padding:18px 0;border-bottom:{_RULE_STRONG}">'
        f'<span style="font:600 14px/1 {SANS}">Total available</span>'
        f'<span style="font:600 22px/1 {SANS}">100 points</span></div></div>'
    )
    return f'<main data-r="spine">{_spine_body(body)}</main>'


def _task_pass_pair(row: dict[str, Any], arm: str, task_id: str) -> str:
    comparison = next(
        (t for t in row.get("task_comparisons") or [] if str(t.get("task_id")) == task_id), {}
    )
    side = comparison.get(arm) or {}
    runs = int(side.get("runs") or 0)
    return f'{int(side.get("passes") or 0)}/{runs}' if runs else "—"


def render_runs_view(dataset: dict[str, Any], chain: str) -> str:
    """Every retained row, including the ones excluded from correctness means."""
    runs = _runs_for(dataset, chain)
    profiles = {
        str(r.get("run_id")): str(r.get("model_profile_id") or "—") for r in runs
    }
    if not runs:
        empty = _callout(
            "No rows match",
            '<p style="margin:0;font-size:13.5px;color:#3d444c">No benchmark row has been '
            f"recorded against {_text(_chain_label(chain))}. Row counts stay accurate — nothing "
            "is hidden, the intersection is genuinely empty.</p>",
            width="56em",
        )
        table = empty
    else:
        body = []
        for run in runs:
            style = _outcome_style(run.get("outcome"))
            metrics = run.get("metrics") or {}
            score = _run_score(run)
            tasks = run.get("tasks") or []
            passed = sum(1 for t in tasks if t.get("passed"))
            complete = str(metrics.get("token_usage_status")) == "complete"
            wall = _num(metrics.get("total_wall_seconds"))
            body.append(
                f'<tr data-outcome="{_attr(run.get("outcome"))}" '
                f'data-model="{_attr(run.get("model"))}" data-arm="{_attr(run.get("arm"))}" '
                f'data-seed="{_attr(run.get("seed"))}" '
                f'data-sort-ts="{_attr(run.get("epoch") or 0)}" '
                f'data-sort-score="{_attr(-1 if score is None else score)}" '
                f'data-sort-tokens="{_attr(_num(metrics.get("total_tokens")) or -1)}" '
                f'data-sort-wall="{_attr(wall if wall is not None else -1)}">'
                + _row_header(
                    f'{_text(run.get("run_id"))}'
                    f'<span style="display:block;font-family:{SANS};font-size:11px;'
                    f'color:#5c636b">{_text(_epoch_label(run))}</span>',
                    mono=True, size="11px",
                )
                + f'<td data-r="hidesm" data-nowrap style="font-family:{MONO};font-size:11.5px">'
                f'{_text(run.get("model"))}</td>'
                f'<td data-r="hidesm" data-arm="{_attr(run.get("arm"))}" data-nowrap '
                f'style="font-weight:600;font-size:12.5px">{_text(run.get("arm"))}</td>'
                f'<td data-r="hidesm" data-num>{_text(run.get("seed"))}</td>'
                f'<td data-nowrap style="color:{style["tone"]};font-size:12.5px">'
                f'<span aria-hidden="true" style="font-size:10px;margin-right:5px">'
                f'{style["glyph"]}</span>{_text(style["label"])}</td>'
                f'<td data-num data-nowrap style="color:'
                f'{TONE["ink"] if score is not None else TONE["infra"]};font-weight:500">'
                f"{_score_cell(score, run)}"
                "</td>"
                f'<td data-r="hidesm" data-num data-nowrap>'
                f'{f"{passed} / {len(tasks)}" if tasks else "—"}</td>'
                f'<td data-r="hidesm" data-num>{_text(metrics.get("model_calls", "—"))}</td>'
                f'<td data-r="hidesm" data-num data-nowrap>'
                f'{_text(metrics.get("provider_attempts", "—"))} / '
                f'{_text(metrics.get("provider_responses", "—"))}</td>'
                f'<td data-r="hidesm" data-num data-nowrap style="color:'
                f'{TONE["ink"] if complete else TONE["incon"]}">'
                f'{_fmt_int(metrics.get("total_tokens")) if complete else "incomplete"}</td>'
                f'<td data-r="hidesm" data-num data-nowrap>'
                f'{_fmt1(wall) + "s" if wall is not None else "—"}</td>'
                f'<td data-r="hidesm" data-num>'
                f'{_text(metrics.get("provider_retry_count", 0))}</td>'
                f'<td data-r="hidesm" data-nowrap style="font-family:{MONO};font-size:11px;'
                f'color:#3d444c">{_text(profiles.get(str(run.get("run_id")), "—"))}</td></tr>'
            )
        head = (
            '<th scope="col">Run</th><th scope="col" data-r="hidesm">Model</th>'
            '<th scope="col" data-r="hidesm">Arm</th>'
            '<th scope="col" data-num data-r="hidesm">Seed</th>'
            '<th scope="col">Outcome</th><th scope="col" data-num>Score</th>'
            '<th scope="col" data-num data-r="hidesm">Tasks</th>'
            '<th scope="col" data-num data-r="hidesm">Calls</th>'
            '<th scope="col" data-num data-r="hidesm">Att / resp</th>'
            '<th scope="col" data-num data-r="hidesm">Tokens</th>'
            '<th scope="col" data-num data-r="hidesm">Agent time</th>'
            '<th scope="col" data-num data-r="hidesm">Retries</th>'
            '<th scope="col" data-r="hidesm">Profile</th>'
        )
        table = _table(
            'Retained benchmark rows. A score of <span style="font-family:'
            f'{MONO}">not scored</span> means excluded from correctness means, not a zero.',
            head,
            "".join(body),
        )
    filters = _run_filters(runs)
    body_html = (
        f'<h1 style="margin:0 0 12px;{H1_PAGE}">Run explorer</h1>'
        f'<p style="margin:0 0 26px;{LEDE};max-width:40em">Every retained evidence row, including '
        "the ones excluded from correctness means. Filters change what is listed, never what a "
        "number means.</p>"
        + filters + table
    )
    return f'<main data-r="spine">{_spine_body(body_html)}</main>'


def _score_cell(score: float | None, run: dict[str, Any]) -> str:
    if score is None:
        return "not scored"
    return f"{_fmt_int(score)} / {_fmt_int(run.get('max_score'))}"


def _epoch_label(run: dict[str, Any]) -> str:
    """Render a row's canonical start time. Derived from the run ID, never from a rebuild clock."""
    epoch = run.get("epoch")
    if not epoch:
        return "no canonical start time"
    try:
        stamp = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "no canonical start time"
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


def _run_filters(runs: list[dict[str, Any]]) -> str:
    def select(name: str, label: str, values: list[str], display: Any = str) -> str:
        options = "".join(
            f'<option value="{_attr(value)}">{_text(display(value))}</option>'
            for value in values
        )
        return (
            '<label style="display:flex;flex-direction:column;gap:5px;font-size:11px;'
            'color:#5c636b">'
            '<span style="font-weight:600;letter-spacing:.08em;text-transform:uppercase">'
            f"{_text(label)}</span>"
            f'<select data-run-filter="{name}">'
            f'<option value="all">All {_text(label.lower())}s</option>{options}</select></label>'
        )

    models = sorted({str(r.get("model")) for r in runs})
    arms = [arm for arm in ARMS if any(str(r.get("arm")) == arm for r in runs)]
    seeds = sorted({str(r.get("seed")) for r in runs}, key=lambda s: (len(s), s))
    outcomes = sorted({str(r.get("outcome")) for r in runs})
    return (
        '<div data-r="noprint" style="display:flex;flex-wrap:wrap;gap:14px 18px;'
        f'align-items:flex-end;padding:16px 0;border-top:{_RULE};border-bottom:{_RULE}">'
        + select("model", "Model", models)
        + select("arm", "Arm", arms, lambda a: ARM_LABELS[a])
        + select("seed", "Seed", seeds, lambda s: f"seed {s}")
        + select("outcome", "Outcome", outcomes, lambda o: _outcome_style(o)["label"])
        + '<button type="button" data-run-clear style="border:1px solid rgba(23,25,28,.22);'
        'padding:0 13px;min-height:40px;border-radius:2px;font-size:12px">Clear filters</button>'
        '<p aria-live="polite" data-run-count style="margin:0 0 0 auto;font-size:12.5px;'
        f'color:#3d444c;font-weight:500">{len(runs)} of {len(runs)} retained rows</p></div>'
        '<div data-r="noprint" style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;'
        'margin:16px 0 12px;font-size:12px;color:#5c636b">'
        '<span style="font-weight:600;letter-spacing:.06em;text-transform:uppercase;'
        'font-size:10.5px">Sort by</span>'
        + "".join(
            f'<button type="button" data-run-sort="{key}" '
            'style="border:1px solid rgba(23,25,28,.22);padding:7px 11px;min-height:40px;'
            f'border-radius:2px;font-size:12px">{_text(label)}</button>'
            for key, label in (
                ("ts", "Timestamp"), ("score", "Score"), ("tokens", "Tokens"),
                ("wall", "Agent time"),
            )
        )
        + "</div>"
    )


def render_methodology_view(dataset: dict[str, Any]) -> str:
    """What the benchmark can claim, and what it cannot."""
    env = dataset.get("environment") or {}
    ladder_rows = "".join(
        "<tr>"
        f'<th scope="row" data-arm="{arm}" style="background:none;text-transform:none;'
        f'letter-spacing:0;font:600 15px/1.2 {SANS};color:#17191c;'
        'border-bottom:1px solid rgba(23,25,28,.10);width:64px">' + arm + "</th>"
        f'<td data-nowrap style="font-family:{MONO};font-size:12px">'
        f'{"Off" if arm in ("A", "B") else "docs-only-v1"}</td>'
        '<td data-nowrap style="font-size:12.5px">'
        f'{"Prohibited by prompt" if arm in ("A", "D") else "Allowed"}</td>'
        f'<td style="font-size:13px;color:#3d444c;max-width:36em">'
        f'{_text(ARM_META[arm]["meaning"])}</td></tr>'
        for arm in ARMS
    )
    step = env.get("step_limit") or "the pinned"
    wall = env.get("wall_time_limit_seconds") or "the pinned"
    minimum = 3
    for row in dataset.get("phase_one_comparisons", []):
        minimum = _readiness(row).get("minimum_scored_runs_per_arm", 3)
        break
    items = (
        ("Why phase one is DevNet-only",
         "Every cell needs a fresh, disposable chain state whose tip, balances and deployed cells "
         "are bound to that one run. A public TestNet cannot be reset per cell, and shared state "
         "would let one run contaminate another. TestNet evidence will require its own cohort; "
         "DevNet numbers are never carried across."),
        ("How B and C receive the same budgets",
         f"Both arms run under the same pinned model profile with a {step}-step limit and a "
         f"{wall}-second wall-time limit. The only intended difference is the MCP surface: off "
         "for B, docs-only-v1 for C."),
        ("Why seeds are matched across arms",
         "The seed determines per-run generated material — recipient addresses, capacity amounts, "
         "cell payloads, preimages. Comparing arm C on one seed against arm B on another would "
         "compare different problems, so a difference is only promoted when the scored seed "
         "multisets are identical."),
        ("How isolation works",
         "The agent container and the verifier container are separate. The agent never reaches "
         "the verifier; the verifier never loads CKB AI. Grading uses direct CKB RPC against the "
         "run-bound DevNet, so the surface being measured cannot grade itself."),
        ("What docs-only-v1 permits and rejects",
         "It exposes curated CKB documentation retrieval only. Chain tools, signing, faucet "
         "access, deployment and transaction submission are outside the measured phase-one "
         "treatment — an agent in arm C still builds and submits everything itself."),
        ("How scoring and Suite Pass@1 work",
         "Each of the five tasks carries a fixed weight summing to 100. Weighted score is points "
         "earned over 100. Suite Pass@1 counts a run only when every scored task passes, which is "
         "why a run can carry most of the points and still fail the suite."),
        ("Why infrastructure failures are excluded from means but still published",
         "A provider or harness fault says nothing about the agent's CKB ability, so scoring it "
         "as zero would fabricate a correctness signal. It stays in recorded counts, in "
         "reliability tables and in the run explorer, and its exclusion is what makes a "
         "comparison completion-conditioned."),
        ("Why incomplete token usage is excluded from efficiency",
         "If any provider attempt returned no valid usage block, the row's token total is unknown "
         "rather than zero. Correctness may still be valid when the run was eventually graded, "
         "but the row cannot enter a token or time mean."),
        ("How retries are bounded and recorded",
         "Retries follow a bounded backoff against an allowlist of transient provider failures. "
         "Every attempt, response and retry is recorded per row, and the failure category is "
         "published."),
        ("What is pinned",
         "Model profiles, suite freeze hash, agent and verifier images, and MCP server version "
         "are all pinned and published. The pinned identity for this report is listed in the "
         "evidence registry."),
        ("Known limitations",
         "Phase one measures one documentation surface, on DevNet, over five tasks, with "
         "single-digit run counts. It cannot support claims about production chains, other CKB "
         "tooling, other task families, or statistical significance. A headline-eligible "
         "difference is descriptive only."),
    )
    details = "".join(
        f'<details style="border-bottom:{_RULE}">'
        '<summary style="display:flex;align-items:baseline;gap:12px;padding:14px 0;'
        f'font:500 14.5px/1.4 {SANS};color:#17191c">'
        f'<span aria-hidden="true" style="font-family:{MONO};font-size:12px;color:#8a5a10">+'
        f"</span><span>{_text(question)}</span></summary>"
        '<p style="margin:0 0 18px 24px;font-size:13.5px;line-height:1.7;color:#3d444c;'
        f'max-width:44em;text-wrap:pretty">{_text(answer)}</p></details>'
        for question, answer in items
    )
    head = (
        f'<h1 style="margin:0 0 12px;{H1_PAGE}">Methodology</h1>'
        f'<p style="margin:0 0 30px;{LEDE};max-width:38em">What this benchmark can claim, and '
        "what it cannot. The condition ladder comes first because every number on this site is "
        "anchored to one of its four arms.</p>"
        + _table(
            "The condition ladder. Arm C against arm B is the phase-one headline comparison.",
            '<th scope="col">Arm</th><th scope="col">CKB AI MCP</th>'
            '<th scope="col">Ordinary web research</th><th scope="col">Meaning</th>',
            ladder_rows,
        )
    )
    rule = _callout(
        "The headline eligibility rule",
        '<p style="margin:0 0 14px;font-size:13.5px;line-height:1.6;color:#3d444c">A B/C '
        "difference may be promoted to a headline only when all of these hold:</p>"
        '<ol style="margin:0 0 14px;padding-left:22px;font-size:13.5px;line-height:1.75;'
        'color:#17191c">'
        f"<li>at least {minimum} scored runs per arm;</li>"
        "<li>equal scored counts in both arms;</li>"
        "<li>matching scored seed sets;</li>"
        "<li>every recorded row in both arms scored.</li></ol>"
        '<p style="margin:0 0 14px;font-size:13.5px;line-height:1.6;color:#3d444c">Token and time '
        "differences additionally require complete usage on every matched scored row. Anything "
        'short of this is published as <span style="font-weight:600;color:#8a5a10">Inconclusive'
        "</span> with its arithmetic visible as provisional detail.</p>"
        '<p style="margin:0;font-size:13px;line-height:1.6;color:#5c636b">Meeting the floor '
        "permits a descriptive headline. It is not a claim of statistical power, and it is not a "
        "claim of universal causality.</p>",
        width="58em",
    )
    controls = (
        f'<h2 style="margin:0 0 18px;{H2_SMALL}">Controls, scoring and limits</h2>'
        f'<div style="border-top:{_RULE_STRONG};max-width:60em">{details}</div>'
    )
    return (
        "<main>"
        + _spine(head, first=True)
        + _spine(rule)
        + _spine(controls, terminal=True)
        + "</main>"
    )


def render_provenance_view(dataset: dict[str, Any], chain: str) -> str:
    """One entry per pinned evidence source, plus the report's own pinned identity."""
    env = dataset.get("environment") or {}
    runs = _runs_for(dataset, chain)
    sources = {
        str(source.get("profile_sha256")): source
        for source in dataset.get("report_sources") or []
    }
    articles = []
    for model in sorted({str(r.get("model")) for r in runs}):
        model_runs = [r for r in runs if str(r.get("model")) == model]
        first = model_runs[0]
        sha = str(first.get("model_profile_sha256") or "")
        source = sources.get(sha) or {}
        returned = sorted({str(r.get("model_response_id")) for r in model_runs})
        stability = str(source.get("model_stability") or "unknown")
        stability_label = {
            "dated_snapshot": "dated snapshot",
            "moving_alias": "moving alias",
            "unknown": "stability unknown",
        }.get(stability, "stability unknown")
        stability_tone = TONE["pos"] if stability == "dated_snapshot" else TONE["incon"]
        surfaces = {str(r.get("mcp_surface_profile") or "unknown") for r in model_runs}
        surface_order = [
            surface for surface in ("off", "docs-only-v1") if surface in surfaces
        ]
        surface_order.extend(sorted(surfaces - set(surface_order)))
        entries = [
            ("Profile", str(first.get("model_profile_id") or "—")),
            ("Profile digest", sha or "—"),
            ("Model returned", ", ".join(returned) or "—"),
            ("Source cohort", str(source.get("cohort") or "results directory")),
            ("Result schema",
             ", ".join(sorted({str(r.get("schema_version")) for r in model_runs}))
             + " · " + str(source.get("schema_adapter") or "native current schema")),
            ("Suite", ", ".join(sorted({str(r.get("suite_semver")) for r in model_runs}))
             + " · freeze " + _short(env.get("suite_freeze_hash"), 12)),
            ("MCP", str(env.get("mcp_server_version") or "—") + " · "
             + " / ".join(surface_order)),
            ("Chain", str(env.get("chain_id") or "—") + " · "
             + str(env.get("lifecycle_policy") or "—")),
            ("Limits", f'{env.get("step_limit", "—")} steps · '
             f'{env.get("wall_time_limit_seconds", "—")}s wall'),
        ]
        rows = "".join(
            f'<dt style="color:#5c636b">{_text(k)}</dt>'
            f'<dd style="margin:0;font-family:{MONO};font-size:11.5px;overflow-wrap:anywhere">'
            f"{_text(v)}</dd>"
            for k, v in entries
        )
        articles.append(
            f'<article style="border-bottom:{_RULE};padding:22px 0">'
            '<div style="display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;'
            'margin-bottom:14px">'
            f'<h2 style="margin:0;font:500 17px/1.2 {MONO}">{_text(model)}</h2>'
            f'<span style="font-size:11px;color:'
            f'{stability_tone};'
            'border:1px solid rgba(23,25,28,.18);padding:2px 7px;border-radius:2px">'
            f'{_text(stability_label)}</span>'
            '<span style="margin-left:auto;font-size:12px;color:#3d444c">'
            f"{len(model_runs)} retained rows</span></div>"
            '<dl style="margin:0;display:grid;'
            'grid-template-columns:minmax(140px,auto) minmax(0,1fr);gap:8px 20px;'
            f'font-size:12.5px">{rows}</dl></article>'
        )
    identity = [
        ("Suite freeze hash", env.get("suite_freeze_hash")),
        ("DevNet genesis", env.get("genesis_hash")),
        ("DevNet config digest", env.get("devnet_config_sha256")),
        ("Chain lifecycle", env.get("lifecycle_policy")),
        ("MCP server version", env.get("mcp_server_version")),
        ("Result schema", env.get("schema_version")),
        ("Results through", str(dataset.get("generated_at", ""))),
    ]
    identity_rows = "".join(
        '<div style="display:flex;flex-wrap:wrap;align-items:center;'
        'justify-content:space-between;gap:14px;padding:11px 0;'
        'border-bottom:1px solid rgba(23,25,28,.10)">'
        f'<dt style="font-size:13px;color:#3d444c">{_text(k)}</dt>'
        f'<dd style="margin:0;font:400 11px/1 {MONO};border:1px solid rgba(23,25,28,.18);'
        f'padding:6px 9px;border-radius:2px" title="{_attr(v or "")}">'
        f"{_text(_short(v, 20) if v else '—')}</dd></div>"
        for k, v in identity
    )
    head = (
        f'<h1 style="margin:0 0 12px;{H1_PAGE}">Evidence registry</h1>'
        f'<p style="margin:0 0 26px;{LEDE};max-width:38em">One entry per pinned evidence source. '
        "Every visible number on this site resolves to one of these cohorts, and nothing here was "
        "mutated to make a comparison look cleaner.</p>"
        f'<div style="display:flex;flex-direction:column;gap:0;border-top:{_RULE_STRONG}">'
        + "".join(articles) + "</div>"
    )
    tail = (
        f'<h2 style="margin:0 0 6px;{H2_SMALL}">Report identity</h2>'
        '<p style="margin:0 0 18px;font-size:13px;color:#5c636b;max-width:46em">Long identifiers '
        "are shortened on screen; the full value is the element title.</p>"
        f'<dl style="margin:0;border-top:{_RULE_STRONG};max-width:60em">{identity_rows}</dl>'
        '<p style="margin:18px 0 0;font-size:12.5px;color:#5c636b;max-width:52em">Report vintage '
        "comes from the newest canonical run ID, not from the wall clock at rebuild time. "
        f"{len(runs)} retained rows passed validation before this report was written.</p>"
    )
    return "<main>" + _spine(head, first=True) + _spine(tail, terminal=True) + "</main>"


# --- view switching --------------------------------------------------------------------------

SCRIPT = """
document.documentElement.classList.add('js');
(function () {
  var views = Array.prototype.slice.call(document.querySelectorAll('[data-view]'));
  var navs = Array.prototype.slice.call(document.querySelectorAll('[data-nav]'));

  function show(route) {
    var known = views.some(function (v) { return v.getAttribute('data-view') === route; });
    if (!known) { route = 'overview'; }
    views.forEach(function (v) {
      v.classList.toggle('is-active', v.getAttribute('data-view') === route);
    });
    navs.forEach(function (a) {
      var on = a.getAttribute('data-nav') === route;
      a.setAttribute('data-active', on ? '1' : '0');
      if (on) { a.setAttribute('aria-current', 'page'); } else { a.removeAttribute('aria-current'); }
    });
    return route;
  }

  function fromHash() {
    var raw = (window.location.hash || '').replace(/^#\\/?/, '').split('?')[0];
    return show(raw.split('/')[0] || 'overview');
  }

  navs.forEach(function (a) {
    a.addEventListener('click', function () { setTimeout(fromHash, 0); });
  });
  window.addEventListener('hashchange', fromHash);
  fromHash();

  function group(attr, cls, value) {
    Array.prototype.forEach.call(document.querySelectorAll('[' + attr + ']'), function (el) {
      el.classList.toggle(cls, el.getAttribute(attr) === value);
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-chain-set]'), function (btn) {
    btn.addEventListener('click', function () {
      var chain = btn.getAttribute('data-chain-set');
      Array.prototype.forEach.call(document.querySelectorAll('[data-chain-set]'), function (b) {
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
      });
      group('data-chain', 'chain-on', chain);
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll('[data-metric-set]'), function (btn) {
    btn.addEventListener('click', function () {
      var metric = btn.getAttribute('data-metric-set');
      Array.prototype.forEach.call(document.querySelectorAll('[data-metric-set]'), function (b) {
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
      });
      Array.prototype.forEach.call(document.querySelectorAll('[data-metric]'), function (el) {
        el.style.display = el.getAttribute('data-metric') === metric ? '' : 'none';
      });
    });
  });

  var firstMetric = document.querySelector('[data-metric-set]');
  if (firstMetric) {
    Array.prototype.forEach.call(document.querySelectorAll('[data-metric]'), function (el) {
      el.style.display =
        el.getAttribute('data-metric') === firstMetric.getAttribute('data-metric-set')
          ? '' : 'none';
    });
  }

  Array.prototype.forEach.call(
    document.querySelectorAll('[data-ladder-select]'),
    function (select) {
      var scope = select.closest('[data-chain]') || document;
      function pick() {
        Array.prototype.forEach.call(scope.querySelectorAll('[data-ladder]'), function (el) {
          el.classList.toggle('ladder-on', el.getAttribute('data-ladder') === select.value);
        });
      }
      select.addEventListener('change', pick);
      pick();
    }
  );

  var table = document.querySelector('[data-run-filter]') &&
    document.querySelector('[data-run-filter]').closest('[data-view]');
  if (!table) { return; }
  var rows = Array.prototype.slice.call(table.querySelectorAll('tbody tr[data-outcome]'));
  var counter = table.querySelector('[data-run-count]');
  var selects = Array.prototype.slice.call(table.querySelectorAll('[data-run-filter]'));
  var sortState = { key: 'ts', dir: -1 };

  function apply() {
    var shown = 0;
    rows.forEach(function (row) {
      var ok = selects.every(function (sel) {
        var want = sel.value;
        return want === 'all' || row.getAttribute('data-' + sel.getAttribute('data-run-filter'))
          === want;
      });
      row.style.display = ok ? '' : 'none';
      if (ok) { shown += 1; }
    });
    if (counter) { counter.textContent = shown + ' of ' + rows.length + ' retained rows'; }
  }

  function sortBy(key) {
    sortState.dir = sortState.key === key ? -sortState.dir : -1;
    sortState.key = key;
    var body = rows.length ? rows[0].parentNode : null;
    if (!body) { return; }
    rows.slice()
      .sort(function (a, b) {
        var x = parseFloat(a.getAttribute('data-sort-' + key)) || 0;
        var y = parseFloat(b.getAttribute('data-sort-' + key)) || 0;
        return (x - y) * sortState.dir;
      })
      .forEach(function (row) { body.appendChild(row); });
    Array.prototype.forEach.call(table.querySelectorAll('[data-run-sort]'), function (btn) {
      var on = btn.getAttribute('data-run-sort') === key;
      btn.setAttribute('aria-sort', on ? (sortState.dir > 0 ? 'ascending' : 'descending') : 'none');
    });
  }

  selects.forEach(function (sel) { sel.addEventListener('change', apply); });
  Array.prototype.forEach.call(table.querySelectorAll('[data-run-sort]'), function (btn) {
    btn.addEventListener('click', function () { sortBy(btn.getAttribute('data-run-sort')); });
  });
  var clear = table.querySelector('[data-run-clear]');
  if (clear) {
    clear.addEventListener('click', function () {
      selects.forEach(function (sel) { sel.value = 'all'; });
      apply();
    });
  }
  apply();
}());
"""


def render_ladder_html(dataset: dict[str, Any]) -> str:
    """Render the complete self-contained evidence report."""
    warning = ""
    if dataset.get("_SYNTHETIC"):
        warning = (
            '<div role="alert" style="background:#9a3324;color:#fdfcfa;padding:12px 34px;'
            f'font:600 12.5px/1.4 {SANS};letter-spacing:.02em">'
            f'{_text(dataset.get("_WARNING", "SYNTHETIC DATA"))}</div>'
        )
    chains = list(dataset.get("chains") or [])
    primary = next((c for c in chains if _chain_has_data(dataset, c)), chains[0] if chains else "")

    def per_chain(builder: Any) -> str:
        return "".join(
            f'<div data-chain="{_attr(chain)}"{_on(chain == primary, "chain-on")}>'
            f"{builder(dataset, chain)}</div>"
            for chain in chains
        )

    views = {
        "overview": per_chain(render_overview),
        "models": per_chain(_view_or_empty(render_models_view, "Model comparison")),
        "tasks": per_chain(render_tasks_view),
        "runs": per_chain(render_runs_view),
        "methodology": render_methodology_view(dataset),
        "provenance": per_chain(render_provenance_view),
    }
    body = "".join(
        f'<div data-view="{route}"{_on(route == "overview", "is-active")}>'
        f"{views[route]}</div>"
        for route, _ in NAV
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        "<title>CKB AI Bench — evidence report</title>"
        f"<style>{STYLE}</style></head><body>"
        f'{warning}<div style="min-height:100vh;background:#f6f4ef">'
        + render_header(dataset)
        + '<div data-r="pad" style="max-width:1320px;margin:0 auto;padding:0 34px">'
        + render_meta_strip(dataset)
        + body
        + render_footer(dataset)
        + "</div></div>"
        f"<script>{SCRIPT}</script></body></html>\n"
    )


def _view_or_empty(builder: Any, title: str) -> Any:
    def build(dataset: dict[str, Any], chain: str) -> str:
        rendered = builder(dataset, chain)
        if rendered:
            return rendered
        body = (
            f'<h1 style="margin:0 0 12px;{H1_PAGE}">{_text(title)}</h1>'
            + _callout(
                f"No {_chain_label(chain)} runs yet",
                '<p style="margin:0;font-size:13.5px;color:#3d444c">No benchmark row has been '
                f"recorded against {_text(_chain_label(chain))}, so there is nothing to compare "
                "here. DevNet evidence is never copied across the chain boundary.</p>",
                width="56em",
            )
        )
        return f'<main data-r="spine">{_spine_body(body)}</main>'

    return build


def write_site(output_dir: Path | str, dataset: dict[str, Any]) -> Path:
    """Write the report to ``output_dir/index.html`` and return that path."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    index = directory / "index.html"
    index.write_text(render_ladder_html(dataset), encoding="utf-8")
    return index
