"""Production agent factory: wires spike_model_loop.py into run_cell (ADR-0008).

Returns a closure that assembles CkbMcpAgent + LitellmModel + LocalEnvironment for one matrix
cell, with arm-aware system prompts so OFF arms (A/B) expose zero MCP surface and no-web arms
(A/D) receive the composed preamble from ArmConfig.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import os

from ckbbench.config import LLM_API_BASE, LLM_API_KEY, resolve_agent_image, rpc_url_for
from ckbbench.run.arm import ArmConfig
from ckbbench.run.defaults import internal_rpc_for, use_docker

# NOTE: minisweagent / ckb_agent / litellm live in the agent fork (agent/), which is on the path
# only at run time, not under the harness test runner (testpaths = ckbbench/containers; agent/ is
# the un-packaged fork). So those imports are LAZY, inside the closure and the default builder,
# matching orchestrate.py (which imports ckb_mcp/ckb_agent inside run_cell for the same reason).
# This keeps `import ckbbench` and these unit tests working with no agent fork on sys.path.

INSTANCE_TEMPLATE = """Task: {{task}}

Work in the current directory. Do the steps, then submit."""

_MCP_TOOL_LIST_NONE = "(none)"

# Agent-visible chain context (ADR-0007, plan §8.1). Every arm gets the same two names for one
# cell, so a no-MCP agent can reach the selected chain without guessing a docker service name.
CHAIN_PROFILE_ENV = "CKBBENCH_CHAIN_PROFILE"
CHAIN_RPC_ENV = "CKB_RPC_URL"

# max_tools=0 means expose every tool the MCP server offers; a cap is only for unusually large
# tool catalogs where the prompt would dominate context (not expected on the pinned server).
_DEFAULT_MAX_TOOLS = 0

# MCP arms (C/D) reach answers in fewer turns via mcp_call; no-MCP arms (A/B) need more shell
# RPC work. Pass step_limit explicitly to force one budget for every arm.
_DEFAULT_STEP_LIMIT_MCP = 40
_DEFAULT_STEP_LIMIT_NO_MCP = 80


def agent_rpc_url(chain: str) -> str:
    """The chain's RPC URL as reachable from the AGENT's namespace, not the harness host's.

    Only DevNet differs by namespace: a docker agent must address the sidecar by service name
    because the host's 127.0.0.1 reaches nothing inside ckbbench-net-internal. Every other case
    is the configured endpoint verbatim, so scheme, port, path, and query survive -- unlike
    ``internal_rpc_for``, which reduces a URL to a host for the allowlist and must not be reused
    as something an agent executes against. Unknown chains raise at the resolver rather than
    falling back to a default.
    """
    if use_docker() and chain == "devnet":
        return internal_rpc_for(chain)
    return rpc_url_for(chain)


def chain_env_for(chain: str) -> dict[str, str]:
    """Chain context injected into every arm's execution environment for one cell."""
    return {CHAIN_PROFILE_ENV: chain, CHAIN_RPC_ENV: agent_rpc_url(chain)}


def render_mcp_tool_list(tools: list[dict[str, Any]], *, max_tools: int = 0) -> str:
    """Compact bullet list for the system prompt (spike_model_loop.py pattern)."""
    if max_tools > 0:
        tools = tools[:max_tools]
    return "\n".join(
        f"- {t['name']} -- {t.get('description', '')[:80]}" for t in tools
    )


def build_system_template(*, mcp_enabled: bool) -> str:
    """Arm-aware system prompt: MCP vocabulary only when ``mcp_enabled`` is True."""
    lines = [
        "You are a CKB engineering agent working in a Linux shell in the current directory.",
        "",
        "Every turn you call the `bash` tool exactly once with a single command.",
    ]
    if mcp_enabled:
        lines.extend(
            [
                "",
                "Two kinds of commands exist:",
                "",
                "1. A normal shell command, e.g. `ls`, `cat file`, `echo hi > out.txt`.",
                "2. An MCP tool call to the CKB AI server. Form (as the bash command string):",
                "       mcp_call <tool_name> <json-args>",
                "   e.g.  mcp_call rpc_get_tip_block_number {}",
                "   The harness intercepts any command whose first word is `mcp_call` and runs the",
                "   MCP tool instead of the shell, returning the tool's text result as the output.",
                "",
                "Available MCP tools (name -- description):",
                "{{mcp_tool_list}}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Run one shell command per turn in the current working directory.",
            ]
        )
    lines.extend(
        [
            "",
            "{{arm_preamble}}",
            "",
            "When the task is fully done, call bash with EXACTLY:",
            "       echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
            "and nothing else. After that you cannot act further.",
        ]
    )
    return "\n".join(lines)


