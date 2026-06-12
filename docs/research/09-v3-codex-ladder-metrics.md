# CKB AI Benchmark v3 - Second Opinion

## 5 revised-for-v3 recommendations

- Make the MVP leaderboard `suite_version x chain x condition`; never rank across suite versions. Show cross-version movement only as "new suite, new line".
- Keep A/B/C as core, make D a small diagnostic slice, and drop E from scored MVP. The real claim is `C - B`: MCP marginal value over normal web research.
- Treat no-research arms as compliance experiments, not hard capability controls, unless every run has egress logs proving no web traffic.
- Promote tokens/time to first-class but split `agent_wall_ms`, `model_wall_ms`, `tool_wall_ms`, `verifier_wall_ms`; report cost-per-correct and paired deltas with CIs.
- Steer MCP arms toward MCP. That is product-realistic, not a confound, if provenance logs show exactly when the agent used MCP, direct RPC, or web.

## 1. Versioned suites

Use an immutable manifest per suite:

`suite_id`, `suite_semver`, `created_at`, `task_ids`, `task_sha256`, `prompt_sha256`, `verifier_sha256`, `docker_image_digest`, `chain_profile`, `mcp_server_version`, `mcp_tools_digest`, `scoring_schema_version`.

Score rows should be append-only:

`suite_id, task_id, chain, condition, model_id, model_digest/version, agent_commit, run_seed, run_id, pass, verifier_status, tokens, wall_ms, cost_usd, provenance_summary`.

Rollup: task-level binary pass, then macro-average across frozen tasks; report by chain and condition first, then overall only as a weighted display field. The leaderboard should default to one suite tab at a time. A "history" chart may show each suite as a separate line, but not a single continuous rank. This matches the direction of living benchmarks such as HELM, which emphasizes standardized prompts/metrics plus raw generations for transparency, and SWE-bench, which separates Lite/Verified/Multilingual subsets rather than pretending they are one comparable pool. Sources: https://arxiv.org/abs/2211.09110, https://www.swebench.com/

## 2. Condition ladder

A proves only innate/model-prior ability under a prompt contract. Since internet is physically on, it is not a true no-research control unless audited.

B proves the value of ordinary web research over prior knowledge: `B - A`.

C proves the product claim: MCP value on top of normal research. This is the load-bearing delta: `C - B`.

D proves a narrower claim: curated MCP can replace open web when web is stale/wrong. It is useful, but expensive and prompt-enforced like A. Run D only on tasks where stale web is plausible: testnet operations, current script hashes, faucet/account workflows, protocol versions, RPC quirks. The useful deltas are `D - A` and `C - D`; if `D ~= C`, MCP is carrying the task. If `C >> D`, web is still doing real work.

E is not a benchmark condition; it is a product demo mode. It will collapse into C unless it adds extra tools not present elsewhere. Drop from scored MVP.

Cheating mitigation: put all agent traffic through an HTTP(S) proxy or container egress logger, block direct DNS except the proxy, and tag destinations as `model_api`, `mcp`, `ckb_rpc`, `package_repo`, `web`. No-research runs with non-allowed web egress should be marked `protocol_violation`, not failed or passed. Prompt adherence alone is known to need verifiable checks; IFEval is built around objectively checkable constraints for this reason. Source: https://arxiv.org/abs/2311.07911

## 3. DevNet and TestNet separation

Do not merge them. DevNet is the primary deterministic engineering score. TestNet is a separate "live network operations" score.

DevNet: fixed node image, fixed genesis/config, pre-funded deterministic keys, reset state per task or per run. Failures are agent failures unless verifier says infra broke.

TestNet: preflight faucet/RPC/indexer health, record tip height/hash, account balances, faucet response, RPC URLs, and verifier timestamps. Split outcomes into `pass`, `agent_fail`, `infra_fail`, and `protocol_violation`. Exclude `infra_fail` from correctness denominator but publish the infra-fail rate. Never let TestNet flakiness drag down DevNet rank.

## 4. Wall-clock and token metrics

Measure with a monotonic clock around phases:

`setup_wall_ms`, `agent_wall_ms`, `model_wall_ms`, `mcp_wall_ms`, `direct_rpc_wall_ms`, `web_wall_ms`, `verifier_wall_ms`, `total_wall_ms`.

For tokens, sum the provider usage objects from every model call. OpenAI Responses expose `usage.input_tokens`, `usage.output_tokens`, and `usage.total_tokens`; LiteLLM normalizes OpenAI-compatible completion usage as `prompt_tokens`, `completion_tokens`, and `total_tokens`, and also exposes latency via `response_ms`. Sources: https://platform.openai.com/docs/api-reference/responses/object, https://docs.litellm.ai/docs/completion/output

Report:

- median and IQR per `(suite, chain, condition, model)`;
- `cost_per_correct = total_cost_usd / number_passed`;
- paired deltas with 95% CIs for correctness, tokens, cost, and time;
- both `billable_tokens_including_retries` and `successful_call_tokens`.

Avoid latency confounds by separating model, MCP, web, RPC, and verifier time. Avoid retry confounds by logging retry count, retry reason, and whether retry tokens are billable. For correctness deltas, use task-paired bootstrap or exact paired tests; with tiny run counts, CIs should be visibly wide. Source: https://www.itl.nist.gov/div898/handbook/eda/section3/eda3668.htm

## 5. MCP steering and RPC fallback flags

Steering MCP arms to prefer MCP is legitimate. You are testing "does this product make agents better when used as intended", not a sterile model-knowledge benchmark. It becomes a confound only if the no-MCP arm is artificially handicapped. So B should allow normal web and direct RPC; C/D should add MCP plus a preference instruction.

Provenance should be event-level:

`run_id, step, action_kind, tool_name_or_command, args_hash, started_at, ended_at, rc, bytes_in/out, destination_class, error_class`.

The fork already routes MCP through `mcp_call` and returns `extra.mcp_tool`; extend the run harness to log every MCP action. Direct RPC provenance should come from an egress proxy and/or wrapper around known CKB RPC URLs, not model self-report. The agent's fallback flag is still useful, but store it as `declared_rpc_fallback=true` plus machine-derived `observed_direct_rpc=true/false`. Do not change score for fallback; publish it as a product gap table by MCP tool/task.

## 6. Matrix sizing

`6 models x 2 chains x 4-5 conditions x >=3 runs x tasks` is too big for MVP and still underpowered. Three runs is enough for smoke/exploration, not for claims about prompt-enforced A/D or flaky TestNet.

MVP-essential:

- Conditions: A/B/C on DevNet; B/C on TestNet.
- Models: 2-3 representative models first, not all six.
- Runs: use paired seeds across conditions; prefer 5 runs on the smaller core over 3 runs on every cell.
- Tasks: balanced slices by task family, with DevNet deterministic tasks carrying primary rank.

Droppable/defer:

- D full matrix: keep only as a diagnostic slice.
- E: drop from scored leaderboard.
- All-six-model sweep: run after the suite and logging stabilize.

Do a one-run screening pass across the broad matrix, then spend repeated runs on cells near decision boundaries or with high variance. The benchmark should measure MCP efficiency explicitly rather than infer it from pass rate.

## Biggest ladder/metrics risk

The no-research arms can create false certainty. If A or D silently uses web, the ladder stops measuring the intended conditions, and token/time deltas become hard to interpret. Egress logging is mandatory for any published no-research claim.

## Pushback

Do not launch v3 with five arms and six models. Ship a smaller, audited A/B/C core with serious provenance and cost accounting; add D only after the core leaderboard is credible.
