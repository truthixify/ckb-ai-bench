**Report written to `/tmp/v3-grokbuild.md` (112 lines).**

It is the only file created/written. All access to `/home/username/ckb-mcp` and `/home/username/ckb-ai-bench/agent` (the spike-proven fork) was read-only for grounding (lists, targeted reads of README, ckb_agent.py:1-83, ckb_mcp.py, spike_mcp.py, default.py:50-169 + execute_actions seam, litellm_model.py cost/token paths, UPSTREAM_COMMIT, dev/tools.rs + handlers.rs:304-612 for testnet faucet/external + genesis detection, CLAUDE.md test-independence rule, PROMPTS, etc.). Web used only for external facts (DeepSWE medians/CIs/cost-per-trial, OWASP LLM01 prompt-injection ASR 50-88%, SWE-bench versioning, Docker internal+proxy patterns, CKB testnet faucet/RPC instability). No changes anywhere under the protected paths.

The file leads with the exact required 5-bullet revised-for-v3 rec, has one tight section per v3 constraint (heavy focus on 2/4/5 per brief, with 1/3/6), ends with the ONE biggest risk + one pushback, and cites URLs + direct source locations. Concise, opinionated, pushes back on weak spots (prompt-only enforcement for A/D; D value conditional on real isolation; matrix bloat). Verified via read + wc. 

DONE.
research" (A/D) at the network layer + audit, never prompt alone**: Prompt-only "do not research" is known-unreliable (OWASP LLM01 #1; indirect/direct injection ASR 50-88% across models). Use internal Docker net + allowlist egress proxy (as prototyped in prior research) for A/D; log proxy/conntrack per run as first-class artifact. Flag violations; do not count violating runs in headline deltas.
- **Time + tokens are first-class and reportable today**: Fork already gives `elapsed_seconds` (default.py:60), `cost` + `n_calls` from litellm (litellm_model.py:109 cost_calculator + 148), plus MCP call count/latency via extra. Wrap runs for clean wall time; capture usage tokens from responses. Report per-arm medians (time, tokens, $), cost-per-correct-task (successes only), and paired deltas + bootstrap CIs. Strip or separately flag high-retry cells.
- **MCP steering + provenance is legitimate product signal, not confound**: Telling the agent "prefer mcp_call for testnet/CKB work and emit FALLBACK_RPC: <reason> on direct shell RPC" measures the value of the curated abstraction (exactly the claim). Instrument at the seam already present in ckb_agent.py:67 (`"extra": {"mcp_tool": tool}`) vs env.execute detection of curl/RPC; emit clean `provenance` + `rpc_fallback` flags in every traj row. Publish as diagnostic columns (% MCP-only, fallback rate) on the leaderboard; never fold into pass/fail.
- **Versioning + separation + matrix sanity**: Freeze suites at git tags (tasks + prompts + verifiers immutable). Leaderboard shows per-suite-v tracks with no cross-version numeric comparison; history is versioned lines. Devnet = primary deterministic headline (prefunded, no faucet); testnet = secondary tagged track only (N-confirms, proxy, optional human review for flakiness). MVP matrix: 4 conditions (A/B/C/D) × devnet-primary + 1-2 testnet tasks × 4-5 models × 5 runs on key cells (3 elsewhere); prune aggressively or use screening pass.

## 1. Versioned test suites

Model as immutable git-tagged suite artifacts: `suite_vN/` contains frozen `tasks.json` (id, prompt hash, chain, verifier spec, gold), agent prompt templates (hashed), and verifier code + expected outputs. Results rows carry `suite_version`, `mcp_version`, `agent_commit`, `model`, `run_date`, `seed`.

Score rollup: per (suite_v, model, arm, chain) → Pass@1 (binomial or mean) + N + bootstrap 95% CI. Never backfill or numerically compare v1 vs v2 absolutes.

