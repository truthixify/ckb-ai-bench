"""CkbMcpAgent: mini-swe-agent's DefaultAgent + native MCP tool calls.

This is the fork's one real addition. We do NOT touch mini-swe-agent's core files.
We subclass DefaultAgent and override `execute_actions` -- its designed extension
seam (default.py:152) -- to dispatch each action by kind:

  * a normal bash command  -> self.env.execute(action)   (unchanged mini-swe behavior)
  * an MCP tool call        -> self.mcp.call_tool(...)     (our addition)

So bash, file editing (via bash), and Docker all keep working exactly as upstream,
and the agent additionally gains the CKB AI MCP tools -- the thing the benchmark
puts under test. When `mcp` is None (the OFF arm), no MCP tools are exposed and MCP-shaped
commands keep upstream shell behavior. Controller-owned task sequencing applies equally to both arms.

Action convention (text-mode): a command whose first token is `mcp_call` is an MCP
tool call. Form:  mcp_call <tool_name> <json-args>
e.g.               mcp_call rpc_get_tip_block_number {}
                   mcp_call ckb_query_address {"address": "ckt1..."}

One tool name is reserved: `resources/read` retrieves a documentation resource body rather than
calling a tool. It is the only non-tool MCP method the model can reach, so the product's
documentation is available to MCP arms without exposing arbitrary JSON-RPC.

An on-chain Task attempt may also carry an attempt-bound signer broker. Its command is
`ckb_sign_and_submit <json-request>`. The broker, not the agent process, owns the key and enforces
the frozen input, output, transfer and fee policy before signing.

An optional `surface` policy narrows what the model may reach. When one is supplied, the same
object decides both the advertised tool list and every dispatch, so a tool can never be hidden from
the prompt while staying callable. Without one the agent keeps its full-catalog behavior for
harness-controlled and spike use.
"""

from __future__ import annotations

import json
import re

from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import InterruptAgentFlow, Submitted

from ckb_mcp import CkbMcpClient
from ckbbench.run.task_sequence import (
    SUBMISSION_COMMAND,
    TaskOrderViolation,
    TaskSequenceController,
)

MCP_ACTION_PREFIX = "mcp_call"
SIGNER_ACTION_PREFIX = "ckb_sign_and_submit"
# Reserved action name; anything else is dispatched as an ordinary MCP tool.
MCP_RESOURCE_ACTION = "resources/read"


class McpSetupError(RuntimeError):
    """The MCP handshake failed. Raised only from the initialize/list_tools boundary.

    Typed narrowly so a caller can classify a server or transport problem as an infrastructure
    failure without also swallowing ordinary programming errors from the rest of construction.
    """


