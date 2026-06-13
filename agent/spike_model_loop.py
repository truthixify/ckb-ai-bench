"""Spike (Tier-1 #3): the REAL model loop.

Replaces the fake model in spike_mcp.py with a real LitellmModel pointed at the
local OpenAI-compatible proxy (grok-composer-2.5-fast), driving the actual
CkbMcpAgent in a real LocalEnvironment. This proves the *model* — not a simulated
message — emits bash actions, mcp_call actions, and the `done` sentinel, and that
the run terminates via mini-swe-agent's Submitted exit.

The ON arm exposes the MCP `mcp_call` vocabulary in the system prompt; the agent
uses it to read the live CKB tip, writes it to a file with bash, and submits.

Run:
  cd agent
  PYTHONPATH="$PWD" .venv/bin/python spike_model_loop.py
Env overrides: BENCH_MODEL, BENCH_API_BASE, MCP_URL.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel

from ckb_mcp import CkbMcpClient
from ckb_agent import CkbMcpAgent

MODEL = os.getenv("BENCH_MODEL", "openai/grok-composer-2.5-fast")
API_BASE = os.getenv("BENCH_API_BASE", "http://localhost:18321/v1")
MCP_URL = os.getenv("MCP_URL", "https://mcp.ckbdev.com/ckbai")

# System prompt: bash convention (from upstream default) + the fork's mcp_call
# vocabulary rendered from the live tool list (the ON arm).
SYSTEM_TEMPLATE = """You are a CKB engineering agent working in a Linux shell.

Every turn you call the `bash` tool exactly once with a single command. Two kinds
of commands exist:

1. A normal shell command, e.g. `ls`, `cat file`, `echo hi > out.txt`.
2. An MCP tool call to the CKB AI server. Form (as the bash command string):
       mcp_call <tool_name> <json-args>
   e.g.  mcp_call rpc_get_tip_block_number {}
   The harness intercepts any command whose first word is `mcp_call` and runs the
   MCP tool instead of the shell, returning the tool's text result as the output.

Available MCP tools (name -- description):
{{mcp_tool_list}}

When the task is fully done, call bash with EXACTLY:
       echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
and nothing else. After that you cannot act further.
"""

INSTANCE_TEMPLATE = """Task: {{task}}

Work in the current directory. Do the steps, then submit."""


def build_model():
    return LitellmModel(
        model_name=MODEL,
        model_kwargs={
            "api_base": API_BASE,
            "api_key": os.getenv("BENCH_API_KEY", "sk-noauth"),
            "temperature": 0,
            "drop_params": True,
        },
        cost_tracking="ignore_errors",
    )


def main() -> int:
    print(f"== model-loop spike ==\nmodel: {MODEL} via {API_BASE}\nmcp: {MCP_URL}\n")

    mcp = CkbMcpClient(url=MCP_URL)
    info = mcp.initialize()
    ver = info.get("serverInfo", {}).get("version")
    tools = mcp.list_tools()
    # Expose a compact tool list (a handful relevant ones is plenty for the spike).
    keep = [t for t in tools if t["name"].startswith("rpc_")][:8]
    tool_list = "\n".join(f"- {t['name']} -- {t.get('description','')[:80]}" for t in keep)
    print(f"MCP v{ver}, {len(tools)} tools; exposing {len(keep)} rpc_ tools to the model")

    workdir = Path(tempfile.mkdtemp(prefix="ckb-modelloop-"))
    print(f"workdir: {workdir}")

    # The MCP-only task. Phrased to require the MCP tool; on the OFF arm there is no
    # mcp_call surface, so the model cannot satisfy it via MCP.
    task = (
        "Use the MCP tool rpc_get_tip_block_number to read the current CKB tip block "
        "number, then write ONLY that value (the raw result text) into a file named "
        "tip.txt in the current directory. Then submit."
    )

    on = _run_arm("ON", mcp, tool_list, task, workdir / "on")
    off = _run_arm("OFF", None, "(none — this is the OFF arm)", task, workdir / "off")

    print("\n=== arm comparison ===")
    print(f"ON : exit={on['exit']} used_mcp={on['used_mcp']} wrote_tip={on['wrote']} tip={on['tip']!r}")
    print(f"OFF: exit={off['exit']} used_mcp={off['used_mcp']} wrote_tip={off['wrote']} tip={off['tip']!r}")

    on_ok = on["exit"] == "Submitted" and on["used_mcp"] and on["wrote"] and on["tip"]
    # OFF arm must have ZERO mcp surface: no tools exposed, and no mcp_call ever
    # succeeded (any mcp_call the model emits routes to bash and errors).
    off_no_mcp = off["n_tools"] == 0 and not off["used_mcp"]

    ok = on_ok and off_no_mcp
    print(
        f"\nRESULT: {'PASS' if ok else 'FAIL'} — "
        f"ON drove bash+mcp_call+done; OFF has no MCP surface (tools={off['n_tools']}, used_mcp={off['used_mcp']})"
    )
    return 0 if ok else 1


def _run_arm(name, mcp, tool_list, task, workdir):
    """Run one arm (ON: mcp client present; OFF: mcp=None) and return a result dict."""
    workdir.mkdir(parents=True, exist_ok=True)
    model = build_model()
    env = LocalEnvironment(cwd=str(workdir), timeout=30)
    agent = CkbMcpAgent(
        model,
        env,
        mcp=mcp,
        system_template=SYSTEM_TEMPLATE,
        instance_template=INSTANCE_TEMPLATE,
        step_limit=10,
        cost_limit=0.0,
    )
    agent.extra_template_vars["mcp_tool_list"] = tool_list

    print(f"\n--- {name} arm (mcp={'client' if mcp else 'None'}) ---")
    try:
        result = agent.run(task)
        exit_status = result.get("exit_status")
    except Exception as e:  # the OFF arm may hit step_limit etc.; capture, don't crash
        exit_status = f"error:{type(e).__name__}"

    tip_file = workdir / "tip.txt"
    wrote = tip_file.exists()
    tip = tip_file.read_text().strip() if wrote else ""
    used_mcp = any(m.get("extra", {}).get("mcp_tool") for m in agent.messages)

    summary = [
        {
            "role": m.get("role"),
            "mcp_tool": m.get("extra", {}).get("mcp_tool"),
            "preview": (m.get("content") or "")[:60].replace("\n", " "),
        }
        for m in agent.messages
        if m.get("role") in ("assistant", "tool", "user", "exit")
    ]
    (workdir / "trajectory_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"  exit={exit_status} calls={agent.n_calls} used_mcp={used_mcp} wrote_tip={wrote}")
    return {
        "exit": exit_status,
        "used_mcp": used_mcp,
        "wrote": wrote,
        "tip": tip,
        "n_tools": len(agent.mcp_tools),
    }


if __name__ == "__main__":
    raise SystemExit(main())
