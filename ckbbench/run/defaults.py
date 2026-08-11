"""Production run seams: docker runner + proxy violation check (ADR-0006)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from ckbbench.config import ARM_MATRIX, MCP_URL, TESTNET_RPC, rpc_url_for
from ckbbench.run.devnet import prepare_devnet
from ckbbench.run.proxy_log import make_violation_check
from ckbbench.run.runner import RunnerConfig, make_docker_runner
from ckbbench.suite.model import Suite

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - import-time path glue
    sys.path.insert(0, str(_REPO_ROOT))

from containers.build_allowlist import build_allowlist  # noqa: E402


def use_docker() -> bool:
    """Return True when CKBBENCH_DOCKER=1 selects the production docker path."""
    return os.getenv("CKBBENCH_DOCKER", "0") == "1"


def internal_rpc_for(chain: str) -> str:
    """Chain RPC URL as seen from the docker internal network (proxy/agent side)."""
    if chain == "devnet":
        return "http://ckbbench-devnet-node:8114"
    if chain == "testnet":
        parsed = urlparse(TESTNET_RPC if "://" in TESTNET_RPC else f"http://{TESTNET_RPC}")
        host = parsed.hostname
        if not host:
            raise ValueError(f"cannot parse host from TESTNET_RPC {TESTNET_RPC!r}")
        if parsed.port:
            return f"http://{host}:{parsed.port}"
        return f"http://{host}"
    raise ValueError(f"unknown chain profile {chain!r}")


def build_cell_allowlist(arm: str, chain: str) -> Path:
    """Write a per-cell allowlist file and return its path."""
    proxy_dir = _REPO_ROOT / "containers" / "proxy"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    fd, path_str = tempfile.mkstemp(
        prefix=f"allowlist.{arm}.{chain}.",
        suffix=".built",
        dir=str(proxy_dir),
    )
    os.close(fd)
    path = Path(path_str)

    mcp_enabled, _ = ARM_MATRIX[arm]
    content = build_allowlist(
        chain_rpc=internal_rpc_for(chain),
        mcp_url=MCP_URL if mcp_enabled else None,
        arm=arm,
    )
    path.write_text(content, encoding="utf-8")
    return path


def production_run_kwargs(
    *,
    arm: str,
    chain: str,
    suite: Suite | None = None,
    log_since: float | None = None,
) -> dict:
    """Return kwargs to pass to run_cell for a production docker run."""
    if not use_docker():
        return {}
    allowlist_path = build_cell_allowlist(arm, chain)
    runner_cfg = (
        RunnerConfig.for_suite(suite)
        if suite is not None
        else RunnerConfig()
    )
    kwargs = {
        "runner": make_docker_runner(config=runner_cfg),
        "violation_check": make_violation_check(
            arm=arm,
            chain=chain,
            allowlist_path=allowlist_path,
            log_since=log_since,
        ),
        # Per-cell allowlist + work volume cleaned after the cell (unless CKBBENCH_KEEP / keep).
        "cleanup_extra_paths": (allowlist_path,),
        "work_volume": runner_cfg.work_volume,
    }
    if chain == "devnet":
        # One fresh chain per Docker DevNet cell. TestNet is a live chain the harness does not own,
        # and a local run has no managed sidecar, so neither is reset here (plan §9.1).
        # The SELECTED endpoint is passed in: with CKBBENCH_DEVNET_RPC pointing elsewhere the
        # lifecycle would otherwise reset and attest the local sidecar while the harness graded a
        # different host -- a split-chain cell carrying local provenance. It is refused instead.
        kwargs["prepare_chain"] = lambda _chain: prepare_devnet(rpc_url=rpc_url_for("devnet"))
    return kwargs