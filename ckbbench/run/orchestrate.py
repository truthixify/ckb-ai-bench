"""Run orchestrator: one matrix cell end to end (ADR-0008/0009/0010).

Promotes spikes/composed-suite/spike_composed_suite.py to production with injectable
seams so the full path is unit-testable without network, docker, MCP, or the LLM proxy.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ckbbench.ckb_rpc import RpcCallable, make_rpc_client
from ckbbench.config import MCP_PINNED_VERSION, MCP_URL, rpc_url_for
from ckbbench.run.arm import ArmConfig, resolve_arm
from ckbbench.run.devnet import DevnetLifecycleError, DevnetState
from ckbbench.run.cleanup import (
    CellCleanupTargets,
    cleanup_cell,
    resolve_work_volume,
    stop_agent_checked,
)
from ckbbench.run.runner import PrepareError, prepare_work_volume
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
from ckbbench.suite.compose import chain_context_text, compose, pointer_prompt, write_instructions
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
# A violation check: given the arm and the run's mount, decide whether a no-research arm (A/D)
# touched a non-allowlisted host (the proxy egress log is the machine-observed signal, ADR-0006).
# Returns True if a protocol violation occurred. Injectable so the production reader (proxy-log
# parser) and a test fake share one seam. None means "no check wired" (e.g. research arms).
ViolationCheck = Callable[[str, Path], bool]


@dataclass(frozen=True)
class VerifierNetworkConfig:
    """Chain-aware verifier reachability.

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


def _compose_for_arm(suite: Suite, arm_config: ArmConfig, chain: str) -> str:
    """Assemble composed prompt with the arm-specific preamble in the composer's structural
    slot (RECOMMENDATION §6). The composer places it right after the base preamble and before
    the task list, so this is no longer a fragile string-splice by the orchestrator. The chain
    context is composed from the CELL's chain and is identical for all four arms (plan §8.1)."""
    return compose(
        suite,
        extra_preamble=arm_config.prompt_preamble,
        chain_context=chain_context_text(chain),
    )


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
    protocol_violated: bool,
    agent_exit_status: str | None,
    all_tasks_passed: bool,
) -> RunOutcome:
    """Run-level outcome (RECOMMENDATION §4).

    A no-research arm that touched the web is a protocol_violation, which downstream Pass@1 counts
    as 0 while publishing the violation rate separately. Infra failure dominates; then a violation;
    then agent correctness.
    """
    if infra_failed:
        return "infra_fail"
    if protocol_violated:
        return "protocol_violation"
    if agent_exit_status != AGENT_DONE_EXIT or not all_tasks_passed:
        return "agent_fail"
    return "pass"


def _make_tip_pinned_rpc(rpc_client: RpcCallable, harness_tip: int) -> RpcCallable:
    """Wrap an RPC client so get_tip_block_number returns the single run-start capture (no second
    network call), passing every other method through unchanged."""

    def tip_pinned(method: str, params: list[Any]) -> Any:
        if method == "get_tip_block_number":
            return hex(harness_tip)
        return rpc_client(method, params)

    return tip_pinned


def _agent_limits(agent: Any) -> dict[str, int | float | None]:
    """Audit-facing agent budgets persisted with each result artifact."""
    cfg = getattr(agent, "config", None)
    return {
        "step_limit": getattr(cfg, "step_limit", None),
        "cost_limit": getattr(cfg, "cost_limit", None),
        "wall_time_limit_seconds": getattr(cfg, "wall_time_limit_seconds", None),
    }


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