class CkbMcpAgent(DefaultAgent):
    def __init__(
        self,
        model,
        env,
        *,
        mcp: CkbMcpClient | None = None,
        surface=None,
        signer=None,
        task_sequence: TaskSequenceController | None = None,
        **kwargs,
    ):
        super().__init__(model, env, **kwargs)
        self.mcp = mcp
        self.mcp_surface = surface
        self.signer = signer
        self.local_protocol_violation_count = 0
        self.task_sequence = task_sequence
        self.mcp_tools: list[dict] = []
        if self.mcp is not None:
            try:
                self.mcp.initialize()
                advertised = self.mcp.list_tools()
                if surface is not None and hasattr(surface, "validate_resources"):
                    surface.validate_resources(self.mcp.list_resources())
            except Exception as exc:
                # `from None`: the original transport text can carry a response body, an endpoint
                # or a token, and a formatted traceback renders a retained cause verbatim.
                raise McpSetupError(f"mcp handshake failed ({type(exc).__name__})") from None
            # Filtering here rather than at render time: a required tool that the server stopped
            # advertising must fail construction, before any model call is spent.
            if surface is None or getattr(self.mcp, "policy", None) is surface:
                self.mcp_tools = advertised
            else:
                self.mcp_tools = surface.filter_tools(advertised)

    # --- our addition: dispatch MCP actions, defer everything else to upstream env ---

    def _is_mcp_action(self, command: str) -> bool:
        stripped = command.strip()
        return self.mcp is not None and (
            stripped == MCP_ACTION_PREFIX
            or bool(re.match(rf"^{re.escape(MCP_ACTION_PREFIX)}\s", stripped))
        )

    def _is_signer_action(self, command: str) -> bool:
        stripped = command.strip()
        return stripped == SIGNER_ACTION_PREFIX or bool(
            re.match(rf"^{re.escape(SIGNER_ACTION_PREFIX)}\s", stripped)
        )

    def _run_signer_action(self, command: str) -> dict:
        if self.signer is None:
            self.local_protocol_violation_count += 1
            return self._refuse("no constrained signer is available for this attempt")
        raw_request = command.strip()[len(SIGNER_ACTION_PREFIX):].strip()
        try:
            request = json.loads(raw_request)
        except json.JSONDecodeError:
            self.local_protocol_violation_count += 1
            return self._refuse("signing request must be a JSON object")
        if not isinstance(request, dict):
            self.local_protocol_violation_count += 1
            return self._refuse("signing request must be a JSON object")
        try:
            result = self.signer.sign_and_submit(request)
        except Exception as exc:
            return {
                "output": f"signing request failed ({type(exc).__name__})",
                "returncode": 1,
                "exception_info": "",
            }
        if (
            not isinstance(result, dict)
            or set(result) != {"tx_hash"}
            or not isinstance(result["tx_hash"], str)
            or re.fullmatch(r"0x[0-9a-f]{64}", result["tx_hash"]) is None
        ):
            return {
                "output": "signing request returned malformed public evidence",
                "returncode": 1,
                "exception_info": "",
            }
        return {
            "output": json.dumps(result, sort_keys=True, separators=(",", ":")),
            "returncode": 0,
            "exception_info": "",
            "extra": {"signer_action": SIGNER_ACTION_PREFIX},
        }

    @property
    def protocol_violation_count(self) -> int:
        surface_count = getattr(self.mcp_surface, "violation_count", 0)
        signer_count = getattr(self.signer, "protocol_violation_count", 0)
        return self.local_protocol_violation_count + surface_count + signer_count

    def _run_mcp_action(self, command: str) -> dict:
        """Parse `mcp_call <tool> <json-args>` and call the tool. Returns a dict shaped
        like mini-swe-agent's env.execute output (including the always-present
        ``exception_info`` key) so the upstream observation templates render unchanged.

        Only the leading ``mcp_call`` keyword and the tool name are tokenized; the
        remainder of the line is passed to ``json.loads`` verbatim. This is deliberate:
        shlex-splitting the whole line would corrupt any JSON argument containing
        spaces or quotes (e.g. ``{"address": "ckt1..."}``)."""
        rest = command.strip()[len(MCP_ACTION_PREFIX):].strip()
        parts = rest.split(maxsplit=1)
        tool = parts[0] if parts else ""
        raw_args = parts[1] if len(parts) == 2 else ""
        if not tool:
            return {"output": "mcp_call requires a tool name", "returncode": 2, "exception_info": ""}
        raw_args = raw_args.strip() or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            return {"output": f"mcp_call args must be JSON: {e}", "returncode": 2, "exception_info": ""}
        if not isinstance(args, dict):
            return {"output": "mcp_call args must be a JSON object", "returncode": 2, "exception_info": ""}
        if tool == MCP_RESOURCE_ACTION:
            return self._run_mcp_resource_read(args)
        if self.mcp_surface is not None and not self.mcp_surface.allows_tool(tool):
            if hasattr(self.mcp_surface, "refuse"):
                self.mcp_surface.refuse()
            return self._refuse(f"mcp_call {tool} is not available on this MCP surface")
        try:
            result = self.mcp.call_tool(tool, args)
            text = CkbMcpClient.result_text(result)
        except Exception as e:  # network / protocol error -> surface as a failed observation
            return {
                "output": f"mcp_call {tool} failed ({type(e).__name__})",
                "returncode": 1,
                "exception_info": "",
            }
        return {
            "output": text,
            "returncode": 1 if result.get("isError") else 0,
            "exception_info": "",
            "extra": {"mcp_tool": tool},
        }

    def _run_mcp_resource_read(self, args: dict) -> dict:
        """Handle the reserved ``resources/read`` action. Arguments are validated locally so a
        malformed call never reaches the network."""
        if set(args) != {"uri"}:
            return {
                "output": f"{MCP_RESOURCE_ACTION} takes exactly one field: uri",
                "returncode": 2,
                "exception_info": "",
            }
        uri = args["uri"]
        if not isinstance(uri, str) or not uri.strip():
            return {
                "output": f"{MCP_RESOURCE_ACTION} uri must be a non-empty string",
                "returncode": 2,
                "exception_info": "",
            }
        if self.mcp_surface is not None and not self.mcp_surface.allows_resource(uri):
            if hasattr(self.mcp_surface, "refuse"):
                self.mcp_surface.refuse()
            return self._refuse(
                f"{MCP_RESOURCE_ACTION} is limited to "
                f"{self.mcp_surface.resource_prefix!r} resources on this MCP surface"
            )
        try:
            # Result extraction stays inside the boundary so malformed server data cannot escape
            # the observation contract.
            text = CkbMcpClient.resource_text(self.mcp.read_resource(uri))
        except Exception as e:
            return {
                "output": f"{MCP_RESOURCE_ACTION} failed ({type(e).__name__})",
                "returncode": 1,
                "exception_info": "",
            }
        if text is None:
            return {
                "output": f"{MCP_RESOURCE_ACTION} returned no readable text content",
                "returncode": 1,
                "exception_info": "",
            }
        return {
            "output": text,
            "returncode": 0,
            "exception_info": "",
            "extra": {"mcp_tool": MCP_RESOURCE_ACTION, "mcp_resource_uri": uri},
        }

    @staticmethod
    def _refuse(reason: str) -> dict:
        """A surface refusal is an ordinary failed observation, and reaches no client method.

        The rejected command and its arguments are deliberately not echoed beyond the tool or
        method name the model already knows it sent.
        """
        return {"output": reason, "returncode": 2, "exception_info": ""}

    def execute_actions(self, message: dict) -> list[dict]:
        """Same contract as DefaultAgent.execute_actions, but route MCP actions to the
        MCP client instead of the shell environment."""
        outputs = []
        released_in_message = False
        for action in message.get("extra", {}).get("actions", []):
            command = action.get("command", "")
            if released_in_message:
                outputs.append(
                    self._refuse(
                        "A new task was released. Read INSTRUCTIONS.md before issuing another command."
                    )
                )
                continue
            try:
                if self.task_sequence is not None:
                    self.task_sequence.before_action()
                    if command.strip() == SUBMISSION_COMMAND and not self.task_sequence.complete:
                        outputs.append(
                            self._refuse(
                                "Submission is unavailable until every task has been released."
                            )
                        )
                        continue
                try:
                    if self._is_signer_action(command):
                        output = self._run_signer_action(command)
                    elif self._is_mcp_action(command):
                        output = self._run_mcp_action(command)
                    else:
                        output = self.env.execute(action)
                except Submitted:
                    if self.task_sequence is None:
                        raise
                    update = self.task_sequence.after_action()
                    if update.complete:
                        raise
                    outputs.append(
                        self._refuse(
                            "Submission is unavailable until every task has been released. "
                            + update.message
                        )
                    )
                    released_in_message = update.advanced
                    continue

                if self.task_sequence is not None:
                    update = self.task_sequence.after_action()
                    if update.message:
                        output = dict(output)
                        prior = str(output.get("output", ""))
                        output["output"] = f"{prior.rstrip()}\n{update.message}".lstrip()
                    released_in_message = update.advanced
                outputs.append(output)
            except TaskOrderViolation:
                raise InterruptAgentFlow(
                    {
                        "role": "exit",
                        "content": "TaskOrderViolation",
                        "extra": {"exit_status": "TaskOrderViolation", "submission": ""},
                    }
                ) from None
        return self.add_messages(
            *self.model.format_observation_messages(message, outputs, self.get_template_vars())
        )
