**Report written to `/tmp/v2-grokbuild.md` (96 lines, structured exactly per brief Output format).**

Only that file was created/written in `/tmp`. No files under `/home/username/ckb-mcp` were read-for-modify or modified in any way (confirmed via listing + status; all ckb-mcp access was read-only for grounding: README/CLAUDE.md/PROMPTS.md + key src for MCP HTTP/3112 details, dev tools + network detection + testnet faucet + independent-RPC verifier rules, docker-compose, offckb references in docs).

Brief read + web verification covered:
- offckb (`offckb node --network devnet|testnet|mainnet`, unified CLI, proxy mode, devnet ephemeral vs. shared testnet realities).
- Minimal MCP Streamable HTTP agents (OpenAI Agents SDK `MCPServerStreamableHttp`, mcp-agent, HF Tiny Agents ~70-line starters, LangChain etc.; all confirm simple direct client loops are feasible; no dominant pinned benchmark-grade one yet).
- Leaderboard/eval patterns (DeepSWE absolutes + history + verifiers with CIs/separation; SWE-bench harness multiplier + paired designs; bootstrap/McNemar for pass@1 + deltas).
- Stats + fairness (versioned frozen tasks, stratification, provenance flags for non-determinism).

The report leads with the required 5-bullet revised rec, has one tight "what changes + concrete proposal" section per constraint (heavy focus on #1 leaderboard+delta coexistence/fairness mechanics, #2 custom pinned agent details + pinning, #3 per-task config + determinism handling, #4 dual verifier + human flagging), then explicit biggest risk + pushback (on prompt-toggling), with inline URLs. Concise, opinionated, decisive. 

(Full content verified via read + wc; ends with key refs section.)
Date | ON % (CI) | OFF % | Δ (pp, sig) | N | Track. History: immutable per-version snapshots. ~50-80 frozen tasks for MVP.

## 1. ONE benchmark yielding both marketable absolute leaderboard + history AND rigorous MCP on/off causal delta (Constraint 1)

**What changes**: Round-1 pure A/B (delta-only, null-MCP OFF, zero-build static) becomes a public, living leaderboard product that must ship on MVP. Absolute ON scores are now the visible marketing asset; deltas are a first-class *dimension* inside it.

**Concrete proposal**:
- Fixed task corpus (v1: 50-80 tasks, ~half devnet-deterministic, half requiring testnet resources or realistic conditions). Tasks + gold verifiers frozen at a tag; new major versions add tasks only.
- Primary leaderboard ranks by **MCP-ON absolute Pass@1** (the "CKB AI makes you better at CKB" claim). Columns/tabs show OFF and Δ. Time-series: per-model line (or table rows) of ON score at each MCP server release tag + eval run date. Example row: `gpt-5.5 | ckb-ai-mcp v0.3.1 | 2026-06-10 | 48% ±4% | 31% | +17pp* (p<0.01) | 62 tasks | 65% devnet`.
- Fairness preservation:
  - **Paired execution**: Every task instance is run twice (ON + OFF) with identical agent code, model call params (temp=0, seed where supported), same starting state. Deltas computed only on matched pairs.
  - **Stratification**: Devnet tasks vs testnet tasks are flagged. Leaderboard supports "Full mix", "Devnet track", "Testnet track" filters or badges. Never claim cross-track comparability for absolutes.
  - **Versioning & anti-gaming**: Task definitions, prompts, agent harness, MCP server version, and verifier are all pinned per eval snapshot. Solutions/verifiers never shipped in prompt context or git history visible to agent. Periodic secret hold-out tasks + human audit (see #4).
- History view: simple table + sparkline of ON scores over MCP versions (the marketable "progress" artifact). Deltas live in an "Ablation / Causal" expandable or separate page.
- Stats (standard for this class of eval): binomial Pass@1 with bootstrap CI; for deltas use McNemar test or paired bootstrap. Report N, CI width, and p-values. See DeepSWE precedent for wide separation + low-verifier-error leaderboards producing credible absolutes + implicit deltas.

This keeps the two goals from corrupting each other by making absolutes the *public face* while deltas remain the *scientific proof* (never hidden, never the ranking key).

## 2. Lightweight built-in MCP-native agent replacing vendor CLIs (Constraint 2)

**What changes**: Codex CLI / Claude Code banned for reproducibility reasons (rapid churn, bugs, non-pinnable).

**Concrete proposal**:
- Build (do not adopt) a minimal custom harness agent. Existing options (OpenAI Agents SDK MCPServerStreamableHttp, lastmile-ai/mcp-agent, HF "Tiny Agents" ~70-line Python examples, LangGraph adapters, Smolagents MCPClient) are excellent starting points and prove Streamable HTTP + OpenAI-compat tool calling is straightforward in <200 LOC, but none are yet "the" pinned benchmark standard and all carry their own scaffolding/dependency drift. A benchmark that must be reproducible for years cannot outsource the agent loop.
- Target: 150-250 LOC single-file (or tightly vendored) Python (or TS). Uses official `mcp` client (streamable HTTP POST/JSON-RPC to http://mcp:3112/mcp) for initialize + tools/list + tools/call. Converts MCP tool schemas to OpenAI `tools` format. Simple ReAct-style or budget-limited loop calling an OpenAI-compatible endpoint (direct OpenAI, or LiteLLM for breadth; pin the client + exact model string + params).
- Pinning strategy: the entire agent lives in the benchmark repo at a tagged commit. `uv.lock` (or pip hash requirements) + exact container image + model provider snapshot. Re-run command is `python agent.py --task-id T42 --mcp-url ... --model gpt-... --on` (or OFF). No external CLIs, no "latest".
- Why this satisfies: speaks native Streamable HTTP MCP (the artifact under test), OpenAI-compatible models (any provider), lightweight, fully reproducible, zero dependence on vendor agent products.

This is the only defensible choice for a credible, long-lived benchmark of an MCP server.

## 3. devnet|testnet configurable per-task with determinism implications (Constraint 3)

**What changes**: OffCKB devnet is no longer the only target; testnet must be a first-class, permanent option (some tasks need testnet-only resources; offckb unifies the CLI).

**Concrete proposal**:
- Task metadata (JSON/YAML in suite): `{ "id": "T17", "chain": "devnet" | "testnet", "description": "...", "prompt": "Build ...", "verifier": {...} }`. Harness (not the model prompt) reads this.
- Execution: per-trial, harness spins the environment.
  - `devnet`: `offckb node --network devnet` (or equivalent containerized) in fresh ephemeral instance + isolated network; inject resulting local RPC (e.g. 28114 proxy or 8114) into `ckb-ai-mcp --ckb-rpc $RPC`. Full control over genesis, pre-funded accounts, no external contention.
  - `testnet`: `offckb node --network testnet` (proxy mode for tx recording/debug) or direct public endpoint (https://testnet.ckb.dev/rpc etc.). Same MCP server binary receives the remote RPC. Faucet tool in MCP (`dev_request_testnet_funds`) will work only here.
- Determinism: 
  - Devnet = high (repeatable given same offckb version + seed/config).
  - Testnet = low (shared chain state, mempool contention, external txs, variable block times, faucet rate-limits, possible congestion).
- Fairness across tracks and over time:
  - Never mix raw absolutes from devnet-heavy vs testnet-heavy evals in the same ranking.
  - Report track composition per row.
  - For history: only same-track or same-mix snapshots are directly comparable. When mix changes, note it explicitly and provide re-baseline numbers.
  - offckb's single `--network` flag makes the harness simple.

Prompts must not contain network hints that differ between arms (see pushback below).

## 4. Verifier that runs in-container against devnet AND against testnet (incl. occasional third-party human reviewers), flagging determinism loss (Constraint 4)

**What changes**: Verifier can no longer assume "always fresh local devnet inside Docker"; must support live testnet RPC and sporadic human overrides.

**Concrete proposal**:
- Verifier is a separate, pure-RPC component (Rust/TS/Go using ckb json-rpc or SDK). Input: task id + agent-produced artifacts (tx hashes, addresses, etc.). It queries the *parameterized* RPC endpoint for the trial (never the MCP server). Matches round-1 baseline and the project's own CLAUDE.md rule: "Setup and verification must use independent CKB RPC calls (NOT MCP)".
- Two modes, same code:
  - Devnet (in-container): verifier points at the ephemeral offckb RPC spun for that trial. Fully deterministic replay possible if container + offckb version pinned.
  - Testnet: points at stable public/testnet proxy RPC. Same checks (tx committed, cell data matches expected, balance deltas, script success, etc.).
- Human reviewers: testnet tasks (or a sampled subset) periodically enter a review queue (GitHub issue, simple dashboard, or email). Third-party humans inspect via explorer + direct RPC, vote pass/fail with justification. Overrides stored with full provenance (`reviewer: "alice@thirdparty", date, rpc_block_height, notes`). Auto-score is never deleted; both values shown.
- Flagging:
  - Every result row carries `env: {network, offckb_version?, rpc_endpoint_hash?, deterministic: bool, verifier_mode: "auto-devnet" | "auto-testnet" | "human-override-testnet"}`.
  - Leaderboard UI: testnet and human-reviewed cells get * or warning badges + "non-deterministic" tooltip. History lines are broken or annotated at network-mix changes.
  - Devnet rows remain the "clean reproducible" reference; testnet rows are the "real-world" credibility check.

This satisfies "runs in-container against devnet AND against testnet" while keeping the scientific claim honest.

## 5. Conciseness

All of the above is achievable with a small harness repo, one agent script, offckb + Docker for envs, direct-RPC verifier, and a static or very thin dynamic site for the leaderboard+history (no phases required for MVP if the above is implemented cleanly).

## ONE Biggest Risk Introduced by These New Constraints

The pressure to ship and maintain a visible, marketable absolute-score leaderboard + history will systematically erode the paired causal rigor. Over successive releases, the easiest way to "show progress" is to run more ON trials, drop noisy testnet tasks, loosen verifiers, or publish only the flattering ON numbers while deltas become buried or computed on non-identical sets. The two goals (marketing asset vs. clean treatment effect) are in tension; the design must actively fight the leaderboard's gravitational pull toward absolutes-only or devnet-only reporting, or the "proving MCP helps" claim becomes marketing, not evidence.

## One Thing in the New Direction I Would Push Back On

**Prompt-toggling of devnet vs testnet is unacceptable for causal validity.** The brief says "toggling it IN THE PROMPT is acceptable." This breaks the core invariant that ON vs OFF must differ *only* by the MCP server implementation. A capable model will see "you are targeting testnet" (or devnet) in the prompt and change its strategy, risk tolerance, cell-selection heuristics, fee logic, or even whether it bothers with certain dev-only shortcuts. That injects a second treatment (different agent policy) into the comparison. Network choice must be 100% harness-controlled and invisible (or identically worded) in the agent's prompt and system instructions for both arms. The MCP server under test already auto-detects chain type via genesis; the agent does not need the hint.

## Key References (URLs cited in text above)
- OffCKB + `--network`: https://docs.nervos.org/docs/sdk-and-devtool/offckb and https://github.com/ckb-devrel/offckb (via ecosystem docs in ckb-mcp)
- DeepSWE leaderboard design, verifier quality, separation, and contamination resistance: https://deepswe.datacurve.ai/blog and https://venturebeat.com/technology/deepswe-blows-up-the-ai-coding-leaderboard-crowns-gpt-5-5-and-finds-claude-opus-exploiting-a-benchmark-loophole
- MCP Streamable HTTP + lightweight agents examples: OpenAI Agents SDK (https://openai.github.io/openai-agents-python/guides/mcp/), lastmile-ai/mcp-agent (https://github.com/lastmile-ai/mcp-agent), Hugging Face Tiny Agents (https://huggingface.co/blog/python-tiny-agents), official quickstarts.
- SWE-bench / general agent harness multiplier and leaderboard pitfalls: https://www.swebench.com/ and DeepSWE analyses.
- CKB MCP server (Streamable HTTP /mcp on 3112, network detection, dev_* tools + testnet faucet, independent-verification rule): direct source in /home/username/ckb-mcp (no external URLs needed).

Report complete. Tight, opinionated, focused on the five constraints.