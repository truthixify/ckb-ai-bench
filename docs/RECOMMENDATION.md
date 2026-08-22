# CKB AI Benchmark — Recommendation (v3)

**Supersedes v2.** Revised for the owner's v3 refinements: versioned suites, a condition *ladder*
(not binary on/off), per-run **time + token** metrics, MCP-for-testnet steering, and a
model×chain×condition matrix. Folds in the **spike** (the mini-swe-agent fork now works end-to-end
against the live server). Two fresh second opinions (gpt-5.5, grok-build) converged. Research in
`docs/research/`. **Date:** 2026-06-12.

---

## TL;DR

A **versioned leaderboard**. Each suite version (v1, v2, …) freezes tasks + prompts + verifiers. For
each suite you run a matrix of **model × chain × condition**, ≥3 times per cell, and score by
**Pass@1** plus **time and tokens** (cost). The conditions form a *ladder* from "no help" to "MCP +
web"; the **load-bearing result is `C − B`** — what the MCP adds *on top of* ordinary web research. The
agent is the **spike-proven fork** of mini-swe-agent (already built).

Eight decisions:

1. **Suites are versioned & immutable** (git-tagged). Score by suite; never rank across versions.
2. **Condition ladder A/B/C** core + **D** diagnostic slice. The headline MCP claim is the **`C − B`** delta.
3. **Internet always on** (your call). The "no-research" arms (A, D) are **prompt-enforced** → treated as
   *compliance-dependent*, with the agent **logging any web access** so we can flag/annotate violations.
4. **DevNet and TestNet are separate scores**, never merged. DevNet = deterministic headline; TestNet =
   "live ops" track, with infra-failures excluded from the correctness denominator (but published).
5. **Time + tokens are first-class metrics.** Split wall-time by phase; report `cost_per_correct` and
   paired deltas with CIs. The fork already exposes the primitives.
6. **MCP steering is legitimate** ("use the product as intended") *if* tool provenance is **machine-logged**
   (MCP vs direct-RPC vs web), not taken from the model's self-report.
