// Spike (NOT production): renderer for the ADR-0011 ladder chart. Reads the
// SYNTHETIC dataset, computes the C-B headline per model via the pure-logic
// module, and writes a SELF-CONTAINED ladder-chart.html: inline SVG, no network,
// no build step, no npm install. Opening the file in any browser shows the chart.
//
// Layout (ADR-0011): X = arm ladder A->B->C->D, Y = Pass@1 [0,1], one polyline
// per model colored by family, a CI band on every point, the B->C segment
// emphasised (it literally is the MCP's marginal value), a chain TOGGLE that
// switches between two separately-rendered chart groups (devnet/testnet are NEVER
// co-plotted), and a headline table of C-B deltas with propagated CIs.

'use strict';

const fs = require('fs');
const path = require('path');
const metrics = require('./ladder-metrics');

const DATA_PATH = path.join(__dirname, 'synthetic-results.json');
const OUT_PATH = path.join(__dirname, 'ladder-chart.html');

const ARMS = ['A', 'B', 'C', 'D'];
const ARM_LABELS = {
  A: 'A · floor',
  B: 'B · web',
  C: 'C · MCP+web',
  D: 'D · MCP-only',
};

// Family -> color. Distinct per family (ADR-0011: color by family). Models within
// a family get the same hue at different lightness so individual lines stay
// distinguishable while the family is still readable as a color group.
const FAMILY_HUE = { Anthropic: 24, xAI: 210, OpenAI: 145 };

function modelColor(family, idxInFamily, familySize) {
  const hue = FAMILY_HUE[family] != null ? FAMILY_HUE[family] : 280;
  // spread lightness 38%..62% across the family's members
  const span = familySize > 1 ? 24 : 0;
  const light = 38 + (familySize > 1 ? (idxInFamily / (familySize - 1)) * span : 12);
  return `hsl(${hue} 70% ${light}%)`;
}

// --- SVG geometry ---
const W = 720;
const H = 440;
const M = { top: 28, right: 220, bottom: 52, left: 56 };
const plotW = W - M.left - M.right;
const plotH = H - M.top - M.bottom;

const xOf = (armIdx) => M.left + (plotW * armIdx) / (ARMS.length - 1);
const yOf = (score) => M.top + plotH * (1 - score); // score in [0,1]

