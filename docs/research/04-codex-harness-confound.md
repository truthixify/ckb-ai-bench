# CKB AI Benchmark Suite Research Report

## Executive Recommendation

- **Harness:** Use a custom thin MCP-native harness as the primary benchmark runner. It should expose only shell/file tools plus MCP tools loaded from `http://ckb-mcp:3112/mcp` in the ON arm. Use Codex CLI and Claude Code only as secondary "supported product" compatibility tracks. Do not use OpenHands/aider for the primary proof. Use mini-swe-agent only with a shim if speed matters more than native MCP semantics.
- **Verifier:** Use a fresh local CKB devnet per trial or per task, not shared testnet, for stateful tasks. Verifiers must use direct CKB RPC, never the MCP server under test. This matches the repo's own testing rule in [CLAUDE.md](/home/username/ckb-mcp/CLAUDE.md:54).
- **Network isolation:** Put agent, CKB node, MCP server, and an LLM proxy on an internal Docker network. Only the proxy gets outbound internet. Web access becomes a separate allowlisted proxy toggle, not a prompt instruction.
- **Stats:** Primary metric is Pass@1 or per-task mean score. Run at least 30 tasks x 5 repeats per arm for a pilot, then 50 to 80 tasks x 5 to 10 repeats per arm for a defensible public claim. Report ON-OFF delta with a paired, task-level bootstrap CI and a paired significance test.
- **Reporting:** Build a static single-page report from `results.json`: paired ON/OFF bars plus a delta-with-CI chart, and a table with tasks, repeats, pass rates, delta, CI, p-value, cost, time, model, harness, MCP version, and verifier commit.

## 1. Harness Choice

### Verified CKB MCP Surface

The artifact under test is exactly what the brief says:

- `ckb-ai-mcp` is a unified MCP server with RPC tools, CKB composite tools, dev tools, documentation resources, and workflow prompts, marked alpha in [README.md](/home/username/ckb-mcp/README.md:5).
- It uses MCP protocol `2025-06-18` over Streamable HTTP, default port `3112`, and endpoint `/mcp` in [README.md](/home/username/ckb-mcp/README.md:24) and [main.rs](/home/username/ckb-mcp/crates/ckb-ai-mcp/src/main.rs:1).
- Runtime flags include `--ckb-rpc`, `--private-key`, `--docs-only`, `--rpc-only`, `--tools-only`, and `--no-prompts` in [main.rs](/home/username/ckb-mcp/crates/ckb-ai-mcp/src/main.rs:35).
- The HTTP router exposes `/health`, `/stats`, `/deploy/file`, `/rpc`, and `/mcp` in [server.rs](/home/username/ckb-mcp/crates/ckb-ai-mcp/src/server.rs:111).
- The server intentionally uses stateless Streamable HTTP to avoid stale MCP session IDs with Codex and Claude Code in [server.rs](/home/username/ckb-mcp/crates/ckb-ai-mcp/src/server.rs:145).
- Tool categories are real: RPC tools, dev tools, CKB composite tools, and search tools are registered in [capabilities.rs](/home/username/ckb-mcp/crates/ckb-ai-mcp/src/capabilities.rs:184), with concrete definitions in [rpc/tools.rs](/home/username/ckb-mcp/crates/ckb-ai-mcp/src/rpc/tools.rs:1), [dev/tools.rs](/home/username/ckb-mcp/crates/ckb-ai-mcp/src/dev/tools.rs:1), [ckb/tools.rs](/home/username/ckb-mcp/crates/ckb-ai-mcp/src/ckb/tools.rs:1), and [search/tools.rs](/home/username/ckb-mcp/crates/ckb-ai-mcp/src/search/tools.rs:1).

External protocol check: MCP Streamable HTTP requires one endpoint, such as `/mcp`, handling POST and optionally GET with JSON-RPC over HTTP/SSE. The 2025-06-18 spec says Streamable HTTP replaced old HTTP+SSE and requires clients to use POST with `Accept: application/json, text/event-stream` for JSON-RPC messages: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports.

