"""Run orchestrator: one matrix cell end to end (ADR-0008/0009/0010).

Promotes spikes/composed-suite/spike_composed_suite.py to production with injectable
seams so the full path is unit-testable without network, docker, MCP, or the LLM proxy.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ckbbench.ckb_rpc import RpcCallable, make_rpc_client
from ckbbench.config import MCP_PINNED_VERSION, MCP_URL, rpc_url_for
from ckbbench.run.arm import ArmConfig, resolve_arm
from ckbbench.run.metrics import RunMetrics, collect_metrics_from_agent
from ckbbench.run.preflight import (
    PreflightError,
    PreflightResult,
    preflight_mcp,
)
from ckbbench.run.result import (
    RESULT_SCHEMA_VERSION,
    RunOutcome,
    RunResult,
    task_outcomes_from_verdicts,
    write_result,
)
from ckbbench.suite.compose import compose, pointer_prompt, write_instructions
from ckbbench.suite.freeze import freeze
from ckbbench.suite.model import Suite, Task
from ckbbench.suite.runparams import (
    RunParams,
    generate_run_params,
    write_prompt_injected,
    write_verifier_private,
)
from ckbbench.verify.codetask import RunnerCallable
from ckbbench.verify.verifier import verify_suite

AGENT_DONE_EXIT = "Submitted"

DEFAULT_PROXY_URL = os.getenv("CKBBENCH_PROXY_URL", "http://ckbbench-proxy:8888")

McpClientFactory = Callable[[str], Any]
AgentFactory = Callable[..., Any]


@dataclass(frozen=True)
class VerifierNetworkConfig:
    """Chain-aware verifier reachability (Phase 3 deferred finding).

    DevNet verify runs on the internal no-NAT network against the co-located sidecar.
    TestNet verify must egress via the allowlisted proxy to reach the external archive node.
    """

    chain: str
    proxy_env: dict[str, str]


def verifier_network_config(
    chain: str,
    *,
    proxy_url: str = DEFAULT_PROXY_URL,
) -> VerifierNetworkConfig:
    """Return proxy env for testnet verify; devnet needs no egress path."""
    if chain == "testnet":
        return VerifierNetworkConfig(
            chain=chain,
            proxy_env={
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
            },
        )
    if chain == "devnet":
        return VerifierNetworkConfig(chain=chain, proxy_env={})
    raise ValueError(f"unknown chain profile {chain!r}")


@contextmanager
def _proxy_env_context(env: Mapping[str, str]) -> Iterator[None]:
    """Temporarily set process proxy env for urllib-based verify RPC."""
    if not env:
        yield
        return
    prior = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        yield
    finally:
        for key, old in prior.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _freeze_hash(registry_root: Path, suite: Suite) -> str:
    doc = freeze(suite, registry_root)
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _compose_for_arm(suite: Suite, arm_config: ArmConfig) -> str:
    """Assemble composed prompt with arm-specific preamble (RECOMMENDATION §6)."""
    body = compose(suite)
    preamble = arm_config.prompt_preamble.strip()
    if not preamble:
        return body
    marker = "Write each Proof file in the\ncurrent working directory."
    if marker in body:
        return body.replace(marker, f"{marker}\n\n{preamble}", 1)
    return preamble + "\n\n" + body


def _make_run_id(
    suite: Suite,
    chain: str,
    arm: str,
    model: str,
    seed: int,
    *,
    now_fn: Callable[[], float],
) -> str:
    ts = int(now_fn())
    safe_model = model.replace("/", "-")
    return f"{suite.suite_semver}-{chain}-{arm}-{safe_model}-s{seed}-{ts}"


def _classify_outcome(
    *,
    infra_failed: bool,
    agent_exit_status: str | None,
    all_tasks_passed: bool,
) -> RunOutcome:
    if infra_failed:
        return "infra_fail"
    if agent_exit_status != AGENT_DONE_EXIT or not all_tasks_passed:
        return "agent_fail"
    return "pass"


def _inject_harness_tip(
    params: RunParams,
    task: Task,
    harness_tip: int,
) -> RunParams:
    """Override per-task harness_tip with the single run-start capture (CONTEXT)."""
    if not any(s.name == "harness_tip" for s in task.param_schema):
        return params
    private = dict(params.verifier_private)
    private["harness_tip"] = harness_tip
    return RunParams(prompt_injected=params.prompt_injected, verifier_private=private)


def run_cell(
    suite: Suite,
    chain: str,
    arm: str,
    model: str,
    seed: int,
    *,
    registry_root: Path | str,
    results_dir: Path | str,
    mcp_url: str = MCP_URL,
    mcp_client_factory: McpClientFactory | None = None,
    agent_factory: AgentFactory | None = None,
    rpc: RpcCallable | None = None,
    runner: RunnerCallable | None = None,
    now_fn: Callable[[], float] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
    mount_dir: Path | str | None = None,
    verifier_private_root: Path | str | None = None,
) -> RunResult:
    """Run one matrix cell: preflight, compose, agent, verify, persist JSON artifact."""
    import time

    clock = now_fn or time.time
    mono = monotonic_fn or time.monotonic
    reg_root = Path(registry_root)
    arm_config = resolve_arm(arm)
    run_id = _make_run_id(suite, chain, arm, model, seed, now_fn=clock)
    freeze_hash = _freeze_hash(reg_root, suite)
    max_score = sum(t.score for t in suite.tasks)
    net_cfg = verifier_network_config(chain)

    mount = Path(mount_dir) if mount_dir is not None else reg_root / ".runs" / run_id / "mount"
    mount.mkdir(parents=True, exist_ok=True)
    vpriv_root = (
        Path(verifier_private_root)
        if verifier_private_root is not None
        else mount.parent / "verifier-private"
    )
    vpriv_root.mkdir(parents=True, exist_ok=True)

    rpc_url = rpc_url_for(chain)
    rpc_client = rpc if rpc is not None else make_rpc_client(rpc_url)

    preflight_version: str | None = None
    if arm_config.mcp_enabled:
        try:
            if mcp_client_factory is not None:
                mcp_for_preflight = mcp_client_factory(mcp_url)
                preflight = preflight_mcp(
                    mcp_url,
                    suite.mcp_server_version,
                    client=mcp_for_preflight,
                )
            else:
                preflight = preflight_mcp(mcp_url, suite.mcp_server_version)
            preflight_version = preflight.server_version
        except PreflightError:
            result = RunResult(
                schema_version=RESULT_SCHEMA_VERSION,
                suite_semver=suite.suite_semver,
                chain=chain,
                arm=arm,
                model=model,
                seed=seed,
                run_id=run_id,
                suite_freeze_hash=freeze_hash,
                mcp_server_version=suite.mcp_server_version,
                outcome="infra_fail",
                total_score=0,
                max_score=max_score,
                tasks=(),
                metrics=RunMetrics(total_wall_seconds=0.0, total_tokens=None),
                agent_exit_status=None,
                preflight_server_version=None,
            )
            write_result(result, results_dir)
            return result

    try:
        harness_tip = int(rpc_client("get_tip_block_number", []), 16)
    except Exception:
        result = RunResult(
            schema_version=RESULT_SCHEMA_VERSION,
            suite_semver=suite.suite_semver,
            chain=chain,
            arm=arm,
            model=model,
            seed=seed,
            run_id=run_id,
            suite_freeze_hash=freeze_hash,
            mcp_server_version=suite.mcp_server_version,
            outcome="infra_fail",
            total_score=0,
            max_score=max_score,
            tasks=(),
            metrics=RunMetrics(total_wall_seconds=0.0, total_tokens=None),
            agent_exit_status=None,
            preflight_server_version=preflight_version,
        )
        write_result(result, results_dir)
        return result

    verifier_private_by_task: dict[str, dict[str, Any]] = {}
    for task in suite.tasks:
        params = generate_run_params(task, rpc_url, rpc=rpc_client)
        params = _inject_harness_tip(params, task, harness_tip)
        write_prompt_injected(params, mount, filename=f"{task.id}.json")
        write_verifier_private(
            params,
            vpriv_root / task.id,
            filename="secret.json",
            mount_dir=mount,
        )
        verifier_private_by_task[task.id] = dict(params.verifier_private)

    composed = _compose_for_arm(suite, arm_config)
    inst_path, _digest = write_instructions(composed, mount)
    pointer = pointer_prompt(inst_path)

    mcp_client = None
    if arm_config.mcp_enabled:
        if mcp_client_factory is not None:
            mcp_client = mcp_client_factory(mcp_url)
        else:
            from ckb_mcp import CkbMcpClient

            mcp_client = CkbMcpClient(url=mcp_url)

    if agent_factory is None:
        raise ValueError("agent_factory is required for run_cell")

    agent = agent_factory(
        mount_dir=mount,
        pointer=pointer,
        arm_config=arm_config,
        mcp_client=mcp_client,
        model=model,
        suite=suite,
    )

    t0 = mono()
    try:
        agent_info = agent.run(pointer)
        agent_exit = agent_info.get("exit_status") if isinstance(agent_info, dict) else None
    except Exception:
        agent_exit = "error"
    wall_seconds = mono() - t0
    metrics = collect_metrics_from_agent(agent, wall_seconds=wall_seconds)

    verdicts = []
    with _proxy_env_context(net_cfg.proxy_env):
        verdicts = verify_suite(
            suite.tasks,
            mount,
            verifier_private_by_task,
            rpc_client,
            registry_root=reg_root,
            runner=runner,
        )

    task_rows = task_outcomes_from_verdicts(suite.tasks, verdicts)
    total_score = sum(t.score_awarded for t in task_rows)
    all_passed = bool(task_rows) and all(t.passed for t in task_rows)
    outcome = _classify_outcome(
        infra_failed=False,
        agent_exit_status=agent_exit,
        all_tasks_passed=all_passed,
    )

    result = RunResult(
        schema_version=RESULT_SCHEMA_VERSION,
        suite_semver=suite.suite_semver,
        chain=chain,
        arm=arm,
        model=model,
        seed=seed,
        run_id=run_id,
        suite_freeze_hash=freeze_hash,
        mcp_server_version=suite.mcp_server_version,
        outcome=outcome,
        total_score=total_score,
        max_score=max_score,
        tasks=task_rows,
        metrics=metrics,
        agent_exit_status=agent_exit,
        preflight_server_version=preflight_version,
    )
    write_result(result, results_dir)
    return result