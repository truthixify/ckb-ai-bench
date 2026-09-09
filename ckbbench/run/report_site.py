"""Established visual language for accepted campaign reports."""

from __future__ import annotations

import html
import math
from decimal import Decimal
from typing import Any

from ckbbench.matrix.render import (
    DOT_MARK,
    H2_SMALL,
    LEDE,
    MONO,
    SANS,
    SERIF,
    STYLE,
    TASK_COPY,
)


PAGE_HEADING = f"font-family:{SERIF};font-weight:500;font-size:38px;line-height:1.1"
EYEBROW_STYLE = f"font:600 10.5px/1 {SANS};text-transform:uppercase;color:var(--muted)"


_NAV = (
    ("models", "Models"),
    ("tasks", "Tasks"),
    ("runs", "Runs"),
    ("methodology", "Methodology"),
    ("provenance", "Provenance"),
)

_TRACK_ORDER = {"all": 0, "testnet": 1, "local-hermetic": 2}
_ARM_LABELS = {"B": "Web only", "C": "CKB AI plus web"}
_OUTCOME_LABELS = {
    "pass": "Pass",
    "agent_fail": "Not a full pass",
    "infra_fail": "Infrastructure failure",
    "protocol_violation": "Protocol violation",
}

_EXTRA_STYLE = """
.js [data-report-view]{display:none}
.js [data-report-view].is-active{display:block}
.js [data-track-panel]{display:none}
.js [data-track-panel].track-on{display:block}
[data-detail][hidden]{display:none!important}
[data-score-track]{position:relative;height:9px;background:var(--track);border:1px solid rgba(var(--ink-rgb),.12)}
[data-score-fill]{position:absolute;inset:0 auto 0 0;background:var(--accent);border-right:2px solid var(--accent-dark)}
[data-arm="B"] [data-score-fill]{background:repeating-linear-gradient(135deg,var(--arm-b-fill),var(--arm-b-fill) 2px,transparent 2px,transparent 5px);border-right-color:var(--arm-b-line)}
[data-status-card]{border-top:1px solid rgba(var(--ink-rgb),.32);padding:17px 0}
[data-status-card]+[data-status-card]{border-top:1px solid rgba(var(--ink-rgb),.14)}
[data-pill]{display:inline-flex;align-items:center;min-height:24px;padding:2px 8px;border:1px solid rgba(var(--ink-rgb),.20);border-radius:2px;font-size:11px;color:var(--muted)}
[data-condition-panel]{border-top:1px solid rgba(var(--ink-rgb),.32);margin-top:14px;padding-top:12px}
.js [data-condition-panel]{display:none}
.js [data-condition-panel].ladder-on{display:block}
[data-methodology-details] [data-details-glyph]::before{content:'+'}
[data-methodology-details][open] [data-details-glyph]::before{content:'−'}
[data-methodology-details]>summary{cursor:pointer;list-style:none}
[data-methodology-details]>summary::-webkit-details-marker{display:none}
.visually-hidden{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
[data-hero-tooltip]{transition:opacity .12s ease,transform .12s ease,visibility 0s linear 0s}
[data-outcome="pass"]{color:var(--pos)}
[data-outcome="agent_fail"],[data-outcome="protocol_violation"]{color:var(--neg)}
[data-outcome="infra_fail"]{color:var(--infra)}
@media(max-width:920px){[data-r="campaign-lb"]{grid-template-columns:minmax(108px,160px) minmax(240px,1fr) 72px 88px!important}}
@media(max-width:760px){[data-r="campaign-lb"]{display:none!important}[data-r="campaign-leader-row"]{display:grid!important;grid-template-columns:minmax(110px,.7fr) minmax(220px,1.5fr)!important}[data-r="campaign-leader-meta"]{display:none!important}}
@media(max-width:620px){
[data-report-header]{flex-wrap:wrap;gap:0!important;padding-top:8px!important}
[data-report-brand]{order:1;min-height:42px}
[data-theme-toggle]{order:2;margin-left:auto}
[data-report-nav]{order:3;flex:1 0 100%!important;width:100%;max-width:100%;min-height:40px;contain:inline-size}
[data-track-selector]{display:grid!important;grid-template-columns:minmax(0,1fr);gap:10px!important}
[data-track-group]{justify-self:start}
[data-track-meta]{gap:6px 16px!important}
[data-report-title]{font-size:42px!important}
[data-hero-cue]{display:none}
[data-hero-tooltip]{position:fixed!important;left:16px!important;right:16px!important;top:auto!important;bottom:16px!important;width:auto!important;max-width:none!important}
[data-r="campaign-leader-row"]{display:block!important}
[data-r="campaign-leader-bars"]{margin-top:12px}
[data-r="campaign-comparison-lane"]{grid-template-columns:90px minmax(80px,1fr) 64px!important;gap:8px!important}
}
@media print{[data-report-view]{display:block!important}[data-track-panel]{display:none!important}[data-track-panel].track-on{display:block!important}[data-run-row][hidden]{display:table-row!important}[data-detail][hidden]{display:block!important}}
*{letter-spacing:0!important}
"""


_SCRIPT = r"""
document.documentElement.classList.add('js');
(function () {
  var THEME_KEY = 'ckbbench.theme';
  function paintTheme(theme) {
    document.body.setAttribute('data-theme', theme);
    var dark = theme === 'dark';
    document.querySelectorAll('[data-theme-glyph]').forEach(function (node) {
      node.textContent = dark ? '\u25D1' : '\u25D0';
    });
    document.querySelectorAll('[data-theme-label]').forEach(function (node) {
      node.textContent = dark ? 'Dark' : 'Light';
    });
    document.querySelectorAll('[data-theme-toggle]').forEach(function (node) {
      node.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
    });
  }
  var initialTheme = null;
  try { initialTheme = localStorage.getItem(THEME_KEY); } catch (error) {}
  if (initialTheme !== 'dark' && initialTheme !== 'light') {
    initialTheme = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark' : 'light';
  }
  paintTheme(initialTheme === 'dark' ? 'dark' : 'light');
  document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
    button.addEventListener('click', function () {
      var next = document.body.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      paintTheme(next);
      try { localStorage.setItem(THEME_KEY, next); } catch (error) {}
    });
  });

  var runFilters = Array.prototype.slice.call(document.querySelectorAll('[data-run-filter]'));
  var runRows = Array.prototype.slice.call(document.querySelectorAll('[data-run-row]'));
  var runCount = document.querySelector('[data-run-count]');
  var activeTrack = document.querySelector('[data-track-set][aria-pressed="true"]');
  activeTrack = activeTrack ? activeTrack.getAttribute('data-track-set') : '';

  function applyRunFilters() {
    var shown = 0;
    runRows.forEach(function (row) {
      var matchesTrack = !activeTrack || activeTrack === 'all' || row.getAttribute('data-track') === activeTrack;
      var matchesFilters = runFilters.every(function (select) {
        var value = select.value;
        return value === 'all' || row.getAttribute('data-' + select.getAttribute('data-run-filter')) === value;
      });
      row.hidden = !(matchesTrack && matchesFilters);
      if (!row.hidden) { shown += 1; }
    });
    if (runCount) { runCount.textContent = shown + ' of ' + runRows.length + ' task attempts'; }
  }

  function showTrack(track) {
    if (!track) { return; }
    activeTrack = track;
    document.querySelectorAll('[data-track-set]').forEach(function (button) {
      button.setAttribute('aria-pressed', button.getAttribute('data-track-set') === track ? 'true' : 'false');
    });
    document.querySelectorAll('[data-track-panel]').forEach(function (panel) {
      panel.classList.toggle('track-on', panel.getAttribute('data-track-panel') === track);
    });
    applyRunFilters();
  }
  document.querySelectorAll('[data-track-set]').forEach(function (button) {
    button.addEventListener('click', function () { showTrack(button.getAttribute('data-track-set')); });
  });
  if (activeTrack) { showTrack(activeTrack); }

  var views = Array.prototype.slice.call(document.querySelectorAll('[data-report-view]'));
  var nav = Array.prototype.slice.call(document.querySelectorAll('[data-nav]'));
  var detailViews = { models: 'model', tasks: 'task', runs: 'run' };

  function showView(route, navRoute) {
    if (!views.some(function (view) { return view.getAttribute('data-report-view') === route; })) {
      route = 'overview';
    }
    views.forEach(function (view) {
      view.classList.toggle('is-active', view.getAttribute('data-report-view') === route);
    });
    nav.forEach(function (link) {
      var active = link.getAttribute('data-nav') === (navRoute || route);
      link.setAttribute('data-active', active ? '1' : '0');
      if (active) { link.setAttribute('aria-current', 'page'); }
      else { link.removeAttribute('aria-current'); }
    });
  }

  function routeFromHash() {
    var raw = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
    var parts = raw.split('/').filter(Boolean);
    var head = parts[0] || 'overview';
    var id = parts.length > 1 ? decodeURIComponent(parts.slice(1).join('/')) : null;
    if (id && detailViews[head]) {
      var matched = [];
      document.querySelectorAll(
        '[data-report-view="' + detailViews[head] + '"] [data-detail]'
      ).forEach(function (node) {
        var active = node.getAttribute('data-detail') === id;
        node.hidden = !active;
        if (active) { matched.push(node); }
      });
      if (matched.length) {
        var tracks = matched.map(function (node) {
          return node.getAttribute('data-track-context');
        });
        if (activeTrack !== 'all' && tracks.indexOf(activeTrack) === -1) {
          var specificTrack = tracks.filter(function (track) { return track !== 'all'; })[0];
          showTrack(specificTrack || tracks[0]);
        }
        showView(detailViews[head], head);
        window.scrollTo(0, 0);
        return;
      }
    }
    showView(head);
    window.scrollTo(0, 0);
  }
  window.addEventListener('hashchange', routeFromHash);
  routeFromHash();

  document.querySelectorAll('[data-comparison-scope]').forEach(function (scope) {
    var buttons = Array.prototype.slice.call(scope.querySelectorAll('[data-metric-set]'));
    var panels = Array.prototype.slice.call(scope.querySelectorAll('[data-metric]'));
    var direction = scope.querySelector('[data-metric-direction]');
    function selectMetric(metric) {
      buttons.forEach(function (button) {
        button.setAttribute('aria-pressed', button.getAttribute('data-metric-set') === metric ? 'true' : 'false');
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.getAttribute('data-metric') !== metric;
      });
      var selected = buttons.find(function (button) {
        return button.getAttribute('data-metric-set') === metric;
      });
      if (direction && selected) {
        direction.textContent = selected.getAttribute('data-metric-direction');
      }
    }
    buttons.forEach(function (button) {
      button.addEventListener('click', function () { selectMetric(button.getAttribute('data-metric-set')); });
    });
    if (buttons.length) { selectMetric(buttons[0].getAttribute('data-metric-set')); }
  });

  document.querySelectorAll('[data-hero]').forEach(function (hero) {
    var rowsRoot = hero.querySelector('[data-hero-rows]');
    if (!rowsRoot) { return; }
    var rows = Array.prototype.slice.call(rowsRoot.querySelectorAll('[data-hero-row]'));
    var pinned = null;

    function focusModel(model) {
      hero.querySelectorAll('[data-hero-point],[data-hero-link]').forEach(function (node) {
        var own = node.getAttribute('data-hero-point') || node.getAttribute('data-hero-link');
        node.style.opacity = !model || own === model ? '1' : '.16';
      });
      rows.forEach(function (row) {
        var own = row.getAttribute('data-hero-row');
        row.style.opacity = !model || own === model ? '1' : '.38';
        row.style.background = pinned && own === pinned ? 'rgba(var(--accent-rgb),.07)' : 'transparent';
        row.style.borderBottomColor = pinned && own === pinned
          ? 'var(--accent)' : 'rgba(var(--ink-rgb),.12)';
      });
    }

    function setPin(model) {
      pinned = model;
      if (!model) {
        var active = document.activeElement;
        if (active instanceof HTMLElement && hero.contains(active) && active.matches('[data-hero-pin]')) {
          active.blur();
        }
        releaseTooltips();
      }
      hero.querySelectorAll('[data-hero-drops]').forEach(function (node) {
        node.style.display = node.getAttribute('data-hero-drops') === model ? 'block' : 'none';
      });
      hero.querySelectorAll('[data-hero-cue]').forEach(function (node) {
        node.style.opacity = model ? '0' : '1';
      });
      hero.querySelectorAll('[data-hero-pin]').forEach(function (button) {
        button.setAttribute('aria-pressed', button.getAttribute('data-hero-pin') === model ? 'true' : 'false');
      });
      var clear = hero.querySelector('[data-hero-clear]');
      if (clear) {
        clear.style.display = model ? 'inline-flex' : 'none';
        var name = clear.querySelector('[data-hero-pinned]');
        if (name) { name.textContent = model || ''; }
      }
      focusModel(model);
    }

    function isolateTooltip(activePoint) {
      hero.querySelectorAll('[data-hero-point]').forEach(function (point) {
        point.toggleAttribute('data-hero-tooltip-muted', point !== activePoint);
      });
    }
    function releaseTooltips() {
      hero.querySelectorAll('[data-hero-tooltip-muted]').forEach(function (point) {
        point.removeAttribute('data-hero-tooltip-muted');
      });
    }

    rows.forEach(function (row) {
      var model = row.getAttribute('data-hero-row');
      row.addEventListener('mouseenter', function () { if (!pinned) { focusModel(model); } });
      row.addEventListener('mouseleave', function () { if (!pinned) { focusModel(null); } });
    });
    hero.querySelectorAll('[data-hero-pin]').forEach(function (button) {
      var model = button.getAttribute('data-hero-pin');
      var point = button.closest('[data-hero-point]');
      button.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        setPin(pinned === model ? null : model);
      });
      button.addEventListener('mouseenter', function () {
        if (point) { isolateTooltip(point); }
        if (!pinned) { focusModel(model); }
      });
      button.addEventListener('mouseleave', function () {
        if (point) { releaseTooltips(); }
        if (!pinned) { focusModel(null); }
      });
      button.addEventListener('focus', function () {
        if (point) { isolateTooltip(point); }
        if (!pinned) { focusModel(model); }
      });
      button.addEventListener('blur', function () {
        if (point) { releaseTooltips(); }
        if (!pinned) { focusModel(null); }
      });
    });
    var clearHero = hero.querySelector('[data-hero-clear]');
    if (clearHero) { clearHero.addEventListener('click', function () { setPin(null); }); }
    hero.querySelectorAll('[data-hero-sort]').forEach(function (button) {
      button.addEventListener('click', function () {
        var key = button.getAttribute('data-hero-sort');
        var ascending = key === 'tokens';
        hero.querySelectorAll('[data-hero-sort]').forEach(function (item) {
          item.setAttribute('aria-pressed', item === button ? 'true' : 'false');
        });
        rows.slice().sort(function (left, right) {
          var a = parseFloat(left.getAttribute('data-sort-' + key));
          var b = parseFloat(right.getAttribute('data-sort-' + key));
          if (Number.isNaN(a)) { return 1; }
          if (Number.isNaN(b)) { return -1; }
          if (a === b) {
            return left.getAttribute('data-hero-row').localeCompare(right.getAttribute('data-hero-row'));
          }
          return ascending ? a - b : b - a;
        }).forEach(function (row) { rowsRoot.appendChild(row); });
      });
    });
    document.addEventListener('click', function (event) {
      var target = event.target instanceof Element ? event.target : null;
      if (!pinned || (target && hero.contains(target.closest('[data-hero-pin],[data-hero-clear]')))) {
        return;
      }
      setPin(null);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && pinned) { setPin(null); }
    });
  });

  document.querySelectorAll('[data-ladder-scope]').forEach(function (scope) {
    var select = scope.querySelector('[data-ladder-select]');
    var panels = Array.prototype.slice.call(scope.querySelectorAll('[data-condition-panel]'));
    if (!select) { return; }
    function showVariant() {
      panels.forEach(function (panel) {
        panel.classList.toggle('ladder-on', panel.getAttribute('data-condition-panel') === select.value);
      });
    }
    select.addEventListener('change', showVariant);
    showVariant();
  });

  runFilters.forEach(function (select) { select.addEventListener('change', applyRunFilters); });
  var clear = document.querySelector('[data-run-clear]');
  if (clear) {
    clear.addEventListener('click', function () {
      runFilters.forEach(function (select) { select.value = 'all'; });
      applyRunFilters();
    });
  }

  document.querySelectorAll('[data-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var value = button.getAttribute('data-copy');
      var acknowledgement = button.nextElementSibling;
      function done(ok) {
        if (!acknowledgement || !acknowledgement.hasAttribute('data-copy-ack')) { return; }
        acknowledgement.textContent = ok ? (button.getAttribute('data-copy-note') || 'Copied') : 'Copy failed';
        acknowledgement.style.color = ok ? 'var(--pos)' : 'var(--neg)';
        clearTimeout(button._ackTimer);
        button._ackTimer = setTimeout(function () { acknowledgement.textContent = ''; }, 2200);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).then(function () { done(true); }, function () { done(false); });
        return;
      }
      var field = document.createElement('textarea');
      field.value = value;
      field.setAttribute('readonly', '');
      field.style.position = 'fixed';
      field.style.opacity = '0';
      document.body.appendChild(field);
      field.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
      document.body.removeChild(field);
      done(ok);
    });
  });
  applyRunFilters();
}());
"""


