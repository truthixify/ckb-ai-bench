# Flat JSON storage and static reporting

## Context

Phase 4 persists one immutable `RunResult` JSON file per matrix cell run. Runs are infrequent and
immutable once a suite is frozen. Two reviewers converged on the same architecture: flat per-run JSON
files are the local source of truth (no database, no live backend), and the
reporting surface is a statically pre-rendered HTML/SVG page built by a deterministic Python step from
those files (ADR-0011).

The weakness both reviewers flagged: JSON enforces no invariants by itself, and derived math (Pass@1,
the headline `C - B` delta, propagated CIs) must be deterministic so a repro check can assert
byte-identical artifacts. RECOMMENDATION §4 splits outcomes (`pass / agent_fail / infra_fail /
protocol_violation`) and §7 requires paired seeds across arms so `C - B` deltas are paired; the storage
and reporting layers must preserve those semantics without drift.

## Decision

1. **Storage:** One JSON file per run under
   `benchmark-output/results/<suite_semver>/`, keyed by
   `(suite, chain, arm, model, seed, run_id)`. Files are the authoritative artifact; no secondary
   database. Each run ID includes an independent nonce as well as its UTC epoch. A complete JSON
   candidate is flushed before an exclusive atomic link publishes it, so an existing artifact is
   never replaced and an interrupted write never becomes an authoritative row.

2. **Validation (fail loud):** Before aggregation or rendering, a strict validator rejects:
   - duplicate `(suite, chain, arm, model, seed, run_id)` cell keys;
   - unknown or invalid `outcome` values (must be `pass`, `agent_fail`, `infra_fail`, or
     `protocol_violation`);
   - an unknown suite version, or any `suite_freeze_hash`, MCP version, task order, task score,
     scored flag or maximum score that differs from the accepted tracked suite contract;
   - a missing or changed `run_params_derivation` value;
   - missing, duplicate, foreign, reordered or malformed task verdicts; task awards that disagree
     with pass status; totals that do not equal awarded task scores; and run outcomes that contradict
     the task ledger or agent exit status;
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
     counts, null tokens or no returned model identity; attempts beyond the reviewed per-call
     ceiling; or a scored `incomplete` row whose model calls did not all receive responses. A
     recovered scored row remains excluded from efficiency;
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
   and writes self-contained HTML with inline SVG to `benchmark-output/site/`. No external
   JS/CSS/CDN. Same inputs yield byte-identical output (repro check). The ladder chart is primary
   (ADR-0011); a secondary
   leaderboard table sits beneath. One current results directory may contain rows from any committed
   profile under `configs/models/`; validation still requires an exact profile identity and digest
   match. An explicit manifest combines separate result directories and names the exact tracked
   profile for each cohort. Results remain grouped by model and profile, with no cross-model pooling
   into a B/C estimate. Correctness readiness and exact efficiency readiness are reported as separate
   gates. When scored rows have incomplete usage, tokens from received responses and measured wall
   time remain visible as observed arm summaries with response coverage; exact efficiency deltas and
   provider billing claims remain unavailable.

5. **Matrix driver:** The driver calls `run_cell` per grid cell with injectable seams. It derives
   randomized prompt-visible task values deterministically from the seed, runs arms adjacently
   within each seed block, and reverses arm order on alternating blocks. It then writes flat JSON,
   validates, aggregates and renders. Appending new run files and re-aggregating requires no schema
   change.

## Consequences

- Results are inspectable local artifacts under one gitignored output root; the site is derived and
  can be regenerated at any time. Publishing or archiving an accepted cohort is an explicit export,
  not an incidental source commit.
- Invalid or drifted result sets cannot silently corrupt the headline chart; the validator is the
  mitigation for JSON's lack of schema enforcement at rest.
- Deterministic rendering enables CI repro checks (`render twice -> identical bytes`).
- Adding runs to deepen a headline cell is a file append plus rebuild, not a migration.
- Historical and current model cohorts can share one static report while preserving their exact
  profile provenance and schema history.

## Undefined correctness is `null`, not `0`

`pass_at1_ci()` returns `(None, None, None)` when `scored_runs == 0`, and `CellAggregate.mean`,
`ci_low` and `ci_high` are nullable. The dataset serializes them as JSON `null` — never `NaN`,
`Infinity`, `0`, or a sentinel — so a consumer cannot mistake an empty denominator for a measurement.

`runs`, `scored_runs`, `infra_fail_rate` and `protocol_violation_rate` stay concrete and published
for the same cell: excluding a row from correctness must not hide that it failed.
