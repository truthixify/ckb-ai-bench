"""Production agent factory: wires spike_model_loop.py into run_cell (ADR-0008).

Returns a closure that assembles CkbMcpAgent + LitellmModel + LocalEnvironment for one matrix
cell, with arm-aware system prompts so OFF arms (A/B) expose zero MCP surface and no-web arms
(A/D) receive the composed preamble from ArmConfig. MCP arms are constructed with the arm's fixed
surface policy (ADR-0013), which governs both the advertised tool list and every dispatch.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import os

from ckbbench.config import (
    CHAIN_PROFILES,
    DEVNET_GENESIS_PRIVKEY,
    LLM_API_BASE,
    LLM_API_KEY,
    resolve_agent_image,
    resolve_agent_network,
    rpc_url_for,
)
from ckbbench.run.arm import ArmConfig
from ckbbench.run.defaults import internal_rpc_for, use_docker
from ckbbench.run.cleanup import cleanup_agent
from ckbbench.run.mcp_surface import McpSurfaceError, McpSurfaceSetupError, policy_for_arm
from ckbbench.run.model_profile import API_STYLE, ModelProfile, ModelProfileError

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
# Signing material for the cell's chain. The value is never rendered into a prompt or a result.
SENDER_PRIVKEY_ENV = "CKB_SENDER_PRIVKEY"
TESTNET_SIGNER_ENV = ("CKBBENCH_TESTNET_SENDER_PRIVKEY", "BENCH_TESTNET_SENDER_PRIVKEY")
# Every signer name the harness knows about, so a cell can blank the ones it must not carry.
SIGNER_ENV_NAMES = (SENDER_PRIVKEY_ENV, *TESTNET_SIGNER_ENV)
# Where the agent image keeps the pinned offline transaction SDK (containers/agent.Dockerfile).
SDK_HOME_ENV = "CKB_SDK_HOME"
SDK_HOME_PATH = "/opt/ckbbench-node"

# max_tools=0 means expose every tool the MCP server offers; a cap is only for unusually large
# tool catalogs where the prompt would dominate context (not expected on the pinned server).
_DEFAULT_MAX_TOOLS = 0

# One budget for every arm (RD2). An arm-dependent step ceiling would make the headline C-B
# difference attributable to the budget as much as to CKB AI availability.
DEFAULT_STEP_LIMIT = 80
DEFAULT_COST_LIMIT = 0.0
DEFAULT_WALL_TIME_LIMIT_SECONDS = 900

# Only parent-supervised diagnostics receive these submounts. Cargo uses target/ by default, while
# the frozen hashlock task deliberately writes its proof beneath build/. Both can contain internal
# hard links that the host scrub must refuse because it cannot exclude an alias outside its tree.
_DIAGNOSTIC_ANONYMOUS_WORKSPACE_DIRS = ("target", "build")


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


def signer_env_for(chain: str) -> dict[str, str]:
    """Signing material the agent may use on this cell's chain, keyed on the CELL's chain.

    DevNet resolves to the public dev.toml genesis fixture, identically for A/B/C/D, so the
    send task is equally reachable in every arm. TestNet keeps its existing contract: the
    operator's key is forwarded from the host only on a TestNet cell (see
    ``testnet_forward_env``), never injected here and never offered to DevNet. Returning it
    explicitly -- rather than forwarding whatever the host exports -- is what stops a stale
    host value from becoming the signer.
    """
    if chain == "devnet":
        return {SENDER_PRIVKEY_ENV: DEVNET_GENESIS_PRIVKEY}
    if chain in CHAIN_PROFILES:
        return {}
    raise ValueError(f"unknown chain profile {chain!r}; expected one of {CHAIN_PROFILES}")


def testnet_forward_env(chain: str) -> list[str]:
    """Host variables forwarded into the container, only where they belong.

    Forwarding the TestNet signer unconditionally handed a live-chain key to every DevNet cell
    for no benefit; it is now scoped to the chain that can actually use it.
    """
    if chain == "testnet":
        return list(TESTNET_SIGNER_ENV)
    return []


def local_signer_sanitizer(chain: str) -> dict[str, str]:
    """Signer names to blank out for a LOCAL cell.

    A docker agent inherits nothing from the host, so scoping ``forward_env`` is enough there. A
    local agent executes with ``os.environ | config.env``, so an operator's exported TestNet key
    is readable from a DevNet cell -- and a stale generic key is readable from a TestNet cell --
    unless the cell overrides those names. Blanking is the available override: the merge cannot
    delete a key, but an empty value carries nothing.
    """
    keeps = set(signer_env_for(chain)) | set(testnet_forward_env(chain))
    return {name: "" for name in SIGNER_ENV_NAMES if name not in keeps}


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
                "Three kinds of commands exist:",
                "",
                "1. A normal shell command, e.g. `ls`, `cat file`, `echo hi > out.txt`.",
                "2. An MCP documentation search. Form (as the bash command string):",
                "       mcp_call <tool_name> <json-args>",
                '   e.g.  mcp_call search_resources {"query": "type id"}',
                "   The harness intercepts any command whose first word is `mcp_call` and runs the",
                "   MCP tool instead of the shell, returning the tool's text result as the output.",
                "3. An MCP documentation read, using the reserved action name:",
                '       mcp_call resources/read {"uri": "<resource-uri>"}',
                "   Use the `search_resources` tool to discover a resource URI first, then read it",
                "   with the action above. It returns the resource's text body.",
                "",
                "The MCP server is for CKB documentation and reference lookup only. Read live chain",
                "state, sign, submit transactions and confirm them through the endpoint in",
                "CKB_RPC_URL, never through mcp_call.",
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


def _profile_model_builder(profile: ModelProfile, api_key: str) -> Any:
    """The accepted phase-one model: reviewed settings, ledger, and bounded attempt policy.

    Always the Responses model. The profile's `api_style` is validated to exactly one value, so a
    reviewed profile cannot select the chat contract the probe no longer proves (ADR-0014).
    """
    from ckb_model import CkbLitellmResponseModel  # lazy: agent fork only on run-time path

    if profile.api_style != API_STYLE:
        raise ModelProfileError(
            f"the accepted phase-one path speaks {API_STYLE}; {profile.profile_id} names another"
        )
    # The key is passed separately, not through model_kwargs: the agent renders and serializes its
    # config, so a credential placed there would reach the trajectory and every diagnostic.
    return CkbLitellmResponseModel(
        model_name=profile.litellm_model_name,
        model_kwargs=profile.model_kwargs(),
        max_query_attempts=profile.max_agent_query_attempts,
        api_key=api_key,
        cost_tracking="ignore_errors",
    )


def _reject_conflicting_api_base(profile: ModelProfile) -> None:
    """An exported endpoint must not silently retarget a reviewed profile.

    Reading it and moving on would let two rows claim one profile while talking to different hosts,
    which is exactly the provenance the profile exists to fix.
    """
    for name in ("CKBBENCH_LLM_API_BASE", "BENCH_API_BASE"):
        exported = os.environ.get(name)
        if exported and exported.rstrip("/") != profile.api_base:
            raise ModelProfileError(
                f"{name} is exported and differs from the {profile.profile_id} api_base; "
                "unset it or run a profile whose endpoint it matches"
            )


def make_agent_factory(
    *,
    api_base: str = LLM_API_BASE,
    # The local proxy needs no auth; LLM_API_KEY defaults to "sk-noauth" and is the single config
    # source of truth (config.py). It is not a secret: a configurable no-auth placeholder, never
    # committed. Threaded through so an operator retargets via config, not a code edit (codex).
    api_key: str = LLM_API_KEY,
    profile: ModelProfile | None = None,
    step_limit: int = DEFAULT_STEP_LIMIT,
    cost_limit: float = DEFAULT_COST_LIMIT,
    wall_time_limit_seconds: int = DEFAULT_WALL_TIME_LIMIT_SECONDS,
    command_timeout: int = 60,
    max_tools: int = _DEFAULT_MAX_TOOLS,
    model_builder: Callable[[str, str, str], Any] = _default_model_builder,
    container_name: str = "",
    container_labels: tuple[str, ...] = (),
    auto_cleanup: bool = True,
) -> Callable[..., Any]:
    """Returns a factory(mount_dir, pointer, arm_config, mcp_client, model, suite, chain)
    -> CkbMcpAgent.

    With a reviewed ``profile`` this is the accepted phase-one path: every arm gets the same
    endpoint, model, temperature and retry policy, and the agent carries a usage ledger. Without
    one it keeps the development behavior, which cannot produce an accepted phase-one artifact.
    """
    if profile is not None:
        _reject_conflicting_api_base(profile)

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
        agent_pin = getattr(getattr(suite, "pins", None), "agent_image_digest", None)
        del pointer, suite  # run_cell passes them; pointer is the task at run() time

        # Lazy: the agent fork (LocalEnvironment, DockerEnvironment, CkbMcpAgent) is on sys.path
        # only at run time.
        from ckb_agent import CkbMcpAgent, McpSetupError

        if profile is not None and model != profile.requested_model:
            raise ModelProfileError(
                f"this factory is bound to {profile.profile_id}; a cell cannot request "
                f"a different model"
            )
        llm = (
            model_builder(model, api_base, api_key)
            if profile is None
            else _profile_model_builder(profile, api_key)
        )
        # Resolved from the CELL's chain, never from the suite default: --chains can override it.
        cell_env = {**chain_env_for(chain), **signer_env_for(chain)}
        if use_docker():
            from minisweagent.environments.docker import DockerEnvironment

            mount_str = str(mount_dir.resolve())
            # forward_env is chain-scoped: the operator's TestNet key reaches a TestNet cell and
            # nothing else. env= takes precedence over forward_env, so the DevNet fixture wins.
            # A parent-supervised diagnostic owns cleanup itself: it must be able to remove the
            # exact container after killing this process, which `--rm` and self-cleanup would
            # race. Ordinary runs keep both.
            run_args = [] if not auto_cleanup else ["--rm"]
            # Keep every declared Cargo output root outside the host scrub tree. One `docker rm -v`
            # through the proved agent ID disposes all of these anonymous volumes.
            diagnostic_build_mounts = (
                [
                    item
                    for directory in _DIAGNOSTIC_ANONYMOUS_WORKSPACE_DIRS
                    for item in (
                        "--mount",
                        f"type=volume,destination={mount_str}/{directory},volume-nocopy",
                    )
                ]
                if not auto_cleanup else []
            )
            env = DockerEnvironment(
                image=resolve_agent_image(agent_pin=agent_pin),
                cwd=mount_str,
                container_name=container_name,
                labels=list(container_labels),
                auto_cleanup=auto_cleanup,
                run_args=[
                    *run_args,
                    "--network",
                    # Same call-time resolver the runner uses: hardcoding the fixed name here
                    # attaches the agent to a network validation never created or proved.
                    resolve_agent_network(),
                    "-v",
                    f"{mount_str}:{mount_str}",
                    *diagnostic_build_mounts,
                ],
                env={
                    **cell_env,
                    SDK_HOME_ENV: SDK_HOME_PATH,
                    "HTTP_PROXY": "http://ckbbench-proxy:8888",
                    "HTTPS_PROXY": "http://ckbbench-proxy:8888",
                },
                forward_env=testnet_forward_env(chain),
                timeout=command_timeout,
            )
        else:
            from minisweagent.environments.local import LocalEnvironment

            # env= wins over the inherited host environment (LocalEnvironment merges
            # os.environ | config.env), so a stale host CKB_RPC_URL or CKB_SENDER_PRIVKEY
            # cannot outrank the cell, and the sanitizer blanks the signer names this chain
            # must not carry. The SDK path is an image contract, so it is docker-only.
            env = LocalEnvironment(
                cwd=str(mount_dir),
                env={**local_signer_sanitizer(chain), **cell_env},
                timeout=command_timeout,
            )
        system_template = build_system_template(mcp_enabled=arm_config.mcp_enabled)
        # Resolved from the ladder, with no injection seam: a caller-supplied policy would let a
        # widened treatment be recorded under a canonical profile name.
        surface = policy_for_arm(arm_config.arm)

        # Construct the agent FIRST: CkbMcpAgent.__init__ already runs the MCP handshake
        # (initialize + list_tools) and stores the result on self.mcp_tools. Rendering the prompt
        # tool list from that, rather than calling mcp_client.list_tools() again here, avoids a
        # redundant round-trip and removes any initialize-before-list ordering assumption (codex).
        try:
            agent = CkbMcpAgent(
                llm,
                env,
                mcp=mcp_client,
                surface=surface,
                system_template=system_template,
                instance_template=INSTANCE_TEMPLATE,
                step_limit=step_limit,
                cost_limit=cost_limit,
                wall_time_limit_seconds=wall_time_limit_seconds,
            )
        except (McpSetupError, McpSurfaceError) as exc:
            # The environment is already running by now, and no agent reaches run_cell's cleanup,
            # so release it here rather than leaving it to a destructor's best-effort stop.
            cleanup_agent(SimpleNamespace(env=env))
            raise McpSurfaceSetupError(str(exc)) from None
        if arm_config.mcp_enabled:
            tool_list_text = render_mcp_tool_list(agent.mcp_tools, max_tools=max_tools)
        else:
            tool_list_text = _MCP_TOOL_LIST_NONE
        agent.extra_template_vars["arm_preamble"] = arm_config.prompt_preamble
        agent.extra_template_vars["mcp_tool_list"] = tool_list_text
        # Read back by run_cell, which requires it: the result must record the surface the agent
        # actually carries, never one derived from the arm after the fact.
        agent.mcp_surface_profile = surface.profile
        agent.model_profile = profile
        return agent

    return agent_factory
