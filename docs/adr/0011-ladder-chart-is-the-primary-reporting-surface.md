# The ladder chart is the primary reporting surface

## Context

The site shows results two ways: a chart and a leaderboard. The chart is primary. The grouping axis
was unsettled between (1) model-tier-within-family (capability scaling) and (2) the condition ladder
per model. The benchmark's purpose is to prove *or disprove* the MCP's value, so the report must read
honestly under a null result, not just a positive one. (Owner delegated the v1 call.)

## Decision

The **primary chart plots the condition ladder on X** (A floor -> B web -> C MCP+web -> D MCP-only),
**one line per model, colored by family**, score on Y, with a confidence band on every point.

The decisive property: the **B->C segment of each line literally is the MCP's marginal value over web
research** (the headline `C - B`), drawn as a slope. A reader sees instantly whether MCP helps — a
visible upward kick at C across models means it does; flat segments mean it does not. This is the only
layout where the chart's *shape* answers the research question rather than a different one.

- **Chain is a toggle**, never co-plotted: DevNet and TestNet are separate scores (see CONTEXT.md /
  ADR-0001) and must not share an axis.
- **The leaderboard is secondary** — supporting evidence below the chart, not the page's reason to
  exist, so a null `C - B` does not make the page read as a failure.
- A **family-trajectory chart** (capability scaling across model tiers) is a worthwhile *secondary*
  view, deferred past v1.

## Consequences

The report is outcome-independent: it presents whatever shape the data has (flat or kicked), which is
the only framing consistent with disprovability. The five data axes (model, family, arm, chain, score)
are handled as: arm=X, model=line, family=color, score=Y, chain=toggle — legible in 2D without
overcrowding. Always showing the CI band makes the "3 runs => wide CIs, disclosed not hidden" rule
visual.

## Zero-denominator arms (added after the Task 20 pilot)

A point, CI band, B-C segment or `C - B` headline exists **only for scored evidence**. `infra_fail`
is excluded from the correctness denominator, so an arm whose runs were all infrastructure failures
has `scored_runs == 0` and an **undefined** Pass@1 — not zero.

Such an arm is displayed as unavailable, never as a null effect:

- no plotted circle, whisker, band vertex or segment endpoint;
- no numeric `data-cb`, no `flat` direction, no fabricated interval; and
- `n/a` wherever its correctness or `C - B` value would appear.

Its model still appears, with its infrastructure- and protocol-failure rates published, because a
health failure must stay visible. A `C - B` headline requires **both** B and C to have
`scored_runs > 0` and finite statistics.

Task 20 is why: two `infra_fail` cells rendered `C - B +0.00 [-1.41,+1.41] flat`, which reads as
"the documentation surface made no difference" when in fact nothing was measured.
