"""Proxy egress log reader for per-arm protocol violations (ADR-0006).

Two policies share one seam. A/D are no-research arms: any established connection to a
non-allowlisted host is a violation. B/C are web-enabled, but their shell must not reach the product
under test directly: CKB AI access is controller-mediated in C and absent in B. Refused/filtered
attempts are NOT violations (the block worked).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlparse

from ckbbench.config import ARM_MATRIX, MCP_URL
from ckbbench.run.orchestrate import ViolationCheck, ViolationEvidenceError

# `docker logs` is evidence collection, not the run; a hung daemon must fail the cell, not stall it.
LOG_FETCH_TIMEOUT_SECONDS = 30
# Diagnostics in an evidence error are bounded so a log body never lands in an exception message.
_ERROR_TAIL_CHARS = 512

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


def mcp_host_from_url(mcp_url: str) -> str:
    """The prohibited hostname for no-MCP arms, derived from the configured endpoint.

    Deriving it keeps policy tied to whatever endpoint the run actually targets; a hardcoded host
    would silently stop matching the moment an operator retargets CKBBENCH_MCP_URL.
    """
    host = urlparse(mcp_url).hostname
    if not host:
        raise ValueError(f"cannot derive an MCP hostname from {mcp_url!r}")
    return host.lower()


def check_mcp_host_violation(log_text: str, mcp_host: str) -> bool:
    """Return True if any established host is exactly the configured MCP host.

    Exact, case-insensitive equality: a lookalike, a suffix, or an ordinary web host that merely
    contains the name is legitimate B research and must not be flagged. Host-level matching is
    deliberate -- tinyproxy records an HTTPS CONNECT destination without its path.
    """
    target = mcp_host.lower()
    return any(host.strip().lower() == target for host in parse_established_hosts(log_text))


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


def _default_log_fetcher(*, since: float | None = None) -> str:
    container = os.getenv("CKBBENCH_PROXY_CONTAINER", "ckbbench-proxy")
    cmd = ["docker", "logs"]
    if since is not None:
        cmd.extend(["--since", str(since)])
    cmd.append(container)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=LOG_FETCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise ViolationEvidenceError(
            f"docker logs {container} timed out after {LOG_FETCH_TIMEOUT_SECONDS}s"
        ) from None
    except OSError as exc:
        raise ViolationEvidenceError(f"could not run docker logs {container}: {exc}") from None
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-_ERROR_TAIL_CHARS:].strip()
        raise ViolationEvidenceError(
            f"docker logs {container} exited {proc.returncode}: {tail}"
        )
    return (proc.stdout or "") + (proc.stderr or "")


def _bound_default_fetcher(log_since: float | None) -> Callable[[], str]:
    def fetch_logs() -> str:
        return _default_log_fetcher(since=log_since)

    return fetch_logs


def _guarded(fetch: Callable[[], str]) -> Callable[[], str]:
    """Normalize evidence-read failures from ANY reader, injected or default.

    Only the named error reaches run_cell's infra branch, so a reader that raises something else
    would otherwise skip result persistence entirely. An empty string stays valid evidence: it means
    no connection was established.
    """

    def guarded() -> str:
        try:
            out = fetch()
        except ViolationEvidenceError:
            raise
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise ViolationEvidenceError(f"proxy log reader failed: {exc}") from None
        if not isinstance(out, str):
            raise ViolationEvidenceError(
                f"proxy log reader returned {type(out).__name__}, expected str"
            )
        return out

    return guarded


def make_violation_check(
    *,
    arm: str,
    chain: str,
    allowlist_path: Path | None = None,
    log_fetcher: Callable[[], str] | None = None,
    log_since: float | None = None,
    mcp_url: str = MCP_URL,
) -> ViolationCheck:
    """Build the per-arm ViolationCheck.

    A/D compare established hosts against the arm allowlist. B/C permit ordinary web research but
    prohibit direct shell access to the product under test, so both compare against the configured
    MCP host. C's legitimate MCP requests are made by the host controller and do not traverse the
    agent proxy.
    """
    if arm not in ARM_MATRIX:
        raise ValueError(f"unknown arm {arm!r}; expected one of {sorted(ARM_MATRIX)}")

    _mcp_enabled, web_allowed = ARM_MATRIX[arm]
    if web_allowed:
        mcp_host = mcp_host_from_url(mcp_url)
        fetch_web_logs = _guarded(log_fetcher or _bound_default_fetcher(log_since))

        def _check_web(_arm: str, _mount: Path) -> bool:
            return check_mcp_host_violation(fetch_web_logs(), mcp_host)

        return _check_web

    resolved_allowlist = allowlist_path or _default_allowlist_path(arm=arm, chain=chain)
    ere_lines = _allowlist_ere_lines_from_path(resolved_allowlist)
    fetch_logs = _guarded(log_fetcher or _bound_default_fetcher(log_since))

    def _check(_arm: str, _mount: Path) -> bool:
        return check_proxy_violation(fetch_logs(), ere_lines)

    return _check
