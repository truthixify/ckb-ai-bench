# CKB AI Benchmark Suite — Research Report (Grok Composer)

**Agent lens:** Reporting site and overall design critique (deepest), with concrete answers across the full brief.  
**Sources reviewed:** `/tmp/ckb-benchThe report is at `/tmp/research-grokcomposer.md`.

**Executive summary**

- **Harness:** Don't use mini-swe-agent alone (no MCP). Prefer a thin custom harness with Streamable HTTP MCP; Codex CLI `codex exec` is a pragmatic v1 fallback.
- **Verifier:** Binary pass/fail via direct CKB RPC on ephemeral OffCKB devnets — never via MCP or public testnet faucet.
- **Network:** Docker `internal: true` + iptables/proxy allowlists; web access toggled by network membership, not prompts.
- **Stats:** Paired Pass@1, Δ with bootstrap CIs, McNemar test; 10–15 reps/task/arm for publishable claims.
- **Reporting:** Zero-build static site — `index.html` + `summary.json` + Observable Plot; one horizontal lollipop chart of **Δ Pass@1 with CI** (not paired bars).

**Design critique (highest risk):** Harness confounds if you run ON/OFF in full Claude Code/Codex without pinning — you'll measure vendor tooling + web search + training knowledge, not MCP alone. The brief also over-builds multi-model leaderboards and under-builds paired design, verifier independence, and devnet isolation.

The report follows the brief's output format with sections for all seven questions, explicit pushback, URLs, and extra depth on the reporting site and reshaping.
an shared testnet and `dev_request_testnet_funds` ([faucet is external HTTP](https://github.com/nervosnetwork/ckb-mcp/blob/develop/crates/ckb-ai-mcp/src/dev/handlers.rs)).

3. **Network isolation:** Put agent + verifier + CKB node + MCP server on a **Docker internal network with `internal: true`**, then add **host-level iptables/nftables egress allowlists** for model API + (optionally) web-search arm. Toggle web access by **joining/leaving an `egress` network**, not by prompt wording ([Docker packet filtering](https://docs.docker.com/engine/network/packet-filtering-firewalls/)).

4. **Statistics:** Report **Pass@1** (primary) with **paired Δ = ON − OFF** per task, aggregated across tasks. Use **blocked paired design** (same task order, same seed policy, same container image digest). **5 repetitions/task/arm** minimum for exploratory runs; **10–15** for publishable claims. CIs: **Wilson interval** on marginal pass rates (like [DeepSWE](https://deepswe.datacurve.ai)); **paired bootstrap** on per-task Δ; significance: **McNemar** on pass/fail pairs + **sign test** on Δ.

5. **Reporting site:** **Zero-build static site** — one `index.html` + one `summary.json` + **Observable Plot** (CDN). Single chart: **horizontal lollipop of MCP Δ Pass@1 with 95% CI per model**, zero line emphasized. No Vite, no notebook export. CI publishes `summary.json`; HTML is hand-maintained.

---

## 1. Harness Choice

### Firm recommendation

**Primary:** a **custom thin harness** (“`ckb-bench-agent`”) modeled on mini-swe-agent’s architecture ([~100-line agent loop](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/agents/default.py)) but extended with a **native MCP client** for Streamable HTTP. This is the only design that simultaneously satisfies:

- MCP is the **sole intentional ON/OFF variable**
- HTTP Streamable transport matches `ckb-ai-mcp` (MCP 2025-06-18, `/mcp` on port 3112 per repo README)
- Deterministic, scriptable Docker runs
- No vendor-specific edit tools, web search, memories, or mystery plugins

**Pragmatic v1 (ship faster):** **Codex CLI** in non-interactive mode (`codex exec --json`) with a **checked-in `.codex/config.toml`** that enables exactly one MCP server in the ON arm and sets `web_search = "disabled"` ([config basics](https://developers.openai.com/codex/config-basic)). Codex natively supports Streamable HTTP MCP via `url = "http://ckb-mcp:3112/mcp"` ([Codex MCP](https://developers.openai.com/codex/mcp)).

**Secondary lane (marketing, not science):** **Claude Code** with project `.mcp.json` / `claude mcp add --transport http ckb-ai http://ckb-mcp:3112/mcp` ([Claude MCP](https://code.claude.com/docs/en/mcp)). Run only after the thin harness proves the delta; treat as “official product reproduction,” not the canonical score.

