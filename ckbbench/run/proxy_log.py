"""Proxy egress log reader for block-mode protocol violations (ADR-0006).

Parses tinyproxy docker logs for successful proxy traversal to non-allowlisted hosts on
no-research arms (A/D). Refused/filtered attempts are NOT violations (the block worked).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from ckbbench.config import EGRESS_MODE_BY_ARM
from ckbbench.run.orchestrate import ViolationCheck

_ESTABLISHED_RE = re.compile(r'Established connection to host "([^"]+)"')
_REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_established_hosts(log_text: str) -> list[str]:
    """Extract destination hosts from tinyproxy Established connection log lines."""
    return _ESTABLISHED_RE.findall(log_text)


def host_matches_allowlist(host: str, allowlist_ere_lines: Sequence[str]) -> bool:
    """Return True if ``host`` matches any POSIX-ERE allowlist line (^host$ style)."""
    for line in allowlist_ere_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(stripped, host):
            return True
    return False


def check_proxy_violation(log_text: str, allowlist_ere_lines: Sequence[str]) -> bool:
    """Return True if ANY established host in the log is not allowlisted."""
    for host in parse_established_hosts(log_text):
        if not host_matches_allowlist(host, allowlist_ere_lines):
            return True
    return False


def _allowlist_ere_lines_from_path(path: Path) -> list[str]:
    return [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _default_allowlist_path(*, arm: str, chain: str) -> Path:
    env_path = os.getenv("CKBBENCH_ALLOWLIST_FILE")
    if env_path:
        return Path(env_path)
    return _REPO_ROOT / "containers" / "proxy" / f"allowlist.{arm}.{chain}.built"


def _default_log_fetcher() -> str:
    container = os.getenv("CKBBENCH_PROXY_CONTAINER", "ckbbench-proxy")
    proc = subprocess.run(
        ["docker", "logs", container],
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def make_violation_check(
    *,
    arm: str,
    chain: str,
    allowlist_path: Path | None = None,
    log_fetcher: Callable[[], str] | None = None,
) -> ViolationCheck:
    """Build a ViolationCheck that reads proxy logs and compares to the arm allowlist.

    Observe arms (B/C) always return False: web egress is permitted and cannot violate the
    no-research rule via this check.
    """
    if EGRESS_MODE_BY_ARM.get(arm) == "observe":
        return lambda _arm, _mount: False

    resolved_allowlist = allowlist_path or _default_allowlist_path(arm=arm, chain=chain)
    ere_lines = _allowlist_ere_lines_from_path(resolved_allowlist)
    fetch_logs = log_fetcher or _default_log_fetcher

    def _check(_arm: str, _mount: Path) -> bool:
        return check_proxy_violation(fetch_logs(), ere_lines)

    return _check