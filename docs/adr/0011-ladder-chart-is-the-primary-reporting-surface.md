# The ladder chart is the primary reporting surface

## Context

The site shows results two ways: a chart and a leaderboard. The chart is primary. The grouping axis
was unsettled between (1) model-tier-within-family (capability scaling) and (2) the condition ladder
per model. The benchmark's purpose is to prove *or disprove* the MCP's value, so the report must read
honestly under a null result, not just a positive one. (Owner delegated the v1 call.)

## Decision

The **primary chart plots the condition ladder on X** (A floor -> B web -> C MCP+web -> D MCP-only),
**one selected model at a time**, score on Y, with a confidence band on every point. A labelled
dropdown switches models. Each model retains a stable color and line pattern, while provider family
remains explicit metadata in the supporting table.

The decisive property: the selected model's **B->C segment literally is the MCP's marginal value over
web research** (the headline `C - B`), drawn as a slope. A reader sees instantly whether MCP helps for
that model; the all-model tables remain available for direct cross-model comparison. This is the only
layout where the chart's *shape* answers the research question rather than a different one.

- **Chain is a toggle**, never co-plotted: DevNet and TestNet are separate scores (see CONTEXT.md /
  ADR-0001) and must not share an axis.
- **Model is a selector**, never co-plotted: overlapping model paths must not obscure one another.
- **The leaderboard is secondary** — supporting evidence below the chart, not the page's reason to
  exist, so a null `C - B` does not make the page read as a failure.
- A **family-trajectory chart** (capability scaling across model tiers) is a worthwhile *secondary*
  view, deferred past v1.

## Consequences

The report is outcome-independent: it presents whatever shape the data has (flat or kicked), which is
the only framing consistent with disprovability. The five data axes (model, family, arm, chain, score)
are handled as: arm=X, model=selector plus line identity, family=table metadata, score=Y,
chain=toggle. This remains legible when multiple models share one provider family or produce
overlapping values. Always showing the CI band makes the "3 runs => wide CIs, disclosed not hidden"
rule visual.

## Zero-denominator arms

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

An early pilot exposed the problem: two `infra_fail` cells rendered
`C - B +0.00 [-1.41,+1.41] flat`, which reads as
"the documentation surface made no difference" when in fact nothing was measured.
