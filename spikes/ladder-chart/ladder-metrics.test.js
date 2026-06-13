// Spike (NOT production): unit tests for the ladder headline math. The TEST is
// the proof of correctness, not the chart picture. Run with node's built-in
// runner: `node --test`. No deps.
//
// These tests encode WHY the math matters (ADR-0011, Rule 9):
//  - the C-B headline must be arithmetically correct on known inputs;
//  - a deliberately FLAT case must report ~0 (so a null result reads as null);
//  - a deliberately NEGATIVE case must report < 0 (so the page cannot spin a
//    regression as a win);
//  - the CI must PROPAGATE (a delta of two uncertain cells is more uncertain,
//    never narrower than either input);
//  - DevNet and TestNet must stay SEPARATE: the API cannot pool them.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const m = require('./ladder-metrics');
const gen = require('./gen-synthetic-data');

const cell = (mean, low, high) => ({ mean, ci_low: low, ci_high: high });

test('C - B delta is correct on known inputs', () => {
  const r = m.headlineDelta(cell(0.50, 0.40, 0.60), cell(0.75, 0.65, 0.85));
  assert.ok(Math.abs(r.delta - 0.25) < 1e-9, `delta ${r.delta} != 0.25`);
  assert.equal(r.direction, 'positive');
});

test('flat C-B yields ~0 and is reported as flat, not positive', () => {
  const r = m.headlineDelta(cell(0.60, 0.50, 0.70), cell(0.60, 0.50, 0.70));
  assert.ok(Math.abs(r.delta) < 1e-9, `flat delta ${r.delta} not ~0`);
  assert.equal(r.direction, 'flat');
  assert.equal(r.significant, false); // a flat result cannot be "significant"
});

test('negative C-B yields < 0 and is reported as negative (no spin)', () => {
  const r = m.headlineDelta(cell(0.53, 0.43, 0.63), cell(0.49, 0.39, 0.59));
  assert.ok(r.delta < 0, `expected negative, got ${r.delta}`);
  assert.equal(r.direction, 'negative');
});

test('CI propagates: the delta band is wider than either input half-width', () => {
  const b = cell(0.50, 0.40, 0.60); // half-width 0.10
  const c = cell(0.75, 0.65, 0.85); // half-width 0.10
  const r = m.headlineDelta(b, c);
  const inputHalf = 0.10;
  assert.ok(r.half_width > inputHalf, `propagated ${r.half_width} not > input ${inputHalf}`);
  // quadrature: sqrt(0.10^2 + 0.10^2) ~ 0.1414
  assert.ok(Math.abs(r.half_width - Math.sqrt(0.02)) < 1e-9);
  // the propagated CI must bracket the delta
  assert.ok(r.ci_low < r.delta && r.delta < r.ci_high);
});

test('wide CIs can straddle 0 -> not significant (honest at n=3)', () => {
  // small delta, wide bands -> CI includes 0
  const r = m.headlineDelta(cell(0.50, 0.20, 0.80), cell(0.55, 0.25, 0.85));
  assert.ok(r.ci_low < 0 && r.ci_high > 0, 'expected CI to straddle 0');
  assert.equal(r.significant, false);
});

test('headlineDelta fails loud when an arm is missing', () => {
  assert.throws(() => m.headlineDelta(null, cell(0.5, 0.4, 0.6)));
  assert.throws(() => m.headlineDelta(cell(0.5, 0.4, 0.6), undefined));
});

test('chains are kept separate: lineSeriesForChain rejects unknown chains', () => {
  const ds = gen.build();
  assert.throws(() => m.lineSeriesForChain(ds, 'all'));
  assert.throws(() => m.lineSeriesForChain(ds, 'merged'));
});

test('chains are NOT pooled: devnet and testnet give distinct series', () => {
  const ds = gen.build();
  const dev = m.lineSeriesForChain(ds, 'devnet');
  const test_ = m.lineSeriesForChain(ds, 'testnet');
  assert.equal(dev.length, 6, 'expected 6 model lines on devnet');
  assert.equal(test_.length, 6, 'expected 6 model lines on testnet');
  // same model, different chain -> different arm-C mean (proves no merge)
  const devOpus = dev.find((l) => l.model === 'Opus').points.C.mean;
  const testOpus = test_.find((l) => l.model === 'Opus').points.C.mean;
  assert.notEqual(devOpus, testOpus, 'devnet and testnet Opus C means must differ (separate scores)');
});

test('refuseChainMerge throws loud on any merge sentinel', () => {
  for (const s of ['all', 'both', 'merged', 'pooled', 'combined', 'MERGED']) {
    assert.throws(() => m.refuseChainMerge(s), new RegExp('separate'));
  }
  // a legit chain passes through
  assert.equal(m.refuseChainMerge('devnet'), 'devnet');
});

test('synthetic dataset embeds the three load-bearing headline shapes', () => {
  const ds = gen.build();
  const dev = m.lineSeriesForChain(ds, 'devnet');
  const by = (name) => dev.find((l) => l.model === name).headline;
  assert.equal(by('Opus').direction, 'positive');
  assert.ok(by('Opus').delta > 0.2, 'Opus C-B should be a strong positive');
  assert.equal(by('Grok-Build').direction, 'positive'); // +0.01 counts as positive sign...
  assert.ok(Math.abs(by('Grok-Build').delta) < 0.03, 'Grok-Build C-B should be ~flat');
  assert.equal(by('GPT-5.5').direction, 'negative');
  assert.ok(by('GPT-5.5').delta < 0, 'GPT-5.5 C-B should be negative');
});
