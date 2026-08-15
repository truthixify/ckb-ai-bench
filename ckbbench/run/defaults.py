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


def _effective_mcp_url(mcp_url: str | None) -> str:
    """One endpoint for the whole cell. Only ``None`` means "no override": an explicit empty or
    unparseable value must fail rather than silently fall back to the module default, which would
    let the agent and B's checker describe different hosts."""
    resolved = MCP_URL if mcp_url is None else mcp_url
    if not urlparse(resolved).hostname:
        raise ValueError(f"unusable MCP endpoint {resolved!r}")
    return resolved


def build_cell_allowlist(
    arm: str, chain: str, mcp_url: str | None = None, proxy_dir: Path | None = None
) -> Path:
    """Write a per-cell allowlist file and return its path.

    ``proxy_dir`` overrides where the file lands. Production keeps writing beside the proxy config
    (the compose stack bind-mounts it from there); callers that must not touch the repository —
    tests, concurrent processes — pass their own directory.
    """
    mcp_url = _effective_mcp_url(mcp_url)
    proxy_dir = proxy_dir if proxy_dir is not None else _REPO_ROOT / "containers" / "proxy"
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
        mcp_url=mcp_url if mcp_enabled else None,
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
    mcp_url: str | None = None,
) -> dict:
    """Return kwargs to pass to run_cell for a production docker run."""
    mcp_url = _effective_mcp_url(mcp_url)
    if not use_docker():
        return {}
    # One effective endpoint for the whole cell: the agent's client, B's checker, and D's allowlist
    # must all describe the same host, or a B connection to the real product could score clean.
    allowlist_path = build_cell_allowlist(arm, chain, mcp_url)
    runner_cfg = (
        RunnerConfig.for_suite(suite)
        if suite is not None
        else RunnerConfig()
    )
    kwargs = {
        "runner": make_docker_runner(config=runner_cfg),
        # The resolved endpoint is threaded explicitly so B's product-host policy follows whatever
        # this run targets rather than a module-level literal.
        "violation_check": make_violation_check(
            arm=arm,
            chain=chain,
            allowlist_path=allowlist_path,
            log_since=log_since,
            mcp_url=mcp_url,
        ),
        # Per-cell allowlist + work volume cleaned after the cell (unless CKBBENCH_KEEP / keep).
        "mcp_url": mcp_url,
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