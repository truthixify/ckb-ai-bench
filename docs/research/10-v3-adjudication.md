# Adjudication — Round 3 (condition ladder, metrics, versioned suites)

**Inputs:** `08-v3-grok-build-...` (xAI grok-build @ max), `09-v3-codex-...` (OpenAI gpt-5.5 @ high),
my own pre-read, and the spike code. **Decision: one round-3 pass sufficient** — the two reviewers
converged on every major point and both read the actual spike code.

## Full convergence (settled)

- **`C − B` is the load-bearing delta** — MCP's marginal value *over ordinary web research*. Both named it
  independently; it's the honest, real-world bar (users have the web). Headline of the leaderboard.
- **Drop E from scored MVP** (product demo; collapses into C). Both.
- **A and D are prompt-enforced → compliance experiments, not hard controls.** Both flagged the loophole
  (prompt-injection bypass rates; DeepSWE's caught-cheating precedent) and both want egress logging.
- **Tokens/time first-class**; codex's refinement adopted: **split wall-time by phase** (model/mcp/rpc/web/
  verifier) and report **both** billable-with-retries and successful-call tokens; `cost_per_correct`;
  paired CIs; medians (heavy tails).
- **MCP steering is legitimate** if provenance is **machine-logged event-level**, not model self-report;
  store both `declared_rpc_fallback` and `observed_direct_rpc`; never score on fallback; publish a gap table.
- **Matrix too big + underpowered at 3 runs.** Both: 2–3 models first, A/B/C, DevNet-primary, screening
  pass, 5 runs on load-bearing cells.
- **Suites versioned & immutable; per-version scoring; no cross-rank; no backfill** (HELM/SWE-bench precedent).
- **DevNet/TestNet separate**; codex's outcome split adopted: `pass / agent_fail / infra_fail /
  protocol_violation`, infra-fail excluded from denominator but published.

## The one place reviewers pushed against the owner — and my ruling

Both reviewers pushed **hard** for network-layer egress *enforcement* of the no-research arms (Docker
`internal` + allowlist proxy), warning that prompt-only "don't research" collapses A→B and D→C and that
publishing D as MCP-superiority proof is "the exact benchmark loophole DeepSWE called out."

**The owner explicitly chose prompt-only enforcement, internet always on.** I did not override that.
**Ruling:** honor the decision, but convert the reviewers' concern from *enforcement* to *detection* —
which is compatible with internet-always-on: the agent logs any web access, no-research runs that touch
the web are marked `protocol_violation` (not pass/fail) and excluded from A/D headline numbers, and the
violation rate is published next to every A/D number. The network-layer enforcement path is documented as
the available, deferred fix if A/D numbers ever need to be airtight. This respects the owner's call while
closing the loophole as far as detection allows. Surfaced in RECOMMENDATION §3 and "the one risk."

## Codex's useful D refinement (adopted)

Run **D only on tasks where stale web is plausible** (testnet ops, current script hashes, faucet/account
workflows, protocol/RPC quirks); informative deltas are `D − A` and `C − D` (D≈C ⇒ MCP carries it;
C≫D ⇒ web still does real work). Keeps D cheap and meaningful instead of a full matrix arm.

## Spike folded in

The fork already exposes the metric/provenance primitives the reviewers built on: `elapsed_seconds`
(default.py:60), litellm cost + token usage, `n_calls`, and the `extra.mcp_tool` provenance tag
(ckb_agent.py:67). So time/token/provenance reporting is mostly wiring, not new infrastructure — both
reviewers verified this against the code.