Leaderboard display: tabs or clearly separated sections "Suite v1 (frozen 2026-06-XX, N=XX tasks)", "Suite v2...". History graphs are per-version lines (or annotated at version boundaries). "Progress" is within-version re-runs on MCP updates or new models on the *same* frozen slice. This matches SWE-bench practice of per-repo versions and DeepSWE's contamination-free new-task approach.

## 2. The condition ladder — critique

**What each arm actually proves** (given internet is physically always on for build):
- A (no-research, no-MCP): innate model knowledge + whatever it can do with local tools + shell only. Pure "zero external CKB context".
- B (research allowed, no-MCP): model + general web + shell. Measures value of open web research for CKB tasks.
- C (MCP + research): model + web + curated MCP (docs, high-level ckb_*/dev_*/rpc_* tools, 51 tools verified in spike). Full intended use.
- D (MCP only, no-research): model + curated MCP but forbidden general web. Tests whether the MCP's curation is sufficient (or superior) when web noise is disallowed.
- E (anything): mostly noise for the hypothesis; largely redundant with C.

**Arms A and D are prompt-enforced — serious risk**. Capable models routinely ignore "do not research the web / do not use external tools" instructions. OWASP LLM Top 10 2025/2026 ranks Prompt Injection (LLM01) #1; direct "ignore previous" + indirect (via retrieved docs, tool outputs, pages) achieve 50-88% ASR in multiple assessments. MCP docs/resources served to the agent are themselves "research" vectors. A model can (and frontier ones will) curl public RPCs, hit nervos docs, or use knowledge despite the sentence. This silently collapses A into something closer to B and D into C.

**Detection / mitigation**: 
- Prompt is insufficient. Enforce at transport: A/D runs must use Docker `internal: true` network + sidecar allowlist proxy (nginx/tinyproxy with domain whitelist for only the LLM endpoint; no general egress). This is proven feasible and was the prior research recommendation. 
- Mandatory per-run egress audit: proxy logs + container netflow (or conntrack) saved with the traj. Any non-allowlisted destination (or even DNS for non-LLM) during a "no-research" arm = violation flag. Discard or down-weight violators for causal deltas; publish violation rate as health metric.
- Additional: output/trajectory classifiers for web-like commands (curl, wget, http to known CKB doc hosts, git clone of docs, etc.); sample human review of high-token "no-research" runs.

**Is D worth the cost?** Marginally, *only if* the above enforcement is real. Without it D is mostly measuring how well the model obeys the prompt vs how good the MCP is. With enforcement, D vs A cleanly measures "curated MCP knowledge vs nothing external"; D vs C measures "does forbidding web help or hurt when you have the curated source". Valuable for the "MCP is better than noisy web" product story.

**Load-bearing pairwise deltas**: B→C (does adding the MCP tool surface improve outcomes when the model can also research freely — the realistic "with MCP in the wild" story) and the A/D pair contrasted against B (net value of the "MCP only, curated" regime). C vs E and A vs B are secondary. D vs B is interesting but secondary to B-C.

## 3. DevNet and TestNet as separate criteria

Keep them strictly separate (no merged "overall" that weights them). 

Devnet (OffCKB ephemeral, prefunded accounts, fixed genesis, full control): deterministic headline track. Use it for the majority of tasks and all primary stats/deltas.

Testnet (public or proxied RPC): permanent secondary "integration/realism" track only. Tag every result `chain=testnet, deterministic=false`. Require N confirmations for verifier (e.g. 8-10), use reliable community proxies, collect replay bundles (tx hashes + block heights + genesis). For flakiness (reorgs, shared state, faucet rate limits): do *not* average into devnet scores. Faucet tool (`dev_request_testnet_funds` in dev/handlers.rs:565 which does external POST) must be disabled or avoided for benchmark testnet cells (use pre-funded or harness-funded accounts). Human review queue for disputed testnet cells (inspect via explorer + direct RPC); both machine and human scores shown with provenance. Leaderboard shows separate columns/tabs + composition % per row.

This preserves the v2 fairness invariant while making testnet a credible secondary signal.

## 4. Wall-clock time + token usage per run