def _default_model_builder(model: str, api_base: str, api_key: str) -> Any:
    from minisweagent.models.litellm_model import LitellmModel  # lazy: agent fork only on run-time path

    return LitellmModel(
        model_name="openai/" + model,
        model_kwargs={
            "api_base": api_base,
            "api_key": api_key,
            "temperature": 0,
            "drop_params": True,
        },
        cost_tracking="ignore_errors",
    )


def make_agent_factory(
    *,
    api_base: str = LLM_API_BASE,
    # The local proxy needs no auth; LLM_API_KEY defaults to "sk-noauth" and is the single config
    # source of truth (config.py). It is not a secret: a configurable no-auth placeholder, never
    # committed. Threaded through so an operator retargets via config, not a code edit (codex).
    api_key: str = LLM_API_KEY,
    step_limit: int | None = None,
    step_limit_no_mcp: int = _DEFAULT_STEP_LIMIT_NO_MCP,
    cost_limit: float = 0.0,
    wall_time_limit_seconds: int = 900,
    command_timeout: int = 60,
    max_tools: int = _DEFAULT_MAX_TOOLS,
    model_builder: Callable[[str, str, str], Any] = _default_model_builder,
) -> Callable[..., Any]:
    """Returns a factory(mount_dir, pointer, arm_config, mcp_client, model, suite, chain)
    -> CkbMcpAgent."""

    def agent_factory(
        *,
        mount_dir: Path,
        pointer: str,
        arm_config: ArmConfig,
        mcp_client: Any | None,
        model: str,
        suite: Any,
        chain: str,
    ) -> Any:
        suite_digest = getattr(getattr(suite, "pins", None), "docker_image_digest", None)
        del pointer, suite  # run_cell passes them; pointer is the task at run() time

        # Lazy: the agent fork (LocalEnvironment, DockerEnvironment, CkbMcpAgent) is on sys.path
        # only at run time.
        from ckb_agent import CkbMcpAgent

        llm = model_builder(model, api_base, api_key)
        # Resolved from the CELL's chain, never from the suite default: --chains can override it.
        chain_env = chain_env_for(chain)
        if use_docker():
            from minisweagent.environments.docker import DockerEnvironment

            mount_str = str(mount_dir.resolve())
            # CKBBENCH_TESTNET_SENDER_PRIVKEY is forwarded from the host when set (see .env.example)
            # so the agent can sign send-tx on TestNet without MCP faucet tools.
            env = DockerEnvironment(
                image=resolve_agent_image(suite_digest=suite_digest),
                cwd=mount_str,
                # --rm so a stopped container is auto-removed; run_cell also calls env cleanup.
                run_args=[
                    "--rm",
                    "--network",
                    "ckbbench-net-internal",
                    "-v",
                    f"{mount_str}:{mount_str}",
                ],
                env={
                    **chain_env,
                    "HTTP_PROXY": "http://ckbbench-proxy:8888",
                    "HTTPS_PROXY": "http://ckbbench-proxy:8888",
                },
                forward_env=[
                    "CKBBENCH_TESTNET_SENDER_PRIVKEY",
                    "BENCH_TESTNET_SENDER_PRIVKEY",
                ],
                timeout=command_timeout,
            )
        else:
            from minisweagent.environments.local import LocalEnvironment

            # env= wins over the inherited host environment (LocalEnvironment merges
            # os.environ | config.env), so a stale host CKB_RPC_URL cannot outrank the cell.
            env = LocalEnvironment(
                cwd=str(mount_dir), env=chain_env, timeout=command_timeout
            )
        system_template = build_system_template(mcp_enabled=arm_config.mcp_enabled)

        # Construct the agent FIRST: CkbMcpAgent.__init__ already runs the MCP handshake
        # (initialize + list_tools) and stores the result on self.mcp_tools. Rendering the prompt
        # tool list from that, rather than calling mcp_client.list_tools() again here, avoids a
        # redundant round-trip and removes any initialize-before-list ordering assumption (codex).
        resolved_step_limit = (
            step_limit
            if step_limit is not None
            else (_DEFAULT_STEP_LIMIT_MCP if arm_config.mcp_enabled else step_limit_no_mcp)
        )
        agent = CkbMcpAgent(
            llm,
            env,
            mcp=mcp_client,
            system_template=system_template,
            instance_template=INSTANCE_TEMPLATE,
            step_limit=resolved_step_limit,
            cost_limit=cost_limit,
            wall_time_limit_seconds=wall_time_limit_seconds,
        )
        if arm_config.mcp_enabled:
            tool_list_text = render_mcp_tool_list(agent.mcp_tools, max_tools=max_tools)
        else:
            tool_list_text = _MCP_TOOL_LIST_NONE
        agent.extra_template_vars["arm_preamble"] = arm_config.prompt_preamble
        agent.extra_template_vars["mcp_tool_list"] = tool_list_text
        return agent

    return agent_factory
