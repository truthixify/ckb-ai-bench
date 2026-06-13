// Spike (NOT production): pure-logic module for the ladder chart's headline math.
// No I/O, no deps. Computes the C - B headline delta per model and propagates the
// confidence interval, and enforces ADR-0011's rule that DevNet and TestNet are
// SEPARATE scores (this module refuses to pool chains).
//
// Semantics (ADR-0011, RECOMMENDATION.md section 2):
//   Arms A/B/C/D. Score = Pass@1 in [0,1] with a CI [ci_low, ci_high] per cell.
//   Headline = C - B = the MCP's marginal value ON TOP OF ordinary web research.
//   Chain (devnet|testnet) is a TOGGLE, never merged onto one axis.

'use strict';

const ARMS = ['A', 'B', 'C', 'D'];
const CHAINS = ['devnet', 'testnet'];

// Half-width of a [low, high] interval around a mean.
function halfWidth(cell) {
  return Math.max(cell.mean - cell.ci_low, cell.ci_high - cell.mean);
}

// The headline delta C - B for one (model, chain) line, with a propagated CI.
// The two cells are independent runs, so the variance of the difference adds;
// for symmetric half-widths we combine them in quadrature (sqrt(hB^2 + hC^2)).
// Returns the delta, its propagated CI, and a `direction` label so a flat or
// negative result is reported honestly rather than spun.
function headlineDelta(cellB, cellC) {
  if (!cellB || !cellC) {
    throw new Error('headlineDelta needs both arm B and arm C cells');
  }
  const delta = cellC.mean - cellB.mean;
  const hB = halfWidth(cellB);
  const hC = halfWidth(cellC);
  const halfW = Math.sqrt(hB * hB + hC * hC);
  // A delta whose CI straddles 0 is not distinguishable from "no effect".
  const ciLow = delta - halfW;
  const ciHigh = delta + halfW;
  let direction;
  if (delta > 1e-9) direction = 'positive';
  else if (delta < -1e-9) direction = 'negative';
  else direction = 'flat';
  const significant = ciLow > 0 || ciHigh < 0; // CI excludes 0
  return {
    delta,
    ci_low: ciLow,
    ci_high: ciHigh,
    half_width: halfW,
    direction,
    significant,
  };
}

// Build the per-chain, per-model line series the chart consumes. Throws if a
// caller tries to ask for a chain that is not one of the two separate scores.
// This is the structural enforcement of "DevNet and TestNet never share an axis":
// you can only ever extract ONE chain at a time, so pooling is not expressible.
function lineSeriesForChain(dataset, chain) {
  if (!CHAINS.includes(chain)) {
    throw new Error(`unknown chain "${chain}"; chains are kept separate: ${CHAINS.join(', ')}`);
  }
  const cells = dataset.cells.filter((c) => c.chain === chain);
  const byModel = new Map();
  for (const c of cells) {
    if (!byModel.has(c.model)) {
      byModel.set(c.model, { model: c.model, family: c.family, points: {} });
    }
    byModel.get(c.model).points[c.arm] = {
      arm: c.arm,
      mean: c.mean,
      ci_low: c.ci_low,
      ci_high: c.ci_high,
    };
  }
  return Array.from(byModel.values()).map((line) => {
    const b = line.points.B;
    const c = line.points.C;
    return { ...line, headline: b && c ? headlineDelta(b, c) : null };
  });
}

// Hard refusal: there is no "merge chains" operation. If a caller asks for it by
// passing the sentinel 'all' / 'both' / 'merged', fail loud (ADR-0001/0011).
function refuseChainMerge(chain) {
  const banned = ['all', 'both', 'merged', 'pooled', 'combined'];
  if (banned.includes(String(chain).toLowerCase())) {
    throw new Error(
      `chains must stay separate (ADR-0011): refusing to merge "${chain}". Pick one of: ${CHAINS.join(', ')}`
    );
  }
  return chain;
}

module.exports = {
  ARMS,
  CHAINS,
  halfWidth,
  headlineDelta,
  lineSeriesForChain,
  refuseChainMerge,
};