### Recommended Primary: Custom Thin MCP-Native Harness

Use a small in-repo benchmark runner, not a product coding agent, for the main A/B claim.

Reason:

- The variable under test is MCP. The harness should make MCP integration explicit, log every tool exposed and called, and let ON/OFF differ only by the MCP server being reachable.
- Claude Code, Codex CLI, and OpenHands all add product-specific prompts, permission systems, tool search, editing tools, session state, and hidden defaults. Those are valuable products but harder to audit as an experimental instrument.
- mini-swe-agent is attractive because it is tiny, but upstream docs and source show its default tool surface is a single `bash` tool. The docs say every response must include a bash tool call and show no MCP config surface: https://mini-swe-agent.com/latest/usage/mini/. The parser source defines only `BASH_TOOL` and rejects unknown tool names: https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/src/minisweagent/models/utils/actions_toolcall.py.

Minimal harness shape:

```text
runner/
  agent.py              # loop: prompt -> model -> tool calls -> observations
  mcp_client.py         # Streamable HTTP initialize/list_tools/call_tool/resources/read
  tools.py              # shell, read_file, write_file, apply_patch
  configs/on.yaml       # mcp_url: http://ckb-mcp:3112/mcp
  configs/off.yaml      # mcp_url: null or null-MCP server
```

ON arm:

```yaml
model: ${MODEL_ID}
tools:
  shell: true
  files: true
mcp:
  ckb_ai:
    url: http://ckb-mcp:3112/mcp
    required: true
    enabled_tools: "*"
```

OFF arm:

```yaml
model: ${MODEL_ID}
tools:
  shell: true
  files: true
mcp:
  ckb_ai:
    url: null
```

Better OFF control: run a **null MCP server** at the same URL that returns zero tools/resources/prompts and the same initialize metadata shape. That makes the only external difference the CKB AI server implementation, not timeout behavior or missing-host errors.

### Claude Code

MCP connection mechanics are clean and documented. Claude Code supports remote HTTP MCP servers:

```bash
claude mcp add --transport http ckb-ai http://ckb-mcp:3112/mcp
```

The CKB repo README uses the same shape for this server: [README.md](/home/username/ckb-mcp/README.md:77). Anthropic's docs specify `claude mcp add --transport http <name> <url>`, bearer headers via `--header`, and JSON config where `type` can be `http` or `streamable-http`: https://code.claude.com/docs/en/mcp.

For benchmark use, prefer a per-run MCP config file:

```json
{
  "mcpServers": {
    "ckb-ai": {
      "type": "http",
      "url": "http://ckb-mcp:3112/mcp",
      "timeout": 300000
    }
  }
}
```

Run:

```bash
claude -p \
  --mcp-config /bench/mcp.on.json \
  --strict-mcp-config \
  --no-chrome \
  --tools "Bash,Read,Edit,Write" \
  --disallowedTools "WebFetch" "WebSearch" \
  --permission-mode bypassPermissions \
  --output-format stream-json \
  --max-turns 80 \
  "$TASK_PROMPT"
```

OFF:

```bash
claude -p \
  --mcp-config /bench/mcp.off.json \
  --strict-mcp-config \
  --no-chrome \
  --tools "Bash,Read,Edit,Write" \
  --disallowedTools "mcp__*" "WebFetch" "WebSearch" \
  --permission-mode bypassPermissions \
  --output-format stream-json \
  --max-turns 80 \
  "$TASK_PROMPT"
```

Claude Code is a good secondary track because CKB AI officially documents it, but it is a poor primary experimental harness. Its default prompt and tool semantics are product-specific. Its CLI docs expose many controls, including print mode, `--mcp-config`, `--strict-mcp-config`, `--tools`, `--disallowedTools`, `--no-chrome`, `--max-turns`, and output formats: https://code.claude.com/docs/en/cli-reference.

### Codex CLI

Codex supports MCP in CLI and IDE extension, including Streamable HTTP servers. Its config uses `[mcp_servers.<name>]` with `url`, optional auth headers, `required`, `enabled`, `enabled_tools`, `disabled_tools`, and tool timeouts: https://developers.openai.com/codex/mcp.