**Reject as primary harness:** mini-swe-agent alone, OpenHands, full SWE-agent, aider — either no MCP or too much scaffold variance.

### MCP connectivity matrix

| Harness | HTTP Streamable MCP | Headless/batch | Pinning quality | MCP A/B suitability |
|---------|---------------------|----------------|-----------------|---------------------|
| **Custom thin + MCP SDK** | Yes (first-class) | Excellent | Excellent | **Best** |
| **Codex CLI** | Yes (`[mcp_servers.*].url`) | Excellent (`codex exec`) | Good with `.codex/config.toml` | **Good (v1)** |
| **Claude Code** | Yes (`--transport http`, `streamable-http` alias) | Moderate (TUI-first) | Moderate (scopes, plugins, claude.ai connectors) | **Fair** |
| **mini-swe-agent** | **No** — bash only | Excellent | Excellent for non-MCP benchmarks | **Poor** without shim |
| **aider / OpenHands** | Varies / heavy | Moderate | Poor | **Poor** |

### mini-swe-agent and the shim question

[mini-swe-agent explicitly has no tools other than bash](https://github.com/SWE-agent/mini-swe-agent). [DeepSWE holds it constant across models](https://deepswe.datacurve.ai/blog#evaluation-harness) — correct for **model** leaderboards, **wrong analogy** for an **MCP** leaderboard. Here the artifact under test *is* MCP.

**Cleanest shim (if you insist on mini-swe-agent):** generate a **stable `ckb-mcp-cli` wrapper** at container start:

```bash
# ON arm only: materialize one shell entrypoint per MCP tool
mcp-cli tools list --server http://ckb-mcp:3112/mcp \
  | jq -r '.tools[].name' \
  | while read t; do
      printf '#!/bin/sh\nexec mcp-cli tools call %s --server http://ckb-mcp:3112/mcp "$@"\n' "$t" \
        > "/usr/local/bin/mcp__$t"
      chmod +x "/usr/local/bin/mcp__$t"
    done
```

Then inject system prompt: “CKB tools are available as `mcp__<tool_name>` shell commands.” **Downside:** converts structured MCP tool calls into error-prone shell JSON; tool discovery, resources (`ckb://docs/...`), and prompts won’t match real MCP clients; you’re benchmarking **bash glue**, not the MCP server as users experience it.

**Verdict:** shim is acceptable for a smoke test; **not** for the headline “CKB AI MCP improves agents” claim.

### Concrete harness container contract

```yaml
# docker-compose fragment (conceptual)
services:
  agent:
    image: ckb-bench-agent:${HARNESS_DIGEST}
    environment:
      ARM: "on"          # or "off"
      MODEL: "gpt-5.5"
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      BENCH_SEED: ${BENCH_SEED}
    volumes:
      - ./tasks/${TASK_ID}:/workspace
      - ./harness/on.mcp.json:/workspace/.mcp.json:ro  # ON arm only
    networks: [bench_internal]
    command: ["run-task", "--task", "${TASK_ID}", "--arm", "${ARM}"]
```

Pin in every trial record: `harness_digest`, `model`, `arm`, `mcp_server_digest`, `ckb_node_genesis_hash`, `web_egress_enabled`.

---

## 2. Confound Control

### What pollutes an MCP A/B test

| Confound | Claude Code / Codex risk | Thin harness |
|----------|--------------------------|--------------|
| Built-in web search | Codex: on by default (`web_search = "cached"`); live under `--yolo` ([features](https://developers.openai.com/codex/cli/features)) | Absent |
| claude.ai connectors / extra MCP | Claude loads remote connectors; `/mcp` merges scopes ([docs](https://code.claude.com/docs/en/mcp)) | Absent |
| Model training knowledge of CKB | Always present | Always present — **mitigate with binary on-chain verifiers**, not doc quizzes |
| Vendor edit tools (`apply_patch`, etc.) | Present | Use bash + heredoc only |
| MCP docs/resources in OFF arm | **Design bug** if task workspace includes CKB docs or `resources/` from ckb-mcp | Strip bundled docs from workspace; OFF arm must not mount `ckb://` resources |
| Faucet / external HTTP | `dev_request_testnet_funds` hits `https://faucet-api.nervos.org` | Use devnet prefund; disable faucet tool in benchmark MCP profile |

### Does this argue for a minimal harness?

**Yes, strongly.** DeepSWE’s lesson is “fix the harness, measure the variable” ([evaluation harness section](https://deepswe.datacurve.ai/blog#evaluation-harness)). For CKB, the variable is **MCP ON/OFF**, not “which vendor CLI feels best.”

**Mandatory OFF-arm controls:**

- Same model, temperature/top_p, max turns, timeout
- `web_search = "disabled"` (Codex) or equivalent
- No MCP servers in config; remove `.mcp.json`
- Deny `curl`/`wget` to non-allowlisted hosts at network layer (see §4)
- Do **not** put “use the CKB MCP server” in the OFF prompt — arm is **config-only**

**Mandatory ON-arm controls:**

- Exactly one MCP server: `http://ckb-mcp:3112/mcp`
- Pin `ckb-ai-mcp` image digest; log tool list hash at startup
- Consider `alwaysLoad: true` equivalent so tool-search deferral doesn’t change discovery dynamics ([Claude tool search](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search))

---

## 3. Deterministic Verification

### Principle (from ckb-mcp itself)

The repo’s test doctrine is explicit: **setup and verify via direct CKB RPC, never via MCP** (`CLAUDE.md` testing section). The benchmark must mirror this: MCP is the subject; RPC is the ruler.

### Good CKB task + verifier pairs

A good task is **behavioral, on-chain, and address-agnostic**:

| Task archetype | Agent deliverable | Verifier checks (direct RPC) |
|----------------|-------------------|------------------------------|
| Deploy data cell | Tx hash or signed tx file | `get_transaction` committed; output data equals spec |
| Lock script / contract | `*.riscv` + deployment tx | `estimate_cycles` / `test_transaction` success; type/lock hash match |
| Transfer / capacity | Signed tx | Input/output capacities balance; lock matches derived address |
| xUDT / Type ID pattern | Type script deployment | Indexer `get_cells` returns expected data prefix |
| Debug failing script | Fixed broken tx in workspace | After fix, `offckb debug <hash>` returns `Run result: 0` ([OffCKB](https://docs.nervos.org/docs/sdk-and-devtool/offckb)) |

**Bad tasks for v1:** “explain CKB economics,” “write a README,” anything graded by LLM judge, anything requiring **public testnet faucet**.

### Avoid flaky verification

| Flake source | Fix |
|--------------|-----|
| Shared testnet contention | **Per-trial ephemeral devnet** via OffCKB (`offckb node`) |
| Faucet rate limits | Pre-funded devnet accounts (20 × 42B CKB in genesis) — **never call faucet in benchmark** |
| Nondeterministic addresses | Verifier derives expected lock hash from **task-supplied key material**, not agent-chosen keys |
| Reorgs | Devnet single-node; wait for `get_tip_block_number` stability (N confirmations) |
| Time-based assertions | Ban wall-clock; use block number deltas only |
| Agent-chosen type IDs | Verifier checks **properties** (data prefix, capacity), not exact outpoint unless task fixes key |

### Ephemeral devnet vs shared testnet

**Use ephemeral devnet.** Shared testnet fails determinism (mempool, faucet, external state). The MCP server’s `--ckb-rpc` should point at the **trial-local** node (`http://ckb-devnet:8114`), started in the same compose stack.

### Verifier implementation sketch

```bash
#!/usr/bin/env bash
# verify.sh — deterministic, no MCP
set -euo pipefail
CKB_RPC="${CKB_RPC:-http://ckb-devnet:8114}"
ARTIFACT="$1"  # e.g. tx_hash.txt

tx_hash=$(cat "$ARTIFACT")
status=$(curl -s "$CKB_RPC" -X POST -H 'Content-Type: application/json' \
  -d "{\"id\":1,\"jsonrpc\":\"2.0\",\"method\":\"get_transaction\",\"params\":[\"$tx_hash\"]}" \
  | jq -r '.result.tx_status.status')

[[ "$status" == "committed" ]] || exit 1
# ... task-specific RPC assertions ...
echo "PASS"
```

Ship a **task manifest** (`task.yaml`) with: `prompt`, `input_files`, `verifier_cmd`, `timeout_sec`, `required_tools` (for ON-arm tool-usage telemetry, not grading).

---

## 4. Network Isolation

### Goal

Agent container may reach:

- **Always:** LLM provider API, CKB RPC, (ON arm only) MCP HTTP
- **Never (default):** public web, GitHub, docs sites, faucet
- **Toggle:** web access for ablation studies — **separate Docker network**, not prompt instructions

### Concrete pattern

```yaml
networks:
  bench_internal:
    internal: true   # no default internet route
  egress:
    internal: false  # controlled bridge to host NAT

services:
  ckb-devnet:
    networks: [bench_internal]
  ckb-mcp:
    networks: [bench_internal]
    # ON arm only in compose profile "mcp-on"
  agent:
    networks:
      - bench_internal
      - ${WEB_EGRESS_NETWORK:-null}  # join `egress` when WEB=on
```

**LLM API egress:** `internal: true` blocks internet entirely. Two workable options:

1. **HTTP proxy on host** (squid) — allowlist `api.openai.com`, `api.anthropic.com`; agent uses `HTTPS_PROXY`.
2. **Host iptables FORWARD rules** on the Docker bridge — allowlist destination IPs ([Docker firewall docs](https://docs.docker.com/engine/network/firewall-iptables/)).

Example allowlist intent (host-side):

```bash
# Pseudocode: allow only OpenAI + deny rest on docker bridge
iptables -I DOCKER-USER -i br-bench -d api.openai.com -j ACCEPT
iptables -I DOCKER-USER -i br-bench -j DROP
```

**Web-search ON arm:** attach agent to `egress` network *in addition to* `bench_internal`; run a **local Squid** with domain allowlist (`docs.nervos.org`, `github.com/nervosnetwork/*`) for reproducibility, or full egress for “realistic” secondary results.

**Enforcement test (CI):** each image must fail `curl -m 2 https://example.com` and succeed `curl -m 2 $CKB_RPC` and (ON arm) `curl -m 2 http://ckb-mcp:3112/health`.

---

## 5. Statistics

### Primary metric

**Pass@1 per (task, arm)** — aligns with [DeepSWE](https://deepswe.datacurve.ai) and avoids subjective partial credit. DeepSWE shows ±2–5% CIs on ~113 tasks × few rollouts; your suite should start smaller but use **paired** analysis.

### Repetitions

| Phase | reps / task / arm | Purpose |
|-------|-------------------|---------|
| Dev / task authoring | 3 | Flake detection |
| Exploratory benchmark | 5 | Directional Δ |
| Publishable claim | **10–15** | Tighten CI; McNemar power |
| Regression gate (CI) | 3 | Fast, high flake risk — OK for MCP server unit tests, not headline claims |

### Paired design (critical)

For each task `t` and rep `r`, run ON and OFF with:

- Same `task_id`, `rep`, `model`, `harness_digest`
- **Different** `trial_id`, but **paired analysis** at (task, rep) level
- Record `pass_on`, `pass_off` → Δ = `pass_on - pass_off`

### Confidence intervals and tests

| Quantity | Method |
|----------|--------|
| Marginal Pass@1 (ON or OFF) | **Wilson score interval** (DeepSWE-style “70% ± 3%”) |
| Overall Δ Pass@1 | **Paired bootstrap** over tasks (resample tasks with replacement, 10k draws) |
| Per-task pass/fail pairs | **McNemar exact test** (discordant pairs: ON pass/OFF fail vs ON fail/OFF pass) |
| Δ magnitude when passes are partial | **Wilcoxon signed-rank** on continuous subscores (if any) — prefer binary |

### Sample size intuition

With **12 tasks × 10 paired reps = 120 pairs**, a true +10pp lift (60%→70%) often yields McNemar p < 0.05 if discordant pairs concentrate on MCP-wins. **Fewer than 8 tasks** makes “measurable improvement” claims easy to falsify — which is fine for v1 honesty.

**Do not** pool OFF from run A and ON from run B across different harness versions.

---

## 6. Reporting Site (deep dive)

### Design goal

Simpler than [DeepSWE](https://deepswe.datacurve.ai): **one page, one chart, one table**, answering exactly:

> “Does CKB AI MCP improve Pass@1, and by how much, with uncertainty?”

DeepSWE optimizes for **model-vs-model** ranking (17 models, cost/time columns, trajectory browser). You need **treatment-vs-control** storytelling — a different visual grammar.

### Recommended stack (minimal)

| Option | Verdict |
|--------|---------|
| **Single `index.html` + `summary.json` + Observable Plot (CDN)** | **Recommended** |
| Static HTML + Chart.js | OK; more boilerplate for error bars |
| uPlot | Great perf, painful CI error bars + labels |
| Vite + React | **Reject** — build chain for one chart |
| Notebook export (Jupyter → HTML) | **Reject** — brittle, fat, poor diffability |

**Files:**

```
site/
  index.html          # ~120 lines, inline CSS
  data/
    summary.json      # generated, sole data artifact
  favicon.svg
```

**Deploy:** GitHub Pages / Cloudflare Pages / S3 — any static host. No server.

### JSON data flow

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌────────────┐
│ run_matrix  │────▶│ results.jsonl│────▶│ aggregate.py│────▶│ summary.json│
│ (CI/local)  │     │ 1 row/trial  │     │ (pandas)    │     │             │
└─────────────┘     └──────────────┘     └─────────────┘     └──────┬─────┘
                                                                      │
                                                                      ▼
                                                               index.html fetch()
```

**`results.jsonl` row (trial):**

```json
{
  "trial_id": "t04-r02-on",
  "task_id": "deploy-hello-cell",
  "rep": 2,
  "arm": "on",
  "model": "gpt-5.5",
  "pass": true,
  "harness_digest": "sha256:…",
  "mcp_digest": "sha256:…",
  "ckb_genesis_hash": "0x…",
  "duration_sec": 412,
  "cost_usd": 0.84
}
```

**`aggregate.py` responsibilities (CI step):**

1. Filter to latest `benchmark_run_id`
2. Compute per-model `pass_rate_on`, `pass_rate_off`, `delta`, `delta_ci_low/high` (paired bootstrap)
3. McNemar `p_value` per model
4. Emit `summary.json`

**`summary.json` (chart input):**

```json
{
  "benchmark_run_id": "2026-06-12T18:00:00Z",
  "harness": { "name": "ckb-bench-agent", "digest": "sha256:…" },
  "mcp": { "version": "v1.6.12", "digest": "sha256:…" },
  "n_tasks": 12,
  "reps_per_arm": 10,
  "models": [
    {
      "id": "gpt-5.5",
      "pass_at_1_off": 0.42,
      "pass_at_1_on": 0.58,
      "pass_at_1_off_ci": [0.34, 0.50],
      "pass_at_1_on_ci": [0.49, 0.66],
      "delta": 0.16,
      "delta_ci": [0.05, 0.27],
      "mcnemar_p": 0.008,
      "n_pairs": 120
    }
  ]
}
```

### The one chart: what to show

**Use: horizontal lollipop / dot-range of Δ Pass@1 with 95% CI, one row per model.**

```
                    −10%    0    +10%   +20%
gpt-5.5        ────────────|──●==========═►  +16% [+5,+27]
claude-sonnet  ───────●════|══════════════►  +11% [+2,+19]
gemini-3.1     ──●════════|════════════════   +3% [-4,+9]
               statistically meaningless ──►   (CI crosses 0)
```

**Why this chart type:**

| Alternative | Why reject for headline |
|-------------|----------------------|
| Paired side-by-side bars (ON/OFF per model) | Buries the claim; readers do mental subtraction |
| Scatter (OFF vs ON per task) | Too busy for N models × N tasks on one page |
| Only ON leaderboard | **Scientifically wrong** — no control arm |
| Task-level heatmap | Good **second** chart; not the one chart |

**Observable Plot sketch (in `index.html`):**

```javascript
const data = summary.models.map(m => ({
  model: m.id,
  delta: m.delta * 100,
  lo: m.delta_ci[0] * 100,
  hi: m.delta_ci[1] * 100,
}));

Plot.plot({
  width: 720, height: 40 + data.length * 36,
  x: { label: "Δ Pass@1 (MCP ON − OFF)", grid: true, domain: [-15, 25] },
  y: { label: null },
  marks: [
    Plot.ruleX([0], { stroke: "#999" }),
    Plot.ruleX(data, { x1: "lo", x2: "hi", y: "model", stroke: "#4c6ef5" }),
    Plot.dot(data, { x: "delta", y: "model", fill: "#4c6ef5", r: 5 }),
    Plot.text(data, { x: "hi", y: "model", text: d => `+${d.delta.toFixed(0)}%`, dx: 6, textAnchor: "start" }),
  ]
});
```

### The table (below chart)

| Model | Pass@1 OFF | Pass@1 ON | Δ | 95% CI (Δ) | McNemar p | n pairs | Median cost ON |
|-------|------------|-----------|---|------------|-----------|---------|----------------|
| gpt-5.5 | 42% | 58% | **+16pp** | [+5, +27] | 0.008 | 120 | $0.84 |

Footnotes (static text):

- Pass@1 = fraction of (task, rep) trials passing verifier
- Paired bootstrap CI over tasks
- Harness digest + MCP version linked to GitHub release

**Deliberately omit (vs DeepSWE):** cost leaderboards, trajectory browser, 91-repo diversity tiles, effort-level toggles. Add **“Raw data”** link to `results.jsonl` in GitHub for reproducibility.

### Optional later (not v1)

- Task drill-down page per `task_id` (still static, generated)
- OFF/ON paired dot plot **per single model** for blog posts
- Trajectory viewer — only if harness emits standard JSONL traces

---

## 7. Reshaping — Skeptical Critique of the First-Draft Design

### What the brief gets right

- Dockerized trials
- Deterministic verifier aspiration
- ON/OFF as core experiment
- Network control instinct
- DeepSWE as quality north star for **verifier rigor**, not for **harness choice**

### What is over-built

| Element | Problem |
|---------|---------|
| **Multi-model leaderboard before MCP proof** | You don’t have a significant Δ on **one** model yet; 5+ models multiplies cost and confounds |
| **“Average score” across runs** | Continuous rubrics invite verifier gaming; use **binary pass** |
| **Shared public testnet** | Imports faucet, mempool, reorg noise; fights determinism |
| **Optional MCP URL** | Too weak — OFF arm needs identically shaped config **minus** MCP block, not “URL absent” |
| **Prompt-controlled web access** | Models ignore prompts; DeepSWE documents prompt-level tool discouragement failing ([web search section](https://deepswe.datacurve.ai/blog#qualitative-analysis)) |
| **Future regression / multi-version scope in v1** | Bake **manifest hashing** now; defer v1 vs v2 MCP regression until Δ is credible |

### What is under-built

| Gap | Risk |
|-----|------|
| **Harness manifest pinning** | Silent upgrades to Claude/Codex change scores |
| **Verifier independence** | Using MCP tools in verifier circularly validates MCP |
| **Paired/block design** | Independent ON/OFF runs attribute variance to wrong source |
| **Task taxonomy** | Without tiers (RPC read → deploy → debug), you won’t know *what* MCP helps |
| **OFF-arm doc leakage** | `ckb-ai-mcp` bundles extensive `docs/` and `resources/` — OFF agents may read copies in workspace |
| **Alpha MCP server** | README warns breaking changes ([ckb-mcp README](https://github.com/nervosnetwork/ckb-mcp)) — benchmark must pin image digest |

### What is wrong (not just incomplete)

1. **Treating Claude Code / Codex as “official” ⇒ using them as the benchmark harness.** Official product support proves **compatibility**, not **experimental control**. DeepSWE explicitly **rejects** native harnesses for leaderboard science ([harness section](https://deepswe.datacurve.ai/blog#evaluation-harness)).

2. **mini-swe-agent as default because DeepSWE uses it.** Category error. DeepSWE measures **models** with bash-only fairness. You measure **MCP** — bash-only can’t see MCP without a shim that isn’t MCP.

3. **Outputs = “files produced by agent.”** CKB truth is **on-chain state**. A correct tx that never lands on-chain must fail; a wrong file with no broadcast must fail.

4. **Using testnet + `dev_request_testnet_funds`.** Introduces **external HTTP** (faucet) into the critical path — violates network isolation goals and adds nondeterminism.

### Single highest-risk part

**Harness confounds** — running OFF vs ON in **Claude Code or Codex** without extreme pinning, while the MCP server exposes docs search, workflow prompts, and 40+ tools. You will measure **(vendor CLI + web search + training knowledge + MCP)**, and when Δ is small, you won’t know which term moved. This risk dominates verifier noise and testnet flakiness.

### Concrete reshaping (provable + maintainable)

**Phase 0 — Prove the effect exists (4–6 weeks)**

1. **12 tasks**, 3 tiers: (A) RPC read/query, (B) deploy cell/script, (C) fix failing tx
2. **1–2 models** only (e.g., GPT-5.5 + Claude Sonnet)
3. **Thin harness** with Streamable HTTP MCP; pin all digests
4. **Ephemeral OffCKB devnet** per trial; verifier = bash + `curl` JSON-RPC
5. **10 paired reps**; pre-register success criterion: **lower bound of Δ CI > 0**

**Phase 1 — Publish (2 weeks)**

6. Static site with one Δ chart + table
7. Release `results.jsonl` + `summary.json` + harness/container digests

**Phase 2 — Expand (later)**

8. Add Codex/Claude **reproduction lane** (secondary table, not headline)
9. MCP v1 vs v2 regression once Δ is established
10. Optional web-egress ablation arm (network toggle)

**Maintainability rules:**

- One `docker-compose.bench.yml`; ON/OFF = **profiles**, not separate compose files
- Tasks are folders; verifiers are `verify.sh` + `task.yaml` — no framework magic
- Aggregate script < 200 lines; no database
- Any trial failure uploads: prompt, env manifest, stdout, **genesis hash** — not full chain data

---

## Explicit Disagreements With the Brief’s Framing

1. **“mini-swe-agent candidate because tiny and deterministic-friendly.”** True for SWE tasks; **false for MCP A/B** without native MCP or you’re testing a shim.

2. **“Claude Code and Codex are the two official platforms — use them in the container.”** Use them for **product validation**, not as the **primary instrument**. Compatibility claims need one pinned reproduction run, not the full matrix.

3. **“Same CKB testnet node for agent and verifier.”** Shared **ephemeral devnet per trial** yes; shared **public testnet** no.

4. **“Several repetitions to get an average score.”** Prefer **paired pass@1** over mean continuous score; report Δ with CI, not just ON rate alone.

5. **DeepSWE as direct template.** Adopt **verifier discipline** and **Wilson CIs**; reject **bash-only harness** as the MCP experiment vehicle.

---

## Uncertainties

- Exact Codex sandbox network defaults in Docker need empirical validation per image version ([sandbox docs](https://developers.openai.com/codex/agent-approvals-security#network-access)).
- Claude Code headless automation in CI is less documented than `codex exec`; trial driver may need `claude -p` patterns (verify against current CLI).
- Optimal task count for CKB-specific skills is unknown; 12 is an engineering guess, not a power calculation.
- Whether MCP **resources** (`ckb://docs/...`) or **prompts** drive Δ more than **tools** — needs ON-arm ablation (tools-only vs docs-only server flags: `--rpc-only`, `--docs-only` per ckb-mcp CLI).

---

## Key References

- [DeepSWE benchmark](https://deepswe.datacurve.ai) and [methodology blog](https://deepswe.datacurve.ai/blog#evaluation-harness)
- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) / [MCP issue #563](https://github.com/SWE-agent/mini-swe-agent/issues/563)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Codex MCP](https://developers.openai.com/codex/mcp) / [Codex config](https://developers.openai.com/codex/config-basic) / [Codex features (web search)](https://developers.openai.com/codex/cli/features)
- [OffCKB devnet tool](https://docs.nervos.org/docs/sdk-and-devtool/offckb)
- [Docker firewall/iptables](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [ckb-mcp README](https://github.com/nervosnetwork/ckb-mcp) (Streamable HTTP MCP, port 3112, alpha status)
- [MCP Streamable HTTP transport](https://modelcontextprotocol.io/) (protocol 2025-06-18 as implemented by ckb-ai-mcp)

---

*Report complete. Written to `/tmp/research-grokcomposer.md`.*