**Report written successfully to `/tmp/research-grokbuild.md` (299 lines, structured per the brief's required Output format).**

The report:
- Leads with the 5-bullet executive recommendation.
- Has dedicated sections for all 7 questions (harness/MCP integration details, confounds, deterministic CKB verification with compile/deploy/RPC asserts/golden checks, ephemeral devnet vs shared testnet comparison, concrete Docker Compose `internal: true` + sidecar proxy web toggle, stats with reps/Pass@k/paired bootstrap/McNemar/t-test + sample size/power guidance, minimal single-file static reporting).
- Verifies CKB/MCP specifics against the live ckb-mcp tree (README, CLAUDE.md, Dockerfile, docker-compose, server.rs, tools in rpc/dev/ckb/search, docs/ references to ckb-script-templates/offckb, test isolation rules, etc.) + real CKB sources (OffCKB docs, ckb-script-templates, networks/faucet pages, public RPC patterns).
- Uses web research for harness MCP support (Claude Code/Codex HTTP streaming), Docker isolation patterns, SWE-bench/DeepSWE stats practices, etc.
- Cites URLs throughout.
- Is opinionated and explicitly pushes back (e.g., shared testnet as default is actively wrong for determinism; ephemeral OffCKB devnet + full isolated compose stack is required; mini-swe-agent unsuitable as primary without native MCP; network toggle must be compose/env-enforced, not prompt-dependent; highest risk is verifier/build nondeterminism).
- Provides concrete commands, YAML snippets, RPC/assert flows, verifier pseudocode, power considerations, and maintainability changes.
- Only the final report was written (no modifications anywhere under /home/username/ckb-mcp; only reads + web research + this output file).

The file is ready for the orchestrator/adjudication.
cell data, script hash, capacity, and functional spend success. Golden-file + structural checks on source. Use fixed genesis/prefunded accounts from the devnet.

- **Network isolation + web toggle**: Run a single Docker Compose stack per run (or batch): agent + ephemeral OffCKB devnet container + ckb-mcp container on one `internal: true` user-defined bridge network (no masquerade, no default route, service DNS only). Agent reaches CKB (e.g. `http://ckb-devnet:8114`) and MCP (`http://ckb-mcp:3112/mcp`) by name only. Web toggle is a clean orthogonal axis: default "off" uses the internal net only; "on" variant adds (or profiles in) a sidecar allowlist egress proxy (nginx/tinyproxy/envoy with domain/IP whitelist for model providers only) + sets `http_proxy`/`https_proxy` + `no_proxy=ckb-devnet,ckb-mcp,localhost` in the agent container. Toggle via compose profile/env, never prompt text. Blocks curl/wget bypasses at the network layer. `--network none` + manual links is inferior (loses easy DNS between services).

- **Statistics**: 5–10 repetitions per (task, arm) to average stochasticity. Primary metric: per-task success rate (binary Pass or 0–1 graded score). For ON-vs-OFF delta use **paired** design: paired bootstrap CI on the per-task difference; McNemar's test on discordant pairs for binary outcomes; paired t-test or Wilcoxon for graded scores. Target 40–80 hand-written contamination-free tasks for a defensible claim on a 15–25 point absolute lift (power analysis via statsmodels or similar; baseline variance from pilot runs). Report mean score + 95% CI per arm, delta + CI, effect size, p-value (adjusted), and Pass@k where k = reps. Pre-register the analysis plan.

- **Reporting site**: Single-file static `index.html` (Tailwind CDN + Chart.js CDN). No build step. Data embedded as JSON (or small fetched .json for GH Pages). Chart: per-task grouped bars (OFF/ON) with delta callouts + error bars from reps; top summary cards for overall means + CI + significance. Tables: task-level (name, OFF mean, ON mean, delta, CI or p), plus aggregate. Deploy via GH Pages. Far simpler than DeepSWE.

**Highest-risk element of the plan (user design + this critique)**: Verifier nondeterminism or leakage (build env drift, reliance on shared chain state, or accidental use of MCP during scoring). Secondary: insufficient task volume or non-paired analysis leading to overstated claims. Ephemeral full-stack compose (devnet + mcp + agent) mitigates most chain variance.

**Major disagreement with first-draft design**: "Usually a testnet node" (shared) for both agent and verifier is the wrong default. It introduces faucet limits, possible reorgs/uncle effects, address nondeterminism, external tx interference, and rate limits—exactly the opposite of the reproducibility goal. Per-run ephemeral devnet (OffCKB) + fully isolated compose is mandatory for credible "deterministic verification." Also, "the container almost always needs network" is solvable cleanly at the compose layer (internal + optional proxy) without polluting prompts or harnesses.

## 1. Harness Choice

**Recommended**: Claude Code CLI (Anthropic) or OpenAI Codex CLI, containerized.

- Both are "officially supported" for CKB AI per the brief.
- Both have first-class remote/streaming HTTP MCP support (MCP 2025-06-18, the exact transport used by ckb-mcp on `/mcp`):
  - Claude: `claude mcp add --scope project --transport http ckb-ai http://ckb-mcp:3112/mcp` (or equivalent config injection into `~/.claude.json` / `.mcp.json`).
  - Codex: `codex mcp add ...` or `~/.codex/config.toml` with `[mcp_servers.ckb] url = "http://..."` (supports streaming HTTP; also stdio).
- Config can be prepared outside the prompt (volume mount a generated config dir or run the `mcp add` command at entrypoint based on `MCP_URL` env). ON arm gets the URL; OFF arm gets an empty/omitted list. The model, prompt text, and all other tools remain identical.
- Docker feasibility: Public examples and devcontainer support for running Claude Code CLI headlessly in containers with volume mounts for workspace, pre-configured MCP, and even egress controls (see Docker blog + community Dockerfiles for claude-code-container patterns). Codex CLI is explicitly lightweight terminal agent and container-friendly.
- Reproducibility: Pin exact CLI version in the image (`npm install -g @anthropic-ai/claude-code@X.Y.Z` or equivalent). Pass model provider base URL + key via env (never baked). Run with non-interactive flags + timeout + output dir.

**Why not mini-swe-agent (or similar tiny bash agents) as primary**:
- It is radically minimal (≈100 LOC core) and excellent for SWE-bench consistency (DeepSWE pins it), with Docker/singularity sandbox support out of the box.
- **No native MCP**. It is bash-only (subprocess actions, linear history). External tools require either (a) teaching the LM to shell out to a custom bridge CLI (e.g. remote-mcp-cli patterns discussed in community) that speaks Streamable HTTP and presents tools as shell commands, or (b) forking the harness to add an MCP client and function-call mapping.
- Either approach changes the action surface between arms (shim present vs absent), making it impossible to claim "the only variable is the presence of the CKB MCP server." The MCP tools (rpc_*, dev_*, ckb_*, search, resources, prompts) would appear to the agent via a different mechanism (or not at all) than in the official clients.
- There is an open issue in the mini-swe-agent repo about "using external tools via mcp". It is not solved for clean A/B.
- Fine for a *secondary* pure-bash baseline (MCP tools emulated as extra shell binaries), but not for the primary "prove the MCP server helps CKB dev" claim when the MCP is explicitly the artifact under test.

**Other options (OpenHands, SWE-agent, aider, custom)**:
- OpenHands / full SWE-agent: heavier, more built-in tools/knowledge, harder to isolate exactly one MCP addition.
- Aider: git-focused, less general agent loop.
- Custom thin agent: viable if you want ultimate control (use LiteLLM or direct Responses API + a small MCP-over-HTTP client library to surface tools identically to function calling). ~200–400 LOC possible, fully auditable, same Docker story. Good if official CLIs prove too stateful or telemetry-heavy. But start with official for credibility with "CKB AI" positioning.

**Confound control (Q2)**:
- Official CLIs have built-in knowledge, web search (model-side), and other tools. Mitigations that keep the delta attributable to MCP:
  - Identical base prompt + identical system instructions across arms.
  - Identical model + temperature + max steps.
  - For web/search: the network isolation below removes general web access in the "off" (and default) condition at the transport layer; any model-internal browsing that requires outbound still goes through the same controlled proxy or is blocked.
  - Disable or scope MCP features in the harness config (e.g. ckb-mcp itself supports `--docs-only`, `--rpc-only`, etc.; the verifier and harness never use the MCP for scoring).
  - Log and audit every tool call (both arms) to show usage of MCP tools only in the ON condition.
- This *does* argue for preferring a minimal or custom harness over a maximally featureful one if the goal is purest attribution. However, since the product claim is "helps with the officially supported CKB AI clients," benchmarking those clients (with/without the MCP) is the most relevant experiment. Run a secondary mini-swe or custom-bash arm as a "pure model + MCP tools" contrast if desired.

**Concrete integration notes** (from ckb-mcp README/CLAUDE.md + MCP client docs):
- Health: `curl http://ckb-mcp:3112/health`.
- MCP endpoint: `http://.../mcp` (Streamable HTTP, stateless mode preferred by the server for Codex/Claude reuse of sessions).
- ckb-mcp serves tools (rpc_get_*, dev_*, ckb_*, search_*), resources (`ckb://docs/...`), and prompts.
- Add/remove is a one-line config change or CLI command—ideal for the ON/OFF axis.

## 3. Deterministic Verification (Q3)

**How to score CKB-dev outputs deterministically using a CKB node** (compile → deploy → on-chain assert via RPC):

1. **Compile / build**:
   - Modern standard (verified in ckb-mcp docs/ and resources): `cargo generate gh:cryptape/ckb-script-templates workspace`, then `make build` (or `make build-contract`).
   - Target: `riscv64imac-unknown-none-elf` (see .cargo/config.toml in templates; rustflags for CKB-VM).
   - Verifier re-runs the exact same steps in a clean image with pinned `rust-toolchain.toml` + same cargo-generate version. Compare produced binary hash (or at minimum, successful build + size) against what the agent emitted. This catches "it worked because of my local env" cases.
   - For pure data cells or small scripts: hex data is trivial.

2. **Deploy**:
   - Use **direct RPC** (ckb-sdk-rust, or raw JSON-RPC via reqwest/httpx + ckb-types, or a thin offckb wrapper under verifier control). Never call the MCP server's `dev_deploy_cell_data` or `rpc_submit_transaction` for verification—see ckb-mcp CLAUDE.md "Test Independence" section: "Use direct CKB RPC client calls (NOT MCP server)".
   - For full contracts: offckb deploy abstracts the code cell + type ID + dep group patterns, but for verifier purity implement (or vendor) the tx construction: create cell with the binary in `data`, appropriate lock (e.g. secp from known devnet key), optional type script. Submit via `submit_transaction` (or `send_transaction`).
   - OffCKB also provides a convenient `offckb deploy --network devnet --target <binary>` for the harness/dev side; the verifier can use the same binary in a controlled call or replicate the tx.

3. **On-chain assertions (RPC)**:
   - Poll `get_transaction(tx_hash)` until status `{"status": "committed", ...}` or timeout/rejected. Assert no "rejected" or script error.
   - `get_live_cell(out_point, with_data=true)` or `rpc_search_cells` / `ckb_query_script_cells` patterns to locate the deployed cell by script hash / args / code_hash.
   - Assert: exact `data` bytes (for code cells), capacity, lock/type script match golden expectations, block_number within expected window.
   - Functional: verifier constructs a follow-up tx that *uses* the deployed script (e.g. a spend that satisfies a custom lock, or mint/burn for UDT), submits it, asserts commit + resulting cell state (new tokens, state change). Use `estimate_cycles` + `test_transaction` first for dry-run.
   - Cell state / balance: `get_cells_capacity`, indexer `search_cells`, DAO helpers if relevant.
   - For dApps: assert specific cell collections exist with correct xUDT/Spore/etc. data, or that a sequence of txs produced the documented state machine.

4. **Golden-file / AST / structural**:
   - Agent must also write `expected/` or metadata (e.g. "this contract implements always-success lock").
   - Verifier diffs key files (existence, contains required patterns, no forbidden anti-patterns from ckb-mcp docs/troubleshooting), parses Cargo.toml / scripts for correct deps, checks binary reproducibility.
   - CKB-specific: check against known system script hashes (from ckb-mcp `docs/reference/` and resources), molecule layout if serializing, syscall usage patterns.

**What makes a good CKB task + verifier pair**:
- Narrow, verifiable on-chain effect (deploy + one successful spend or state transition).
- Output is a complete minimal workspace (source + build artifacts) rather than "just the .rs".
- Verifier is self-contained, uses only the node RPC + local build, finishes in <60–120s, no external services.
- Tasks avoid nondet (fixed addresses, no timestamps in assertions, explicit fees/cycles where possible).
- Coverage of the MCP surface: one task per major category (pure RPC query, dev deploy + faucet, high-level ckb_* composites, doc-driven contract from `ckb://docs/...`, workflow prompt).
- Hand-written, post-cutoff, private until release (contamination-free like DeepSWE).

**Ephemeral per-run CKB devnet vs shared testnet** (core of the lens):

**Ephemeral wins decisively for determinism**:

- **OffCKB** (recommended, https://docs.nervos.org/docs/sdk-and-devtool/offckb and https://github.com/RetricSu/offckb): `npx @offckb/cli@latest start` (or Docker equivalent). Instant local node on 8114 (plus proxy), 1s block production on demand, pre-funded test accounts with "plenty of CKB", *all system scripts pre-deployed*, visual explorer. No PoW. Deterministic genesis. Perfect for "per-run".
- Local CKB binary with a dev chain spec + miner also works but OffCKB is the documented quickstart for contract dev.
- Advantages: zero faucet limits (prefunded or unlimited in dev mode), no reorgs (single controlled node, fixed block times), fixed known addresses from genesis, full control over mempool/epochs, can reset between tasks, no external traffic.
- The ckb-mcp server itself documents devnet ports (28114) and requires `CKB_RPC_URL` for tests/tools; its CLAUDE.md stresses test independence using direct RPC.
- Run the devnet + ckb-mcp + agent as siblings in the same compose stack for the run. Tear down after scoring. 100% reproducible across machines/dates.

**Shared testnet (Pudge) drawbacks** (https://docs.nervos.org/docs/getting-started/ckb-networks):
- Public RPCs (testnet.ckb.dev, testnet.ckbapp.dev, etc.) and https://faucet.nervos.org/.
- Faucet rate limits / per-IP or per-address caps (the MCP's own `dev_request_testnet_funds` tool will hit the same limits; repeated runs will 429 or exhaust).
- Possible reorgs / fork blocks (CKB dev logs mention uncle rates and testnet maintenance; `get_fork_block` RPC exists for a reason).
- Address nondeterminism: fresh random keys each run; cells get consumed by others; indexer state polluted.
- External txs can interfere with assumptions (mempool contention, fee spikes).
- Rate limits on public indexers/RPC during load.
- Not "per-run ephemeral"—state is shared and evolves.

**Conclusion for this benchmark**: Always use per-run ephemeral devnet (OffCKB or equivalent Dockerized CKB dev chain) for the *verifier* (and preferably the agent too, for identical environment). Shared testnet is acceptable only for a "realism / integration" secondary arm after the primary deterministic results are in. Using a shared testnet as the main path makes "deterministic verification" claims hard to defend.

Concrete verifier pseudocode sketch (independent of MCP):
```python
# 1. Rebuild
subprocess.check_call(["make", "build"])
binary = Path("target/.../my-contract")
expected_hash = ...
assert hash(binary) == expected_hash or just assert exists

# 2. Deploy (direct RPC via ckb-sdk or raw)
client = CkbRpcClient(ckb_rpc_url)  # e.g. http://ckb-devnet:8114 from compose
tx_hash = deploy_code_cell(client, binary.read_bytes(), privkey=KNOWN_DEVNET_KEY)
status = wait_committed(client, tx_hash, timeout=120)
assert status == "committed"

# 3. Assert cell + script
cells = client.search_cells(...)  # or get_live_cell(outpoint)
assert any(cell.data == binary and script_matches(cell.type or lock) for cell in ...)

# 4. Functional
spend_tx = build_spend_that_uses_the_script(...)
spend_res = client.submit_transaction(spend_tx)
assert wait_committed(...) == "committed"
assert produced_expected_output_cell(...)
```

## 4. Network Isolation (Q4)

**Exact recipe for "only CKB node + MCP, no public web"**:

Use Docker Compose v2+ with a user-defined network marked internal.

```yaml
# docker-compose.bench.yml
networks:
  bench-net:
    driver: bridge
    internal: true   # Key: no masquerade/NAT, no default gateway to host/internet. Containers talk to each other by name only.

services:
  agent:
    build: ./agent-image   # contains pinned claude-code / codex + rust + make etc.
    networks: [bench-net]
    environment:
      - CKB_RPC_URL=http://ckb-devnet:8114
      - MCP_URL=http://ckb-mcp:3112/mcp   # or absent for OFF
      - http_proxy=          # empty or unset when web=off
      - https_proxy=
    # no extra networks, no host net
    ...

  ckb-devnet:
    image: ... or build with offckb or ckb binary + dev spec
    # or service that does: npx @offckb/cli start -- in background
    networks: [bench-net]
    # exposes 8114 internally only

  ckb-mcp:
    image: ghcr.io/.../ckb-mcp or local build
    environment:
      - CKB_RPC_URL=http://ckb-devnet:8114
    networks: [bench-net]
    # port 3112 internal
```

- Agent reaches `ckb-devnet` and `ckb-mcp` via Docker DNS. No route to 8.8.8.8 or api.* outside.
- `--network none` on the agent + `--link` or manual /etc/hosts is possible but loses the clean service discovery and multi-container simplicity; internal network is superior.
- iptables/DOCKER-USER rules on the host can supplement (e.g. block the bridge subnet from external except specific), but internal network is the Docker-native, portable way and survives rootless in many cases.

**Clean web-access toggle independent of prompt**:
- Two (or parameterized) compose files / profiles:
  - `web=off` (default): the above internal-only stack. Agent has no outbound.
  - `web=on`: add a second network `egress-net` (normal bridge with internet), add a `web-proxy` service (nginx stream or tinyproxy or envoyfilter) attached to both `bench-net` (internal) and `egress-net`. Configure the proxy with a strict allowlist (only the model provider hostnames/ports that the harness needs for the LLM calls, e.g. `api.anthropic.com`, `api.openai.com`; *not* general search, github raw unless explicitly needed for the task, etc.). Set in agent:
    ```
    http_proxy=http://web-proxy:8080
    https_proxy=http://web-proxy:8080
    no_proxy=ckb-devnet,ckb-mcp,localhost,127.0.0.1
    ```
- The proxy can be a one-container sidecar with a static configmap of allowed upstreams. Toggling is `docker compose --profile web-on up` vs default, or an env `BENCH_WEB_ACCESS=off|proxy` that conditionally includes the proxy service and injects the env vars. The prompt text never mentions network; the sandbox enforces it.
- Additional hardening: run agent as non-root, drop caps, read-only rootfs where possible, seccomp. For DNS leakage, the internal net + explicit no_proxy + proxy-only resolution prevents easy bypass.
- If the model provider calls themselves must be blocked for a "zero external knowledge" arm, the proxy simply omits the LLM hosts (but then the agent can't run at all—use a local model or pre-fetched responses for that extreme arm).

This is standard for sandboxed agent evals (see various "egress proxy + internal network" patterns in Docker security posts and Nomad/Hashi discussions). iptables on the host bridge or a privileged netns container are alternatives but more brittle across Docker modes and less "compose-native."

The ckb-mcp Dockerfile and compose already demonstrate multi-service + health + volume patterns; extend them for the bench harness.

## 5. Statistics (Q5)

**Repetitions**: 5–10 per (task × arm). Enough to smooth per-task variance (LLM sampling, minor timing, occasional rate-limit blips even in devnet) without exploding cost. Report both per-attempt mean and Pass@k (k = reps) = fraction of tasks where *at least one* of the k runs succeeded.

**Metrics**:
- Binary: success (verifier returns 1/0 or pass/fail).
- Graded (preferred for partial credit on CKB tasks): 0.0–1.0 based on (build success 0.3 + on-chain deploy success 0.3 + all asserts 0.4) or similar rubric. Deterministic rubric in the verifier.
- Primary reporting: mean score per arm (across tasks and reps), per-task means.

**ON vs OFF delta (paired because same tasks)**:
- **Paired bootstrap CI**: for each task compute delta_i = score_ON_i - score_OFF_i (average over its reps). Bootstrap resample the vector of delta_i (with replacement, 10k+ iterations), take 2.5/97.5 percentiles for 95% CI on mean delta. Non-parametric, handles the dependence.
- **McNemar's test** (binary): count b = tasks where OFF pass and ON fail, c = ON pass OFF fail. Test statistic ( |b-c|-1 )^2 / (b+c) ~ chi^2(1). Directly tests whether the MCP arm "wins" more of the discordant pairs.
- Paired t-test or Wilcoxon signed-rank on the per-task deltas (graded scores).
- Report both the CI on delta and the p-value from the appropriate test. Also raw counts of "ON strictly better", "tied", "OFF better".

**Sample size for defensible claim**:
- Pilot 10–15 tasks × 5 reps to estimate per-task variance and baseline success rate (p0).
- Power: for binary paired, use McNemar power or simulation. For a 20-point absolute lift (e.g. 35% → 55%) with moderate discordance, ~40–60 tasks often suffices for 80% power at alpha=0.05 two-sided. For smaller lifts or higher variance, 80–120+.
- Use exact or bootstrap methods rather than normal approximations for small N.
- Always report uncertainty (CI) rather than point estimates alone. A "statistically significant" result on 15 tasks is weak; a 18-point lift with CI [8, 29] on 50 tasks is credible.
- Total runs scale with tasks × reps × 2 arms. Parallelize across tasks (different ephemeral stacks); sequential within a task's reps only if needed for quota.
- Pre-register: number of tasks, reps, exact success rubric, analysis code, stopping rules.

**Pass@k vs mean score**:
- Mean per-attempt score directly answers "does MCP improve typical output quality?"
- Pass@k answers "with k tries, how often do you get at least one good output?" (useful for "the agent eventually succeeds").
- Report both. In the DeepSWE style, emphasize Pass@1 (or the single-attempt mean) with CIs for the primary story.
- Variance reduction from pairing is large; always analyze paired.

**Practical notes**:
- Tasks must be independent (fresh devnet each run or reset between tasks).
- Stratify tasks by difficulty/category and report subgroup results.
- Log every seed, prompt hash, config hash, git SHA of harness + ckb-mcp + verifier for exact reproducibility.
- Contamination: all tasks written for this benchmark, not scraped from public CKB tutorials that may be in training data.

## 6. Reporting Site (Q6)

**Minimal stack**: Single self-contained `index.html` (or `report.html`).
- Tailwind CSS via CDN (`<link href="https://cdn.tailwindcss.com">` + script init).
- Charting: Chart.js via CDN (or a tiny wrapper). One canvas for the main visual.
- Data: either a `<script>window.BENCH_DATA = { tasks: [...], summary: {...} }</script>` baked at report generation time, or a sibling `data.json` (still trivial deploy).
- No Node, no bundler, no external runtime deps beyond CDNs (which are reliable for this use).
- Deploy: push the single (or two) files to a `gh-pages` branch or repo root; GitHub serves it. Or any static host. Update by re-running the analysis script that emits the HTML/JSON.

**What the page must show for a compelling ON-vs-OFF story**:
- Header: "CKB AI MCP A/B (model X, N tasks, R reps/arm)". Date + SHAs.
- Summary cards (3–4): OFF mean (CI), ON mean (CI), Delta (CI, color green if >0), "Statistically significant (McNemar p=0.012)" or equivalent.
- Main chart: horizontal or grouped bar per task (or per task-group) showing OFF bar, ON bar, delta annotation. Sort by delta or baseline difficulty. Hover for per-task CI or raw reps.
- Optional second view: scatter of OFF vs ON scores (points above diagonal = MCP win).
- Full table (sortable, filterable with vanilla JS): Task | Category | OFF mean (reps) | ON mean (reps) | Delta | 95% CI (boot) | Notes / asserts passed.
- "About the harness / isolation / devnet" footnote with links to the exact compose + verifier commit.
- Download: "raw_results.json" link.

Keep it opinionated and scannable—one chart + one table tells the paired story better than 10 subplots.

Alternatives if you want slightly more: a tiny Vite + vanilla TS build that outputs a single HTML (esbuild inline), but the CDN single-file is simpler and sufficient.

## 7. Reshaping the Design + Risks (Q7)

**Where the first-draft design is wrong or under-built**:
- **Shared testnet as default**: Actively harmful to the determinism goal. Faucet limits + reorg risk + nondet + shared state make "same inputs => same score" false in practice. Replace with "always ephemeral OffCKB devnet (or equivalent) per run/batch, fully inside the compose stack." Shared testnet only for a "production-like integration" appendix.
- **"Container almost always needs network" left as prompt or vague**: This must be a first-class, prompt-independent control. The internal-network + optional proxy pattern above is concrete, portable, and auditable. Do not rely on the agent "not searching the web because we asked nicely."
- **Harness complexity**: Acknowledged concern is valid. Official Claude/Codex are the right primary because they are the delivery vehicle for CKB AI and have native MCP. But the harness runner must treat MCP addition as pure config, not prompt engineering. Provide a reference container image + entrypoint that does exactly the add/remove + run + capture outputs.
- **Verifier as afterthought**: The verifier *is* the source of truth for the claim. It must be written first (or in parallel), be 100% independent of the MCP, and itself be versioned + tested against known-good contract outputs. Include build-reproducibility checks.
- **Under-specified isolation for the MCP and CKB node themselves**: If the MCP or devnet container can reach the public internet, an agent could in theory coerce them (or use them as proxies). Keep the whole trio on the internal net; only the optional web-proxy (when enabled) has egress.
- **Statistics hand-wavy**: "Repeat several times" is not enough. Commit to paired analysis, power targets, and pre-registration. Otherwise claims will not survive adjudication.
- **Reporting**: Good instinct on "simpler than DeepSWE." Single-file static wins for credibility and maintainability.

**Concrete changes that increase provability and maintainability**:
- Make the full harness a small repo with: `bench/`, `verifier/`, `docker/`, `tasks/`, `analysis/`, `report/`. `make bench TASK=foo ARMS=on,off REPS=5` launches the right compose variant, captures, scores, appends to results DB.
- Every task has a `task.yaml` (prompt, verifier script path or inline rubric, expected artifacts).
- Verifier always starts its own fresh OffCKB (or connects to the one the agent used) and tears it down.
- Config for MCP is a volume or env that the entrypoint translates into the exact `claude mcp add` or config.toml edit—zero prompt text differs.
- Add a "no-MCP, no-web, local-model" arm later for even cleaner isolation.
- Continuous: the same harness can regression-test new ckb-mcp releases (OFF = old MCP image or docs-only mode vs new).

**Single highest-risk part**: The verifier + build determinism loop. If two "identical" runs produce different binaries (toolchain drift, cargo cache, non-reproducible deps, timestamp in wasm/elf) or the on-chain assertions are sensitive to block height / fee rate / indexer lag, the scores become noisy and the delta untrustworthy. Mitigate by: (1) fully containerized reproducible builds with explicit `rust-toolchain.toml` + vendored or locked deps where possible; (2) assertions that are tolerant (committed within window, data exact match, functional success) but not timing-dependent; (3) always re-verify a known-good golden task in CI; (4) log full tx JSON and cell state on every run for post-mortem.

**Other risks**: Model provider side effects (different "knowledge" or tool use between runs even with same prompt); prompt leakage of network/MCP details; cost explosion if reps/tasks too high without parallelization; MCP server itself having state (the stats DB, the private key for dev deploys) that leaks across runs—use fresh MCP containers with `--private-key` and ephemeral stats.

This design, with the corrections above, can produce a credible, defensible demonstration that the CKB MCP server improves agent performance on CKB dev tasks—or that it does not. The emphasis on ephemeral devnets, network-layer controls, paired stats, and verifier independence directly serves the "prove or disprove" objective.

## References (selected)

- OffCKB & devnet: https://docs.nervos.org/docs/sdk-and-devtool/offckb , https://docs.nervos.org/docs/node/run-devnet-node , https://github.com/RetricSu/offckb
- CKB networks + faucet: https://docs.nervos.org/docs/getting-started/ckb-networks
- Script templates / modern Rust: https://github.com/cryptape/ckb-script-templates (and ckb-mcp's own docs/tools/contract-workspace.md)
- ckb-mcp architecture & tools: /home/username/ckb-mcp/README.md , CLAUDE.md , crates/ckb-ai-mcp/src/{rpc,dev,ckb,server}.rs (and the generated tool lists)
- MCP HTTP transport & clients: Claude Code / Codex docs (e.g. developers.openai.com/codex/mcp , platform.claude.com MCP connector)
- Docker isolation patterns: internal networks (Docker docs), egress proxy examples in security / compose discussions
- Benchmark statistics: SWE-bench Verified methodology (https://www.swebench.com/ , OpenAI announcement), DeepSWE (deepswe.datacurve.ai), paired bootstrap/McNemar references in stats literature
- Public RPC examples: https://github.com/nervosnetwork/ckb/wiki/Public-JSON-RPC-nodes (historical)

All CKB-specific claims cross-checked against the above + the on-disk docs/ and resources/ in ckb-mcp. 

Report complete. Ready for adjudication.
