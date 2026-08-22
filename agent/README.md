# CKB Bench Agent — fork spike

A **proof-of-concept fork** of [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
that adds native MCP so it can use the CKB AI MCP server — the thing the benchmark puts under test.

**Status: spike PASSED end-to-end against the live server `https://mcp.ckbdev.com/ckbai`.**
The production harness wires this fork via `ckbbench.run.agent_factory` (real LitellmModel loop
over the LLM proxy, arm-aware MCP on/off).

## What this proves

The open question was whether forking mini-swe-agent and adding MCP is clean and works. It is:

- The fork adds MCP **without modifying a single upstream source file** (`agent/minisweagent/` is
  byte-identical to upstream commit in `UPSTREAM_COMMIT.txt`; verified via `diff -rq`). All MCP logic
  lives in two new files. This means we can pull upstream updates with zero merge conflicts.
- The MCP client is ~90 lines of stdlib + `requests` — no SDK, because the server is stateless
  Streamable HTTP (no session id, no `initialized` handshake required): just POST JSON-RPC, read the
  `data:` SSE line.
- The agent gains MCP and controller-owned task transitions via mini-swe-agent's **designed extension
  seam** — subclassing `DefaultAgent` and overriding `execute_actions` (default.py:152). Ordinary
  Bash / file-edit / Docker actions still use the upstream environment.

## Files (the entire fork addition)

| File | Role |
|---|---|
| `ckb_mcp.py` | Native Streamable-HTTP MCP client (`initialize` / `tools/list` / `tools/call`). |
| `ckb_agent.py` | `CkbMcpAgent(DefaultAgent)` — routes MCP actions, ordinary shell actions, and the arm-symmetric staged-task controller. `mcp=None` keeps a clean OFF arm. |
| `spike_mcp.py` | The proof: init + list + live tool call + drives the real `execute_actions` path. No LLM needed. |
| `minisweagent/` | Vendored upstream, **unmodified**. |
| `UPSTREAM_COMMIT.txt` | The exact upstream commit forked from (provenance/pinning). |
| `spike-requirements.txt` | Pinned deps the spike used. |

`mcp_call` reserves one non-tool action: `mcp_call resources/read {"uri": "..."}` returns a
documentation resource's text body. It takes exactly the `uri` field, validated locally before any
request, and is the only MCP method besides `tools/call` the model can reach. Discovery goes through
the ordinary `search_resources` tool.

This fork runs in the **host harness process**, not inside the execution image. The container is the
shell and build environment; it does not carry this source or the MCP endpoint.

## Run the spike

```bash
cd agent
uv venv --python 3.12 .venv && . .venv/bin/activate
uv pip install -r spike-requirements.txt
PYTHONPATH="$PWD" python spike_mcp.py            # defaults to the live endpoint
PYTHONPATH="$PWD" python spike_mcp.py http://localhost:3112/mcp   # or a local server
```

## Spike result (2026-06-12, live server)

```
[1] initialize OK -> ckb-ai-mcp v1.6.12 (protocol 2025-06-18)
[2] tools/list OK -> 51 tools
[3] tools/call rpc_get_tip_block_number OK -> tip=0x1469b6c (21404524) isError=False
[4] CkbMcpAgent built; 51 MCP tools exposed; routed mcp_call ckb_query_chain_status
    through execute_actions -> 2093-char live chain-status observation
[5] OFF arm (mcp=None): 0 tools, treats mcp_call as MCP? False
RESULT: PASS - fork uses live MCP end-to-end
```

## What this is NOT yet (remaining production polish)

The model loop, arm-aware MCP on/off, and system-prompt tool exposure are wired in production via
`ckbbench.run.agent_factory` (see `spike_model_loop.py` for the spike that proved the pattern).
Still open:

1. **A proper edit tool** — mini-swe-agent edits files via bash only. Decide whether bash-grade editing
   is enough or add a `write_file`/`apply_patch` action (also routed in `execute_actions`).
2. ~~**Tool-search awareness**~~ — **settled.** Phase one exposes neither `search_tools` nor the
   full `tools/list`: an MCP arm receives exactly `search_resources` plus reserved `ckb://docs/`
   resource reads, enforced by the `surface` policy this agent is constructed with (ADR-0013).
3. ~~**Docker packaging + pinning**~~ — **settled.** The agent and verifier images are built and
   pinned by digest in the suite manifest, and the harness resolves them from there.

See `../docs/RECOMMENDATION.md` for how this agent fits the overall benchmark design.