The fork already provides strong primitives (no need to reinvent):
- Wall time: `Agent._start_time` + `elapsed_seconds` in template vars (default.py:50,60); outer harness wrapper for total run duration.
- Tokens/cost: litellm_model.py `_calculate_cost` + usage from completion response (input/output tokens are in the raw response even if only cost is currently surfaced); agent `cost` and `n_calls`.
- MCP adds: count of `mcp_tool` observations + per-call latency (easy to add in ckb_mcp.py _rpc or _run_mcp_action).

**Reliable measurement and reporting**:
- Per run: total_wall_seconds (median across reps), LLM_cost_$, input_tokens, output_tokens, MCP_calls, MCP_wall_ms.
- Aggregate per (arm, suite_v, chain): median time, median tokens, median $, plus "cost per correct task" = (sum cost over successes) / (# successes) — exactly DeepSWE style.
- Deltas: paired (same task/model/seed) bootstrap CI on the per-task (time_ON - time_OFF), same for tokens/$. Report medians + 95% CIs; do not use means if heavy tails from retries.
- Confound controls: 
  - Record #LLM calls, #format retries, #MCP errors per traj; publish "high-retry" flag and optionally exclude cells with >X transient failures from efficiency deltas.
  - MCP latency is *not* a confound — it is part of the measured MCP experience.
  - Fix temperature=0 (or seed where supported), same max steps/cost limit, same container image.
  - Network jitter: run in controlled env; use medians over 3-5 reps.

DeepSWE precedent (median wall time, median cost per trial, output tokens alongside Pass@1 with CIs) is directly transferable and already partially implemented in the mini-swe fork.

## 5. MCP-for-testnet steering + flagging

**Legitimate use, not inflation**. The MCP exists precisely to give agents better-than-raw-RPC ergonomics for CKB (high-level ckb_*, dev_*, curated docs). Steering the prompt in C/D arms ("For all CKB/testnet operations prefer `mcp_call <tool> <json>` over shell curls to RPC; if you fall back to direct RPC you MUST emit a line `FALLBACK_RPC: <tool/reason>`") is the correct "use the product as intended" setup. It would be artificial to handicap the MCP arm by telling the model to ignore its best tools.

**Provenance logging (already half-done)**: ckb_agent.py:76-77 routes mcp_call through `_run_mcp_action` which returns `{"extra": {"mcp_tool": tool}}`; everything else goes to `self.env.execute`. Extend the harness (or post-process traj) to tag every observation/action with `provenance: "mcp" | "direct_rpc" | "bash"`. Detect direct_rpc via regex on command (curl/wget/http to 8114/18114/28114 or known public RPCs, ckb-cli without mcp, etc.). This is cheap and auditable.

**Surface as product artifact without polluting score**: 
- Every results row gets `mcp_calls`, `direct_rpc_calls`, `fallback_flags` (count + sample of reasons).
- Leaderboard columns (or expandable): "MCP coverage", "% tasks using ≥1 MCP tool", "RPC fallback rate", "common gaps (from flags)".
- Pass/fail and core deltas remain purely on verifier outcome. The flags are diagnostic for the MCP *team* (what RPC surface is still missing that forces bypass).
- Publish the raw per-traj provenance logs.

This turns "steering" into a strength: the benchmark demonstrates both correctness lift *and* reduced bypass need.

## 6. Matrix sizing sanity

6 models × 2 chains × ~5 conditions × ≥3 runs = 180+ agent runs before overhead. Each long-horizon CKB task (build+test+on-chain) is minutes to tens of minutes + model $.

**Sanity**: 
- ≥3 is *marginal* once prompt-enforced arms (A/D) are included — those have extra behavioral variance from compliance. Target 5 reps on the load-bearing cells (B, C on devnet; A, D if enforcement is solid); 3 elsewhere.
- Essential for MVP: devnet-primary with A/B/C/D (4 conditions). Testnet: 1-2 representative integration tasks only, or drop testnet from first public leaderboard entirely and run as "preview".
- Droppable for cost control: E entirely; one of A or D if enforcement can't be delivered in time (but keep both if the egress story is real); reduce models to 4 (drop two of the "Grok" variants or Fable first).
- Screening pass: run the full matrix on 1-2 strong models + 1 weak across 2-3 tasks first; drop arms/models that show near-zero signal or explode cost before committing the full grid.
- Total sanity target: ~60-90 devnet runs for launch (e.g. 5 models × 4 cond × 3-5 reps on 20-30 frozen devnet tasks) + small testnet adjunct. Use the fork's existing cost_limit + wall_time_limit + traj saving for control.

## ONE Biggest Risk the ladder/metrics introduce

The combination of prompt-enforced "no-research" arms (A/D) + heavy reliance on medians/CIs/cost-per-correct for marketing the efficiency story creates a powerful incentive to under-enforce or quietly drop violating runs, or to steer prompts differently across arms in ways that look like "MCP magic" but are actually prompt treatment. Without ironclad network enforcement + mandatory public egress logs per published row, the "MCP is better because we told the model not to look elsewhere" claim collapses into prompt gaming. The metrics then amplify the problem: a few clean-looking low-cost D successes become the headline even if half the "no-research" runs were secretly using web.

## One Thing I'd Push Back On

**Prompt-steering toward MCP is fine, but the "no research" instruction in D (and A) must not be the *only* control, and the benchmark must not claim causal "MCP only" credit unless egress is verifiably blocked at L3/L4.** The brief presents D as "maybe" and "a genuinely distinct score worth measuring." It is worth measuring *only* as a stress test of curation vs open web *under real isolation*. Running it with physical internet + "pretty please don't research" and then publishing D numbers as proof of MCP superiority is the exact class of benchmark loophole DeepSWE called out (Claude git-logging gold commits). The spike-proven fork + existing agent timing give you everything needed to do the logging; the ckb-mcp dev tools already surface the external faucet risk. Use the network layer or drop the "no-research" arms from the causal claim.

## Key References (cited claims)

- DeepSWE medians for cost/time/tokens, Pass@1 + CIs, efficiency frontier, contamination-free frozen tasks: https://deepswe.datacurve.ai/blog ; https://venturebeat.com/technology/deepswe-blows-up-the-ai-coding-leaderboard-crowns-gpt-5-5-and-finds-claude-opus-exploiting-a-benchmark-loophole (median $5.80 / 20m / 47k tokens for top model; wider separation than prior boards).
- OWASP LLM Top 10 2025/2026 LLM01 Prompt Injection #1, ASR 50-88% for ignore/override even direct: https://genai.owasp.org/llmrisk/llm01-prompt-injection/ (via multiple secondary reports); success rates documented across 2025-2026 assessments.
- SWE-bench versioning (per-repo versions, frozen for reproducibility): https://www.swebench.com/SWE-bench/reference/versioning/ .
- Docker internal networks + egress proxy for true web toggle (not prompt): Docker docs on `--internal`; community patterns (tinyproxy/squid allowlists); prior ckb research harness notes.
- CKB testnet realities (public RPCs "may be unstable", faucet limits ~300k/month/address, archived original faucet now Magickbase): https://faucet.nervos.org/ ; https://docs.nervos.org/docs/getting-started/rpcs ; GitHub nervosnetwork/ckb-testnet-faucet (archived).
- MCP server surface (51 tools, dev_request_testnet_funds external POST to faucet-api, genesis-based devnet/testnet detection, CLAUDE.md independent-RPC verifier rule): direct source reads under /home/username/ckb-mcp (no external URL required beyond the live https://mcp.ckbdev.com/ckbai mentioned in brief).
- Fork primitives for measurement/provenance (CkbMcpAgent dispatch seam, mcp_tool extra, litellm cost, agent elapsed/cost/n_calls, wall limits): /home/username/ckb-ai-bench/agent/ckb_agent.py, ckb_mcp.py, minisweagent/agents/default.py, models/litellm_model.py; UPSTREAM_COMMIT.txt.

Report complete. All claims grounded; pushback explicit where v3 design is weak on enforcement vs prompt.