#!/usr/bin/env python3
"""Generate per-arm compose overrides (ADR-0006 allowlist wiring).

Writes allowlist.built and a small .env file for docker compose variable substitution.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ckbbench.config import MCP_URL, rpc_url_for

_CONTAINERS = Path(__file__).resolve().parent
# build_allowlist is a sibling script (not part of the ckbbench package); make it importable
# regardless of the caller's cwd (running `python3 containers/compose_builder.py` from the repo
# root would otherwise fail to find it).
if str(_CONTAINERS) not in sys.path:  # pragma: no cover - import-time path glue
    sys.path.insert(0, str(_CONTAINERS))

from build_allowlist import build_allowlist  # noqa: E402  (path set up just above)


def compose_env_for_arm(
    *,
    arm: str,
    chain: str,
    mcp_url: str | None = None,
    proxy_host: str = "ckbbench-proxy",
) -> tuple[str, Path]:
    """Build allowlist + return (.env contents, allowlist path)."""
    chain_rpc = rpc_url_for(chain)
    # Inside docker network, devnet RPC is the sidecar service name.
    if chain == "devnet":
        chain_rpc = "http://ckbbench-devnet-node:8114"

    allowlist_path = _CONTAINERS / "proxy" / f"allowlist.{arm}.{chain}.built"
    content = build_allowlist(
        chain_rpc=chain_rpc,
        proxy_host=proxy_host,
        mcp_url=mcp_url or MCP_URL,
        arm=arm,
    )
    allowlist_path.write_text(content, encoding="utf-8")

    env = f"CKBBENCH_ALLOWLIST_FILE={allowlist_path}\n"
    return env, allowlist_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate per-arm compose env for ckbbench topology.")
    parser.add_argument("--arm", required=True)
    parser.add_argument("--chain", required=True, choices=("devnet", "testnet"))
    parser.add_argument("--mcp-url", default=None)
    parser.add_argument("-o", "--output-env", type=Path, default=_CONTAINERS / ".env.arm")
    args = parser.parse_args()

    env, allowlist = compose_env_for_arm(arm=args.arm, chain=args.chain, mcp_url=args.mcp_url)
    args.output_env.write_text(env, encoding="utf-8")
    print(f"allowlist: {allowlist}")
    print(f"env: {args.output_env}")


if __name__ == "__main__":
    main()