ON config:

```toml
# /bench/codex-on/config.toml
model = "gpt-5.4"
sandbox_mode = "workspace-write"
approval_policy = "never"

[sandbox_workspace_write]
# Let shell/test commands reach internal Docker services. Docker/proxy policy,
# not Codex prompt policy, blocks public web.
network_access = true
writable_roots = ["/workspace"]

[mcp_servers.ckb_ai]
url = "http://ckb-mcp:3112/mcp"
required = true
startup_timeout_sec = 20
tool_timeout_sec = 300
```

Run:

```bash
CODEX_HOME=/bench/codex-on \
codex exec \
  --skip-git-repo-check \
  --ignore-rules \
  --json \
  --ephemeral \
  --output-last-message /workspace/final.txt \
  "$TASK_PROMPT"
```

OFF config:

```toml
# /bench/codex-off/config.toml
model = "gpt-5.4"
sandbox_mode = "workspace-write"
approval_policy = "never"

[sandbox_workspace_write]
network_access = true
writable_roots = ["/workspace"]

[mcp_servers.ckb_ai]
url = "http://null-mcp:3112/mcp"
required = true
startup_timeout_sec = 20
tool_timeout_sec = 300
```

Codex `exec` is scriptable and can emit JSONL. Official docs say `codex exec` is for non-interactive automation, supports `--json`, `--ephemeral`, explicit sandbox settings, and machine-readable events including MCP tool calls: https://developers.openai.com/codex/noninteractive. In this benchmark, set Codex command networking on and enforce egress at Docker/proxy level so shell tools can reach the CKB node while public web remains blocked.

Codex CLI is the best official secondary track because its MCP config is plain TOML and its non-interactive mode is designed for CI-style runs. It still should not be the primary proof because the product harness is not fully transparent.

### mini-swe-agent

Verdict: **No native MCP support found in current upstream docs/source.**

What it supports:

```bash
mini -t "$TASK_PROMPT" -c mini.yaml -m "$MODEL" -y -o trajectory.json
```

Docs show `mini` uses config files, model selection, yolo mode, and a bash-centered interaction loop: https://mini-swe-agent.com/latest/usage/mini/. The default prompt says the agent can execute bash commands and edit files, and every response must include at least one bash tool call. Source confirms only a `bash` function tool is parsed: https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/src/minisweagent/models/utils/actions_toolcall.py.

Cleanest shim if you use it:

1. Install the same `ckb-mcp` bridge CLI in both ON and OFF images.
2. Put identical instructions in both arms: "The command `ckb-mcp` may expose CKB documentation and chain tools. Discover with `ckb-mcp tools`."
3. ON points `CKB_MCP_URL=http://ckb-mcp:3112/mcp`.
4. OFF points `CKB_MCP_URL=http://null-mcp:3112/mcp`.
5. The bridge logs `initialize`, `tools/list`, `resources/list`, `prompts/list`, all calls, args, durations, and errors.

Bridge commands:

```bash
ckb-mcp tools
ckb-mcp search-tools "deploy transaction"
ckb-mcp resources
ckb-mcp search-resources "xUDT lock script"
ckb-mcp read ckb://docs/concepts/cell-model
ckb-mcp call ckb_query_chain_status '{}'
ckb-mcp call rpc_get_tip_block_number '{}'
ckb-mcp call dev_generate_lock_info '{"private_key":"..."}'
```

This is auditable and DeepSWE-like, but it no longer tests native MCP UX. It tests "model plus bash bridge to MCP." Use it as a robustness track, not the headline result.

DeepSWE's reason for mini-swe-agent is valid for model comparison: all models get the same bash tool and prompt, avoiding native product scaffolding. They explicitly run all models on mini-swe-agent for consistency and say native harnesses would mix model capability with product scaffolding: https://deepswe.datacurve.ai/ and https://deepswe.datacurve.ai/blog.

### OpenHands

OpenHands supports MCP and has a CLI/headless mode, but is overbuilt for this benchmark.

Connection:

```bash
openhands mcp add ckb-ai --transport http http://ckb-mcp:3112/mcp
openhands --headless --json -t "$TASK_PROMPT" > trajectory.jsonl
```

OpenHands docs say it supports SSE, Streamable HTTP, and stdio MCP transports. There is a documentation split: the settings page shows `[mcp]` TOML with `shttp_servers`, while the CLI MCP page says recent versions use `~/.openhands/mcp.json` and `openhands mcp add <name> --transport http <url>`. Prefer the CLI-managed JSON path for reproducible headless runs: https://docs.openhands.dev/openhands/usage/settings/mcp-settings and https://docs.openhands.dev/openhands/usage/cli/mcp-servers. Headless mode always runs in always-approve mode: https://docs.openhands.dev/openhands/usage/cli/headless.

Not recommended for primary: it brings a larger runtime, UI/server concepts, more built-in tools, and browser/search capabilities that must be disabled. It is useful if you later want a "real agent platform" compatibility track.

### aider

Not recommended.

Aider is a terminal pair-programming tool focused on editing files in a repo: https://aider.chat/docs/usage.html. Its options page documents model/API/base URL, repo-map, git, lint/test, message, and browser/playwright-related controls, but no MCP configuration surface: https://aider.chat/docs/config/options.html. It can be forced through a shell/CLI shim, but that is a worse version of the mini-swe-agent bridge because aider is less naturally a tool-calling agent.

## 2. Confound Control

Disagreement with the first draft: **"same model, same tasks, MCP ON vs OFF" is not enough.** The harness can easily leak the real treatment through web access, system prompts, native tools, local docs, approval behavior, and product-specific editing affordances.

Controls to enforce:

- Same model ID, provider endpoint, reasoning effort, temperature/top-p if configurable, max turns, wall-clock budget, token budget, and retry policy.
- Same harness image digest and same harness config except the MCP endpoint target.
- Same task prompt bytes. Do not add "use MCP" only in the ON arm.
- Same local workspace. Do not mount `/home/username/ckb-mcp`, CKB docs, generated answer keys, or verifier files into the agent workspace.
- Same bridge binary in both arms if using mini-swe-agent. ON gets real MCP. OFF gets null MCP.
- Same CKB node topology and same chain seed.
- Same network policy except MCP endpoint. Web must be blocked by firewall/proxy, not by instruction.
- Same available built-in tools. For Claude/Codex, disable browser/web tools and extra MCP servers. For Codex, use isolated `CODEX_HOME`. For Claude, use `--strict-mcp-config`.
- Same secrets except the minimal model API credential. Do not expose CKB MCP private key or verifier keys to the agent unless the task explicitly needs signing.
- Randomize run order across ON/OFF to avoid provider drift, rate-limit drift, and node state drift.
- Log everything: prompt hash, model response IDs, tool list hash, MCP tool calls, shell commands, network policy ID, chain genesis hash, verifier version, container image digests.

Built-in CKB knowledge is not removable. It is balanced by using the same model in both arms. To make the MCP delta visible anyway, tasks should require current, repo-specific, or chain-state-specific information that the model cannot know from pretraining but can obtain through the MCP server or direct CKB RPC. The OFF arm may still solve some tasks by using installed SDKs or direct RPC, which is fine. The benchmark should measure improvement, not absolute dependence.

## 3. Deterministic Verification

Primary verifier rule: **the verifier never calls the MCP server.** It uses direct CKB RPC and local toolchains. The CKB repo says this explicitly for its own tests: setup and verification should use direct CKB RPC, not MCP, to avoid circular dependencies and cascading MCP failures in [CLAUDE.md](/home/username/ckb-mcp/CLAUDE.md:54).

Good CKB task/verifier pairs:

