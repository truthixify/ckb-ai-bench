# Flat JSON storage and static reporting

## Context

Phase 4 persists one immutable `RunResult` JSON file per matrix cell run. Runs are infrequent and
immutable once a suite is frozen. Two reviewers converged on the same architecture: flat per-run JSON
files committed to the repo are the single source of truth (no database, no live backend), and the
reporting surface is a statically pre-rendered HTML/SVG page built by a deterministic Python step from
those files (ADR-0011).

The weakness both reviewers flagged: JSON enforces no invariants by itself, and derived math (Pass@1,
the headline `C - B` delta, propagated CIs) must be deterministic so a repro check can assert
byte-identical artifacts. RECOMMENDATION §4 splits outcomes (`pass / agent_fail / infra_fail /
protocol_violation`) and §7 requires paired seeds across arms so `C - B` deltas are paired; the storage
and reporting layers must preserve those semantics without drift.

## Decision

1. **Storage:** One JSON file per run under `results/<suite_semver>/`, keyed by
   `(suite, chain, arm, model, seed, run_id)`. Files are the authoritative artifact; no secondary
   database.

2. **Validation (fail loud):** Before aggregation or rendering, a strict validator rejects:
   - duplicate `(suite, chain, arm, model, seed, run_id)` cell keys;
   - unknown or invalid `outcome` values (must be `pass`, `agent_fail`, `infra_fail`, or
     `protocol_violation`);
   - frozen-suite drift within the same `suite_semver` (`suite_freeze_hash` or `mcp_server_version`
     disagreement);
   - chains not in `CHAIN_PROFILES`;
   - a `schema_version` that is missing, blank, or not the current schema. Legacy rows predate
     `mcp_surface_profile`, so their treatment is unknown; they are refused rather than migrated in
     place or inferred;
   - a missing, blank, unknown, or wrong-for-its-arm `mcp_surface_profile`. A and B must record
     `off`, C and D must record `docs-only-v1` (ADR-0013). The check is per row, so the verdict
     cannot depend on which trial is loaded first;
   - a missing, blank, unknown or malformed `model_profile_id` / `model_profile_sha256`, a row
     whose `model` is not the profile's requested model, or a digest that is not the tracked
     phase-one profile's (ADR-0014);
   - malformed `metrics` fields, counts or `token_usage_status`; negative, boolean, float,
     numeric-string or partially present token triples; a broken `total = prompt + completion`
     identity; `not_started` carrying activity or tokens; `complete` with zero attempts, unequal
     counts, null tokens or no returned model identity; or `incomplete` on a correctness-scored
     outcome, since a cell whose usage could not be established is infrastructure evidence;
   - B/C drift in model profile digest or returned model identity, checked order-independently;
   - missing or malformed `agent_limits` provenance for any run that reached an agent;
   - mixed concrete B/C agent budgets. Within one comparison identity
     `(suite_semver, suite_freeze_hash, mcp_server_version, chain, model)`, every concrete B and C
     row — across all seeds, trials and run IDs — must share one
     `(step_limit, cost_limit, wall_time_limit_seconds)` tuple. Drift between two trials of the same
     arm fails too, because that is already mixed methodology. A and D are excluded: they use the
     same production defaults but are not the headline pair. The exception is a pre-agent
     `infra_fail`, whose three limits are all null and which is skipped by the comparison; a
     partially null limits object is rejected as provenance.

3. **Metrics (pure, tested):** Ladder metrics are pure Python with no I/O. Pass@1 excludes
   `infra_fail` from the denominator; `agent_fail` and `protocol_violation` count as 0. `infra_fail`
   and `protocol_violation` rates are published separately as health numbers, never folded into
   Pass@1. The headline `C - B` delta propagates CIs in quadrature (ADR-0011, RECOMMENDATION §2).
   DevNet and TestNet are never merged (chain-separation guard).

4. **Reporting (static, offline):** A deterministic build step loads results, validates, aggregates,
   and writes self-contained HTML with inline SVG to `site/`. No external JS/CSS/CDN. Same inputs
   yield byte-identical output (repro check). The ladder chart is primary (ADR-0011); a secondary
   leaderboard table sits beneath.

5. **Matrix driver:** The driver calls `run_cell` per grid cell with injectable seams, uses paired
   seeds across arms (RECOMMENDATION §7), writes flat JSON, then validate + aggregate + render.
   Appending new run files to a cell and re-aggregating requires no schema change.

## Consequences

- Results are inspectable, diffable, and git-versioned; the site is a derived artifact that can be
  regenerated at any time.
- Invalid or drifted result sets cannot silently corrupt the headline chart; the validator is the
  mitigation for JSON's lack of schema enforcement at rest.
- Deterministic rendering enables CI repro checks (`render twice -> identical bytes`).
- Adding runs to deepen a headline cell is a file append plus rebuild, not a migration.
