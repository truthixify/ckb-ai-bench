#!/usr/bin/env python3
"""Build per-arm/per-chain tinyproxy allowlists (ADR-0006).

Phase 4 orchestrator calls this at run time. Phase 3 ships the template + builder.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse

from ckbbench.config import ARM_MATRIX, EGRESS_MODE_BY_ARM

_OBSERVE = Path(__file__).resolve().parent / "proxy" / "allowlist.observe"

_BLOCK_HEADER = (
    "# Built per-arm block-mode allowlist (ADR-0006). One POSIX-ERE per line; FilterDefaultDeny\n"
    "# is On, so ONLY these hosts are permitted, all others refused AND logged.\n"
)


def _host_from_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"cannot parse host from {url!r}")
    return host


def _ere_host_line(host: str) -> str:
    """Anchored POSIX-ERE line matching exactly ``host``. Only ``.`` is a metachar in a hostname
    that we must neutralize (so a lookalike domain cannot match); we do NOT use re.escape, which
    is Python-regex escaping and over-escapes (e.g. ``\\-``) for ERE."""
    return "^" + host.replace(".", r"\.") + "$"


def build_allowlist(
    *,
    chain_rpc: str,
    proxy_host: str = "ckbbench-proxy",
    mcp_url: str | None = None,
    arm: str,
) -> str:
    """Return allowlist file contents for the given arm and chain RPC URL.

    Observe arms (B/C) get the permissive ``allowlist.observe`` (web permitted, all logged).
    Block arms (A/D) get EXACTLY: chain RPC host, the proxy, and (on MCP arms) the MCP host. The
    output is the rule lines only (plus a short header comment); no template placeholders leak in.
    """
    if arm not in EGRESS_MODE_BY_ARM:
        raise ValueError(f"unknown arm {arm!r}")

    if EGRESS_MODE_BY_ARM[arm] == "observe":
        return _OBSERVE.read_text(encoding="utf-8")

    lines = [_ere_host_line(_host_from_url(chain_rpc)), _ere_host_line(proxy_host)]
    mcp_enabled, _ = ARM_MATRIX[arm]
    if mcp_enabled:
        if not mcp_url:
            raise ValueError(f"arm {arm} requires MCP URL for block-mode allowlist")
        lines.append(_ere_host_line(_host_from_url(mcp_url)))
    return _BLOCK_HEADER + "\n".join(lines) + "\n"


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