- **Contract implementation task:** Agent writes a lock/type script. Verifier builds it with pinned toolchain, deploys to local devnet, submits valid and invalid transactions, and checks accept/reject behavior.
- **Transaction builder task:** Agent writes a script/function that constructs a transaction from fixed inputs. Verifier dry-runs and submits against devnet, then checks `get_transaction`, `get_live_cell`, and resulting cell data.
- **Indexer/query task:** Verifier pre-seeds cells, asks agent to write query code, runs it, and compares canonical JSON output.
- **Docs/API task:** Agent must use correct CKB structures, script hashes, Molecule encodings, or syscalls. Verifier compiles and exercises behavior, not just text.
- **Deployment workflow task:** Agent writes deploy script/config. Verifier runs it against devnet with a test key, then validates the code cell and cell deps.

Prefer local ephemeral devnet over shared testnet:

- Shared testnet creates faucet rate limits, reorg exposure, indexer lag, mempool contention, fee variance, nondeterministic balances, and accidental cross-trial cell consumption.
- A fresh devnet gives deterministic genesis, funded accounts, controlled block production, and resettable state.
- If testnet coverage is needed, keep it read-only or quarantine it as a separate "integration stress" suite, not the main statistical claim.

Verifier mechanics:

```text
setup:
  start fresh devnet
  record genesis hash, chain type, funded key
  copy agent output into verifier workspace

verify:
  build with pinned toolchain
  run static checks only for forbidden shortcuts
  deploy or submit with verifier-controlled key
  wait for deterministic block inclusion
  assert chain state via direct JSON-RPC
  write score.json with pass/fail, subchecks, logs
```

Avoid flaky verification:

- Fresh devnet or unique per-trial funded key and cell namespace.
- No public faucet in verifiers.
- No wall-clock assertions unless using chain block/epoch time under verifier control.
- No dependence on live tip except waiting for a known transaction status.
- Indexer checks wait until indexer tip >= transaction block.
- Verifier retries only idempotent reads, never state-changing submissions.
- Verifier authoring requires three clean repeated runs before the task enters the benchmark, mirroring DeepSWE's flaky-verifier screen: https://deepswe.datacurve.ai/blog.

## 4. Network Isolation

Disagreement with the draft: **Docker containers alone do not solve web isolation.** Docker bridge networks usually provide external access via masquerading, while user-defined bridges mainly scope container-to-container communication: https://docs.docker.com/engine/network/drivers/bridge/. Docker `--network none` fully isolates networking but leaves only loopback, so it cannot reach the model API, CKB node, or MCP server: https://docs.docker.com/engine/network/drivers/none/.

Recommended topology:

```yaml
networks:
  bench_internal:
    internal: true
  llm_egress:
    driver: bridge

services:
  agent:
    image: ckb-bench-agent:${HARNESS_DIGEST}
    networks: [bench_internal]
    environment:
      OPENAI_BASE_URL: http://llm-proxy:8080/v1
      CKB_RPC_URL: http://ckb:8114
      CKB_MCP_URL: http://ckb-mcp:3112/mcp

  ckb:
    image: nervos/ckb:${CKB_DIGEST}
    networks: [bench_internal]

  ckb-mcp:
    image: ckb-ai-mcp:${MCP_DIGEST}
    command: >
      ckb-ai-mcp --host 0.0.0.0 --port 3112
      --ckb-rpc http://ckb:8114
      --private-key ${MCP_TEST_KEY}
    networks: [bench_internal]

  null-mcp:
    image: null-mcp:${DIGEST}
    networks: [bench_internal]

  llm-proxy:
    image: bench-llm-proxy:${DIGEST}
    networks: [bench_internal, llm_egress]
```

Controls:

- Agent has no route to public internet. It reaches only internal DNS names.
- LLM proxy enforces an allowlist to provider API hosts and strips non-LLM destinations.
- For web-off, no `web-proxy` service exists and firewall drops all agent egress except internal services.
- For web-on, add a `web-proxy` service and set `HTTPS_PROXY=http://web-proxy:3128`; log all requested URLs. This is a separate experiment dimension, not mixed into MCP ON/OFF.
- Block external DNS from the agent. Use Docker internal names or static `/etc/hosts`.
- Add host-level `DOCKER-USER` nftables/iptables rules as a second layer if the host allows it.

## 5. Statistics

Primary endpoint: Pass@1 per trial, aggregated as per-task success probability. For graded tasks, use a deterministic score in `[0,1]` and aggregate task means.