function fmt(x) {
  return (x >= 0 ? '+' : '') + x.toFixed(2);
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Render one chain's chart group (axes + every model line + CI bands + headline
// legend). Returns an SVG <g> string. devnet and testnet each get their own
// group; the toggle shows exactly one. They are NEVER drawn on the same axis.
function renderChainGroup(dataset, chain, visible) {
  const lines = metrics.lineSeriesForChain(dataset, chain);

  // assign a color per model, grouped by family
  const families = {};
  for (const l of lines) (families[l.family] = families[l.family] || []).push(l);
  const colorByModel = {};
  for (const fam of Object.keys(families)) {
    families[fam].forEach((l, i) => {
      colorByModel[l.model] = modelColor(fam, i, families[fam].length);
    });
  }

  const parts = [];
  parts.push(
    `<g class="chart" data-chain="${esc(chain)}" style="display:${visible ? 'block' : 'none'}">`
  );

  // --- axes & gridlines ---
  // Y gridlines at 0,0.25,0.5,0.75,1
  for (const gy of [0, 0.25, 0.5, 0.75, 1]) {
    const y = yOf(gy);
    parts.push(
      `<line class="grid" x1="${M.left}" y1="${y.toFixed(1)}" x2="${(M.left + plotW).toFixed(1)}" y2="${y.toFixed(1)}"/>`
    );
    parts.push(
      `<text class="axis-label y" x="${M.left - 8}" y="${(y + 4).toFixed(1)}">${gy.toFixed(2)}</text>`
    );
  }
  // X arm ticks
  ARMS.forEach((arm, i) => {
    const x = xOf(i);
    parts.push(
      `<line class="tick" x1="${x.toFixed(1)}" y1="${M.top}" x2="${x.toFixed(1)}" y2="${(M.top + plotH).toFixed(1)}"/>`
    );
    parts.push(
      `<text class="axis-label x" x="${x.toFixed(1)}" y="${(M.top + plotH + 20).toFixed(1)}">${esc(ARM_LABELS[arm])}</text>`
    );
  });
  // Y axis title
  parts.push(
    `<text class="axis-title" transform="translate(16 ${(M.top + plotH / 2).toFixed(1)}) rotate(-90)">Pass@1</text>`
  );

  // --- per model: CI band path + polyline + B->C emphasis ---
  for (const line of lines) {
    const color = colorByModel[line.model];
    // collect points in arm order; skip arms with no cell (defensive)
    const pts = ARMS.map((arm, i) => {
      const p = line.points[arm];
      if (!p) return null;
      return { i, arm, x: xOf(i), mean: p.mean, low: p.ci_low, high: p.ci_high };
    }).filter(Boolean);

    const safe = line.model.replace(/[^A-Za-z0-9]+/g, '-');

    // CI band: a filled polygon along high then back along low (a band per line).
    const upper = pts.map((p) => `${p.x.toFixed(1)},${yOf(p.high).toFixed(1)}`);
    const lower = pts.slice().reverse().map((p) => `${p.x.toFixed(1)},${yOf(p.low).toFixed(1)}`);
    parts.push(
      `<polygon class="ci-band" data-model="${esc(line.model)}" points="${upper.concat(lower).join(' ')}" fill="${color}" fill-opacity="0.12" stroke="none"/>`
    );

    // the model line (mean polyline)
    const poly = pts.map((p) => `${p.x.toFixed(1)},${yOf(p.mean).toFixed(1)}`).join(' ');
    parts.push(
      `<polyline class="model-line" data-model="${esc(line.model)}" data-family="${esc(line.family)}" points="${poly}" fill="none" stroke="${color}" stroke-width="2.2"/>`
    );

    // emphasise the B->C segment (the headline) with a thicker overlay
    const b = pts.find((p) => p.arm === 'B');
    const c = pts.find((p) => p.arm === 'C');
    if (b && c) {
      parts.push(
        `<line class="bc-segment" data-model="${esc(line.model)}" x1="${b.x.toFixed(1)}" y1="${yOf(b.mean).toFixed(1)}" x2="${c.x.toFixed(1)}" y2="${yOf(c.mean).toFixed(1)}" stroke="${color}" stroke-width="5" stroke-linecap="round" stroke-opacity="0.55"/>`
      );
    }

    // point markers + per-point CI whiskers
    for (const p of pts) {
      parts.push(
        `<line class="ci-whisker" data-model="${esc(line.model)}" data-arm="${p.arm}" x1="${p.x.toFixed(1)}" y1="${yOf(p.high).toFixed(1)}" x2="${p.x.toFixed(1)}" y2="${yOf(p.low).toFixed(1)}" stroke="${color}" stroke-width="1.3" stroke-opacity="0.7"/>`
      );
      parts.push(
        `<circle class="pt pt-${safe}" data-model="${esc(line.model)}" data-arm="${p.arm}" cx="${p.x.toFixed(1)}" cy="${yOf(p.mean).toFixed(1)}" r="3.2" fill="${color}"/>`
      );
    }
  }

  // --- legend + headline (C-B) table, in the right margin ---
  let ly = M.top + 4;
  parts.push(`<text class="legend-title" x="${M.left + plotW + 16}" y="${ly}">model · C−B (CI)</text>`);
  ly += 18;
  for (const line of lines) {
    const color = colorByModel[line.model];
    const h = line.headline;
    const badge = h
      ? `${fmt(h.delta)} [${fmt(h.ci_low)},${fmt(h.ci_high)}] ${h.direction}${h.significant ? '*' : ''}`
      : 'n/a';
    parts.push(
      `<rect class="legend-swatch" x="${M.left + plotW + 16}" y="${(ly - 9).toFixed(1)}" width="12" height="12" fill="${color}"/>`
    );
    parts.push(
      `<text class="legend-row" data-model="${esc(line.model)}" data-cb="${h ? h.delta.toFixed(3) : ''}" data-direction="${h ? h.direction : ''}" x="${M.left + plotW + 34}" y="${ly.toFixed(1)}">${esc(line.model)}</text>`
    );
    ly += 15;
    parts.push(
      `<text class="legend-cb dir-${h ? h.direction : 'na'}" x="${M.left + plotW + 34}" y="${ly.toFixed(1)}">${esc(badge)}</text>`
    );
    ly += 19;
  }
  parts.push(`<text class="legend-note" x="${M.left + plotW + 16}" y="${(ly + 4).toFixed(1)}">* CI excludes 0</text>`);

  parts.push(`</g>`);
  return parts.join('\n');
}

function build() {
  const dataset = JSON.parse(fs.readFileSync(DATA_PATH, 'utf8'));
  if (!dataset._SYNTHETIC) {
    throw new Error('refusing to render: dataset is not marked _SYNTHETIC (this spike only renders fake data)');
  }
  const chains = dataset.chains; // ['devnet','testnet']

  // pre-render one chart group per chain; toggle shows exactly one
  const groups = chains
    .map((ch, i) => renderChainGroup(dataset, ch, i === 0))
    .join('\n');

  const toggleBtns = chains
    .map(
      (ch, i) =>
        `<button class="chain-btn${i === 0 ? ' active' : ''}" data-chain="${esc(ch)}" onclick="showChain('${esc(ch)}')">${esc(ch)}</button>`
    )
    .join('');

  // tiny inline JS: switch which chain group is visible. No network.
  const js = `
function showChain(chain){
  document.querySelectorAll('svg .chart').forEach(function(g){
    g.style.display = (g.getAttribute('data-chain')===chain)?'block':'none';
  });
  document.querySelectorAll('.chain-btn').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-chain')===chain);
  });
}
`;

  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CKB AI Bench  -  Condition Ladder (SYNTHETIC)</title>
<style>
  body { font: 14px/1.45 system-ui, sans-serif; margin: 24px; color: #1a1a1a; background:#fff; }
  .synthetic-banner {
    background: #b00020; color: #fff; font-weight: 700; letter-spacing: .03em;
    padding: 8px 12px; border-radius: 6px; margin-bottom: 14px;
  }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: #555; margin: 0 0 14px; }
  .toolbar { margin: 0 0 8px; }
  .chain-btn {
    font: inherit; padding: 5px 14px; margin-right: 6px; cursor: pointer;
    border: 1px solid #888; background: #f4f4f4; border-radius: 5px;
  }
  .chain-btn.active { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  svg { border: 1px solid #e3e3e3; border-radius: 6px; background: #fff; }
  .grid { stroke: #eee; stroke-width: 1; }
  .tick { stroke: #f3f3f3; stroke-width: 1; }
  .axis-label { fill: #555; font-size: 11px; }
  .axis-label.y { text-anchor: end; }
  .axis-label.x { text-anchor: middle; }
  .axis-title { fill: #333; font-size: 12px; text-anchor: middle; }
  .legend-title { fill: #222; font-size: 11px; font-weight: 700; }
  .legend-row { fill: #222; font-size: 12px; }
  .legend-cb { font-size: 10.5px; }
  .legend-cb.dir-positive { fill: #0a7a2f; }
  .legend-cb.dir-negative { fill: #b00020; }
  .legend-cb.dir-flat { fill: #777; }
  .legend-note { fill: #888; font-size: 10px; }
  .note { color:#555; font-size:12px; margin-top:10px; max-width:680px; }
</style>
</head>
<body>
  <div class="synthetic-banner">SYNTHETIC DATA  -  fabricated, NOT a real benchmark result. Do not cite.</div>
  <h1>CKB AI Bench  -  Condition Ladder</h1>
  <p class="sub">X = condition ladder A→B→C→D · Y = Pass@1 · one line per model, colored by family · CI band on every point · headline = <strong>C−B</strong> (MCP value on top of web research). DevNet and TestNet are <strong>separate scores</strong> (toggle below; never merged).</p>
  <div class="toolbar">chain: ${toggleBtns}</div>
  <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="condition ladder chart">
${groups}
  </svg>
  <p class="note">The bold <strong>B→C</strong> segment of each line is literally the MCP's marginal value over ordinary web research. A visible upward kick at C = MCP helps; a flat or downward B→C = it does not. This page renders whatever the data shows (positive, flat, or negative) without spin (ADR-0011).</p>
  <p class="note">Source: spikes/ladder-chart/synthetic-results.json (generated by gen-synthetic-data.js). Generated_at: ${esc(dataset.generated_at)}.</p>
  <script>${js}</script>
</body>
</html>
`;

  fs.writeFileSync(OUT_PATH, html);
  process.stderr.write(`wrote ladder chart -> ${OUT_PATH} (${html.length} bytes, chains: ${chains.join(', ')})\n`);
  return OUT_PATH;
}

if (require.main === module) build();

module.exports = { build, OUT_PATH };
