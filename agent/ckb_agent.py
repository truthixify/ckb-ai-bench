"""CkbMcpAgent: mini-swe-agent's DefaultAgent + native MCP tool calls.

This is the fork's one real addition. We do NOT touch mini-swe-agent's core files.
We subclass DefaultAgent and override `execute_actions` -- its designed extension
seam (default.py:152) -- to dispatch each action by kind:

  * a normal bash command  -> self.env.execute(action)   (unchanged mini-swe behavior)
  * an MCP tool call        -> self.mcp.call_tool(...)     (our addition)

So bash, file editing (via bash), and Docker all keep working exactly as upstream,
and the agent additionally gains the CKB AI MCP tools -- the thing the benchmark
puts under test. When `mcp` is None (the OFF arm), no MCP tools are exposed and the
agent is byte-for-byte upstream behavior.

Action convention (text-mode): a command whose first token is `mcp_call` is an MCP
tool call. Form:  mcp_call <tool_name> <json-args>
e.g.               mcp_call rpc_get_tip_block_number {}
                   mcp_call ckb_query_address {"address": "ckt1..."}
"""

from __future__ import annotations

import json
import shlex

from minisweagent.agents.default import DefaultAgent

from ckb_mcp import CkbMcpClient

MCP_ACTION_PREFIX = "mcp_call"


class CkbMcpAgent(DefaultAgent):
    def __init__(self, model, env, *, mcp: CkbMcpClient | None = None, **kwargs):
        super().__init__(model, env, **kwargs)
        self.mcp = mcp
        self.mcp_tools: list[dict] = []
        if self.mcp is not None:
            self.mcp.initialize()
            self.mcp_tools = self.mcp.list_tools()

    # --- our addition: dispatch MCP actions, defer everything else to upstream env ---

    def _is_mcp_action(self, command: str) -> bool:
        return self.mcp is not None and command.strip().startswith(MCP_ACTION_PREFIX + " ")

    def _run_mcp_action(self, command: str) -> dict:
        """Parse `mcp_call <tool> <json-args>` and call the tool. Returns a dict shaped
        like mini-swe-agent's env.execute output so observation formatting is unchanged."""
        try:
            _, tool, *rest = shlex.split(command, posix=True)
        except ValueError as e:
            return {"output": f"mcp_call parse error: {e}", "returncode": 2}
        raw_args = " ".join(rest).strip() or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            return {"output": f"mcp_call args must be JSON: {e}", "returncode": 2}
        try:
            result = self.mcp.call_tool(tool, args)
        except Exception as e:  # network / protocol error -> surface as a failed observation
            return {"output": f"mcp_call {tool} failed: {e}", "returncode": 1}
        text = CkbMcpClient.result_text(result)
        return {
            "output": text,
            "returncode": 1 if result.get("isError") else 0,
            "extra": {"mcp_tool": tool},
        }

    def execute_actions(self, message: dict) -> list[dict]:
        """Same contract as DefaultAgent.execute_actions, but route MCP actions to the
        MCP client instead of the shell environment."""
        outputs = []
        for action in message.get("extra", {}).get("actions", []):
            command = action.get("command", "")
            if self._is_mcp_action(command):
                outputs.append(self._run_mcp_action(command))
            else:
                outputs.append(self.env.execute(action))
        return self.add_messages(
            *self.model.format_observation_messages(message, outputs, self.get_template_vars())
        )
