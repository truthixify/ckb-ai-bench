# Self-research (orchestrator's own subagent): mini-swe-agent, harness MCP support, egress

Conducted by the orchestrator via a dedicated research subagent (web + official docs), then
cross-checked directly against `/home/username/ckb-mcp`. Cited findings below.

## 1. mini-swe-agent (github.com/SWE-agent/mini-swe-agent)

- "100 line AI agent that solves GitHub issues"; >74% on SWE-bench verified. Core loop ~100–188 LoC.
- Philosophy (quoted): no tools other than bash; doesn't use the tool-calling interface; completely
  linear message history; executes each action with `subprocess.run` (no stateful shell).
- **MCP: NO native support.** Code search for "mcp" returns 0 hits. Maintainer guidance (issue #563):
  "just adding a command line program to your environment and then telling mini about it is the way to go."
  Native-MCP request #470 closed as not planned. → MCP must be wrapped as a CLI/bash command.
- **Docker: YES** — `DockerEnvironment` (`environment_class: docker`), uses `docker exec ["bash","-lc"]`;
  config fields image/cwd/env/forward_env/timeout/run_args. Also local/podman/singularity/bubblewrap.
- **Deterministic/minimal: YES** by design.
- **DeepSWE rationale**: harness held fixed across every model so the leaderboard reflects model
  capability, not the scaffolding ("every model a single bash tool, no vendor editing primitives").
- **Providers: litellm under the hood** → any OpenAI-compatible endpoint via YAML
  `model_kwargs: {api_base, api_key}`. (This is exactly what we need for benchmarking arbitrary models.)

URLs: https://github.com/SWE-agent/mini-swe-agent ; https://mini-swe-agent.com/latest/ ;
issues #563, #470 ; src/minisweagent/environments/docker.py ; .../models/litellm_model.py ;
https://deepswe.datacurve.ai/ ; https://github.com/datacurve-ai/pier

## 2. Claude Code MCP support

- Add remote HTTP MCP non-interactively: `claude mcp add --transport http <name> <url>`;
  headers via `--header "Authorization: Bearer ..."`; `--transport sse` deprecated; `--scope local|project|user`.
- Headless: `claude -p "..."` (print mode), built for CI/containers; `--bare` for reproducible scripted runs.
- Disable web tools by exact name: `--disallowedTools "WebSearch" "WebFetch"` (or settings.json `permissions.deny`).
- **Providers: Anthropic Messages / Bedrock / Vertex / Foundry only.** Gateways via `ANTHROPIC_BASE_URL`
  must speak Anthropic/Bedrock/Vertex format. **Arbitrary OpenAI-compatible base URLs are NOT supported.**
  → This is a real limitation for a model-agnostic benchmark.

URLs: https://code.claude.com/docs/en/mcp ; .../headless ; .../cli-reference ; .../tools-reference ;
.../model-config ; .../llm-gateway

## 3. OpenAI Codex CLI MCP support

- Remote HTTP MCP in config.toml: `[mcp_servers.<id>] url = "https://.../mcp"`,
  `bearer_token_env_var`, `http_headers`. Also stdio servers. `codex mcp add/list/remove/login` exists.
- Headless: `codex exec "..."` (JSONL to stdout). Sandbox: `--sandbox read-only|workspace-write|danger-full-access`.
- **OpenAI-compatible base URLs: YES** via `[model_providers.<id>]` (base_url, env_key, wire_api).
- **Network sandbox: YES** — network DISABLED by default under workspace-write; opt in via
  `[sandbox_workspace_write] network_access = true`. → clean built-in web toggle.

URLs: https://developers.openai.com/codex/mcp ; .../config-reference ; .../noninteractive ;
.../concepts/sandboxing

## 4. OpenHands & aider

- **OpenHands: YES** — remote MCP via `shttp_servers` (Streamable HTTP, preferred) + `sse_servers`
  in `[mcp]`. https://docs.openhands.dev/openhands/usage/settings/mcp-settings
- **aider: NO** native remote HTTP MCP (feature request #3314 open; PRs #3672/#3937 closed unmerged).

## 5. MCP transport + stdio→HTTP bridges

- **Streamable HTTP** (rev 2025-03-26; current 2025-06-18): single endpoint, `Mcp-Session-Id` sessions,
  resumability; explicitly replaces the 2024-11-05 HTTP+SSE two-endpoint transport. This is what
  ckb-ai-mcp implements. https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- stdio→remote HTTP bridges: `mcp-proxy` (sparfenyuk/mcp-proxy) `--transport streamablehttp <url>`;
  `supergateway` (supercorp-ai/supergateway). → the bridge for any bash-only harness.

## 6. Docker egress control (web on/off, enforced at network layer not prompt)

- **Cleanest toggle**: `docker network create --internal` agent net (no NAT / no default route) +
  an allowlisting forward proxy (tinyproxy/squid, `FilterDefaultDeny Yes`) on a second egress bridge,
  selected via `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`. Domain-based allowlist (proxy does DNS, no IP pinning).
  Declarative in one compose file; survives restarts.
- Defense-in-depth: `DOCKER-USER` iptables chain (processed before Docker's rules). Host-global, IP-based,
  must allow DNS; no DOCKER-USER under the nftables backend. Messier than the proxy approach.

URLs: https://docs.docker.com/network/proxy/ ; https://docs.docker.com/engine/network/firewall-iptables/ ;
tinyproxy.conf(5)

## Cross-check against the actual ckb-mcp repo (verified directly, not via agent)

- `CLAUDE.md` mandates **test independence**: "Use direct CKB RPC client calls (NOT MCP server)" for BOTH
  setup and verify. → the verifier-independence requirement is the project's own convention, not just opinion.
- `server.rs` runs **stateless** (`NeverSessionManager`) BECAUSE "Codex and Claude Code both reuse session IDs"
  → both official CLIs are first-class, designed-for targets; server was built around them.
- Real tool surface confirmed: `rpc_*` (get_block, get_transaction, search_cells, get_live_cell, ...),
  `dev_*`, `ckb_*`, `search_*`; docs served as `ckb://docs/...` resources; workflow prompts.
- `dev_request_testnet_funds` calls an external faucet → must be disabled/avoided in the benchmark
  (external HTTP + rate limits = nondeterminism).
