#!/usr/bin/env python3
"""Build per-arm/per-chain tinyproxy allowlists (ADR-0006).

Phase 4 orchestrator calls this at run time. Phase 3 ships the template + builder.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlparse

from ckbbench.config import ARM_MATRIX, EGRESS_MODE_BY_ARM

_TEMPLATE = Path(__file__).resolve().parent / "proxy" / "allowlist.template"
_OBSERVE = Path(__file__).resolve().parent / "proxy" / "allowlist.observe"


def _host_from_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"cannot parse host from {url!r}")
    return host


def _regex_host(host: str) -> str:
    return re.escape(host)


def build_allowlist(
    *,
    chain_rpc: str,
    proxy_host: str = "ckbbench-proxy",
    mcp_url: str | None = None,
    arm: str,
) -> str:
    """Return allowlist file contents for the given arm and chain RPC URL."""
    if arm not in EGRESS_MODE_BY_ARM:
        raise ValueError(f"unknown arm {arm!r}")

    mode = EGRESS_MODE_BY_ARM[arm]
    if mode == "observe":
        return _OBSERVE.read_text(encoding="utf-8")

    chain_host = _regex_host(_host_from_url(chain_rpc))
    proxy_line = f"^{_regex_host(proxy_host)}$"
    mcp_enabled, _ = ARM_MATRIX[arm]
    mcp_line = ""
    if mcp_enabled:
        if not mcp_url:
            raise ValueError(f"arm {arm} requires MCP URL for block-mode allowlist")
        mcp_line = f"^{_regex_host(_host_from_url(mcp_url))}$"

    template = _TEMPLATE.read_text(encoding="utf-8")
    return (
        template.replace("^{{CHAIN_RPC_HOST}}$", f"^{chain_host}$")
        .replace("^{{PROXY_HOST}}$", proxy_line)
        .replace("{{MCP_LINE}}", mcp_line)
        .strip()
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build tinyproxy allowlist for an arm/chain pair.")
    parser.add_argument("--arm", required=True, choices=EGRESS_MODE_BY_ARM.keys())
    parser.add_argument("--chain-rpc", required=True, help="Chain RPC URL as seen from the proxy")
    parser.add_argument("--mcp-url", default=None, help="MCP endpoint URL (required on MCP arms in block mode)")
    parser.add_argument("--proxy-host", default="ckbbench-proxy")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    content = build_allowlist(
        chain_rpc=args.chain_rpc,
        proxy_host=args.proxy_host,
        mcp_url=args.mcp_url,
        arm=args.arm,
    )
    args.output.write_text(content, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()