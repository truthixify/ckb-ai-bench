"""Spike: prove the forked agent can use the live CKB AI MCP server end-to-end.

This does NOT call an LLM -- the spike validates the MCP plumbing and the fork's
action-dispatch seam, which is the part that was uncertain. It:

  1. initializes the native MCP client against the live server,
  2. lists the real tools,
  3. calls a real tool (live testnet round-trip),
  4. drives the actual CkbMcpAgent.execute_actions() path with a simulated model
     message containing an `mcp_call` action -- proving the fork routes MCP calls
     correctly through mini-swe-agent's real observation-formatting flow.

Run:  python agent/spike_mcp.py [endpoint_url]
"""

from __future__ import annotations

import sys

from ckb_mcp import CkbMcpClient
from ckb_agent import CkbMcpAgent

DEFAULT_URL = "https://mcp.ckbdev.com/ckbai"


# --- minimal fakes so we can exercise the real agent without an LLM or Docker ---

class _FakeEnv:
    """Stands in for a mini-swe-agent Environment. The spike never hits bash, so this
    only needs to exist; a real run uses LocalEnvironment / DockerEnvironment."""

    def execute(self, action):  # pragma: no cover - not exercised in the spike
        return {"output": f"(bash) {action.get('command','')}", "returncode": 0}

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {}


class _FakeModel:
    """Stands in for a mini-swe-agent Model. We only need format_observation_messages
    (used by execute_actions) and a couple of no-op hooks."""

    def format_observation_messages(self, message, outputs, template_vars):
        msgs = []
        for out in outputs:
            msgs.append(
                {
                    "role": "user",
                    "content": out.get("output", ""),
                    "extra": {"returncode": out.get("returncode"), **out.get("extra", {})},
                }
            )
        return msgs

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {}


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"== CKB AI MCP fork spike ==\nendpoint: {url}\n")

    # 1. initialize
    client = CkbMcpClient(url=url)
    info = client.initialize()
    si = info.get("serverInfo", {})
    print(f"[1] initialize OK -> {si.get('name')} v{si.get('version')} (protocol {info.get('protocolVersion')})")

    # 2. list tools
    tools = client.list_tools()
    print(f"[2] tools/list OK -> {len(tools)} tools; first 6: {[t['name'] for t in tools[:6]]}")

    # 3. call a real tool (live round-trip)
    res = client.call_tool("rpc_get_tip_block_number", {})
    tip_hex = CkbMcpClient.result_text(res).strip().strip('"')
    print(f"[3] tools/call rpc_get_tip_block_number OK -> tip={tip_hex} ({int(tip_hex, 16)}) isError={res.get('isError')}")

    # 4. drive the FORK's real dispatch path with a simulated model action.
    #    system/instance templates are required by AgentConfig but unused here (no LLM call).
    _stub_cfg = {"system_template": "x", "instance_template": "x"}
    agent = CkbMcpAgent(_FakeModel(), _FakeEnv(), mcp=client, **_stub_cfg)
    print(f"[4] CkbMcpAgent built; {len(agent.mcp_tools)} MCP tools exposed to the model")
    simulated_model_message = {
        "extra": {"actions": [{"command": 'mcp_call ckb_query_chain_status {}'}]}
    }
    obs = agent.execute_actions(simulated_model_message)
    body = obs[0]["content"]
    rc = obs[0]["extra"].get("returncode")
    ok = rc == 0 and len(body) > 0
    print(f"    -> agent routed mcp_call through execute_actions; returncode={rc}, obs_len={len(body)}")
    print(f"    -> observation preview: {body[:160].replace(chr(10), ' ')}...")

    # sanity: OFF arm exposes no MCP tools and does not treat mcp_call specially
    off = CkbMcpAgent(_FakeModel(), _FakeEnv(), mcp=None, **_stub_cfg)
    off_is_mcp = off._is_mcp_action("mcp_call rpc_get_tip_block_number {}")
    print(f"[5] OFF arm (mcp=None): tools={len(off.mcp_tools)}, treats mcp_call as MCP? {off_is_mcp} (want: 0, False)")

    all_ok = ok and len(tools) > 0 and not off_is_mcp and len(off.mcp_tools) == 0
    print(f"\nRESULT: {'PASS - fork uses live MCP end-to-end' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
