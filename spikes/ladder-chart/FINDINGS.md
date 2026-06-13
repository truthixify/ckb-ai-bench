# Spike: the ladder chart reporting surface on synthetic data  -  FINDINGS (2026-06-12)

Goal (ADR-0011): de-risk the DELIVERABLE before any real run exists. Prove the reporting pipeline
works end to end on clearly-labeled fake data: fabricated A/B/C/D x 6-model x 2-chain results with a
per-cell mean + CI flow into a chart artifact where (a) the headline `C - B` slope is computed and
rendered, (b) CI bands render, (c) the chain toggle works, (d) a deliberately flat and a deliberately
negative `C - B` both render honestly. The chart's SHAPE must answer the research question (the B->C
kick = the MCP's marginal value over ordinary web research), and the page must be disprovable.

This spike touches no chain, no model, no network. It is pure reporting-pipeline plumbing on synthetic
input, dependency-free (Node v22 built-ins only: no npm install, no CDN, no build step).

## What was built (all under spikes/ladder-chart/)

- `gen-synthetic-data.js`  -  writes `synthetic-results.json`, the fabricated dataset. The file carries
  an explicit `_SYNTHETIC: true` flag and a `_WARNING` string so it can never be mistaken for a real
  result. The renderer refuses to run on any dataset not marked `_SYNTHETIC`.
- `ladder-metrics.js`  -  pure-logic module (no I/O, no deps): the `C - B` headline delta, CI
  propagation, and the chain-separation guard. This is the load-bearing code; the unit test is the
  proof, not the picture.
- `ladder-metrics.test.js`  -  10 tests on Node's built-in `node:test` runner. Asserts the delta math,
  the flat/negative honesty cases, CI propagation, and chain separation.
- `render-ladder.js`  -  reads the dataset, computes the headline via `ladder-metrics`, and writes
  `ladder-chart.html`: a self-contained inline-SVG ladder chart with a tiny inline-JS chain toggle.
- `run-spike.sh`  -  self-verifying orchestrator (`set -euo pipefail`, exit-code `check` helper).
- `synthetic-results.json` and `ladder-chart.html` are regenerated artifacts (gitignored).

## Synthetic-data shape

One row per cell = model x chain x arm (6 x 2 x 4 = 48 cells):

```
{ "model": "Opus", "family": "Anthropic", "chain": "devnet", "arm": "C",
  "runs": 3, "mean": 0.78, "ci_low": 0.55, "ci_high": 1.0 }
```

- `mean` = Pass@1 in [0,1]; `ci_low`/`ci_high` bound it. CIs are deliberately WIDE (3 runs/cell), as
  the design mandates: `ciHalfWidth` is ~0.07..0.33, widest near mean 0.5, floored so every band is
  visible. They are never hidden.
- Six models across three families: Sonnet/Opus/Fable (Anthropic), Grok-Build/Grok-Compose (xAI),
  GPT-5.5 (OpenAI). One line per model, color by family (ADR-0011).
- Two chains: `devnet` and `testnet`, generated as SEPARATE scores. TestNet is DevNet plus a fixed
  per-arm offset (a touch lower, the live-ops track), so the two are visibly distinct lines and can
  never be a copy or a merge.
- Arm means hand-set so the three load-bearing headline shapes are unambiguous (see honesty proof).

## How the `C - B` headline is computed and rendered

Computed in `ladder-metrics.headlineDelta(cellB, cellC)`:

- `delta = C.mean - B.mean` (the marginal MCP value over web research, RECOMMENDATION.md section 2).
- CI PROPAGATION: B and C are independent runs, so the half-widths combine in quadrature,
  `half_width = sqrt(hB^2 + hC^2)`. The propagated band is wider than either input (a difference of
  two uncertain numbers is more uncertain), and it brackets the delta.
- `direction` = positive / flat / negative; `significant` is true only when the propagated CI
  excludes 0. At n=3 most cells are NOT significant, which the page shows honestly.

Rendered in `render-ladder.js`:

- X axis = the arm ladder A->B->C->D (4 fixed x-positions); Y = Pass@1 [0,1].
- One `<polyline class="model-line">` per model (the means), plus a `<polygon class="ci-band">` and
  per-point `<line class="ci-whisker">` for the confidence interval on every point.
- The B->C segment is drawn a SECOND time as a thick `<line class="bc-segment">` overlay so the
  headline slope is the most visually legible segment of each line (the ADR's decisive property).
- A right-margin legend lists each model with its `C - B` value, propagated CI, and direction, color
  coded (`dir-positive` green, `dir-negative` red, `dir-flat` grey), with a `*` when the CI excludes 0.

## How chain separation is enforced (DevNet and TestNet never merged)

- In code: `lineSeriesForChain(dataset, chain)` only accepts `devnet` or `testnet` and returns ONE
  chain's series, so pooling is not expressible. `refuseChainMerge` throws loud on any merge sentinel
  (`all`/`both`/`merged`/`pooled`/`combined`). A test asserts both, and asserts the same model has a
  DIFFERENT arm-C mean on the two chains (proving they are distinct scores, not a merge).
- In the artifact: each chain is a separate pre-rendered `<g class="chart" data-chain="...">`. The
  toggle shows exactly one; they are never on the same axis. `run-spike.sh` asserts exactly two chart
  groups exist and that DevNet vs TestNet Opus-C means differ.

## Honest-rendering proof (flat + negative both render without spin)

The synthetic data bakes in the three cases on purpose; the unit test and `run-spike.sh` both assert
they render:

| Model | DevNet B | DevNet C | C - B | Rendered direction |
|---|---|---|---|---|
| Opus | 0.52 | 0.78 | +0.26 | positive (strong upward B->C kick) |
| Grok-Build | 0.60 | 0.61 | +0.01 | ~flat (no kick) |
| GPT-5.5 | 0.53 | 0.49 | -0.04 | negative (downward B->C, shown in red) |

The page renders whatever the data shows. A flat or negative B->C is drawn and labeled honestly, in
red for negatives, with no leaderboard-style spin. This is the disprovability property ADR-0011
requires: the chart's shape, not its framing, answers the question.

## Self-verification (the proof)

`bash run-spike.sh` runs 26 exit-code-checked assertions (no masking through pipes) and exits 0:
regenerates the dataset (48 cells, `_SYNTHETIC` marked), runs the 10 unit tests, renders the HTML,
then structurally asserts the artifact contains the synthetic banner, both chains, the chain toggle,
all six model lines, the B->C headline segments, the CI bands and whiskers, the +0.26 / +0.01 / -0.04
headline values, a `dir-negative` badge, and per-chain that each group has exactly 6 lines + 6 CI
bands + 4 arm positions + 6 B->C segments. Latest run: 26/26, exit 0. Closing line:
"ALL CHECKS PASSED".

Because I cannot open a browser, the chart is verified STRUCTURALLY (the SVG/markup contains one
polyline per model, four arm x-positions A-D, CI band + whisker elements per point, two separate chain
groups, and a working `showChain` toggle in inline JS). The rendered HTML is static and offline, so
the structure fully determines what a browser would draw.

## Residual / tracked caveats (honest)

- The chart is structurally verified, not pixel-verified: I asserted the SVG elements exist and the
  toggle JS is present, but did not render it in a real browser. A future visual smoke test (headless
  browser screenshot) would close this. The markup is static and offline, so the risk is low.
- The CI model is a SPIKE simplification: synthetic half-widths are a binomial-style heuristic, and the
  delta CI uses symmetric quadrature. The real pipeline will replace `gen-synthetic-data.js` with the
  scorer's actual per-cell means and bootstrap CIs, and may use an asymmetric propagation. The chart
  and metrics interfaces (cell shape, `headlineDelta`, `lineSeriesForChain`) are what carries forward;
  the numbers are placeholder.
- This spike proves the REPORTING surface only. It does not touch scoring, the agent, the verifier, or
  any chain. It de-risks the deliverable's last mile, nothing upstream of it.