Do not use Pass@k as the headline. Pass@k answers "can repeated attempts eventually solve it?" The MCP claim is about ordinary agent capability improvement, so Pass@1 or mean score is cleaner. Pass@k can be a secondary chart.

Recommended design:

- Pilot: 30 tasks x 5 repeats x 2 arms = 300 trials.
- Public v1: 50 to 80 tasks x 5 repeats x 2 arms = 500 to 800 trials.
- Expensive frontier run: at least 40 tasks x 3 repeats x 2 arms, but call it preliminary if CI is wide.

Analysis:

- Compute per-task `delta_t = mean(score_on_t) - mean(score_off_t)`.
- Main estimate: `mean(delta_t)` across tasks, not raw trial pooling.
- CI: paired bootstrap over tasks. If repeats vary, bootstrap tasks first and trial slots inside task second.
- Significance: paired permutation/sign-flip test over task deltas for scores. For one paired binary rollout per task, McNemar is appropriate; statsmodels documents `mcnemar` for paired square contingency tables: https://www.statsmodels.org/stable/generated/statsmodels.stats.contingency_tables.mcnemar.html.
- For bootstrap implementation, SciPy's `bootstrap(..., paired=True)` supports paired resampling and CI computation: https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html.
- Report CIs, not only p-values. Pre-register the primary model, task set, repeats, timeout policy, and exclusion rules.

Practical threshold: do not claim "CKB AI improves agents" unless the ON-OFF CI excludes 0 and the lower CI bound is practically meaningful. A +2 point delta with CI `[0.2, 3.8]` may be statistically positive but not worth product claims. A +10 point delta with CI `[5, 15]` is much stronger.

## 6. Reporting Site

Use a static site generated from JSON. No app framework.

Recommended files:

```text
site/
  index.html
  results.json
  chart.js        # vendored Chart.js or Observable Plot
  style.css
```

Primary chart:

- X-axis: pass rate or mean score.
- Rows: model or task group.
- Bars: OFF and ON.
- Adjacent delta marker: ON-OFF with 95% CI.

Primary table columns:

```text
model | harness | tasks | repeats | off | on | delta | 95% CI | p |
cost/trial | time/trial | tokens/trial | MCP version | CKB node image |
verifier commit | excluded trials
```

Include a downloadable `results.jsonl` with every trial:

```json
{
  "task_id": "ckb-lock-001",
  "arm": "on",
  "model": "gpt-5.4",
  "harness": "thin-mcp-agent@sha256:...",
  "mcp_version": "ckb-ai-mcp 0.x sha ...",
  "prompt_sha256": "...",
  "tool_list_sha256": "...",
  "score": 1,
  "verifier": "verifier@sha...",
  "cost_usd": 1.23,
  "wall_seconds": 912
}
```

## 7. Reshaping the Draft Design

What I would change:

- Replace "usually a testnet node" with "ephemeral devnet for main benchmark, testnet only for a separate integration suite."
- Replace "agent harness inside container" with "agent harness plus explicit model proxy, network proxy, verifier, CKB node, MCP/null-MCP services."
- Make ON and OFF use the same prompt and same installed bridge/harness. The only difference is real MCP server versus null/no MCP.
- Do not benchmark Claude Code or Codex CLI first. They are important compatibility tracks but poor instruments for proving the MCP server's causal effect.
- Do not let the agent call public web in the main A/B. If web is studied, make it a second factor: MCP ON/OFF x web ON/OFF.
- Do not use the MCP server to verify itself. Direct RPC only.
- Pre-register tasks and exclusion rules. Excluding "API errors, timeouts, transient harness failures" after seeing results can bias the delta.

Single highest-risk part: **confounding the treatment.** If the ON arm differs by prompt wording, web reachability, hidden product tools, docs mounted on disk, session config, or a shim only present in ON, a positive result will not prove the MCP server helped. The harness and network design must make the treatment mechanically narrow: same model, same harness, same prompt, same workspace, same node, same tools, real MCP versus null/no MCP only.

