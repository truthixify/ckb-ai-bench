# Spike: real model loop (grok-composer via proxy) — FINDINGS (2026-06-12)

Goal (Tier-1 #3): replace the simulated model in `spike_mcp.py` with a REAL model loop and prove
the *model* — not a hand-built message — emits bash actions, `mcp_call` actions, and the `done`
sentinel, terminating via mini-swe-agent's `Submitted` exit.

## Wiring

- `spike_model_loop.py`: `LitellmModel(model_name="openai/grok-composer-2.5-fast")` pointed at the
  local proxy (`api_base=http://localhost:18321/v1`, `api_key=sk-noauth`), driving the real
  `CkbMcpAgent` in a real `LocalEnvironment` (isolated temp dir).
- The ON arm renders 8 live `rpc_*` MCP tool names into the system prompt so the model knows the
  `mcp_call <tool> <json>` vocabulary; the fork intercepts those commands before the shell.
- Deps added to the spike venv: `litellm`, `tenacity` (the litellm-model path imports them).

## Proxy supports native tool-calling

A direct litellm probe confirmed grok-composer-2.5-fast returns a proper `bash` tool_call
(`{"command":"echo hi"}`, `finish_reason=tool_calls`) through the proxy. So the upstream
toolcall-based `LitellmModel` path works unmodified against the proxy.

## Result (live)

```
exit_status: Submitted
n_model_calls: 3
used mcp_call at least once: True
wrote tip.txt: True  contents: '0x146a83b'
RESULT: PASS - real model drove bash + mcp_call + done
```

Trajectory: task -> assistant tool_call `mcp_call rpc_get_tip_block_number {}` (live result
`"0x146a83b"`) -> assistant bash writes tip.txt -> assistant `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
-> Submitted exit. Every action was emitted by the model.

## Real fork bug found and fixed

The fork's `_run_mcp_action` (ckb_agent.py) returned an output dict WITHOUT the `exception_info`
key, but every upstream `env.execute` output (docker.py, local.py) always includes it, and the
default `observation_template` renders `{% if output.exception_info %}`. With `StrictUndefined`,
an MCP observation therefore crashed Jinja with `'dict object' has no attribute 'exception_info'`.

Fix: all four return paths of `_run_mcp_action` now include `"exception_info": ""`, restoring the
env-output contract the docstring already promised. The original `spike_mcp.py` (ON + OFF arms)
still passes after the fix — no regression. This is exactly the kind of integration gap the real
model loop was meant to surface (the simulated model's fake `format_observation_messages` never
exercised the real template).

## Hardening after adversarial review (2026-06-12)

Reviewers (grok-build, grok-composer, codex) raised three points; all addressed:

1. **OFF-arm coverage.** The spike now runs BOTH arms on the same task. ON: Submitted, used_mcp=True,
   tip via MCP. OFF (`mcp=None`): **0 MCP tools, used_mcp=False** — the MCP surface is genuinely
   absent. This proves the C-B arm isolation at the tool level.
   - **Finding worth surfacing loudly:** on `LocalEnvironment` the OFF arm still wrote a tip (via
     host `curl`/network), i.e. it answered without MCP. That is NOT an OFF-arm leak (no MCP was
     used) but it confirms the task was not truly MCP-gated, because LocalEnvironment has open
     network. This is exactly what ADR-0006's egress proxy is for: the OFF arm must be network-
     restricted so MCP is the only route to CKB data. The spike makes this gap visible rather than
     hiding it. Net: the model loop + arm wiring is proven; true OFF-arm gating needs the proxy.

2. **`mcp_call` JSON parsing.** The original `shlex.split(...)` + `" ".join(rest)` corrupted JSON
   args containing spaces or quotes (e.g. `{"address": "ckt1 ..."}`). Fixed: only the `mcp_call`
   keyword + tool name are tokenized; the rest of the line is passed to `json.loads` verbatim.
   Covered by `agent/test_ckb_agent.py` (spaces, nested arrays, escaped quotes, malformed input,
   and the bash-not-hijacked cases). Non-dict JSON is now rejected cleanly too.

3. **exception_info parity (codex).** Codex independently confirmed the fix matches both the local
   and Docker env output contracts for that key. (Success path also carries `extra`; error paths
   don't — tolerated by the `.get`-based templates. Not all env-error keys like `exception_type`
   are mirrored, which is acceptable since MCP errors are surfaced via the output text + returncode.)

## Still next (out of scope for this spike, tracked)

- Docker packaging + egress-proxy so the OFF arm is network-gated (the point above).
- System-prompt tool exposure at scale (full 51 tools vs. search_tools deferred loading).
- A `write_file`/`apply_patch` action if bash-grade editing proves insufficient.

## Reproduce

```
cd agent
uv pip install --python .venv/bin/python litellm tenacity   # once
PYTHONPATH="$PWD" MSWEA_COST_TRACKING=ignore_errors .venv/bin/python spike_model_loop.py
```