7. **Launch the full matrix, bold** (owner's call): all 6 models × both chains × the ladder × 3 runs.
   Always **show the CI** next to every number (3 runs = wide CIs; disclose them). Screening is a
   build-time activity (many quick spot passes), then a straight-to-full-matrix launch run.
8. **Pin everything**; the MCP server is alpha (live `ckb-ai-mcp v1.6.12`).

---

## 1. Versioned suites

A suite is an **immutable git-tagged manifest**: `suite_semver`, `task_ids` + `task_sha256`,
`prompt_sha256`, `verifier_sha256`, `agent_image_digest`, `verifier_image_digest`, `chain_profile`, `mcp_server_version`,
`mcp_tools_digest`, `scoring_schema_version`.

**Score rollup:** task-level binary pass → macro-average across the frozen tasks, **reported by
(chain, condition) first**, "overall" only as a display field. **Leaderboard shows one suite tab at a
time;** history = each suite as its own line. **Never** a single continuous rank across versions, and
**never backfill** (the v2 rule, now primary). This mirrors how HELM/SWE-bench keep subsets separate
rather than pooling them.

## 2. The condition ladder

Internet is physically on the whole time; arms differ by **prompt** (research allowed?) and **capability**
(MCP present?):

| Arm | MCP | Web research | Proves | MVP |
|---|---|---|---|---|
| **A** Floor | no | prompt: no | innate model ability (compliance-dependent) | core |
| **B** Research | no | yes | value of ordinary web research | core |
| **C** MCP+web | yes | yes | **MCP value on top of research** | **core** |
| **D** MCP-only | yes | prompt: no | curated MCP can replace stale/wrong web | diagnostic slice |

**The deltas are the story, not the absolute arms:**
- `B − A` = what web research buys.
- **`C − B` = the headline MCP claim** — its marginal value when the model *already* has the web. This is
  a harder, more honest bar than "MCP vs nothing," and it's the real-world question (users have the web).
- `D − A` and `C − D` (diagnostic) = is the MCP's curation carrying the task, or is the web still doing
  real work? Run **D only on tasks where stale web is plausible** (testnet ops, current script hashes,
  faucet/account workflows, protocol/RPC quirks) — not the full matrix.

## 3. The no-research arms — honest about enforcement

You chose **internet always on, research restricted by prompt only.** That's the decision. Be aware of
its one real cost, and the mitigation:

- Arms A and D are **prompt-enforced**, and capable models can ignore "don't research the web" (both
  reviewers flagged this; prompt-injection/instruction-bypass rates are high, and DeepSWE caught a model
  exploiting exactly this kind of loophole). So A/D are **compliance experiments, not hard capability
  controls.**
- **Mitigation that fits "internet always on" (no network cut):** the agent **logs every web access**
  (it already routes actions through one seam — we tag web-fetch/`curl`/`wget`-to-web commands). A
  no-research run that touched the web is marked **`protocol_violation`** — not pass, not fail — and
  counted as 0 in Pass@1, with the violation rate published as a health metric.
- **If a no-research claim ever needs to be airtight**, the only real fix is network-layer egress
  blocking for those arms (Docker `internal` net + allowlist proxy). We researched that and it's
  feasible; it's **deferred by your choice**, available if the A/D numbers later need to be defensible
  beyond "the model complied." Flagging this so it's a known, owned tradeoff.

## 4. DevNet vs TestNet (separate scores)

- **DevNet** = primary deterministic engineering score: fixed node image, fixed genesis, pre-funded keys,
  state reset per task/run. A failure is an agent failure.
- **TestNet** = separate "live network operations" score, permanent. Preflight health (tip, balances,
  RPC), require N confirmations, collect replay bundles. **Split outcomes into `pass / agent_fail /
  infra_fail / protocol_violation`;** exclude `infra_fail` from the correctness denominator but **publish
  the infra-fail rate.** TestNet flakiness must never drag DevNet rank. Avoid the MCP's external faucet
  tool (`dev_request_testnet_funds`) in trials — pre-fund instead.

## 5. Time + token metrics (cost is a headline)

The hypothesis — MCP may match correctness while using fewer tokens / less time — is a first-class result.
The fork already exposes the primitives (`elapsed_seconds`, litellm cost + token usage, `n_calls`, and the
`mcp_tool` provenance tag).

- **Split wall-time by phase:** `model_wall`, `mcp_wall`, `direct_rpc_wall`, `web_wall`, `verifier_wall`,
  `total_wall` — so MCP/network latency isn't confounded with model thinking time.
- **Tokens:** sum provider `usage` per call; report **both** `billable_tokens_incl_retries` **and**
  `successful_call_tokens`; log retry count + reason.
- **Report:** per-(suite, chain, condition, model) **median + IQR**; **`cost_per_correct = total_cost /
  passes`** (DeepSWE-style); **paired bootstrap CIs** on the per-task ON/OFF deltas for correctness,
  tokens, cost, and time. Use medians (heavy tails from retries). MCP latency *is* part of the measured
  MCP experience — not a confound.

## 6. MCP steering + provenance

Steering the MCP arms ("for CKB/testnet work prefer `mcp_call`; if you fall back to direct RPC, emit
`FALLBACK_RPC: <reason>`") is **legitimate** — it tests the product as intended, and the no-MCP arm isn't
handicapped (B keeps web + direct RPC). The guard is **machine-logged, event-level provenance**, not the
model's word:

- Tag every action `provenance: mcp | direct_rpc | bash | web` (the fork's `extra.mcp_tool` already marks
  MCP; detect direct-RPC by destination, not self-report). Store **both** `declared_rpc_fallback` (the
  agent's flag) and `observed_direct_rpc` (machine-derived).
- **Never change the score for fallback.** Publish it as a **product-gap table** (RPC-fallback rate, which
  tools/tasks force bypass) — that's the signal for improving the MCP. Also publish "% tasks using ≥1 MCP
  tool" and MCP coverage as diagnostic columns.

## 7. Matrix sizing — launch the full matrix, bold (owner's call)

**Decision: launch the full matrix.** All six models (Sonnet, Opus, Fable, Grok-Build, Grok-Compose,
GPT-5.5) × both chains × the full ladder × **3 runs per cell.** Breadth is the marketing asset; a
complete grid is a stronger debut than a cautious slice. The reviewers' "start with 2–3 models" was a
*cost/power* caution, not a correctness blocker — overridden deliberately.

The one caveat I'm keeping from that caution (it's arithmetic, not timidity): **at 3 runs the per-cell
CIs are wide, so always show the CI next to every number.** A wide CI that is *disclosed* is honest and
survives scrutiny; a wide CI *hidden behind a point estimate* is exactly what a skeptic tears apart on
launch day. So: bold and complete on breadth, disclosed on uncertainty.

- **Cells:** the whole ladder that's scored (A/B/C + D-as-diagnostic) on **DevNet**, plus the
  internet-on arms (B/C, and D where stale-web is plausible) on **TestNet**. 3 runs each, **paired seeds**
  across conditions so the `C − B` deltas are paired.
- **Screening is a development activity, not a published phase.** Throughout the build cycle, run many
  quick spot/screening passes (1 run, a few tasks/models) to catch broken tasks, broken arms, and
  zero-signal cells *before* they reach the suite. The launch run is then **straight to full matrix** on
  the frozen suite.
- **Deepen later without re-architecting:** the schema scores per cell, so if a *headline* delta (a
  specific `C − B`) ever needs tighter CIs, add runs to that cell — no grid redo. Keep that path open from
  day one.

---

## What changed from v2

| v2 | v3 |
|---|---|
| Binary MCP on/off | **Condition ladder A/B/C** (+ D diagnostic); headline delta = **C − B** |
| (one suite) | **Versioned, immutable suites**; score per version, never cross-rank |
| Pass@1 (+ cost noted) | Pass@1 **+ time + tokens as first-class**, `cost_per_correct`, phase-split time |
| Web off by network layer | **Internet always on**; no-research = **prompt-only + violation logging** |
| Devnet headline / testnet tagged | Same, sharpened: **`pass/agent_fail/infra_fail/violation`** outcome split |
| (steering unspecified) | **MCP steering legit + event-level machine provenance** + RPC-fallback gap table |
| (kept) fork agent, direct-RPC verifier, pinning, separate-metric discipline | (kept) |

## The one risk to engineer against

**The no-research arms can manufacture false certainty.** If A or D silently used the web, the ladder
stops measuring what it claims and the token/time deltas become uninterpretable. Given internet stays on,
**the violation logging in §3 is not optional** — it's what keeps A/D honest. Publish the violation rate
next to every A/D number.

## Still your call

- Final model set for the first public suite (reviewers suggest starting with 2–3, then scaling to your six).
- DevNet:TestNet task ratio for v1 (recommend DevNet-heavy).
- Toolchain pinner: `mise` vs a pinned Node base image (open inside ADR-0004).

## Decisions since v3 (see docs/adr/ and CONTEXT.md)

A design-interview pass settled the harness internals. The ADRs in `docs/adr/` are now the live source
of truth; where they differ from the sections above, **the ADRs win.** Notable changes:

- **Provenance simplified (supersedes §6's event-level provenance).** Score integrity comes from
  **task weighting** — trivial MCP-substitutable Tasks carry negligible weight, the headline rests on
  complex authored Tasks no MCP tool can complete (ADR-0002). v1 ships **no provenance flag and no
  fused event log.** A per-run "MCP-was-actually-used" flag and the RPC-fallback gap table are
  **deferred future enhancements**, not v1.
- **Metrics simplified for v1 (supersedes §5's phase-split/cost_per_correct).** v1 records only **total
  wall-time and total tokens** per run, raw — no phase-split, no `cost_per_correct`, no per-task
  attribution (the single-pass composed run makes per-task token/time unmeasurable). **Deferred future
  enhancement:** per-task token/time tracking, which requires a per-Task "complete" tool-call signal
  (that signal would also enable next-Task nudging).
- **No-research enforcement upgraded (supersedes §3's prompt-only stance).** All container egress flows
  through a logging proxy; on arms A/D it **blocks** to an allowlist (chain RPC + MCP + proxy) — a hard
  network control, not prompt-only (ADR-0006).
- **D ships in v1** (was open): build it in; final keep/cut is late-stage after dev-phase results.
- **Task model fixed:** a Task = prompt + score + verifier executable (ADR-0003); Suite = registry of
  task dirs, delivered as one composed prompt, strictly independent in v1 (ADR-0008); run params split
  into prompt-injected vs verifier-private (ADR-0009).
- **Containers/chains fixed:** fat pinned build image (ADR-0004); Verifier runs in a **clean hermetic
  container** fed by the mount (ADR-0005, supersedes the in-agent-container draft); DevNet is a
  **nervos/ckb sidecar**, OffCKB dropped (ADR-0007, supersedes the in-container/OffCKB draft); MCP
  version pinned + preflight-enforced (ADR-0010); on-chain Proof integrity is stateless (ADR-0001).
- **Tier-1 spikes done + certified (2026-06-12, `spikes/`).** The three load-bearing unknowns are
  proven end to end: (1) hidden-suite Code-Task grading catches always-0, hardcode, and length-only
  cheats; (2) the stateless on-chain Proof+verifier rejects stale/wrong-nonce/nonexistent/borrowed
  Proofs (high-entropy verifier-private nonce); (3) a real model loop drives the forked agent over
  MCP with a working OFF arm. Three rounds of adversarial review (codex + both grok models, parallel)
  reached a fix-free clean round. Two real fork bugs were found and fixed (see each `FINDINGS.md` +
  `spikes/ADVERSARIAL_REVIEW.md`). The remaining spike-level unknown is the **ADR-0006 egress proxy**
  (OFF-arm data isolation is visible but not yet enforced).
- **Selectable model profiles and provider-attested tokens (supersedes §5's best-effort token
  collection).** Each tracked JSON file under `configs/models/` fixes one provider/model pair, safe
  endpoint, exact route and model-supported parameters, `drop_params`, **zero** LiteLLM retries and
  at most **four** benchmark-owned attempts per model turn. The operator selects a reviewed alias
  from `./bench models`; no Python or endpoint edit is required. Retries are allowed only for the fixed transient
  categories after fixed 4, 8 and 16 second waits; authentication, authorization, request,
  unsupported-parameter, context-window, harness, agent, MCP, grading and whole-cell failures stop
  immediately. Every attempt, retry, scheduled delay and allowlisted failure category is counted.
  Each profile records its own stability, provider route, reasoning and temperature contract.
  LiteLLM 1.72.0 drops Responses `extra_body`; a pinned final-boundary adapter inserts only this
  reviewed route and fails on URL, model or route drift. A recovered row can contribute correctness
  but never token or wall-time efficiency because a failed
  attempt may be billed without usage and its retry delay is provider-health overhead. Tokens come
  only from the provider `usage` object, with
  `prompt`/`completion`/`total` recorded and never derived; each result carries `token_usage_status`
  of `not_started`, `complete` or `incomplete`, and an incomplete observation contributes no
  efficiency; it contributes correctness only when every model turn ultimately received a response
  under the pinned identity. Cost and per-task token attribution stay out of scope (ADR-0014).
- **DevNet-safe MCP surface (RD3, supersedes §6's "for CKB/testnet work prefer `mcp_call`"
  steering).** Scored phase-one runs are DevNet-only and the pinned endpoint is TestNet-bound, so
  C/D run under one fixed profile, `docs-only-v1`: exactly `search_resources` plus reserved
  `resources/read` calls under `ckb://docs/`. Every other tool name is absent from the model-visible
  catalog and rejected client-side before any request; A/B stay `off`. Prompts send live chain state,
  signing, submission and confirmation to the selected `CKB_RPC_URL` in every arm, and the
  `FALLBACK_RPC` marker is retired — it is meaningless once chain-bound MCP calls are outside the
  treatment. The configured profile is persisted as `mcp_surface_profile` (schema `1.2.0`) and a row
  whose profile is missing, unknown or wrong for its arm fails validation before aggregation or
  rendering. **RD3 is closed for this scoped treatment.** The headline is *the marginal effect of the
  pinned CKB AI documentation surface over ordinary web research on the frozen five-task DevNet
  suite*; the hosted chain tools, faucet, signing and deployment helpers are not measured (ADR-0013).
- **Matched B/C agent budget (RD2, supersedes the arm-aware step ceiling).** The production factory
  gives A, B, C and D one budget: **120 steps, 0.0 cost, 1200 seconds**. The earlier 80-for-A/B,
  40-for-C/D split made the headline `C - B` causally ambiguous — a difference could reflect CKB AI,
  the step ceiling, or both. The first uniform ceiling of 80 removed that asymmetry, but subsequent
  runs showed multiple model profiles exhausting all 80 steps. Raising the shared ceiling to 120
  reduces that censoring without giving either treatment more opportunity. Equal ceilings are not
  equal token use or elapsed time, which
  remain measured outcomes, and they do not erase the deliberate prompt/tool-surface difference: the
  1200-second ceiling replaces the original 900-second value after a matched cohort exhausted that
  limit in all three C cells and one B cell; the increase is symmetric and must be evaluated only
  with fresh rows. The only intended treatment difference is still CKB AI availability and
  steering. Every result persists the limits actually read from the agent's runtime config, and a
  result set whose concrete B/C budgets disagree fails validation before aggregation or rendering
  (ADR-0012).
  A row stopped by either ceiling keeps its verified grade and remains in the matched comparison;
  the report records the stop without estimating hypothetical work beyond the fixed budget.
