"""Spike (Tier-2 #3): composed-prompt multi-task run (ADR-0008).

Proves the Suite delivery path end to end, beyond the single-task Tier-1 model loop:
  registry (task dirs)  ->  composed prompt (preamble + ordered fragments + postamble)
  written to the mount  ->  agent reads a thin POINTER  ->  works ALL tasks in ONE pass
  ->  writes N independent Proofs  ->  a verifier grades each Proof INDEPENDENTLY.

It also surfaces, honestly, the two things ADR-0008/0009 call out:
  - strict Task INDEPENDENCE (no fragment references another task's output),
  - per-task token/time attribution LOSS in a single composed pass (only a run TOTAL
    is available), which is why per-task metrics are deferred.

Run:
  cd spikes/composed-suite
  PYTHONPATH="$PWD/../../agent:$PWD" ../../agent/.venv/bin/python spike_composed_suite.py
Env overrides: BENCH_MODEL, BENCH_API_BASE, MCP_URL.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel

from ckb_mcp import CkbMcpClient
from ckb_agent import CkbMcpAgent

import compose as composer
from verify import verify_all, apply_provenance_gate

MODEL = os.getenv("BENCH_MODEL", "openai/grok-composer-2.5-fast")
API_BASE = os.getenv("BENCH_API_BASE", "http://localhost:18321/v1")
MCP_URL = os.getenv("MCP_URL", "https://mcp.ckbdev.com/ckbai")
REGISTRY = Path(__file__).parent / "registry"

SYSTEM_TEMPLATE = """You are a CKB engineering agent working in a Linux shell.

Every turn you call the `bash` tool exactly once with a single command. Two kinds of
commands exist:

1. A normal shell command, e.g. `ls`, `cat file`, `printf '%s' value > out.txt`.
2. An MCP tool call to the CKB AI server. Form (as the bash command string):
       mcp_call <tool_name> <json-args>
   e.g.  mcp_call rpc_get_tip_block_number {}
   The harness intercepts any command whose first word is `mcp_call` and runs the MCP
   tool instead of the shell, returning the tool's text result as the output.

Available MCP tools (name -- description):
{{mcp_tool_list}}

When every task is fully done and every Proof file is written, call bash with EXACTLY:
       echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
and nothing else. After that you cannot act further.
"""

INSTANCE_TEMPLATE = """{{task}}"""


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
    print(f"== composed-suite spike ==\nmodel: {MODEL} via {API_BASE}\nmcp: {MCP_URL}\n")

    mcp = CkbMcpClient(url=MCP_URL)
    info = mcp.initialize()
    ver = info.get("serverInfo", {}).get("version")
    tools = mcp.list_tools()
    # Expose the rpc_ tools the tasks need (a compact, relevant subset).
    keep = [t for t in tools if t["name"].startswith("rpc_")][:12]
    tool_list = "\n".join(f"- {t['name']} -- {t.get('description','')[:80]}" for t in keep)
    print(f"MCP v{ver}, {len(tools)} tools; exposing {len(keep)} rpc_ tools to the model")

    # 1. COMPOSE: registry -> composed prompt + per-task metas.
    composed, metas = composer.compose(REGISTRY)
    print(f"composed {len(metas)} tasks from the registry (manifest order)")

    # 2. MOUNT: write instructions file; inject only a pointer.
    mount = Path(tempfile.mkdtemp(prefix="ckb-composed-"))
    inst_path, digest = composer.write_instructions(composed, mount)
    pointer = composer.pointer_prompt(inst_path)
    print(f"mount: {mount}")
    print(f"instructions: {inst_path.name} (sha256 {digest[:16]}...)  pointer injected, not the wall of text")

    # 3. RUN: one agent, one pass, all tasks.
    model = build_model()
    env = LocalEnvironment(cwd=str(mount), timeout=40)
    agent = CkbMcpAgent(
        model, env, mcp=mcp,
        system_template=SYSTEM_TEMPLATE,
        instance_template=INSTANCE_TEMPLATE,
        step_limit=20,
        cost_limit=0.0,
    )
    agent.extra_template_vars["mcp_tool_list"] = tool_list

    t0 = time.monotonic()
    try:
        result = agent.run(pointer)
        exit_status = result.get("exit_status")
    except Exception as e:
        exit_status = f"error:{type(e).__name__}"
    elapsed = time.monotonic() - t0

    # The set of MCP tools the agent ACTUALLY invoked, from its trajectory. Each mcp_call is
    # recorded with extra.mcp_tool = <tool_name> (see CkbMcpAgent._run_mcp_action).
    tools_invoked = {m.get("extra", {}).get("mcp_tool") for m in agent.messages}
    tools_invoked.discard(None)
    used_mcp = bool(tools_invoked)
    print(f"\nagent exit={exit_status} calls={agent.n_calls} used_mcp={used_mcp} "
          f"tools_invoked={sorted(tools_invoked)} elapsed={elapsed:.1f}s")

    # 4. VERIFY: grade EACH proof independently, by direct RPC (never the MCP).
    #    A proof's value matching the chain is necessary but NOT sufficient (a cheating agent
    #    could fetch the value by direct curl, or hardcode a public constant like block 1's
    #    hash). So each task ALSO requires PROVENANCE: the agent must have invoked that task's
    #    specific rpc_ tool over MCP (the delivered mechanism), not just produced a value.
    #    This closes the adversarial "proof-without-work" finding (round 1, both grok models).
    print("\n=== independent per-task verification (value by direct RPC + MCP provenance) ===")
    verdicts = verify_all(metas, mount, MCP_URL)
    for v, m in zip(verdicts, metas):
        apply_provenance_gate(v, m, tools_invoked)
        print(f"  {v['id']:18s} proof={v['proof']!r:24s} -> {'PASS' if v['pass'] else 'FAIL'}  ({v['reason']})")

    n_pass = sum(1 for v in verdicts if v["pass"])
    total_score = sum(m["score"] for m, v in zip(metas, verdicts) if v["pass"])

    # 5. SURFACE the per-task attribution loss honestly: only a run TOTAL exists.
    attribution = {
        "per_task_tokens_available": False,
        "per_task_time_available": False,
        "run_total_calls": agent.n_calls,
        "run_total_elapsed_s": round(elapsed, 2),
        "reason": "single composed pass to one `done`; the loop emits no per-task complete signal, so tokens/time cannot be split per task (ADR-0009 deferred enhancement).",
    }
    (mount / "attribution.json").write_text(json.dumps(attribution, indent=2))

    # Independence assertion: no fragment references another task's proof file.
    proof_files = {m["proof_file"] for m in metas}
    independence_ok = True
    for m in metas:
        frag = (REGISTRY / m["id"] / "prompt.txt").read_text()
        others = proof_files - {m["proof_file"]}
        if any(o in frag for o in others):
            independence_ok = False

    print(f"\nscore: {total_score} ({n_pass}/{len(metas)} tasks pass)")
    print(f"independence: {'OK (no fragment names another task proof)' if independence_ok else 'VIOLATED'}")
    print(f"attribution: per-task tokens/time = NOT available (single composed pass); run total only -> {attribution['run_total_calls']} calls, {attribution['run_total_elapsed_s']}s")

    ok = (
        exit_status == "Submitted"
        and used_mcp
        and n_pass == len(metas)
        and independence_ok
    )
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} -- composed {len(metas)} independent tasks, agent worked all in one pass, each Proof graded independently")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