def _text(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _short(value: Any, keep: int = 10) -> str:
    text = str(value or "").removeprefix("sha256:")
    if len(text) <= keep + 4:
        return text or "-"
    return f"{text[:keep]}...{text[-4:]}"


def _track_label(track: str) -> str:
    return {"all": "All", "testnet": "TestNet", "local-hermetic": "Local"}.get(track, track)


def _task_name(task_id: str) -> str:
    return str(TASK_COPY.get(task_id, {}).get("name") or task_id)


def _fmt_percent(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.1f}%"


def _fmt_delta(value: Any) -> str:
    return "withheld" if value is None else f"{float(value):+.1f} pp"


def _fmt_int(value: Any) -> str:
    return "n/a" if value is None else f"{int(value):,}"


def _fmt_cost(value: Any, status: str) -> str:
    if value is None:
        return "n/a"
    prefix = ">= " if status != "complete" else ""
    return f"{prefix}${Decimal(str(value)):,.6f}".rstrip("0").rstrip(".")


def _fmt_compact(value: Any) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(number) >= divisor:
            shown = f"{number / divisor:.1f}".rstrip("0").rstrip(".")
            return f"{shown}{suffix}"
    return f"{number:.0f}"


def _variant_label(row: dict[str, Any]) -> str:
    return f'{row["requested_model"]} · {row["thinking_level"]}'


def _comparison_available(row: dict[str, Any]) -> bool:
    return row["matched"]["comparison_status"] == "available"


def _campaign_cell(campaign_id: str) -> str:
    return (
        f'<code title="{_attr(campaign_id)}">'
        f"{_text(campaign_id.removeprefix('campaign-')[:8])}</code>"
    )


def _spine(
    body: str,
    *,
    number: str | None = None,
    first: bool = False,
    terminal: bool = False,
) -> str:
    node_top = "41px" if first else "29px"
    if terminal:
        rule = (
            '<div style="position:absolute;right:0;top:0;height:32px;width:1px;'
            'background:rgba(var(--ink-rgb),.14)"></div>'
        )
        node = (
            f'<div style="position:absolute;right:-4.5px;top:{node_top};width:9px;height:9px;'
            'border:1px solid var(--ink);background:var(--bg)"></div>'
        )
    else:
        rule_top = "44px" if first else "0"
        rule = (
            f'<div style="position:absolute;right:0;top:{rule_top};bottom:0;width:1px;'
            'background:rgba(var(--ink-rgb),.14)"></div>'
        )
        node = (
            f'<div style="position:absolute;right:-3.5px;top:{node_top};width:7px;height:7px;'
            'background:var(--ink)"></div>'
        )
    border = "" if first else "border-top:1px solid rgba(var(--ink-rgb),.14)"
    padding = "38px 0 44px 30px" if first else "30px 0 42px 30px"
    station = (
        f'<div data-r="hidesm" style="position:absolute;right:14px;top:'
        f'{"44px" if first else "32px"};font:500 9.5px/1 {MONO};color:var(--faint)">'
        f"{_text(number)}</div>"
        if number is not None
        else ""
    )
    return (
        f'<section data-r="spine" style="{border}"><div style="position:relative">'
        f'{rule}{node}{station}</div><div data-r="body" style="padding:{padding};min-width:0">'
        f"{body}</div></section>"
    )


def _spine_page(body: str) -> str:
    return (
        '<main data-r="spine"><div style="position:relative">'
        '<div style="position:absolute;right:0;top:44px;bottom:0;width:1px;'
        'background:rgba(var(--ink-rgb),.14)"></div>'
        '<div style="position:absolute;right:-3.5px;top:41px;width:7px;height:7px;'
        'background:var(--ink)"></div></div>'
        f'<div data-r="body" style="padding:38px 0 60px 30px;min-width:0">{body}</div></main>'
    )


def _table(caption: str, head: str, body: str, *, hide_caption: bool = False) -> str:
    caption_class = ' class="visually-hidden"' if hide_caption else ""
    return (
        '<div data-r="scroll" style="border-top:1px solid rgba(var(--ink-rgb),.32)">'
        f"<table><caption{caption_class}>{caption}</caption><thead><tr>{head}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _header() -> str:
    links = "".join(
        f'<a href="#/{"" if route == "overview" else route}" data-nav="{route}" '
        f'data-active="{"1" if route == "overview" else "0"}" '
        'style="display:flex;align-items:center;padding:0 11px;font-size:12.5px;'
        'color:var(--muted);text-decoration:none;white-space:nowrap;'
        'border-bottom:2px solid transparent">'
        f"{_text(label)}</a>"
        for route, label in _NAV
    )
    return (
        '<header data-r="noprint" style="position:sticky;top:0;z-index:20;background:var(--bg);'
        'border-bottom:1px solid rgba(var(--ink-rgb),.28)"><div data-r="pad" '
        'data-report-header '
        'style="max-width:1320px;margin:0 auto;padding:0 34px;display:flex;align-items:stretch;'
        'justify-content:space-between;gap:24px;min-height:58px"><div data-report-brand style="display:flex;'
        'align-items:center;gap:14px;flex:none"><a href="#/" data-nav="overview" '
        'style="display:flex;align-items:center;gap:9px;color:var(--ink);text-decoration:none">'
        f'{DOT_MARK}<span style="font:600 14.5px/1 {SANS}">CKB AI Bench</span></a></div>'
        '<nav data-report-nav aria-label="Report sections" style="display:flex;align-items:stretch;gap:2px;'
        f'min-width:0;flex:1 1 auto;overflow-x:auto;scrollbar-width:none">{links}</nav>'
        '<button type="button" data-theme-toggle aria-label="Switch theme" '
        'style="flex:none;display:flex;align-items:center;gap:7px;padding:0 11px;min-height:40px;'
        'align-self:center;border:1px solid rgba(var(--ink-rgb),.22);border-radius:2px;'
        'font-size:11.5px;color:var(--muted)"><span aria-hidden="true" data-theme-glyph>'
        '&#9680;</span><span data-r="hidemd" data-theme-label>Light</span></button></div></header>'
    )


def _annotated_rows(sources: list[tuple[dict[str, Any], str]], name: str) -> list[dict[str, Any]]:
    return [
        {**row, "_campaign_id": document["campaign"]["campaign_id"]}
        for document, _digest in sources
        for row in document[name]
    ]


def _arm_usage(
    acquisitions: list[dict[str, Any]],
    summary: dict[str, Any],
    arm: str,
) -> dict[str, Any]:
    selected = [
        row
        for row in acquisitions
        if row["_campaign_id"] == summary["_campaign_id"]
        and row["model_variant_id"] == summary["model_variant_id"]
        and (
            summary["chain_track"] == "all"
            or row["chain_track"] == summary["chain_track"]
        )
        and row["arm"] == arm
    ]
    token_rows = [row for row in selected if row["total_tokens"] is not None]
    statuses = {row["token_status"] for row in selected}
    if selected and statuses == {"complete"}:
        token_status = "complete"
    elif token_rows:
        token_status = "observed lower bound"
    else:
        token_status = "unavailable"
    costs = [
        Decimal(str(row["observed_cost_usd"]))
        for row in selected
        if row["observed_cost_usd"] is not None
    ]
    cost_statuses = {row["cost_status"] for row in selected}
    if selected and cost_statuses == {"complete"} and len(costs) == len(selected):
        cost_status = "complete"
    elif costs:
        cost_status = "lower bound"
    else:
        cost_status = "unavailable"
    return {
        "slots": len(selected),
        "tokens": sum(int(row["total_tokens"]) for row in token_rows),
        "token_status": token_status,
        "cost": sum(costs, Decimal("0")) if costs else None,
        "cost_status": cost_status,
        "model_calls": sum(int(row["model_calls"]) for row in selected),
        "provider_attempts": sum(int(row["provider_attempts"]) for row in selected),
        "provider_responses": sum(int(row["provider_responses"]) for row in selected),
        "retries": sum(int(row["provider_retry_count"]) for row in selected),
        "agent_seconds": sum(float(row["timings"]["agent_seconds"]) for row in selected),
    }


def _summary_rows(
    sources: list[tuple[dict[str, Any], str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    acquisitions = _annotated_rows(sources, "slot_acquisitions")
    summaries = _annotated_rows(sources, "variant_summaries")
    for row in summaries:
        row["_usage"] = {
            arm: _arm_usage(acquisitions, row, arm) for arm in ("B", "C")
        }
    return summaries, acquisitions


def _score_percent(awarded: int, possible: int) -> float | None:
    return None if possible == 0 else round(100.0 * awarded / possible, 6)


def _combined_summary_rows(
    summaries: list[dict[str, Any]],
    acquisitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in summaries:
        grouped.setdefault(
            (row["_campaign_id"], row["model_variant_id"]),
            [],
        ).append(row)

    combined = []
    identity_fields = (
        "model_profile_id",
        "model_profile_sha256",
        "requested_model",
        "thinking_level",
    )
    for (campaign_id, variant_id), rows in sorted(grouped.items()):
        first = rows[0]
        if any(
            row[field] != first[field]
            for row in rows[1:]
            for field in identity_fields
        ):
            raise ValueError("report execution tracks disagree on model identity")

        arms: dict[str, dict[str, Any]] = {}
        for arm in ("B", "C"):
            awarded = sum(int(row["arms"][arm]["score_awarded"]) for row in rows)
            possible = sum(int(row["arms"][arm]["score_possible"]) for row in rows)
            arms[arm] = {
                "correctness_observations": sum(
                    int(row["arms"][arm]["correctness_observations"]) for row in rows
                ),
                "infra_failures": sum(
                    int(row["arms"][arm]["infra_failures"]) for row in rows
                ),
                "score_awarded": awarded,
                "score_percent": _score_percent(awarded, possible),
                "score_possible": possible,
                "slots": sum(int(row["arms"][arm]["slots"]) for row in rows),
            }

        pairs = sum(int(row["matched"]["pairs"]) for row in rows)
        correctness_pairs = sum(
            int(row["matched"]["correctness_pairs"]) for row in rows
        )
        b_awarded = sum(int(row["matched"]["b_score_awarded"]) for row in rows)
        c_awarded = sum(int(row["matched"]["c_score_awarded"]) for row in rows)
        possible = sum(
            int(row["matched"]["score_possible_per_arm"]) for row in rows
        )
        comparison_available = (
            pairs > 0
            and correctness_pairs == pairs
            and all(
                row["matched"]["comparison_status"] == "available" for row in rows
            )
        )
        b_percent = _score_percent(b_awarded, possible)
        c_percent = _score_percent(c_awarded, possible)
        summary = {
            "_campaign_id": campaign_id,
            "arms": arms,
            "chain_track": "all",
            "matched": {
                "b_score_awarded": b_awarded,
                "c_minus_b_score_percent": (
                    round(c_percent - b_percent, 6)
                    if comparison_available
                    and b_percent is not None
                    and c_percent is not None
                    else None
                ),
                "c_score_awarded": c_awarded,
                "comparison_status": (
                    "available" if comparison_available else "withheld"
                ),
                "correctness_pairs": correctness_pairs,
                "pairs": pairs,
                "score_percent_b": b_percent if comparison_available else None,
                "score_percent_c": c_percent if comparison_available else None,
                "score_possible_per_arm": possible,
            },
            "model_profile_id": first["model_profile_id"],
            "model_profile_sha256": first["model_profile_sha256"],
            "model_variant_id": variant_id,
            "requested_model": first["requested_model"],
            "thinking_level": first["thinking_level"],
        }
        summary["_usage"] = {
            arm: _arm_usage(acquisitions, summary, arm) for arm in ("B", "C")
        }
        combined.append(summary)
    return combined


def _track_selector(
    tracks: list[str],
    counts: dict[str, int],
    *,
    campaign_count: int,
    model_count: int,
) -> str:
    buttons = "".join(
        f'<button type="button" data-track-set="{_attr(track)}" '
        f'aria-pressed="{"true" if index == 0 else "false"}" '
        'style="padding:0 14px;min-height:40px;font-size:12.5px;'
        'border-right:1px solid rgba(var(--ink-rgb),.18)">'
        f"{_text(_track_label(track))}</button>"
        for index, track in enumerate(tracks)
    )
    count_panels = "".join(
        f'<span data-track-panel="{_attr(track)}" class="{"track-on" if index == 0 else ""}">'
        f'{counts.get(track, 0)} task attempts</span>'
        for index, track in enumerate(tracks)
    )
    return (
        '<div data-r="noprint" data-track-selector style="display:flex;flex-wrap:wrap;align-items:center;gap:12px 26px;'
        'padding:13px 0;border-bottom:1px solid rgba(var(--ink-rgb),.14)">'
        '<div data-track-group role="group" aria-label="Task scope" style="display:flex;border:1px solid '
        f'rgba(var(--ink-rgb),.28);border-radius:2px;overflow:hidden">{buttons}</div>'
        f'<div data-track-meta style="display:flex;flex-wrap:wrap;gap:6px 22px;font-size:12px;'
        f'color:var(--muted)"><span>{campaign_count} '
        f'{"campaign" if campaign_count == 1 else "campaigns"}</span><span>{model_count} '
        f'{"model variant" if model_count == 1 else "model variants"}</span>{count_panels}</div></div>'
    )


def _identity_station(
    track: str,
    summaries: list[dict[str, Any]],
    sources: list[tuple[dict[str, Any], str]],
    generated_at: str,
) -> str:
    first = sources[0][0]
    campaign = first["campaign"]
    rows = (
        ("Suite", campaign["suite_semver"]),
        ("Freeze hash", _short(campaign["suite_freeze_sha256"], 12)),
        ("Scope", _track_label(track)),
        ("Models", str(len(summaries))),
        ("Results through", generated_at.replace("T", " ")[:16] + " UTC"),
    )
    identity = "".join(
        f'<dt style="color:var(--muted);white-space:nowrap">{_text(label)}</dt>'
        f'<dd style="margin:0;font-family:{MONO};font-size:11.5px;overflow-wrap:anywhere">'
        f"{_text(value)}</dd>"
        for label, value in rows
    )
    return (
        '<div data-r="two"><div><h1 data-report-title style="margin:0;'
        f'font-family:{SERIF};font-weight:500;font-size:52px;line-height:1.06;letter-spacing:0;'
        'max-width:15em;text-wrap:pretty">CKB AI Bench</h1></div>'
        '<div style="border-left:1px solid rgba(var(--ink-rgb),.14);padding-left:26px;'
        f'align-self:start"><h2 style="margin:0 0 12px;{EYEBROW_STYLE}">Report identity</h2>'
        '<dl style="margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);'
        f'gap:7px 16px;font-size:12.5px">{identity}</dl></div></div>'
    )


def _hero_axis(summary_rows: list[dict[str, Any]]) -> float:
    values = [
        float(row["_usage"][arm]["tokens"])
        for row in summary_rows
        for arm in ("B", "C")
        if row["_usage"][arm]["tokens"] > 0
    ]
    largest = max(values, default=1.0) * 1.08
    magnitude = 10 ** math.floor(math.log10(largest))
    fraction = largest / magnitude
    step = 1 if fraction <= 1 else 2 if fraction <= 2 else 5 if fraction <= 5 else 10
    return float(step * magnitude)


def _hero_x(tokens: float, axis: float) -> float:
    return max(0.0, min(100.0, (1.0 - tokens / axis) * 100.0))


def _leaderboard(summary_rows: list[dict[str, Any]]) -> str:
    columns = "minmax(112px,172px) minmax(0,1fr) 74px 74px 96px"
    heading = "".join(
        f'<span style="font:600 9.5px/1.3 {SANS};text-transform:uppercase;'
        f'color:var(--muted);{extra}">{_text(label)}</span>'
        for label, extra in (
            ("Model / arm", ""),
            ("Weighted score · higher better", "white-space:nowrap"),
            ("Tokens", "text-align:right"),
            ("Agent time", "text-align:right"),
            ("C - B", "text-align:right"),
        )
    )
    body = []
    for row in sorted(
        summary_rows,
        key=lambda item: (-float(item["matched"].get("c_minus_b_score_percent") or 0), _variant_label(item)),
    ):
        variant = row["model_variant_id"]
        bars: list[str] = []
        tokens: list[str] = []
        times: list[str] = []
        for arm in ("B", "C"):
            score = float(row["arms"][arm]["score_percent"] or 0)
            usage = row["_usage"][arm]
            bars.append(
                '<div style="display:grid;grid-template-columns:15px minmax(0,1fr);gap:6px;'
                f'align-items:center" data-arm="{arm}"><span style="font:600 10.5px/1 {SANS}">'
                f'{arm}</span><div style="position:relative;height:15px;background:'
                'rgba(var(--ink-rgb),.045)"><div data-bar style="position:absolute;inset:0 auto 0 0;'
                f'width:{max(.5, score):.3f}%"></div></div></div>'
            )
            token_note = "" if usage["token_status"] == "complete" else "*"
            tokens.append(
                f'<span title="{_attr(usage["token_status"])}" style="height:15px;display:flex;'
                f'justify-content:flex-end;align-items:center;font:500 11.5px/1 {MONO}">'
                f'{_text(_fmt_compact(usage["tokens"]) + token_note)}</span>'
            )
            times.append(
                f'<span style="height:15px;display:flex;justify-content:flex-end;align-items:center;'
                f'font:500 11.5px/1 {MONO}">{usage["agent_seconds"]:.1f}s</span>'
            )
        delta = row["matched"].get("c_minus_b_score_percent")
        b_tokens = row["_usage"]["B"]["tokens"]
        c_tokens = row["_usage"]["C"]["tokens"]
        token_delta = c_tokens - b_tokens
        exact = all(row["_usage"][arm]["token_status"] == "complete" for arm in ("B", "C"))
        body.append(
            f'<div data-r="campaign-leader-row" data-hero-row="{_attr(variant)}" '
            f'data-sort-score="{_attr(row["arms"]["C"]["score_percent"])}" '
            f'data-sort-delta="{_attr(delta if delta is not None else "")}" '
            f'data-sort-tokens="{_attr(c_tokens)}" style="display:grid;grid-template-columns:'
            f'{columns};gap:0 20px;align-items:center;padding:15px 12px;border-bottom:'
            '1px solid rgba(var(--ink-rgb),.12);transition:opacity .15s ease-out">'
            '<div style="min-width:0">'
            f'<a href="#/models/{_attr(variant)}" data-nav="models" style="display:block;'
            f'font:500 12.5px/1.25 {MONO};overflow-wrap:anywhere">{_text(row["requested_model"])}</a>'
            f'<span style="display:block;font-size:10.5px;color:var(--muted);margin-top:3px">'
            f'{_text(row["thinking_level"])} thinking · {_campaign_cell(row["_campaign_id"])}</span></div>'
            f'<button type="button" data-hero-pin="{_attr(variant)}" aria-pressed="false" '
            f'aria-label="Show only {_attr(_variant_label(row))} in the chart" style="display:flex;'
            f'flex-direction:column;gap:6px;width:100%;min-width:0;min-height:40px;padding:5px 0;'
            f'text-align:initial">{"".join(bars)}</button>'
            f'<div data-r="campaign-leader-meta" style="display:flex;flex-direction:column;gap:6px">'
            f'{"".join(tokens)}</div><div data-r="campaign-leader-meta" style="display:flex;'
            f'flex-direction:column;gap:6px">{"".join(times)}</div>'
            f'<div data-r="campaign-leader-meta" style="text-align:right"><span style="display:block;'
            f'font:600 14px/1.15 {SANS};color:var(--accent)">{_text(_fmt_delta(delta))}</span>'
            f'<span style="display:block;margin-top:3px;font:500 10.5px/1.2 {MONO};color:'
            f'{"var(--muted)" if exact else "var(--caution)"}">{token_delta:+,} tok'
            f'{"" if exact else " observed"}</span></div></div>'
        )
    controls = "".join(
        f'<button type="button" data-hero-sort="{key}" aria-pressed="'
        f'{"true" if key == "delta" else "false"}" style="border:1px solid '
        f'rgba(var(--ink-rgb),.22);padding:0 11px;min-height:40px;border-radius:2px;font-size:12px">'
        f'{_text(label)}</button>'
        for key, label in (("score", "C score"), ("delta", "C - B"), ("tokens", "C tokens"))
    )
    return (
        f'<h3 style="margin:0 0 5px;font:600 13px/1.3 {SANS}">Leaderboard</h3>'
        '<div data-r="noprint" style="display:flex;flex-wrap:wrap;align-items:center;gap:9px;'
        'margin-bottom:16px;font-size:12px;color:var(--muted)"><span style="font-weight:600;'
        'text-transform:uppercase;font-size:10px">Sort rows by</span>'
        f'{controls}<button type="button" data-hero-clear style="display:none;align-items:center;'
        'gap:7px;border:1px solid var(--accent);color:var(--accent);padding:0 11px;min-height:40px;'
        'border-radius:2px;font-size:12px"><span aria-hidden="true">×</span><span>Showing '
        '<span data-hero-pinned></span> only</span></button></div>'
        f'<div data-r="campaign-lb" style="display:grid;grid-template-columns:{columns};gap:0 20px;'
        f'align-items:end;padding:0 12px 7px;border-bottom:1px solid rgba(var(--ink-rgb),.42)">'
        f'{heading}</div><div data-hero-rows>{"".join(body)}</div>'
    )


def _chart(summary_rows: list[dict[str, Any]], track: str) -> str:
    if not summary_rows:
        return ""
    axis = _hero_axis(summary_rows)
    y_values = (100, 75, 50, 25, 0)
    y_labels = "".join(
        f'<span style="position:absolute;right:11px;top:{100-value}%;transform:translateY(-50%);'
        f'font:400 10px/1 {MONO};color:var(--faint)">{value}</span>'
        for value in y_values
    )
    y_rules = "".join(
        f'<span aria-hidden="true" style="position:absolute;inset:{100-value}% 0 auto;height:1px;'
        'background:rgba(var(--ink-rgb),.10)"></span>'
        for value in y_values
    )
    ticks = [axis * index / 4 for index in range(5)]
    x_rules = "".join(
        f'<span aria-hidden="true" style="position:absolute;top:0;bottom:0;left:'
        f'{_hero_x(value, axis):.2f}%;width:1px;background:rgba(var(--ink-rgb),.10)"></span>'
        for value in ticks
    )
    x_labels = "".join(
        f'<span style="position:absolute;left:{_hero_x(value, axis):.2f}%;top:0;'
        'transform:translateX(-50%);text-align:center"><span style="display:block;width:1px;'
        'height:4px;background:rgba(var(--ink-rgb),.3);margin:0 auto 4px"></span>'
        f'<span style="font:400 10px/1 {MONO};color:var(--faint)">{_text(_fmt_compact(value))}'
        '</span></span>'
        for value in ticks
    )
    label_points = [
        (
            row["model_variant_id"],
            _hero_x(float(row["_usage"]["C"]["tokens"]), axis),
            100 - float(row["arms"]["C"]["score_percent"] or 0),
        )
        for row in summary_rows
    ]
    label_sides: dict[str, str] = {}
    for variant, x, top in label_points:
        nearby = [
            other_x
            for other_variant, other_x, other_top in label_points
            if other_variant != variant
            and abs(top - other_top) < 8
            and abs(x - other_x) < 22
        ]
        if any(other_x > x for other_x in nearby):
            label_sides[variant] = "right:14px;text-align:right"
        elif any(other_x < x for other_x in nearby):
            label_sides[variant] = "left:14px"
        else:
            label_sides[variant] = "left:14px" if x <= 62 else "right:14px;text-align:right"
    links: list[str] = []
    drops: list[str] = []
    points: list[str] = []
    for row_index, row in enumerate(summary_rows):
        variant = row["model_variant_id"]
        own: list[dict[str, Any]] = []
        for arm in ("B", "C"):
            usage = row["_usage"][arm]
            score = float(row["arms"][arm]["score_percent"] or 0)
            tokens = float(usage["tokens"])
            own.append({
                "arm": arm,
                "x": _hero_x(tokens, axis),
                "top": 100 - score,
                "score": score,
                "tokens": tokens,
                "usage": usage,
            })
        b, c = own
        dash = "0" if all(item["usage"]["token_status"] == "complete" for item in own) else "3.5 3"
        links.append(
            f'<polyline data-hero-link="{_attr(variant)}" points="{b["x"]:.2f},{b["top"]:.2f} '
            f'{c["x"]:.2f},{c["top"]:.2f}" fill="none" stroke="var(--accent)" '
            f'stroke-width="1.25" stroke-dasharray="{dash}" vector-effect="non-scaling-stroke">'
            '</polyline>'
        )
        drop_parts = []
        for item in own:
            x = item["x"]
            top = item["top"]
            drop_parts.append(
                f'<span aria-hidden="true" style="position:absolute;left:{x:.2f}%;top:{top:.2f}%;'
                'bottom:0;width:1px;background:repeating-linear-gradient(to bottom,var(--accent),'
                'var(--accent) 2px,transparent 2px,transparent 5px)"></span>'
                f'<span style="position:absolute;left:{x:.2f}%;bottom:'
                f'{7 if item["arm"] == "B" else 29}px;transform:translateX(-50%);white-space:nowrap;'
                f'background:var(--surface);border:1px solid var(--accent);border-radius:2px;'
                f'padding:2px 6px;font:500 10.5px/1.25 {MONO};color:var(--accent)">'
                f'{item["arm"]} {_text(_fmt_compact(item["tokens"]))}</span>'
                f'<span aria-hidden="true" style="position:absolute;top:{top:.2f}%;left:0;'
                f'width:{x:.2f}%;height:1px;background:repeating-linear-gradient(to right,'
                'var(--accent),var(--accent) 2px,transparent 2px,transparent 5px)"></span>'
                f'<span style="position:absolute;top:{min(max(top, 3.2), 96.8):.2f}%;left:9px;'
                f'transform:translateY(-50%);white-space:nowrap;background:var(--surface);'
                f'border:1px solid var(--accent);border-radius:2px;padding:2px 6px;'
                f'font:500 10.5px/1.25 {MONO};color:var(--accent)">{item["arm"]} '
                f'{item["score"]:.1f}</span>'
            )
        drops.append(
            f'<div data-hero-drops="{_attr(variant)}" style="display:none;position:absolute;'
            f'inset:0;pointer-events:none">{"".join(drop_parts)}</div>'
        )
        for point_index, item in enumerate(own):
            arm = item["arm"]
            tooltip_id = f'campaign-hero-tip-{track}-{row_index}-{point_index}'
            right = item["x"] <= 62
            tooltip_side = "left:14px" if right else "right:14px"
            label_side = label_sides[variant]
            label_vertical = (
                "top:11px;transform:none"
                if item["top"] <= 8
                else "top:0;transform:translateY(-50%)"
            )
            shape = (
                '<span style="position:absolute;left:-6px;top:-6px;width:12px;height:12px;'
                'border:1.75px solid var(--arm-b-line);background:var(--surface);border-radius:50%;'
                'pointer-events:none"></span>'
                if arm == "B" else
                '<span style="position:absolute;left:-5.5px;top:-5.5px;width:11px;height:11px;'
                'background:var(--accent);pointer-events:none"></span>'
            )
            label = (
                f'<span data-r="hidesm" style="position:absolute;{label_side};{label_vertical};'
                f'white-space:nowrap;pointer-events:none;font:600 '
                f'11.5px/1.2 {MONO}">{_text(row["requested_model"])}</span>'
                if arm == "C" else ""
            )
            tooltip = (
                f'<span id="{tooltip_id}" data-hero-tooltip role="tooltip" style="position:absolute;'
                f'{tooltip_side};top:14px;z-index:8;width:max-content;max-width:min(230px,70vw);'
                'padding:9px 11px;background:var(--ink);color:var(--surface);border:1px solid '
                'rgba(var(--surf-rgb),.2);border-radius:3px;box-shadow:0 8px 24px '
                f'rgba(var(--ink-rgb),.16);pointer-events:none;text-align:left"><strong style="font:'
                f'600 12px/1.25 {SANS}">{_text(_variant_label(row))}</strong><span style="display:'
                f'block;margin-top:4px;font:400 10.5px/1.45 {MONO}">{arm}: '
                f'{_text(_ARM_LABELS[arm])}<br>Weighted score: {item["score"]:.1f}%<br>'
                f'Observed response tokens: {_fmt_int(item["tokens"])}<br>'
                f'Response coverage: {item["usage"]["provider_responses"]} of '
                f'{item["usage"]["provider_attempts"]}<br>{_text(item["usage"]["token_status"])}'
                '</span></span>'
            )
            points.append(
                f'<div data-arm="{arm}" data-hero-point="{_attr(variant)}" style="position:absolute;'
                f'left:{item["x"]:.2f}%;top:{item["top"]:.2f}%;width:0;height:0;transition:'
                'opacity .15s ease-out">'
                f'<button type="button" data-hero-pin="{_attr(variant)}" aria-pressed="false" '
                f'aria-describedby="{tooltip_id}" aria-label="{_attr(_variant_label(row))}, '
                f'arm {arm}, {item["score"]:.1f} percent, {_fmt_int(item["tokens"])} tokens" '
                'style="position:absolute;left:-20px;top:-20px;width:40px;height:40px;'
                f'border-radius:50%;background:transparent"></button>{shape}{label}{tooltip}</div>'
            )
    legend = (
        '<span style="display:inline-flex;align-items:center;gap:8px"><span aria-hidden="true" '
        'style="width:12px;height:12px;border:1.75px solid var(--arm-b-line);background:'
        'var(--surface);border-radius:50%"></span>B: web only</span>'
        '<span style="display:inline-flex;align-items:center;gap:8px"><span aria-hidden="true" '
        'style="width:11px;height:11px;background:var(--accent)"></span>C: CKB AI plus web</span>'
        '<span style="display:inline-flex;align-items:center;gap:8px"><span aria-hidden="true" '
        'style="width:24px;height:2px;background:var(--accent)"></span>B to C shift</span>'
        '<span style="display:inline-flex;align-items:center;gap:8px"><span aria-hidden="true" '
        'style="width:24px;height:2px;background:repeating-linear-gradient(to right,var(--accent),'
        'var(--accent) 5px,transparent 5px,transparent 9px)"></span>partial usage</span>'
    )
    return (
        '<figure data-hero style="margin:34px 0 0;border-top:1px solid rgba(var(--ink-rgb),.32);'
        'padding-top:20px"><figcaption style="display:flex;flex-wrap:wrap;align-items:baseline;'
        'justify-content:space-between;gap:12px 24px;margin-bottom:20px"><span style="font:600 '
        f'14px/1.3 {SANS}">Score against observed token usage</span><span style="font-size:12px;'
        f'color:var(--muted)">{_text(_track_label(track))}</span></figcaption>'
        '<div style="display:grid;grid-template-columns:52px minmax(0,1fr);margin-bottom:34px">'
        f'<div style="position:relative;height:340px">{y_labels}<span style="position:absolute;'
        f'left:2px;top:50%;transform:translate(-50%,-50%) rotate(-90deg);font:600 9.5px/1 '
        f'{SANS};text-transform:uppercase;color:var(--muted);white-space:nowrap">Weighted score'
        '</span></div><div data-hero-plot style="position:relative;height:340px;background:'
        'var(--surface);border-left:1px solid rgba(var(--ink-rgb),.45);border-bottom:1px solid '
        f'rgba(var(--ink-rgb),.45)">{y_rules}{x_rules}<span data-hero-cue style="position:absolute;'
        f'right:14px;top:13px;font:500 10.5px/1 {SANS};color:var(--accent);transition:opacity '
        f'.15s ease-out">↗ higher score, fewer tokens</span>{"".join(drops)}<svg viewBox="0 0 '
        f'100 100" preserveAspectRatio="none" aria-hidden="true" style="position:absolute;inset:0;'
        f'width:100%;height:100%;overflow:visible">{"".join(links)}</svg>{"".join(points)}</div>'
        f'<span></span><div style="position:relative;height:34px">{x_labels}<span style="position:'
        f'absolute;right:0;bottom:0;font:600 9.5px/1 {SANS};text-transform:uppercase;color:'
        'var(--muted)">Observed response tokens · fewer is better →</span></div></div>'
        '<div style="display:flex;flex-wrap:wrap;gap:10px 26px;margin:-22px 0 34px;font-size:'
        f'11.5px;color:var(--ink-2)">{legend}</div>{_leaderboard(summary_rows)}</figure>'
    )


def _status_station(summary_rows: list[dict[str, Any]]) -> str:
    cards = []
    for row in sorted(summary_rows, key=lambda item: item["requested_model"]):
        matched = row["matched"]
        available = matched["comparison_status"] == "available"
        label = "Comparison available" if available else "Comparison withheld"
        tone = "var(--pos)" if available else "var(--caution)"
        token_statuses = {row["_usage"][arm]["token_status"] for arm in ("B", "C")}
        exact_tokens = token_statuses == {"complete"}
        no_infra = sum(row["arms"][arm]["infra_failures"] for arm in ("B", "C")) == 0
        checks = (
            (matched["correctness_pairs"] == matched["pairs"], "All matched slots scored"),
            (no_infra, "No infrastructure-failed slots"),
            (exact_tokens, "Complete response-token accounting"),
        )
        check_list = "".join(
            '<li style="display:grid;grid-template-columns:14px minmax(0,1fr);gap:7px;'
            f'font-size:11.5px;color:{"var(--pos)" if passed else "var(--caution)"}">'
            f'<span aria-hidden="true">{"✓" if passed else "✕"}</span>'
            f'<span>{_text(text)}</span></li>' for passed, text in checks
        )
        delta = matched["c_minus_b_score_percent"]
        cards.append(
            '<article data-status-card><div data-r="split" style="gap:28px;align-items:start">'
            '<div><div style="display:flex;align-items:center;gap:9px;margin-bottom:8px">'
            f'<span aria-hidden="true" style="color:{tone};font-size:13px">'
            f'{"●" if available else "◑"}</span><span style="font:600 16px/1.2 {SANS};'
            f'color:{tone}">{_text(label)}</span></div><h3 style="margin:0 0 7px;font:500 '
            f'14px/1.3 {MONO}"><a href="#/models/{_attr(row["model_variant_id"])}" '
            f'data-nav="models">{_text(_variant_label(row))}</a></h3><p style="margin:0;'
            f'font-size:13px;color:var(--ink-2)">B {matched["b_score_awarded"]} and C '
            f'{matched["c_score_awarded"]} of {matched["score_possible_per_arm"]} points; '
            f'C - B {_text(_fmt_delta(delta))}.</p></div><div style="border-left:1px solid '
            f'rgba(var(--ink-rgb),.18);padding-left:22px"><h4 style="margin:0 0 10px;'
            f'{EYEBROW_STYLE}">Comparison basis</h4><ul style="margin:0;padding:0;list-style:none;'
            f'display:flex;flex-direction:column;gap:6px">{check_list}</ul></div></div></article>'
        )
    return (
        f'<h2 style="margin:0 0 5px;font-family:{SERIF};font-weight:500;font-size:24px">'
        f'Comparison status</h2>{"".join(cards)}'
    )


_COMPARISON_METRICS = (
    ("score", "Weighted score", "%", "higher"),
    ("tokens", "Response tokens", "tokens", "lower"),
    ("time", "Agent time", "seconds", "lower"),
)


def _metric_value(row: dict[str, Any], arm: str, metric: str) -> float:
    if metric == "score":
        return float(row["arms"][arm]["score_percent"] or 0)
    if metric == "tokens":
        return float(row["_usage"][arm]["tokens"])
    return float(row["_usage"][arm]["agent_seconds"])


def _metric_display(value: float, metric: str) -> str:
    if metric == "score":
        return f"{value:.1f}%"
    if metric == "tokens":
        return f"{int(value):,}"
    return f"{value:.1f}s"


def _comparison_figure(
    row: dict[str, Any],
    metric: str,
    axis: float,
) -> str:
    lanes = []
    values = {arm: _metric_value(row, arm, metric) for arm in ("B", "C")}
    for arm in ("B", "C"):
        value = values[arm]
        width = max(0.6, min(100.0, value / axis * 100)) if value else 0.6
        lanes.append(
            f'<div data-arm="{arm}" data-r="campaign-comparison-lane" style="display:grid;'
            'grid-template-columns:154px minmax(0,1fr) 122px;gap:14px;align-items:center">'
            '<div style="display:flex;align-items:center;gap:8px;min-width:0">'
            f'<span aria-hidden="true" style="font-size:10px">{"○" if arm == "B" else "■"}</span>'
            f'<span style="font-size:12.5px;font-weight:500">{_text(_ARM_LABELS[arm])}</span></div>'
            '<div style="position:relative;height:22px;background:var(--track);border:1px solid '
            'rgba(var(--ink-rgb),.12)"><div data-bar style="position:absolute;inset:0 auto 0 0;'
            f'width:{width:.2f}%;transition:width .42s cubic-bezier(.2,.7,.2,1)"></div></div>'
            f'<div style="text-align:right;font:600 13px/1.2 {MONO};white-space:nowrap">'
            f'{_text(_metric_display(value, metric))}</div></div>'
        )
    ticks = "".join(
        f'<span style="position:absolute;left:{fraction * 100:.0f}%;top:0;transform:'
        f'translateX(-50%);font:400 10px/1 {MONO};color:var(--faint)">'
        f'{_text(_metric_display(axis * fraction, metric))}</span>'
        for fraction in (0, 0.25, 0.5, 0.75, 1)
    )
    delta = values["C"] - values["B"]
    available = _comparison_available(row) if metric == "score" else all(
        row["_usage"][arm]["token_status"] == "complete" for arm in ("B", "C")
    ) if metric == "tokens" else True
    delta_label = _metric_display(abs(delta), metric)
    delta_text = f'{"+" if delta >= 0 else "-"}{delta_label}'
    if not available:
        delta_text += " observed"
    return (
        f'<figure data-metric="{metric}" style="margin:0;border-top:1px solid '
        'rgba(var(--ink-rgb),.32);padding:20px 0 18px"><figcaption style="display:flex;'
        'flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:12px;'
        f'margin-bottom:16px"><span><a href="#/models/{_attr(row["model_variant_id"])}" '
        f'data-nav="models" style="display:block;font:500 15px/1.2 {MONO}">'
        f'{_text(_variant_label(row))}</a></span><span style="font:600 14px/1.2 {SANS};'
        f'color:var(--accent)">C - B {delta_text}</span></figcaption>'
        f'<div style="display:flex;flex-direction:column;gap:7px">{"".join(lanes)}'
        '<div data-r="campaign-comparison-lane" style="display:grid;grid-template-columns:154px '
        'minmax(0,1fr) 122px;gap:14px"><span></span><div style="position:relative;height:16px">'
        f'{ticks}</div><span></span></div></div></figure>'
    )


def _comparison_station(summary_rows: list[dict[str, Any]]) -> str:
    axes = {
        metric: 100.0 if metric == "score" else max(
            1.0,
            max(
                (_metric_value(row, arm, metric) for row in summary_rows for arm in ("B", "C")),
                default=1.0,
            ),
        ) * 1.12
        for metric, _label, _unit, _better in _COMPARISON_METRICS
    }
    buttons = "".join(
        f'<button type="button" data-metric-set="{metric}" aria-pressed="'
        f'{"true" if index == 0 else "false"}" data-metric-direction="'
        f'{_attr(f"{label} · {better} is better")}" style="padding:0 13px;min-height:40px;'
        'font-size:12.5px;border-right:1px solid rgba(var(--ink-rgb),.18)">'
        f'{_text(label)}</button>'
        for index, (metric, label, _unit, better) in enumerate(_COMPARISON_METRICS)
    )
    figures = "".join(
        _comparison_figure(row, metric, axes[metric])
        for metric, _label, _unit, _better in _COMPARISON_METRICS
        for row in sorted(summary_rows, key=lambda item: item["requested_model"])
    )
    default_label, default_better = _COMPARISON_METRICS[0][1], _COMPARISON_METRICS[0][3]
    return (
        '<div data-comparison-scope><div style="display:flex;flex-wrap:wrap;align-items:flex-end;'
        'justify-content:space-between;gap:18px;margin-bottom:20px"><div>'
        f'<h2 style="margin:0 0 5px;font-family:{SERIF};font-weight:500;font-size:24px">'
        'B versus C</h2><p data-metric-direction style="margin:0;font-size:11.5px;'
        f'color:var(--muted)">{_text(f"{default_label} · {default_better} is better")}</p>'
        '</div><div role="group" aria-label="Metric" data-r="noprint" '
        'style="display:flex;flex-wrap:wrap;border:1px solid rgba(var(--ink-rgb),.28);'
        f'border-radius:2px;overflow:hidden">{buttons}</div></div>{figures}</div>'
    )


def _task_station(task_rows: list[dict[str, Any]]) -> str:
    body = "".join(
        "<tr>"
        f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
        f'font-weight:500"><a href="#/tasks/{_attr(row["task_id"])}" data-nav="tasks">'
        f'{_text(_task_name(row["task_id"]))}</a><span style="display:block;'
        f'font:400 10.5px/1.4 {MONO};color:var(--muted)">{_text(row["task_id"])}</span></th>'
        f'<td style="font-family:{MONO};font-size:11.5px">{_text(row["requested_model"])}</td>'
        f'<td data-num>{row["matched"]["b_score_awarded"]} / '
        f'{row["matched"]["score_possible_per_arm"]}</td>'
        f'<td data-num>{row["matched"]["c_score_awarded"]} / '
        f'{row["matched"]["score_possible_per_arm"]}</td>'
        f'<td data-num>{_fmt_delta(row["matched"]["c_minus_b_score_percent"])}</td>'
        "</tr>"
        for row in sorted(task_rows, key=lambda item: (item["task_id"], item["requested_model"]))
    )
    return (
        f'<h2 style="margin:0 0 5px;font-family:{SERIF};font-weight:500;font-size:24px">'
        'Where B and C differ, task by task</h2><p style="margin:0 0 18px;'
        'font-size:13px;color:var(--muted)">All-or-zero scoring.</p>'
        + _table(
            "Awarded points by task, model and arm.",
            '<th scope="col">Task</th><th scope="col">Model</th>'
            '<th scope="col" data-num>B</th><th scope="col" data-num>C</th>'
            '<th scope="col" data-num>C minus B</th>',
            body,
        )
    )


def _efficiency_station(summary_rows: list[dict[str, Any]]) -> str:
    show_cost = any(
        row["_usage"][arm]["cost_status"] != "unavailable"
        for row in summary_rows
        for arm in ("B", "C")
    )

    def cost_columns(row: dict[str, Any]) -> str:
        return (
            f'<td data-num>{_fmt_cost(row["_usage"]["B"]["cost"], row["_usage"]["B"]["cost_status"])}'
            f'<span style="display:block;font-size:10.5px;color:var(--muted)">'
            f'{_text(row["_usage"]["B"]["cost_status"])}</span></td>'
            f'<td data-num>{_fmt_cost(row["_usage"]["C"]["cost"], row["_usage"]["C"]["cost_status"])}'
            f'<span style="display:block;font-size:10.5px;color:var(--muted)">'
            f'{_text(row["_usage"]["C"]["cost_status"])}</span></td>'
        ) if show_cost else ""

    body = "".join(
        "<tr>"
        f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
        f'font:500 12px/1.4 {MONO}">{_text(row["requested_model"])}</th>'
        f'<td data-num>{_fmt_int(row["_usage"]["B"]["tokens"])}<span style="display:block;'
        f'font-size:10.5px;color:var(--muted)">{_text(row["_usage"]["B"]["token_status"])}</span></td>'
        f'<td data-num>{_fmt_int(row["_usage"]["C"]["tokens"])}<span style="display:block;'
        f'font-size:10.5px;color:var(--muted)">{_text(row["_usage"]["C"]["token_status"])}</span></td>'
        f'{cost_columns(row)}'
        f'<td data-num>{row["_usage"]["B"]["agent_seconds"]:.1f}s / '
        f'{row["_usage"]["C"]["agent_seconds"]:.1f}s</td>'
        "</tr>"
        for row in sorted(summary_rows, key=lambda item: item["requested_model"])
    )
    return (
        f'<h2 style="margin:0 0 5px;font-family:{SERIF};font-weight:500;font-size:24px">'
        'Efficiency</h2>'
        + _table(
            "Incomplete totals are lower bounds.",
            '<th scope="col">Model</th><th scope="col" data-num>B tokens</th>'
            '<th scope="col" data-num>C tokens</th>'
            + (
                '<th scope="col" data-num>B cost</th><th scope="col" data-num>C cost</th>'
                if show_cost else ""
            )
            + '<th scope="col" data-num>Agent time B / C</th>',
            body,
        )
    )


def _reliability_station(summary_rows: list[dict[str, Any]]) -> str:
    body = "".join(
        "<tr>"
        f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
        f'font:500 12px/1.4 {MONO}">{_text(row["requested_model"])}</th>'
        f'<td data-num>{row["arms"]["B"]["correctness_observations"]} / '
        f'{row["arms"]["B"]["slots"]}</td>'
        f'<td data-num>{row["arms"]["C"]["correctness_observations"]} / '
        f'{row["arms"]["C"]["slots"]}</td>'
        f'<td data-num>{row["arms"]["B"]["infra_failures"]} / '
        f'{row["arms"]["C"]["infra_failures"]}</td>'
        f'<td data-num>{row["_usage"]["B"]["provider_attempts"]} / '
        f'{row["_usage"]["B"]["provider_responses"]}</td>'
        f'<td data-num>{row["_usage"]["C"]["provider_attempts"]} / '
        f'{row["_usage"]["C"]["provider_responses"]}</td>'
        f'<td data-num>{row["_usage"]["B"]["retries"]} / '
        f'{row["_usage"]["C"]["retries"]}</td></tr>'
        for row in sorted(summary_rows, key=lambda item: item["requested_model"])
    )
    return (
        f'<h2 style="margin:0 0 5px;font-family:{SERIF};font-weight:500;font-size:24px">'
        'Reliability</h2>'
        + _table(
            "Correctness observations, infrastructure failures and provider response health.",
            '<th scope="col">Model</th><th scope="col" data-num>B scored / slots</th>'
            '<th scope="col" data-num>C scored / slots</th>'
            '<th scope="col" data-num>Infra B / C</th>'
            '<th scope="col" data-num>B attempts / responses</th>'
            '<th scope="col" data-num>C attempts / responses</th>'
            '<th scope="col" data-num>Retries B / C</th>',
            body,
        )
    )


def _condition_station(summary_rows: list[dict[str, Any]]) -> str:
    ordered = sorted(summary_rows, key=lambda item: item["requested_model"])
    options = "".join(
        f'<option value="{_attr(row["model_variant_id"])}">'
        f'{_text(row["requested_model"])} / {_text(row["thinking_level"])}</option>'
        for row in ordered
    )
    panels = []
    for index, row in enumerate(ordered):
        lanes = []
        for arm in ("B", "C"):
            score = float(row["arms"][arm]["score_percent"] or 0)
            lanes.append(
                f'<div data-arm="{arm}" style="display:grid;grid-template-columns:24px '
                'minmax(150px,1fr) 82px;gap:12px;align-items:center;padding:8px 0">'
                f'<strong style="font:600 12px/1 {MONO}">{arm}</strong><div data-score-track>'
                f'<span data-score-fill style="width:{score:.3f}%"></span></div>'
                f'<span data-num style="font:600 11px/1 {MONO};text-align:right">'
                f'{score:.1f}%</span></div>'
            )
        panels.append(
            f'<div data-condition-panel="{_attr(row["model_variant_id"])}" '
            f'class="{"ladder-on" if index == 0 else ""}">'
            f'<p style="margin:0 0 5px;font:500 12px/1.4 {MONO}">'
            f'{_text(row["requested_model"])}</p>{"".join(lanes)}</div>'
        )
    return (
        f'<h2 style="margin:0 0 5px;font-family:{SERIF};font-weight:500;font-size:24px">'
        'Condition ladder</h2>'
        '<div data-ladder-scope><label style="display:flex;align-items:center;gap:10px;'
        f'font-size:11px;color:var(--muted)"><span>Model</span><select data-ladder-select>'
        f'{options}</select></label>{"".join(panels)}</div>'
    )


def _source_station(
    summary_rows: list[dict[str, Any]],
    source_digests: dict[str, str],
) -> str:
    body = "".join(
        '<article style="border-top:1px solid rgba(var(--ink-rgb),.32);padding:16px 0">'
        '<div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:18px">'
        f'<div><strong style="font:500 12.5px/1.4 {MONO}">'
        f'{_text(row["requested_model"])}</strong><span style="display:block;font-size:11px;'
        f'color:var(--muted);margin-top:4px">{_text(row["thinking_level"])} thinking</span></div>'
        f'<div style="text-align:right;font-size:11px;color:var(--muted)">Campaign '
        f'{_campaign_cell(row["_campaign_id"])}<br>Dataset '
        f'<code>{_text(_short(source_digests[row["_campaign_id"]], 14))}</code></div></div></article>'
        for row in sorted(summary_rows, key=lambda item: item["requested_model"])
    )
    return (
        f'<h2 style="margin:0 0 5px;font-family:{SERIF};font-weight:500;font-size:24px">'
        'Sources</h2>' + body
    )


def _overview(
    track: str,
    summaries: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    sources: list[tuple[dict[str, Any], str]],
    generated_at: str,
) -> str:
    source_digests = {
        document["campaign"]["campaign_id"]: digest for document, digest in sources
    }
    return (
        '<main>'
        + _spine(
            _identity_station(track, summaries, sources, generated_at)
            + _chart(summaries, track),
            number="00",
            first=True,
        )
        + _spine(_status_station(summaries), number="01")
        + _spine(_comparison_station(summaries), number="02")
        + _spine(_model_station(summaries), number="03")
        + _spine(_task_station(tasks), number="04")
        + _spine(_efficiency_station(summaries), number="05")
        + _spine(_reliability_station(summaries), number="06")
        + _spine(
            _condition_station(summaries)
            + '<div style="margin-top:34px;padding-top:30px;border-top:1px solid '
            'rgba(var(--ink-rgb),.14)">'
            + _source_station(summaries, source_digests)
            + "</div>",
            number="07",
            terminal=True,
        )
        + "</main>"
    )


def _model_table(
    summary_rows: list[dict[str, Any]],
    caption: str,
    *,
    hide_caption: bool = False,
) -> str:
    body = "".join(
        "<tr>"
        f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
        f'font:500 12px/1.4 {MONO}"><a href="#/models/'
        f'{_attr(row["model_variant_id"])}" data-nav="models">'
        f'{_text(row["requested_model"])}</a></th>'
        f'<td>{_text(row["thinking_level"])}</td><td>{_campaign_cell(row["_campaign_id"])}</td>'
        f'<td data-num>{row["arms"]["B"]["correctness_observations"]} / '
        f'{row["arms"]["C"]["correctness_observations"]}</td>'
        f'<td data-num>{row["matched"]["b_score_awarded"]} / '
        f'{row["matched"]["score_possible_per_arm"]}</td>'
        f'<td data-num>{row["matched"]["c_score_awarded"]} / '
        f'{row["matched"]["score_possible_per_arm"]}</td>'
        f'<td data-num style="font-weight:600;color:var(--accent)">'
        f'{_fmt_delta(row["matched"]["c_minus_b_score_percent"])}</td>'
        f'<td data-num>{_fmt_int(row["_usage"]["B"]["tokens"])}</td>'
        f'<td data-num>{_fmt_int(row["_usage"]["C"]["tokens"])}</td>'
        f'<td data-num>{row["arms"]["B"]["infra_failures"]} / '
        f'{row["arms"]["C"]["infra_failures"]}</td></tr>'
        for row in sorted(summary_rows, key=lambda item: item["requested_model"])
    )
    return _table(
        caption,
        '<th scope="col">Model</th><th scope="col">Thinking</th><th scope="col">Campaign</th>'
        '<th scope="col" data-num>Scored B / C</th><th scope="col" data-num>B points</th>'
        '<th scope="col" data-num>C points</th><th scope="col" data-num>C minus B</th>'
        '<th scope="col" data-num>B tokens</th><th scope="col" data-num>C tokens</th>'
        '<th scope="col" data-num>Infra B / C</th>',
        body,
        hide_caption=hide_caption,
    )


def _model_station(summary_rows: list[dict[str, Any]]) -> str:
    return (
        f'<h2 style="margin:0 0 5px;font-family:{SERIF};font-weight:500;font-size:24px">'
        'Model comparison</h2>'
        + _model_table(summary_rows, "Results by model.", hide_caption=True)
        + '<p style="margin:14px 0 0;font-size:12px"><a href="#/models" data-nav="models">'
        'Open model comparison →</a></p>'
    )


def _models_view(summary_rows: list[dict[str, Any]]) -> str:
    return _spine_page(
        f'<h1 style="margin:0 0 12px;{PAGE_HEADING}">Model comparison</h1>'
        f'<p style="margin:0 0 30px;{LEDE};max-width:40em">Scores, usage and run health '
        'by model and thinking level.</p>'
        + _model_table(summary_rows, "Campaign results by model.", hide_caption=True)
    )


def _tasks_view(task_rows: list[dict[str, Any]]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in task_rows:
        grouped.setdefault(row["task_id"], []).append(row)
    articles = []
    for task_id, rows in grouped.items():
        first = rows[0]
        outcomes = "".join(
            '<tr>'
            f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
            f'font:500 11.5px/1.4 {MONO}">{_text(row["requested_model"])}</th>'
            f'<td data-num>{row["matched"]["b_score_awarded"]} / '
            f'{row["matched"]["score_possible_per_arm"]}</td>'
            f'<td data-num>{row["matched"]["c_score_awarded"]} / '
            f'{row["matched"]["score_possible_per_arm"]}</td>'
            f'<td data-num>{_fmt_delta(row["matched"]["c_minus_b_score_percent"])}</td></tr>'
            for row in sorted(rows, key=lambda item: item["requested_model"])
        )
        budget = first["budget"]
        articles.append(
            '<article style="padding:24px 0;border-bottom:1px solid rgba(var(--ink-rgb),.14)">'
            '<div data-r="two" style="align-items:start"><div><div style="display:flex;'
            'flex-wrap:wrap;align-items:baseline;gap:12px;margin-bottom:8px">'
            f'<h2 style="margin:0;font:600 17px/1.25 {SANS}"><a href="#/tasks/'
            f'{_attr(task_id)}" data-nav="tasks">{_text(_task_name(task_id))}</a></h2>'
            f'<span style="font:400 11.5px/1 {MONO};color:var(--muted)">'
            f'{_text(task_id)}</span><span data-pill>{first["matched"]["score_possible_per_arm"]} '
            'points</span></div><p style="margin:0;font-size:12.5px;color:var(--muted)">'
            f'{budget["step_limit"]} steps / {budget["wall_time_limit_seconds"]}s / '
            f'{budget["provider_call_limit"]} provider calls</p></div><div>'
            + _table(
                "Matched outcomes by model.",
                '<th scope="col">Model</th><th scope="col" data-num>B</th>'
                '<th scope="col" data-num>C</th><th scope="col" data-num>C minus B</th>',
                outcomes,
                hide_caption=True,
            )
            + "</div></div></article>"
        )
    return _spine_page(
        f'<h1 style="margin:0 0 12px;{PAGE_HEADING}">Task suite</h1>'
        f'<p style="margin:0 0 26px;{LEDE};max-width:40em">Budgets and B/C outcomes by task.</p>'
        '<div style="border-top:1px solid rgba(var(--ink-rgb),.32)">'
        f'{"".join(articles)}</div>'
    )


def _filter_select(name: str, label: str, values: list[tuple[str, str]]) -> str:
    options = "".join(
        f'<option value="{_attr(value)}">{_text(display)}</option>' for value, display in values
    )
    return (
        '<label style="display:flex;flex-direction:column;gap:5px;font-size:11px;'
        'color:var(--muted)"><span style="font-weight:600;letter-spacing:0;'
        f'text-transform:uppercase">{_text(label)}</span><select data-run-filter="{_attr(name)}">'
        f'<option value="all">All</option>{options}</select></label>'
    )


def _runs_view(attempts: list[dict[str, Any]], profiles: dict[str, dict[str, Any]]) -> str:
    filters = (
        _filter_select(
            "model",
            "Model",
            sorted({(row["model_variant_id"], row["requested_model"]) for row in attempts}),
        )
        + _filter_select("arm", "Arm", [("B", "B"), ("C", "C")])
        + _filter_select(
            "outcome",
            "Outcome",
            sorted({(row["outcome"], _OUTCOME_LABELS[row["outcome"]]) for row in attempts}),
        )
        + _filter_select(
            "campaign",
            "Campaign",
            sorted({(row["_campaign_id"], row["_campaign_id"].removeprefix("campaign-")[:8]) for row in attempts}),
        )
    )
    body = "".join(
        f'<tr data-run-row data-track="{_attr(row["chain_track"])}" '
        f'data-model="{_attr(row["model_variant_id"])}" data-arm="{_attr(row["arm"])}" '
        f'data-outcome="{_attr(row["outcome"])}" data-campaign="{_attr(row["_campaign_id"])}">'
        f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
        f'font:400 10.5px/1.4 {MONO}" title="{_attr(row["attempt_id"])}">'
        f'<a href="#/runs/{_attr(row["attempt_id"])}" data-nav="runs">'
        f'{_text(_short(row["attempt_id"], 14))}</a></th><td>{_campaign_cell(row["_campaign_id"])}</td>'
        f'<td><a href="#/tasks/{_attr(row["task_id"])}" data-nav="tasks">'
        f'{_text(_task_name(row["task_id"]))}</a><span style="display:block;font:400 '
        f'10px/1.4 {MONO};color:var(--muted)">{_text(row["task_id"])}</span></td>'
        f'<td><a href="#/models/{_attr(row["model_variant_id"])}" data-nav="models">'
        f'{_text(row["requested_model"])}</a><span style="display:block;font-size:10.5px;'
        f'color:var(--muted)">{_text(profiles[row["model_variant_id"]]["thinking_level"])} thinking</span></td>'
        f'<td data-num>{_text(row["arm"])}</td><td data-outcome="{_attr(row["outcome"])}">'
        f'{_text(_OUTCOME_LABELS[row["outcome"]])}</td><td data-num>'
        f'{row["score_awarded"]} / {row["max_score"]}</td><td>{_text(_diagnostic(row))}</td>'
        f'<td data-num>{_fmt_int(row["usage"]["total_tokens"])}<span style="display:block;'
        f'font-size:10.5px;color:var(--muted)">{_text(row["usage"]["token_usage_status"])}</span></td>'
        f'<td data-num>{sum(float(value) for key, value in row["timings"].items() if key != "measurement_status"):.1f}s</td>'
        f'<td data-num>{row["retry_ordinal"]}</td></tr>'
        for row in attempts
    )
    return _spine_page(
        f'<h1 style="margin:0 0 12px;{PAGE_HEADING}">Run explorer</h1>'
        f'<p style="margin:0 0 26px;{LEDE};max-width:40em">Task attempts, including '
        'infrastructure failures.</p><div data-r="noprint" style="display:flex;'
        'flex-wrap:wrap;gap:14px 18px;align-items:flex-end;padding:16px 0;border-top:1px solid '
        f'rgba(var(--ink-rgb),.14);border-bottom:1px solid rgba(var(--ink-rgb),.14)">{filters}'
        '<button type="button" data-run-clear style="border:1px solid rgba(var(--ink-rgb),.22);'
        'padding:0 13px;min-height:40px;border-radius:2px;font-size:12px">Clear filters</button>'
        f'<p aria-live="polite" data-run-count style="margin:0 0 0 auto;font-size:12.5px;'
        f'color:var(--muted)">{len(attempts)} visible task attempts</p></div>'
        + _table(
            "Task attempts.",
            '<th scope="col">Attempt</th><th scope="col">Campaign</th><th scope="col">Task</th>'
            '<th scope="col">Model</th><th scope="col" data-num>Arm</th><th scope="col">Outcome</th>'
            '<th scope="col" data-num>Score</th><th scope="col">Verifier criteria</th>'
            '<th scope="col" data-num>Tokens</th><th scope="col" data-num>Measured time</th>'
            '<th scope="col" data-num>Retry</th>',
            body,
            hide_caption=True,
        )
    )


def _diagnostic(row: dict[str, Any]) -> str:
    value = row["verification_diagnostics"]
    if value["status"] == "unavailable":
        return "Unavailable"
    if value["status"] == "not_evaluated":
        return f"0 / {value['criteria_total']} evaluated"
    return f"{value['criteria_passed']} passed, {value['criteria_failed']} failed"


def _breadcrumb(parent: str, route: str, current: str) -> str:
    return (
        '<p style="margin:0 0 14px;font-size:12px;color:var(--muted)">'
        f'<a href="#/{route}" data-nav="{route}">{_text(parent)}</a> '
        '<span style="color:var(--faint-2)">/</span> '
        f'<span style="font-family:{MONO};color:var(--ink)">{_text(current)}</span></p>'
    )


def _model_detail_views(
    summary_rows: list[dict[str, Any]],
    task_rows: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
) -> str:
    pages = []
    for row in sorted(summary_rows, key=lambda item: item["requested_model"]):
        variant = row["model_variant_id"]
        track = row["chain_track"]
        selected_tasks = [item for item in task_rows if item["model_variant_id"] == variant]
        selected_attempts = [item for item in attempts if item["model_variant_id"] == variant]
        available = _comparison_available(row)
        status = "Comparison available" if available else "Comparison withheld"
        tone = "var(--pos)" if available else "var(--caution)"
        head = (
            _breadcrumb("Models", "models", _variant_label(row))
            + '<div data-r="two" style="align-items:start"><div>'
            f'<h1 style="margin:0 0 10px;font:500 32px/1.15 {MONO};overflow-wrap:anywhere">'
            f'{_text(row["requested_model"])}</h1><p style="margin:0 0 14px;font-size:13px;'
            f'color:{tone}">{_text(status)}</p><p style="margin:0;font-size:13.5px;'
            f'color:var(--ink-2)">{_text(_track_label(track))} · '
            f'{_text(row["thinking_level"])} thinking · {_text(len(selected_attempts))} '
            'task attempts</p></div><div style="border-left:1px solid rgba(var(--ink-rgb),.18);'
            f'padding-left:24px"><h2 style="margin:0 0 12px;{EYEBROW_STYLE}">Model profile</h2>'
            '<dl style="margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);'
            f'gap:7px 16px;font-size:12px"><dt style="color:var(--muted)">Variant</dt><dd '
            f'style="margin:0;font-family:{MONO};overflow-wrap:anywhere">{_text(variant)}</dd>'
            '<dt style="color:var(--muted)">Profile</dt><dd style="margin:0;font-family:'
            f'{MONO}">{_text(row["model_profile_id"])}</dd><dt style="color:var(--muted)">'
            f'Digest</dt><dd style="margin:0;font-family:{MONO};overflow-wrap:anywhere">'
            f'{_text(row["model_profile_sha256"])}</dd><dt style="color:var(--muted)">Campaign'
            f'</dt><dd style="margin:0">{_campaign_cell(row["_campaign_id"])}</dd></dl></div></div>'
        )
        metrics = []
        for metric, label, unit, better in _COMPARISON_METRICS:
            b_value = _metric_value(row, "B", metric)
            c_value = _metric_value(row, "C", metric)
            delta = c_value - b_value
            exact = available if metric == "score" else all(
                row["_usage"][arm]["token_status"] == "complete" for arm in ("B", "C")
            ) if metric == "tokens" else True
            metrics.append(
                '<tr>'
                f'<th scope="row" style="background:none;text-transform:none">{_text(label)}'
                f'<span style="display:block;font-size:10.5px;color:var(--muted)">{_text(unit)}; '
                f'{_text(better)} is better</span></th><td data-num>'
                f'{_text(_metric_display(b_value, metric))}</td><td data-num>'
                f'{_text(_metric_display(c_value, metric))}</td><td data-num style="font-weight:600;'
                f'color:var(--accent)">{"+" if delta >= 0 else "-"}'
                f'{_text(_metric_display(abs(delta), metric))}</td><td>'
                f'{"Exact" if exact else "Observed lower bound"}</td></tr>'
            )
        task_body = "".join(
            '<tr>'
            f'<th scope="row" style="background:none;text-transform:none"><a href="#/tasks/'
            f'{_attr(item["task_id"])}" data-nav="tasks">{_text(_task_name(item["task_id"]))}'
            f'</a><span style="display:block;font:400 10.5px/1.4 {MONO};color:var(--muted)">'
            f'{_text(item["task_id"])}</span></th><td data-num>'
            f'{item["matched"]["b_score_awarded"]} / '
            f'{item["matched"]["score_possible_per_arm"]}</td><td data-num>'
            f'{item["matched"]["c_score_awarded"]} / '
            f'{item["matched"]["score_possible_per_arm"]}</td><td data-num>'
            f'{_fmt_delta(item["matched"]["c_minus_b_score_percent"])}</td></tr>'
            for item in sorted(selected_tasks, key=lambda value: value["task_id"])
        )
        run_body = "".join(
            '<tr>'
            f'<th scope="row" style="background:none;text-transform:none;font:400 11px/1.4 '
            f'{MONO}"><a href="#/runs/{_attr(item["attempt_id"])}" data-nav="runs">'
            f'{_text(_short(item["attempt_id"], 14))}</a></th><td>'
            f'{_text(_task_name(item["task_id"]))}</td><td data-num>{_text(item["arm"])}</td>'
            f'<td data-outcome="{_attr(item["outcome"])}">'
            f'{_text(_OUTCOME_LABELS[item["outcome"]])}</td><td data-num>'
            f'{item["score_awarded"]} / {item["max_score"]}</td><td data-num>'
            f'{_fmt_int(item["usage"]["total_tokens"])}</td></tr>'
            for item in selected_attempts
        )
        pages.append(
            f'<main data-detail="{_attr(variant)}" data-track-context="{_attr(track)}" hidden>'
            + _spine(head, first=True)
            + _spine(
                f'<h2 style="margin:0 0 14px;{H2_SMALL}">B versus C</h2>'
                + _table(
                    "Primary measures for this model variant.",
                    '<th scope="col">Measure</th><th scope="col" data-num>B</th>'
                    '<th scope="col" data-num>C</th><th scope="col" data-num>C minus B</th>'
                    '<th scope="col">Basis</th>',
                    "".join(metrics),
                    hide_caption=True,
                )
            )
            + _spine(
                f'<h2 style="margin:0 0 14px;{H2_SMALL}">Task outcomes</h2>'
                + _table(
                    "Awarded points by task.",
                    '<th scope="col">Task</th><th scope="col" data-num>B</th>'
                    '<th scope="col" data-num>C</th><th scope="col" data-num>C minus B</th>',
                    task_body,
                )
            )
            + _spine(
                f'<h2 style="margin:0 0 14px;{H2_SMALL}">Task attempts</h2>'
                + _table(
                    "Attempts for this model.",
                    '<th scope="col">Attempt</th><th scope="col">Task</th>'
                    '<th scope="col" data-num>Arm</th><th scope="col">Outcome</th>'
                    '<th scope="col" data-num>Score</th><th scope="col" data-num>Tokens</th>',
                    run_body,
                    hide_caption=True,
                ),
                terminal=True,
            )
            + "</main>"
        )
    return "".join(pages)


def _task_detail_views(
    task_rows: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    *,
    context: str | None = None,
) -> str:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in task_rows:
        grouped.setdefault(row["task_id"], []).append(row)
    pages = []
    for task_id, rows in sorted(grouped.items()):
        first = rows[0]
        track = first["chain_track"]
        copy = TASK_COPY.get(task_id, {})
        budget = first["budget"]
        facts = (
            ("Category", copy.get("category", "n/a")),
            ("Type", copy.get("kind", "n/a")),
            ("Fresh state", copy.get("fresh", "n/a")),
            ("Required proof", copy.get("proof", "n/a")),
            ("Verification", copy.get("verify", "n/a")),
        )
        facts_html = "".join(
            f'<dt style="color:var(--muted)">{_text(label)}</dt><dd style="margin:0;'
            f'color:var(--ink-2)">{_text(value)}</dd>' for label, value in facts
        )
        model_rows = "".join(
            '<tr>'
            f'<th scope="row" style="background:none;text-transform:none;font:500 11.5px/1.4 '
            f'{MONO}"><a href="#/models/{_attr(row["model_variant_id"])}" data-nav="models">'
            f'{_text(row["requested_model"])}</a></th><td data-num>'
            f'{row["matched"]["b_score_awarded"]} / '
            f'{row["matched"]["score_possible_per_arm"]}</td><td data-num>'
            f'{row["matched"]["c_score_awarded"]} / '
            f'{row["matched"]["score_possible_per_arm"]}</td><td data-num>'
            f'{_fmt_delta(row["matched"]["c_minus_b_score_percent"])}</td></tr>'
            for row in sorted(rows, key=lambda value: value["requested_model"])
        )
        ids = {attempt_id for row in rows for attempt_id in row["attempt_ids"]}
        selected_attempts = [row for row in attempts if row["attempt_id"] in ids]
        attempt_rows = "".join(
            '<tr>'
            f'<th scope="row" style="background:none;text-transform:none;font:400 11px/1.4 '
            f'{MONO}"><a href="#/runs/{_attr(row["attempt_id"])}" data-nav="runs">'
            f'{_text(_short(row["attempt_id"], 14))}</a></th><td>'
            f'{_text(row["requested_model"])}</td><td data-num>{_text(row["arm"])}</td>'
            f'<td data-outcome="{_attr(row["outcome"])}">'
            f'{_text(_OUTCOME_LABELS[row["outcome"]])}</td><td data-num>'
            f'{row["score_awarded"]} / {row["max_score"]}</td><td>{_text(row["grade_reason"])}</td>'
            '</tr>'
            for row in selected_attempts
        )
        head = (
            _breadcrumb("Tasks", "tasks", task_id)
            + '<div data-r="two" style="align-items:start"><div>'
            f'<h1 style="margin:0 0 10px;font-family:{SERIF};font-weight:500;font-size:34px">'
            f'{_text(_task_name(task_id))}</h1><p style="margin:0 0 16px;font:400 12px/1.4 '
            f'{MONO};color:var(--muted)">{_text(task_id)} · {_text(_track_label(track))}</p>'
            f'<p style="margin:0;font-family:{SERIF};font-size:17px;line-height:1.55;max-width:38em">'
            f'{_text(copy.get("objective", ""))}</p></div><div style="border-left:1px solid '
            'rgba(var(--ink-rgb),.18);padding-left:24px"><div style="display:flex;align-items:'
            f'baseline;gap:8px"><strong style="font:600 44px/1 {SANS}">{first["matched"]["score_possible_per_arm"]}'
            '</strong><span style="color:var(--muted)">points</span></div><p style="margin:14px 0 0;'
            f'font-size:12px;color:var(--muted)">{budget["step_limit"]} steps · '
            f'{budget["wall_time_limit_seconds"]}s · {budget["provider_call_limit"]} provider calls'
            '</p></div></div>'
        )
        pages.append(
            f'<main data-detail="{_attr(task_id)}" data-track-context="{_attr(context or track)}" hidden>'
            + _spine(head, first=True)
            + _spine(
                f'<h2 style="margin:0 0 14px;{H2_SMALL}">Task contract</h2><dl style="margin:0;'
                'display:grid;grid-template-columns:minmax(110px,auto) minmax(0,1fr);gap:10px '
                f'20px;font-size:13px;max-width:64em">{facts_html}</dl>'
            )
            + _spine(
                f'<h2 style="margin:0 0 14px;{H2_SMALL}">B versus C by model</h2>'
                + _table(
                    "Awarded points for this task.",
                    '<th scope="col">Model</th><th scope="col" data-num>B</th>'
                    '<th scope="col" data-num>C</th><th scope="col" data-num>C minus B</th>',
                    model_rows,
                )
            )
            + _spine(
                f'<h2 style="margin:0 0 14px;{H2_SMALL}">Attempt outcomes</h2>'
                + _table(
                    "Verifier results.",
                    '<th scope="col">Attempt</th><th scope="col">Model</th>'
                    '<th scope="col" data-num>Arm</th><th scope="col">Outcome</th>'
                    '<th scope="col" data-num>Score</th><th scope="col">Verifier result</th>',
                    attempt_rows,
                ),
                terminal=True,
            )
            + "</main>"
        )
    return "".join(pages)


def _run_detail_views(attempts: list[dict[str, Any]]) -> str:
    pages = []
    for row in attempts:
        usage = row["usage"]
        track = row["chain_track"]
        identity = (
            ("Campaign", row["_campaign_id"]),
            ("Task", row["task_id"]),
            ("Arm", row["arm"]),
            ("Model", row["requested_model"]),
            ("Thinking", row["thinking_level"]),
            ("Model profile", row["model_profile_id"]),
            ("Chain profile", row["chain_profile_id"]),
            ("Treatment profile", row["treatment_profile_id"]),
        )
        identity_html = "".join(
            f'<dt style="color:var(--muted)">{_text(label)}</dt><dd style="margin:0;font:'
            f'400 11.5px/1.45 {MONO};overflow-wrap:anywhere">{_text(value)}</dd>'
            for label, value in identity
        )
        usage_rows = (
            ("Model calls", usage["model_calls"]),
            ("Provider attempts / responses", f'{usage["provider_attempts"]} / {usage["provider_responses"]}'),
            ("Provider retries", usage["provider_retry_count"]),
            ("Prompt tokens", _fmt_int(usage["prompt_tokens"])),
            ("Completion tokens", _fmt_int(usage["completion_tokens"])),
            ("Total tokens", _fmt_int(usage["total_tokens"])),
            ("Token status", usage["token_usage_status"]),
            ("Reported cost", _fmt_cost(usage["provider_reported_cost_usd"], usage["cost_status"])),
        )
        timing_rows = tuple(
            (key.replace("_", " ").title(), f"{float(value):.3f}s")
            for key, value in row["timings"].items()
            if key != "measurement_status"
        )
        operational = (
            ("Preflight", row["preflight_status"]),
            ("Cleanup", f'{row["cleanup_status"]} ({row["cleanup_receipt_count"]} receipt)'),
            ("Controller requests", f'{row["controller_request_count"]} ({row["controller_request_count_status"]})'),
            ("Retry ordinal", row["retry_ordinal"]),
            ("Agent exit", row["agent_exit_status"]),
            ("Failure", row["failure_category"] or "none"),
        )
        usage_html = "".join(
            '<div style="display:flex;justify-content:space-between;gap:20px;padding:9px 0;'
            'border-bottom:1px solid rgba(var(--ink-rgb),.10)">'
            f'<dt style="color:var(--ink-2)">{_text(label)}</dt><dd style="margin:0;font:'
            f'400 11.5px/1.4 {MONO};text-align:right">{_text(value)}</dd></div>'
            for label, value in usage_rows + timing_rows
        )
        operational_html = "".join(
            f'<dt style="color:var(--muted)">{_text(label)}</dt><dd style="margin:0">'
            f'{_text(value)}</dd>' for label, value in operational
        )
        outcome = _OUTCOME_LABELS[row["outcome"]]
        head = (
            _breadcrumb("Runs", "runs", "attempt detail")
            + '<div data-r="two" style="align-items:start"><div style="min-width:0">'
            f'<h1 style="margin:0 0 12px;font:500 23px/1.35 {MONO};overflow-wrap:anywhere">'
            f'{_text(row["attempt_id"])}</h1><p data-outcome="{_attr(row["outcome"])}" '
            f'style="margin:0 0 12px;font-weight:600">{_text(outcome)}</p><p style="margin:0;'
            f'font-size:13.5px;color:var(--ink-2)">{_text(_track_label(track))} · '
            f'{_text(_task_name(row["task_id"]))} · arm {_text(row["arm"])}</p></div>'
            '<div style="border-left:1px solid rgba(var(--ink-rgb),.18);padding-left:24px">'
            f'<span style="font:600 42px/1 {SANS}">{row["score_awarded"]}</span> '
            f'<span style="color:var(--muted)">of {row["max_score"]} points</span><p style="'
            f'margin:13px 0 0;color:var(--ink-2)">{_text(row["grade_reason"])}</p></div></div>'
        )
        pages.append(
            f'<main data-detail="{_attr(row["attempt_id"])}" data-track-context="{_attr(track)}" hidden>'
            + _spine(head, first=True)
            + _spine(
                '<div data-r="split" style="gap:38px;align-items:start"><div><h2 style="'
                f'margin:0 0 14px;{H2_SMALL}">Run identity</h2><dl style="margin:0;display:'
                f'grid;grid-template-columns:auto minmax(0,1fr);gap:8px 16px">{identity_html}</dl>'
                f'</div><div><h2 style="margin:0 0 14px;{H2_SMALL}">Run state</h2><dl '
                'style="margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px '
                f'16px;font-size:12.5px">{operational_html}</dl></div></div>'
            )
            + _spine(
                '<div data-r="split" style="gap:38px;align-items:start"><div><h2 style="'
                f'margin:0 0 14px;{H2_SMALL}">Usage and time</h2><dl style="margin:0;border-top:'
                f'1px solid rgba(var(--ink-rgb),.32)">{usage_html}</dl></div><div><h2 style="'
                f'margin:0 0 14px;{H2_SMALL}">Artifact digest</h2><code style="'
                f'overflow-wrap:anywhere">{_text(row["artifact_reference_sha256"])}</code></div></div>',
                terminal=True,
            )
            + "</main>"
        )
    return "".join(pages)


def _methodology_view(methodology: dict[str, str]) -> str:
    labels = {
        "accepted_evidence": "Which runs are included?",
        "selection": "How are campaigns selected?",
        "comparison": "How are B and C compared?",
        "correctness": "What counts toward the score?",
        "diagnostics": "Can a task earn partial credit?",
        "acquisition_usage": "How are retries counted?",
        "health": "How are failures reported?",
    }
    details = "".join(
        '<details data-methodology-details style="border-bottom:1px solid rgba(var(--ink-rgb),.14)">'
        '<summary style="display:flex;align-items:baseline;gap:12px;padding:14px 0;'
        f'font:500 14.5px/1.4 {SANS};color:var(--ink)"><span data-details-glyph '
        f'aria-hidden="true" style="width:12px;font-family:{MONO};font-size:12px;'
        'color:var(--caution)"></span>'
        f'<span>{_text(labels[key])}</span></summary><p style="margin:0 0 18px 24px;'
        f'font-size:13.5px;line-height:1.7;color:var(--ink-2);max-width:44em">'
        f'{_text(methodology[key])}</p></details>'
        for key in labels
    )
    condition_rows = "".join(
        '<tr>'
        f'<th scope="row" style="background:none;text-transform:none;letter-spacing:0;'
        f'font:600 15px/1.2 {SANS};width:64px">{arm}</th>'
        f'<td>{_text("Off" if arm == "B" else "Enabled")}</td>'
        '<td>Allowed</td>'
        f'<td>{_text("Baseline" if arm == "B" else "Treatment")}</td></tr>'
        for arm in ("B", "C")
    )
    return (
        '<main>'
        + _spine(
            f'<h1 style="margin:0 0 12px;{PAGE_HEADING}">Methodology</h1>'
            f'<p style="margin:0 0 30px;{LEDE};max-width:38em">Scoring and inclusion rules.</p>'
            + _table(
                "Matched conditions.",
                '<th scope="col">Arm</th><th scope="col">CKB AI</th>'
                '<th scope="col">Web research</th><th scope="col">Role</th>',
                condition_rows,
            ),
            first=True,
        )
        + _spine(
            f'<h2 style="margin:0 0 18px;{H2_SMALL}">Reporting rules</h2>'
            f'<div style="border-top:1px solid rgba(var(--ink-rgb),.32);max-width:60em">'
            f"{details}</div>",
            terminal=True,
        )
        + "</main>"
    )


def _provenance_view(
    sources: list[tuple[dict[str, Any], str]],
    publication_dataset_sha256: str | None,
) -> str:
    articles = []
    for document, digest in sources:
        campaign = document["campaign"]
        profiles = document["profiles"]["model_variants"]
        rows = [
            ("Campaign", campaign["campaign_id"]),
            ("Manifest", campaign["manifest_sha256"]),
            ("Resolution", document["resolution"]["sha256"]),
            ("Source dataset", digest),
        ]
        for profile in profiles:
            rows.extend((
                ("Model variant", profile["model_variant_id"]),
                ("Model profile", profile["model_profile_id"]),
                ("Profile digest", profile["model_profile_sha256"]),
            ))
        entries = "".join(
            f'<dt style="color:var(--muted)">{_text(label)}</dt><dd style="margin:0;'
            f'font-family:{MONO};font-size:11.5px;overflow-wrap:anywhere">{_text(value)}</dd>'
            for label, value in rows
        )
        articles.append(
            '<article style="padding:22px 0;border-bottom:1px solid rgba(var(--ink-rgb),.14)">'
            '<div style="display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;margin-bottom:14px">'
            f'<h2 style="margin:0;font:500 17px/1.2 {MONO}">'
            f'{_text(", ".join(profile["requested_model"] for profile in profiles))}</h2>'
            f'<span data-pill>{len(profiles)} '
            f'{"variant" if len(profiles) == 1 else "variants"}</span></div>'
            '<dl style="margin:0;display:grid;grid-template-columns:minmax(140px,auto) '
            f'minmax(0,1fr);gap:8px 20px;font-size:12.5px">{entries}</dl></article>'
        )
    first = sources[0][0]
    campaign = first["campaign"]
    common = [
        ("Suite", campaign["suite_semver"]),
        ("Suite freeze", campaign["suite_freeze_sha256"]),
        ("Execution revision", campaign["execution_source"]["repository_revision"]),
        ("Execution tree", campaign["execution_source"]["source_tree_sha256"]),
        ("Toolchain", campaign["execution_source"]["toolchain_sha256"]),
        ("Agent image", campaign["execution_source"]["agent_image_digest"]),
        ("Verifier image", campaign["execution_source"]["verifier_image_digest"]),
        ("Concurrency", campaign["concurrency_contract"]),
        (
            "Retry policy",
            f'{campaign["retry_policy_id"]}@{campaign["retry_policy_sha256"]}',
        ),
        (
            "Stopping rule",
            f'{campaign["stopping_rule_id"]}@{campaign["stopping_rule_sha256"]}',
        ),
        ("Report revision", first["report_builder"]["repository_revision"]),
        ("Report tree", first["report_builder"]["source_tree_sha256"]),
        ("Publication dataset", publication_dataset_sha256 or "single campaign"),
    ]
    common.extend(
        (
            "Chain profile",
            f'{row["profile"]["profile_id"]}@{row["sha256"]}',
        )
        for row in first["profiles"]["chain_profiles"]
    )
    common.extend(
        (
            "Treatment profile",
            f'{row["profile"]["profile_id"]}@{row["sha256"]}',
        )
        for row in first["profiles"]["treatment_profiles"]
    )
    common_rows = "".join(
        '<div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:14px;'
        'padding:11px 0;border-bottom:1px solid rgba(var(--ink-rgb),.10)">'
        f'<dt style="font-size:13px;color:var(--ink-2)">{_text(label)}</dt>'
        f'<dd style="margin:0;font:11px/1.5 {MONO};overflow-wrap:anywhere;text-align:right;'
        f'max-width:68%">{_text(value)}</dd></div>'
        for label, value in common
    )
    return (
        '<main>'
        + _spine(
            f'<h1 style="margin:0 0 12px;{PAGE_HEADING}">Provenance</h1>'
            f'<p style="margin:0 0 26px;{LEDE};max-width:38em">Campaign and source identities.</p>'
            f'<div style="border-top:1px solid '
            f'rgba(var(--ink-rgb),.32)">{"".join(articles)}</div>',
            first=True,
        )
        + _spine(
            f'<h2 style="margin:0 0 14px;{H2_SMALL}">Execution</h2>'
            f'<dl style="margin:0;border-top:1px solid rgba(var(--ink-rgb),.32);max-width:68em">'
            f"{common_rows}</dl>",
            terminal=True,
        )
        + "</main>"
    )


def render_report_site(
    sources: list[tuple[dict[str, Any], str]],
    *,
    publication_dataset_sha256: str | None = None,
) -> bytes:
    """Render validated campaign datasets in the established six-view report layout."""
    if not sources:
        raise ValueError("report rendering needs at least one campaign dataset")
    summaries, acquisitions = _summary_rows(sources)
    tasks = _annotated_rows(sources, "task_comparisons")
    attempts = _annotated_rows(sources, "attempts")
    profiles = {
        row["model_variant_id"]: row
        for document, _digest in sources
        for row in document["profiles"]["model_variants"]
    }
    tracks = sorted(
        {row["chain_track"] for row in summaries},
        key=lambda value: (_TRACK_ORDER.get(value, 99), value),
    )
    scopes = ["all", *tracks]
    summaries_by_scope = {
        "all": _combined_summary_rows(summaries, acquisitions),
        **{
            track: [row for row in summaries if row["chain_track"] == track]
            for track in tracks
        },
    }
    tasks_by_scope = {
        "all": tasks,
        **{
            track: [row for row in tasks if row["chain_track"] == track]
            for track in tracks
        },
    }
    attempts_by_scope = {
        "all": attempts,
        **{
            track: [row for row in attempts if row["chain_track"] == track]
            for track in tracks
        },
    }
    generated_at = max(row["result_created_utc"] for row in attempts)
    counts = {
        scope: len(attempts_by_scope[scope]) for scope in scopes
    }

    def panels(
        builder: Any,
        rows_by_scope: dict[str, list[dict[str, Any]]],
    ) -> str:
        return "".join(
            f'<div data-track-panel="{_attr(scope)}" class="{"track-on" if index == 0 else ""}">'
            f'{builder(rows_by_scope[scope])}</div>'
            for index, scope in enumerate(scopes)
        )

    overview_parts = []
    for index, scope in enumerate(scopes):
        selected = "track-on" if index == 0 else ""
        overview_parts.append(
            f'<div data-track-panel="{_attr(scope)}" class="{selected}">'
            f"{_overview(scope, summaries_by_scope[scope], tasks_by_scope[scope], sources, generated_at)}</div>"
        )
    overview = "".join(overview_parts)
    model_details = []
    task_details = []
    for index, scope in enumerate(scopes):
        selected = "track-on" if index == 0 else ""
        model_details.append(
            f'<div data-track-panel="{_attr(scope)}" class="{selected}">'
            f'{_model_detail_views(summaries_by_scope[scope], tasks_by_scope[scope], attempts_by_scope[scope])}</div>'
        )
        task_details.append(
            f'<div data-track-panel="{_attr(scope)}" class="{selected}">'
            f'{_task_detail_views(tasks_by_scope[scope], attempts_by_scope[scope], context=scope)}</div>'
        )
    views = {
        "overview": overview,
        "models": panels(_models_view, summaries_by_scope),
        "model": "".join(model_details),
        "tasks": panels(_tasks_view, tasks_by_scope),
        "task": "".join(task_details),
        "runs": _runs_view(attempts, profiles),
        "run": _run_detail_views(attempts),
        "methodology": _methodology_view(sources[0][0]["methodology"]),
        "provenance": _provenance_view(sources, publication_dataset_sha256),
    }
    route_order = ["overview", *(route for route, _label in _NAV), "model", "task", "run"]
    body = "".join(
        f'<div data-report-view="{route}" class="{"is-active" if route == "overview" else ""}">'
        f'{views[route]}</div>'
        for route in route_order
    )
    document = (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><title>CKB AI Bench</title>'
        f"<style>{STYLE}{_EXTRA_STYLE}</style></head><body data-theme=\"light\">"
        '<div style="min-height:100vh;background:var(--bg)">'
        + _header()
        + '<div data-r="pad" style="max-width:1320px;margin:0 auto;padding:0 34px">'
        + _track_selector(
            scopes,
            counts,
            campaign_count=len(sources),
            model_count=len(profiles),
        )
        + body
        + "</div></div>"
        + f"<script>{_SCRIPT}</script></body></html>\n"
    )
    return document.encode("utf-8")
