# CKB AI Benchmark v2 - Second Opinion

## Revised Recommendation

- Ship one benchmark harness with two published views: `absolute_score` leaderboard rows and a separately labeled `paired_mcp_delta` panel from the same run manifest.
- Freeze a canonical paired task slice for causal claims; let the full leaderboard grow, but never mix ad hoc/testnet-only tasks into the delta statistic.
- Build the agent into the harness: a small pinned TypeScript/Python runner using the official MCP client transport and OpenAI-compatible `/v1/chat/completions`, not Codex CLI or Claude Code.
- Treat `chain_target = devnet|testnet` as part of task identity, score partitioning, and leaderboard filters; never compare devnet and testnet tasks as one undifferentiated score.
- Make verifier modes explicit: deterministic in-container devnet verification is the primary score; testnet verification is valid but tagged `external-testnet`, with replay artifacts and optional human review notes.

## 1. One Benchmark, Leaderboard Plus Causal Delta

Use a single immutable `run_manifest.jsonl`: `{task_id, task_version, model_id, model_endpoint_hash, agent_version, mcp_server_version, mcp_arm, chain_target, verifier_mode, seed, result}`.

Public leaderboard row: `score = pass@1 on the selected public suite`, plus cost/time/tool-call columns, like DeepSWE and SWE-bench already do for marketable public comparisons. DeepSWE publishes Pass@1 with cost/time/token columns and confidence bands; SWE-bench publishes percent resolved and comparison views. Cite both patterns, but do not copy their agent stack. Sources: https://deepswe.datacurve.ai/ and https://www.swebench.com/.

Causal claim: only computed on tasks that have paired ON and OFF results for the same `{model_id, task_id, chain_target, seed}`. Show `absolute_on`, `absolute_off`, `delta_pp`, paired bootstrap CI, and McNemar p-value. SciPy supports paired bootstrap via shared resampling indices, and statsmodels exposes McNemar for paired binary outcomes. Sources: https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html and https://www.statsmodels.org/stable/generated/statsmodels.stats.contingency_tables.mcnemar.html.

History: append-only daily/weekly snapshots keyed by `suite_version`. The public graph is absolute score over time; the rigorous graph is delta over time on the frozen paired slice. If the suite changes, start a new line, do not backfill.

## 2. Built-In MCP-Native Agent

Yes: build it. A 200-400 LoC custom agent is the right MVP if it is deliberately boring: loop over model response, tool calls, MCP calls, shell edits, verifier. Existing minimal agents are useful references, but mini-SWE-agent is not acceptable as-is because it is bash-only and explicitly does not use model tool-calling, so it cannot test MCP natively. Source: https://mini-swe-agent.com/latest/.

Spec:

- MCP client: Streamable HTTP MCP, endpoint configurable, default `http://localhost:3112/mcp`. MCP 2025-06-18 requires JSON-RPC over POST/GET at one endpoint and `Accept: application/json, text/event-stream`. Source: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports.
- Model API: OpenAI-compatible chat completions, pinned `base_url`, `model`, temperature, max tokens, tool schema rendering, and response parser. LiteLLM documents the OpenAI-compatible endpoint shape and `/v1` base-url convention. Source: https://docs.litellm.ai/docs/providers/openai_compatible.
- SDK: prefer pinned official MCP TypeScript SDK `@modelcontextprotocol/client` v1.x, because it ships client libraries and Streamable HTTP transports; avoid pre-alpha v2. Source: https://github.com/modelcontextprotocol/typescript-sdk.
- Reproducibility: container image digest, lockfile, agent git SHA, MCP server git SHA, prompt hash, null-MCP implementation hash, model endpoint hash. Model providers can still drift, so leaderboard rows must include `run_date` and exact provider metadata.

Local grounding: `/home/username/ckb-mcp` is already a Rust alpha Streamable HTTP MCP server on port `3112`, `/mcp`, with RPC/dev/docs/search tools and `rmcp` streamable HTTP enabled.

## 3. Devnet/Testnet Axis

Make `chain_target` first-class in suite YAML:

```yaml
chain_target: devnet | testnet
rpc_url: ...
funding: prefunded | faucet | provided
determinism: deterministic | testnet-dependent
```

Prompting the agent with the target is fine, but config is authoritative. OffCKB supports `--network` across devnet/testnet/mainnet paths locally in the repo; its vendored README says deploy/deposit/transfer/balance are devnet/testnet-oriented, and its source validates `devnet`, `testnet`, `mainnet`.

Fairness rule: publish separate suite tracks: `core-devnet`, `testnet-integration`, and `combined-weighted`. The headline MVP should default to `core-devnet`; testnet tasks are a visible secondary track, not hidden noise inside the main score.

## 4. Verifier In Container And On Testnet

Verifier contract:

- It never calls the MCP server.
- It reads only task output, workspace, and direct CKB RPC/indexer.
- It emits structured evidence: tx hashes, block numbers, cell outpoints, script hashes, RPC endpoint, chain genesis hash, confirmations, and reviewer IDs when human review is used.

Modes:

- `devnet-container`: start OffCKB/local CKB inside the container, seed accounts, run agent, run verifier against container RPC. This is the deterministic default and leaderboard primary.
- `testnet-rpc`: run against configured testnet RPC, require minimum confirmations, retry read-only verification only, and mark result `testnet-dependent`.
- `human-testnet-review`: third-party reviewer checks replay artifacts and tx evidence; score remains machine result unless the task rubric explicitly includes human judgment.

Testnet results should be reproducible enough to audit, not deterministic. Store replay bundles so a reviewer can rerun verifier logic against historical tx hashes.

## 5. Keep It Short

MVP scope should be strict: 20-40 tasks, one built-in agent, one null-MCP arm, one static leaderboard, one frozen paired slice. Add models and tasks later. Do not add vendor CLIs, multi-agent orchestration, or rich UI before the scoring contract is stable.

## Biggest New Risk

The leaderboard incentive will pressure the team to add attractive but non-paired, testnet-dependent tasks, then accidentally cite those scores as MCP causal evidence. Prevent this structurally: the site must render absolute scores and MCP delta as different metrics with different eligibility rules.

## Pushback

I would push back on "testnet must always remain viable" if it means testnet contributes to the headline score. Keep testnet permanent, public, and useful, but make `core-devnet` the canonical headline until the verifier has enough replay evidence to prove testnet variance is small.