def _early_infra_result(
    *,
    suite: Suite,
    chain: str,
    arm: str,
    model: str,
    seed: int,
    run_id: str,
    freeze_hash: str,
    max_score: int,
    preflight_version: str | None,
    results_dir: Path | str,
    devnet_state: DevnetState | None = None,
) -> RunResult:
    """Build, persist, and return an infra_fail RunResult (no agent, no verify). One place so the
    preflight-fail and tip-fail early exits cannot drift apart as the schema evolves."""
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
        devnet_state=devnet_state,
    )
    write_result(result, results_dir)
    return result


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
    violation_check: ViolationCheck | None = None,
    now_fn: Callable[[], float] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
    mount_dir: Path | str | None = None,
    verifier_private_root: Path | str | None = None,
    keep: bool | None = None,
    cleanup_extra_paths: Sequence[Path | str] | None = None,
    work_volume: str | None = None,
    prepare_chain: Callable[[str], DevnetState | None] | None = None,
) -> RunResult:
    """Run one matrix cell: preflight, compose, agent, verify, persist JSON artifact.

    After the cell, ephemeral resources are deleted unless ``keep=True`` or
    ``CKBBENCH_KEEP=1``: agent container, docker work volume (when docker is on),
    harness-owned host run dir under ``ckbbench-runs/``, and any ``cleanup_extra_paths``
    (e.g. per-cell allowlist files).
    """
    import time

    clock = now_fn or time.time
    mono = monotonic_fn or time.monotonic
    reg_root = Path(registry_root)
    arm_config = resolve_arm(arm)
    run_id = _make_run_id(suite, chain, arm, model, seed, now_fn=clock)
    freeze_hash = _freeze_hash(reg_root, suite)
    # Only SCORED tasks count toward the denominator; PLACEHOLDER scaffolds (scored=False) load and
    # run but never inflate the headline (grok-build).
    max_score = sum(t.score for t in suite.tasks if t.scored)
    net_cfg = verifier_network_config(chain)

    # The agent mount must live OUTSIDE the registry tree: if it sat under reg_root, an agent could
    # read the hidden suite (and other tasks' verifier code) via a relative path like
    # ../../task-xx/hidden/ (grok-build). Default to an out-of-tree per-run dir; a caller-supplied
    # mount is guarded below.
    owned_host_run = mount_dir is None
    if mount_dir is not None:
        mount = Path(mount_dir)
    else:
        import tempfile

        mount = Path(tempfile.gettempdir()) / "ckbbench-runs" / run_id / "mount"
    # Refuse a mount inside the registry tree (the hidden suite would be reachable from the agent)
    # BEFORE creating it, so a rejected mount is never even made on disk.
    reg_resolved = reg_root.resolve()
    mount_resolved = mount.resolve()
    if reg_resolved == mount_resolved or reg_resolved in mount_resolved.parents:
        raise ValueError(
            f"agent mount {mount} must not be inside the registry tree {reg_root} "
            "(the hidden suite would be agent-readable); use an out-of-tree mount"
        )
    mount.mkdir(parents=True, exist_ok=True)
    vpriv_root = (
        Path(verifier_private_root)
        if verifier_private_root is not None
        else mount.parent / "verifier-private"
    )
    vpriv_root.mkdir(parents=True, exist_ok=True)

    host_run_dir = mount.parent if owned_host_run else None
    extra_paths = tuple(Path(p) for p in (cleanup_extra_paths or ()))
    resolved_work = resolve_work_volume(explicit=work_volume)
    agent: Any | None = None

    try:
        rpc_url = rpc_url_for(chain)
        rpc_client = rpc if rpc is not None else make_rpc_client(rpc_url)

        # Fresh chain state FIRST: before MCP preflight, the run-start tip, run params, the agent
        # factory and any model call, so a cell can never observe the previous cell's writes and a
        # reset failure is recorded as infra_fail without starting an agent (plan §9.1).
        chain_state: DevnetState | None = None
        if prepare_chain is not None:
            try:
                chain_state = prepare_chain(chain)
            except DevnetLifecycleError:
                return _early_infra_result(
                    suite=suite, chain=chain, arm=arm, model=model, seed=seed, run_id=run_id,
                    freeze_hash=freeze_hash, max_score=max_score, preflight_version=None,
                    results_dir=results_dir,
                )

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
                return _early_infra_result(
                    suite=suite, chain=chain, arm=arm, model=model, seed=seed, run_id=run_id,
                    freeze_hash=freeze_hash, max_score=max_score, preflight_version=None,
                    results_dir=results_dir, devnet_state=chain_state,
                )

        try:
            harness_tip = int(rpc_client("get_tip_block_number", []), 16)
        except Exception:
            return _early_infra_result(
                suite=suite, chain=chain, arm=arm, model=model, seed=seed, run_id=run_id,
                freeze_hash=freeze_hash, max_score=max_score, preflight_version=preflight_version,
                results_dir=results_dir, devnet_state=chain_state,
            )

        # Run-params draw through a tip-pinned RPC: the single run-start harness_tip is reused for
        # every task (CONTEXT: one Harness tip per run), so no task makes a second get_tip_block_number
        # call. _inject_harness_tip still overrides verifier-private as belt-and-suspenders.
        tip_pinned = _make_tip_pinned_rpc(rpc_client, harness_tip)

        verifier_private_by_task: dict[str, dict[str, Any]] = {}
        for task in suite.tasks:
            params = generate_run_params(task, rpc_url, rpc=tip_pinned)
            params = _inject_harness_tip(params, task, harness_tip)
            # A Code Task's hidden suite is graded with a fresh per-run BENCH_PASSWORD it never saw
            # (code-task FINDINGS / ADR-0009): a contract that hardcodes a guess fails. This secret is
            # generated here, kept verifier-private (never the mount), and consumed by grade_code_task.
            if task.kind == "code":
                params = RunParams(
                    prompt_injected=params.prompt_injected,
                    verifier_private={
                        **params.verifier_private,
                        "BENCH_PASSWORD": secrets.token_hex(16),
                    },
                )
            write_prompt_injected(params, mount, filename=f"{task.id}.json")
            write_verifier_private(
                params,
                vpriv_root / task.id,
                filename="secret.json",
                mount_dir=mount,
            )
            verifier_private_by_task[task.id] = dict(params.verifier_private)

        composed = _compose_for_arm(suite, arm_config, chain)
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
            chain=chain,
        )
        agent_limits = _agent_limits(agent)

        t0 = mono()
        try:
            agent_info = agent.run(pointer)
            agent_exit = agent_info.get("exit_status") if isinstance(agent_info, dict) else None
        except Exception:
            agent_exit = "error"
        wall_seconds = mono() - t0
        metrics = collect_metrics_from_agent(agent, wall_seconds=wall_seconds)

        # Before grade: checked agent stop + fresh work volume (no fail-open; PrepareError → infra).
        infra_failed = False
        try:
            stop_agent_checked(agent)
            if resolved_work is not None:
                prepare_work_volume(resolved_work)
        except PrepareError:
            infra_failed = True

        verdicts = []
        if not infra_failed:
            try:
                with _proxy_env_context(net_cfg.proxy_env):
                    verdicts = verify_suite(
                        suite.tasks,
                        mount,
                        verifier_private_by_task,
                        rpc_client,
                        registry_root=reg_root,
                        runner=runner,
                    )
            except PrepareError:
                infra_failed = True

        task_rows = task_outcomes_from_verdicts(suite.tasks, verdicts)
        # Only SCORED tasks contribute to total/max and gate the outcome; PLACEHOLDER scaffolds never
        # do (they already award 0, but sum over scored_rows so the headline can never leak a scaffold).
        scored_rows = [t for t in task_rows if t.scored]
        total_score = sum(t.score_awarded for t in scored_rows)
        all_passed = bool(scored_rows) and all(t.passed for t in scored_rows)

        # A no-research arm (A/D) that touched the web is a protocol_violation. The proxy egress log
        # is the machine-observed signal (ADR-0006); violation_check is the injectable reader. Only
        # the no-research arms are checked (research arms cannot violate a no-research rule).
        protocol_violated = False
        if violation_check is not None and arm_config.egress_mode == "block":
            protocol_violated = bool(violation_check(arm, mount))

        outcome = _classify_outcome(
            infra_failed=infra_failed,
            protocol_violated=protocol_violated,
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
            agent_limits=agent_limits,
            agent_exit_status=agent_exit,
            preflight_server_version=preflight_version,
            devnet_state=chain_state,
        )
        write_result(result, results_dir)
        return result
    finally:
        cleanup_cell(
            CellCleanupTargets(
                agent=agent,
                work_volume=resolved_work,
                host_run_dir=host_run_dir,
                extra_paths=extra_paths,
            ),
            keep=keep,
        )
