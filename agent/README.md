# CKB Bench Agent — fork spike

A **proof-of-concept fork** of [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
that adds native MCP so it can use the CKB AI MCP server — the thing the benchmark puts under test.

**Status: spike PASSED end-to-end against the live server `https://mcp.ckbdev.com/ckbai`.**

## What this proves

The open question was whether forking mini-swe-agent and adding MCP is clean and works. It is:

- The fork adds MCP **without modifying a single upstream source file** (`agent/minisweagent/` is
  byte-identical to upstream commit in `UPSTREAM_COMMIT.txt`; verified via `diff -rq`). All MCP logic
  lives in two new files. This means we can pull upstream updates with zero merge conflicts.
- The MCP client is ~90 lines of stdlib + `requests` — no SDK, because the server is stateless
  Streamable HTTP (no session id, no `initialized` handshake required): just POST JSON-RPC, read the
  `data:` SSE line.
- The agent gains MCP via mini-swe-agent's **designed extension seam** — subclassing `DefaultAgent`
  and overriding `execute_actions` (default.py:152). Bash / file-edit / Docker behavior is untouched.

## Files (the entire fork addition)

| File | Role |
|---|---|
| `ckb_mcp.py` | Native Streamable-HTTP MCP client (`initialize` / `tools/list` / `tools/call`). |
| `ckb_agent.py` | `CkbMcpAgent(DefaultAgent)` — routes `mcp_call <tool> <json>` actions to MCP; everything else to the shell env. `mcp=None` → clean OFF arm (no MCP tools, no special handling). |
| `spike_mcp.py` | The proof: init + list + live tool call + drives the real `execute_actions` path. No LLM needed. |
| `minisweagent/` | Vendored upstream, **unmodified**. |
| `UPSTREAM_COMMIT.txt` | The exact upstream commit forked from (provenance/pinning). |
| `spike-requirements.txt` | Pinned deps the spike used. |

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

## What this is NOT yet (next steps if we proceed to the full build)

This spike validates the *plumbing*, not a full agent run. To become the benchmark's agent it still needs:

1. **A real model loop** — wire in an OpenAI-compatible model (mini-swe's litellm model, or a thin
   custom one) so the *model* emits `mcp_call`/bash actions instead of our simulated message.
2. **A proper edit tool** — mini-swe-agent edits files via bash only. Decide whether bash-grade editing
   is enough or add a `write_file`/`apply_patch` action (also routed in `execute_actions`).
3. **System-prompt tool exposure** — render the 51 MCP tool names + schemas into the system template so
   the model knows the `mcp_call` vocabulary (the ON arm); omit entirely in the OFF arm.
4. **Tool-search awareness** — the server advertises deferred loading via `search_tools`; decide whether
   the agent leans on that or on the full `tools/list` (both are available).
5. **Docker packaging + pinning** for reproducible benchmark trials.

See `../docs/RECOMMENDATION.md` for how this agent fits the overall benchmark design.
