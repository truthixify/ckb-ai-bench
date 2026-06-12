# CKB AI Benchmark — Research & Recommendation

This folder holds the research and design recommendation for a benchmark suite that proves (or
disproves) whether the **CKB AI MCP server** (`/home/username/ckb-mcp`) measurably improves an AI
coding agent at Nervos CKB development. Modeled loosely on [DeepSWE](https://deepswe.datacurve.ai),
but simpler. The MVP is a **living leaderboard** (absolute Pass@1 scores + history) that also reports
the **MCP on/off causal delta** from the same runs.

## Start here

- **[RECOMMENDATION.md](RECOMMENDATION.md)** — the current (**v3**) architecture. Read this first.
  v3 added: versioned suites, a condition *ladder* (headline delta = `C − B`, MCP value over web
  research), per-run time + token metrics, MCP steering + provenance, and the model×chain×condition
  matrix. The v2 → v3 delta is in its "What changed" table.
- **[../agent/README.md](../agent/README.md)** — the spike-proven mini-swe-agent fork that adds native
  MCP (PASSED end-to-end against the live server).

## Research (raw inputs, preserved)

Three rounds, 2026-06-12. **R1** (00–05): four model families + repo verification → baseline. **R2**
(06–07): leaderboard-first / no-vendor-CLI / dual-chain revision. **R3** (08–10): condition ladder,
time/token metrics, versioned suites. A working-code **spike** (the fork) sits between R2 and R3.

- [research/00-deepswe-reference.md](research/00-deepswe-reference.md) — the DeepSWE site captured live.
- [research/01-grok-build-verification-isolation-stats.md](research/01-grok-build-verification-isolation-stats.md)
  — xAI grok-build @ max: deterministic CKB verification, Docker network isolation, statistics.
- [research/02-grok-composer-reporting-design-critique.md](research/02-grok-composer-reporting-design-critique.md)
  — xAI grok-composer: reporting site + skeptical critique.
- [research/03-self-research-harness-mcp-egress.md](research/03-self-research-harness-mcp-egress.md)
  — Anthropic Opus subagent + repo reads: harness MCP support matrix, transport, egress control.
- [research/04-codex-harness-confound.md](research/04-codex-harness-confound.md) — OpenAI gpt-5.5 @ xhigh:
  harness choice + confound control, line-precise repo citations.
- [research/05-adjudication.md](research/05-adjudication.md) — round-1 convergence/divergence + rulings.
- [research/06-v2-grok-build-leaderboard-secondopinion.md](research/06-v2-grok-build-leaderboard-secondopinion.md)
  — round-2 grok-build: leaderboard + delta coexistence, built-in agent, dual-chain, fairness.
- [research/07-v2-codex-leaderboard-secondopinion.md](research/07-v2-codex-leaderboard-secondopinion.md)
  — round-2 gpt-5.5: same questions; converged with grok-build.
- [research/08-v3-grok-build-ladder-metrics.md](research/08-v3-grok-build-ladder-metrics.md)
  — round-3 grok-build: condition ladder critique, time/token metrics, suite versioning (read the spike code).
- [research/09-v3-codex-ladder-metrics.md](research/09-v3-codex-ladder-metrics.md)
  — round-3 gpt-5.5: same; phase-split timing, event-level provenance, D-as-diagnostic.
- [research/10-v3-adjudication.md](research/10-v3-adjudication.md) — round-3 convergence + the ruling on
  prompt-only vs network-layer enforcement of the no-research arms.

## The one-paragraph answer (v3)

Ship a **versioned leaderboard**. Each suite version freezes tasks + prompts + verifiers; you score by
suite and never cross-rank versions. For each suite, run a **model × chain × condition** matrix, ≥3×
per cell, scoring **Pass@1 + wall-time + tokens** (cost is a headline). Conditions form a **ladder** —
A no-research, B web research, C MCP+web, plus a D MCP-only diagnostic slice — and the **headline result
is `C − B`**: what the MCP adds *on top of* ordinary web research (a harder, more honest bar than
on/off). Internet stays **always on** (owner's call); the no-research arms are **prompt-enforced** and
therefore *compliance-dependent*, so the agent **logs any web access** and violating runs are flagged out
of the A/D numbers. **DevNet** (deterministic) and **TestNet** (live-ops, `infra_fail` excluded from the
denominator) are **separate scores**. **MCP steering is legitimate** with **machine-logged event-level
provenance** (MCP vs direct-RPC vs web) + an RPC-fallback gap table. The agent is the **spike-proven fork**
of mini-swe-agent + a native MCP client. **Launch is the full matrix** (all 6 models × both chains ×
ladder × 3 runs) — bold and complete, with **CIs shown** on every number (3 runs ⇒ wide CIs, disclosed
not hidden); screening happens during the build, then a straight-to-full-matrix launch run. Pin every digest.
