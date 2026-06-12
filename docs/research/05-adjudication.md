# Adjudication of Round 1 Research

**Adjudicator:** orchestrating agent (Opus). **Date:** 2026-06-12.
**Inputs:** four independent streams + direct verification of `/home/username/ckb-mcp`:
- `04-codex-harness-confound.md` — OpenAI gpt-5.5 @ xhigh (harness + confound lens)
- `01-grok-build-...md` — xAI grok-build @ max (verification + isolation + stats lens)
- `02-grok-composer-...md` — xAI grok-composer-2.5-fast (reporting + design-critique lens)
- `03-self-research-...md` — Anthropic Opus subagent (harness MCP support + egress) + my own repo reads
- `00-deepswe-reference.md` — the live DeepSWE site (precedent)

**Decision on rounds: ONE round is sufficient.** Four independent model families plus the DeepSWE
precedent converged on the same architecture with no genuine contradictions — only differences of
emphasis, all resolvable. A second round would refine wording, not change conclusions. Spending it
would violate "minimum work that solves the problem." If implementation later surfaces a real unknown
(e.g. an OffCKB/devnet quirk), that is a targeted follow-up, not another broad research round.

## Where all sources converged (high confidence — treat as settled)

1. **Harness: a thin, MCP-native custom runner is the primary instrument; official CLIs are a
   secondary "product compatibility" track; mini-swe-agent only as a shimmed robustness track.**
   - The DeepSWE→mini-swe-agent analogy is a **category error** for our case (composer + codex agree
     explicitly). DeepSWE holds the harness constant to measure *models*; we measure *MCP*. A bash-only
     harness literally cannot see MCP without a shim that *isn't* MCP — so it would benchmark bash glue,
     not the server as users experience it.
   - Claude Code / Codex add product-specific prompts, tool search, edit tools, web search, session
     state, and hidden defaults — valuable products, poor experimental instruments.
   - All four independently rank: **custom thin (best) > Codex CLI (best official) > Claude Code (fair) >
     OpenHands (overbuilt) > mini-swe-agent w/ shim (robustness only) > aider (no MCP).**

2. **Verifier: ephemeral per-trial CKB devnet (OffCKB), never shared public testnet; verifier uses
   direct CKB RPC and NEVER the MCP server.** This is confirmed by the repo's OWN `CLAUDE.md` rule
   ("Use direct CKB RPC client calls (NOT MCP server)" for setup and verify). grok-build calls the
   user's "usually a testnet node" default *"actively wrong"* for determinism; every source agrees.
   Truth of a CKB task = on-chain state, not "files produced."

3. **Network isolation: a Docker `internal: true` network for agent+node+MCP, with an allowlisting
   proxy as the only egress; web access is a separate, network-layer toggle, NEVER a prompt
   instruction.** All four agree models ignore "please don't search the web." Codex adds the sharp
   point that plain Docker bridges masquerade to the internet and `--network none` is too isolated to
   reach the node — so `internal: true` + proxy is the correct middle.

4. **Statistics: binary Pass@1 (not continuous mean) as the headline; PAIRED task-level analysis;
   paired bootstrap CI on per-task deltas + McNemar/sign test; report CIs not just p-values;
   pre-register tasks/reps/exclusions.** Convergent numbers: pilot ~30 tasks ×5 reps; public claim
   ~50–80 tasks ×5–10 reps. Claim only when the ON−OFF CI excludes 0 AND the lower bound is
   practically meaningful.

5. **Reporting: a zero-build static site (single `index.html` + `summary.json` + a small CDN chart
   lib).** The one chart should foreground the **delta with CI** (treatment-vs-control story), not
   ON/OFF bars that force the reader to subtract. A table carries the numbers + provenance.

6. **Provenance/pinning: pin every digest** (harness image, MCP image — it's ALPHA, CKB node image,
   verifier commit, model id, prompt hash, tool-list hash, chain genesis hash) into every trial record.

## Where sources diverged, and my rulings

| Topic | Divergence | Ruling |
|---|---|---|
| OFF arm shape | composer: "remove `.mcp.json`". codex: run a **null-MCP server** at the same URL returning zero tools but identical `initialize` metadata. | **Adopt codex's null-MCP.** It makes the only difference the server *implementation*, not connection errors/timeouts/missing-host. Strongest confound control; cheap to build. |
| Primary harness now vs later | grok-build leaned "official CLIs as primary because they're the product." composer/codex: thin custom is the scientific instrument; CLIs are compatibility tracks. | **Thin custom = primary** for the causal claim; **Codex CLI = the official compatibility track** run second. grok-build's point survives as *why the CLI track matters* (it's what users actually run), not as the instrument for the headline number. |
| Egress enforcement | grok-build/composer: allowlist proxy (tinyproxy/squid) + optional `DOCKER-USER` iptables. codex: LLM-proxy in front of the provider API too. | **Allowlist forward proxy as the toggle; LLM calls also go through a proxy** so even provider egress is controlled and logged. iptables `DOCKER-USER` as defense-in-depth where the host allows. |
| Chart lib | composer: Observable Plot. grok-build: Chart.js. codex: either. | **Either works; default to Chart.js** (most familiar, error bars via a small plugin) unless we want Plot's terser API. Not load-bearing — decide at build time. |
| Task count | composer floated as few as 8–12 for v1 honesty; codex/grok-build want 30→50–80 for a public claim. | **Phase it:** 12-task pilot to prove the *effect exists*, then scale to 50–80 for the *public* claim. Honest labeling at each stage. |

## Non-obvious insights worth preserving

- **Built-in CKB knowledge is not removable** (codex). It's balanced by same-model-both-arms. To make
  the MCP delta *visible*, bias tasks toward current / repo-specific / chain-state-specific info the
  model can't have memorized but can obtain via MCP or direct RPC. The benchmark measures *improvement*,
  not absolute dependence — OFF solving some tasks via installed SDKs/RPC is fine and expected.
- **OFF-arm doc leakage is a real design bug** (composer): the MCP repo bundles extensive `docs/` and
  `resources/`. If any of that is mounted into the agent workspace, the OFF arm gets the MCP's knowledge
  for free. The agent workspace must be clean of CKB docs and of the MCP repo itself.
- **The MCP server is ALPHA** (codex, verified in README) — pin its image digest or the benchmark isn't
  reproducible across MCP changes. This also makes the suite the natural regression gate for new versions.
- **The server is stateless by design** (`NeverSessionManager`) *because* Codex and Claude Code reuse
  session IDs — so both official CLIs are first-class, designed-for clients. Good for the compatibility track.
- **Disable the faucet path.** `dev_request_testnet_funds` hits an external faucet (rate limits + external
  HTTP). Use devnet pre-funded genesis keys; never call the faucet in a trial.
- **The MCP server has feature flags** (`--docs-only`, `--rpc-only`, `--tools-only`, `--no-prompts`).
  These enable a future **ablation**: is the win from tools, from docs/resources, or from prompts? Not v1,
  but the design should not preclude it.

## Confidence statement

The harness/verifier/isolation/stats architecture is **high confidence** — multi-family convergence +
the repo's own conventions + the DeepSWE precedent. The **open decisions genuinely belonging to the
user** (not resolvable from research) are carried into the recommendation as explicit choices:
(a) is the headline claim about the *thin instrument* or the *official-CLI product*; (b) which model(s)
to feature first; (c) v1 task count / timeline appetite. These are product/strategy calls, not technical
unknowns.
