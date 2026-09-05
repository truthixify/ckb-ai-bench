"""Unit tests for tinyproxy log violation reader (ADR-0006, no docker/network)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ckbbench.run.proxy_log import (
    LOG_FETCH_TIMEOUT_SECONDS,
    _default_allowlist_path,
    _default_log_fetcher,
    check_mcp_host_violation,
    check_proxy_violation,
    host_matches_allowlist,
    make_violation_check,
    mcp_host_from_url,
    parse_established_hosts,
)
from ckbbench.run.orchestrate import ViolationEvidenceError

# Sample snippets from spikes/egress-proxy/FINDINGS.md and run-spike.sh.
_LOG_ALLOW_CHAIN = (
    'INFO     Jun 12 10:00:00.123 tinyproxy[1]: Established connection to host "192.168.0.73"\n'
)
_LOG_REFUSE_WEB = (
    'INFO     Jun 12 10:00:01.456 tinyproxy[1]: Proxying refused on filtered domain "example.com"\n'
)
_LOG_REFUSE_IP = (
    'INFO     Jun 12 10:00:02.789 tinyproxy[1]: refused on filtered url "http://1.1.1.1/"\n'
)

_ALLOWLIST_A = [
    r"^192\.168\.0\.73$",
    r"^ckbbench-proxy$",
]


def test_parse_established_hosts_extracts_allowlisted_chain_rpc():
    hosts = parse_established_hosts(_LOG_ALLOW_CHAIN)
    assert hosts == ["192.168.0.73"]


def test_parse_established_hosts_ignores_refused_lines():
    log = _LOG_REFUSE_WEB + _LOG_REFUSE_IP
    assert parse_established_hosts(log) == []


def test_parse_established_hosts_collects_multiple_established():
    log = _LOG_ALLOW_CHAIN + _LOG_ALLOW_CHAIN.replace("192.168.0.73", "ckbbench-proxy")
    assert parse_established_hosts(log) == ["192.168.0.73", "ckbbench-proxy"]


def test_host_matches_allowlist_accepts_anchored_ere():
    assert host_matches_allowlist("192.168.0.73", _ALLOWLIST_A) is True
    assert host_matches_allowlist("ckbbench-proxy", _ALLOWLIST_A) is True


def test_host_matches_allowlist_rejects_lookalike_domain():
    # Anchored + escaped dots: a substring or dot-wildcard match must not pass.
    assert host_matches_allowlist("example.com", _ALLOWLIST_A) is False
    assert host_matches_allowlist("X192.168.0.73", _ALLOWLIST_A) is False


def test_host_matches_allowlist_skips_comments_and_blank_lines():
    lines = ["", "# comment", r"^192\.168\.0\.73$"]
    assert host_matches_allowlist("192.168.0.73", lines) is True
    assert host_matches_allowlist("evil.com", lines) is False


def test_check_proxy_violation_false_when_only_allowlisted_established():
    log = _LOG_ALLOW_CHAIN + _LOG_REFUSE_WEB
    assert check_proxy_violation(log, _ALLOWLIST_A) is False


def test_check_proxy_violation_true_when_non_allowlisted_established():
    log = _LOG_ALLOW_CHAIN + _LOG_ALLOW_CHAIN.replace("192.168.0.73", "example.com")
    assert check_proxy_violation(log, _ALLOWLIST_A) is True


def test_check_proxy_violation_false_when_only_refusals():
    assert check_proxy_violation(_LOG_REFUSE_WEB + _LOG_REFUSE_IP, _ALLOWLIST_A) is False


def test_make_violation_check_web_arms_allow_ordinary_web(tmp_path: Path):
    for arm in ("B", "C"):
        check = make_violation_check(
            arm=arm,
            chain="devnet",
            allowlist_path=tmp_path / "unused",
            log_fetcher=lambda: _LOG_ALLOW_CHAIN.replace("192.168.0.73", "example.com"),
        )
        assert check(arm, tmp_path) is False


def test_make_violation_check_block_arm_no_violation(tmp_path: Path):
    allowlist = tmp_path / "allowlist"
    allowlist.write_text(
        "# block list\n^192\\.168\\.0\\.73$\n^ckbbench-proxy$\n",
        encoding="utf-8",
    )
    check = make_violation_check(
        arm="A",
        chain="devnet",
        allowlist_path=allowlist,
        log_fetcher=lambda: _LOG_ALLOW_CHAIN + _LOG_REFUSE_WEB,
    )
    assert check("A", tmp_path) is False


def test_make_violation_check_block_arm_detects_violation(tmp_path: Path):
    allowlist = tmp_path / "allowlist"
    allowlist.write_text(r"^192\.168\.0\.73$\n", encoding="utf-8")
    bad_log = _LOG_ALLOW_CHAIN.replace("192.168.0.73", "example.com")
    check = make_violation_check(
        arm="D",
        chain="testnet",
        allowlist_path=allowlist,
        log_fetcher=lambda: bad_log,
    )
    assert check("D", tmp_path) is True


def test_default_allowlist_path_uses_env(monkeypatch):
    monkeypatch.setenv("CKBBENCH_ALLOWLIST_FILE", "/tmp/custom.allowlist")
    assert _default_allowlist_path(arm="A", chain="devnet") == Path("/tmp/custom.allowlist")


def test_default_allowlist_path_falls_back_to_built_file(monkeypatch):
    monkeypatch.delenv("CKBBENCH_ALLOWLIST_FILE", raising=False)
    path = _default_allowlist_path(arm="D", chain="testnet")
    assert path.name == "allowlist.D.testnet.built"
    assert path.parent.name == "proxy"


def test_default_log_fetcher_runs_docker_logs(monkeypatch):
    recorded: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = _LOG_ALLOW_CHAIN
        stderr = ""

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return FakeProc()

    monkeypatch.setattr("ckbbench.run.proxy_log.subprocess.run", fake_run)
    monkeypatch.setenv("CKBBENCH_PROXY_CONTAINER", "my-proxy")
    text = _default_log_fetcher()
    assert "192.168.0.73" in text
    assert recorded == [["docker", "logs", "my-proxy"]]


def test_default_log_fetcher_passes_since_to_docker_logs(monkeypatch):
    recorded: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = _LOG_ALLOW_CHAIN
        stderr = ""

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return FakeProc()

    monkeypatch.setattr("ckbbench.run.proxy_log.subprocess.run", fake_run)
    monkeypatch.setenv("CKBBENCH_PROXY_CONTAINER", "my-proxy")
    _default_log_fetcher(since=1718188800.5)
    assert recorded == [["docker", "logs", "--since", "1718188800.5", "my-proxy"]]


def test_make_violation_check_uses_default_log_fetcher(tmp_path: Path, monkeypatch):
    allowlist = tmp_path / "allowlist"
    allowlist.write_text(r"^192\.168\.0\.73$\n", encoding="utf-8")

    class FakeProc:
        returncode = 0
        stdout = _LOG_ALLOW_CHAIN.replace("192.168.0.73", "example.com")
        stderr = ""

    monkeypatch.setattr(
        "ckbbench.run.proxy_log.subprocess.run",
        lambda *a, **k: FakeProc(),
    )
    check = make_violation_check(arm="A", chain="devnet", allowlist_path=allowlist)
    assert check("A", tmp_path) is True


def test_make_violation_check_default_allowlist_path(tmp_path: Path, monkeypatch):
    built = tmp_path / "allowlist.A.devnet.built"
    built.write_text("^192\\.168\\.0\\.73$\n", encoding="utf-8")
    monkeypatch.setenv("CKBBENCH_ALLOWLIST_FILE", str(built))
    check = make_violation_check(
        arm="A",
        chain="devnet",
        log_fetcher=lambda: _LOG_ALLOW_CHAIN,
    )
    assert check("A", tmp_path) is False


def test_make_violation_check_log_since_scopes_docker_logs(tmp_path: Path, monkeypatch):
    allowlist = tmp_path / "allowlist"
    allowlist.write_text("^192\\.168\\.0\\.73$\n", encoding="utf-8")
    recorded: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = _LOG_ALLOW_CHAIN
        stderr = ""

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return FakeProc()

    monkeypatch.setattr("ckbbench.run.proxy_log.subprocess.run", fake_run)
    monkeypatch.setenv("CKBBENCH_PROXY_CONTAINER", "scoped-proxy")
    check = make_violation_check(
        arm="A",
        chain="devnet",
        allowlist_path=allowlist,
        log_since=1718188800.0,
    )
    assert check("A", tmp_path) is False
    assert recorded == [["docker", "logs", "--since", "1718188800.0", "scoped-proxy"]]


_LOG_LINE = 'INFO     Jun 12 10:00:00.123 tinyproxy[1]: Established connection to host "{host}"\n'
_MCP_URL = "https://mcp.ckbdev.com/ckbai"


def _log(*hosts: str) -> str:
    return "".join(_LOG_LINE.format(host=h) for h in hosts)


def _check(arm: str, log_text: str, *, mcp_url: str = _MCP_URL) -> bool:
    check = make_violation_check(
        arm=arm, chain="devnet", log_fetcher=lambda: log_text, mcp_url=mcp_url
    )
    return check(arm, Path("."))


def test_mcp_host_is_derived_from_the_configured_url():
    assert mcp_host_from_url("https://mcp.ckbdev.com/ckbai") == "mcp.ckbdev.com"
    assert mcp_host_from_url("http://MCP.Example.COM:8080/x") == "mcp.example.com"


@pytest.mark.parametrize("bad", ["", "not-a-url", "/just/a/path", "https:///nohost"])
def test_unparseable_mcp_url_is_rejected(bad):
    with pytest.raises(ValueError, match="cannot derive an MCP hostname"):
        mcp_host_from_url(bad)


def test_b_reaching_the_configured_mcp_host_is_a_violation():
    assert _check("B", _log("mcp.ckbdev.com")) is True


def test_b_host_match_is_case_insensitive():
    assert _check("B", _log("MCP.CKBDEV.COM")) is True


@pytest.mark.parametrize(
    "host",
    [
        "docs.nervos.org",
        "github.com",
        "notmcp.ckbdev.com",
        "mcp.ckbdev.com.evil.example",
        "evil.example.mcp.ckbdev.com.attacker.net",
        "mcp-ckbdev.com",
    ],
    ids=["docs", "github", "prefix", "suffix", "embedded", "hyphenated"],
)
def test_ordinary_b_web_and_lookalikes_are_not_violations(host):
    assert _check("B", _log(host)) is False


def test_b_violation_is_found_among_ordinary_web_traffic():
    assert _check("B", _log("docs.nervos.org", "mcp.ckbdev.com", "github.com")) is True


def test_b_policy_follows_a_retargeted_mcp_url():
    """Policy must track the configured endpoint, not a module-level literal."""
    assert _check("B", _log("other.example.com"), mcp_url="https://other.example.com/x") is True
    assert _check("B", _log("mcp.ckbdev.com"), mcp_url="https://other.example.com/x") is False


def test_c_reaching_the_configured_mcp_host_directly_is_a_violation():
    assert _check("C", _log("mcp.ckbdev.com")) is True


def test_c_ordinary_web_research_is_not_a_violation():
    assert _check("C", _log("docs.nervos.org", "github.com")) is False


def test_unknown_arm_is_rejected_rather_than_permissive():
    with pytest.raises(ValueError, match="unknown arm"):
        make_violation_check(arm="Z", chain="devnet", mcp_url=_MCP_URL)



def _fetcher_result(monkeypatch, **kwargs):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(**kwargs))
    return _default_log_fetcher(since=1.0)


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_log_fetcher_collects_stdout_and_stderr(monkeypatch):
    assert _fetcher_result(monkeypatch, stdout="out", stderr="err") == "outerr"


def test_log_fetcher_uses_the_exact_since_and_timeout(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["timeout"] = cmd, kwargs.get("timeout")
        return _Completed(stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _default_log_fetcher(since=12.5)
    assert "--since" in seen["cmd"] and "12.5" in seen["cmd"]
    assert seen["timeout"] == LOG_FETCH_TIMEOUT_SECONDS


def test_log_fetcher_honours_the_container_override(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Completed()

    monkeypatch.setenv("CKBBENCH_PROXY_CONTAINER", "ckbbench-task08-proxy")
    monkeypatch.setattr(subprocess, "run", fake_run)
    _default_log_fetcher(since=None)
    assert seen["cmd"][-1] == "ckbbench-task08-proxy"


def test_nonzero_docker_logs_is_an_evidence_error(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Completed(returncode=1, stderr="no such container")
    )
    with pytest.raises(ViolationEvidenceError, match="exited 1"):
        _default_log_fetcher(since=None)


def test_evidence_error_bounds_its_diagnostic_tail(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Completed(returncode=1, stderr="x" * 5000)
    )
    with pytest.raises(ViolationEvidenceError) as excinfo:
        _default_log_fetcher(since=None)
    assert len(str(excinfo.value)) < 5000


def test_docker_logs_timeout_is_an_evidence_error(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker logs", timeout=LOG_FETCH_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ViolationEvidenceError, match="timed out"):
        _default_log_fetcher(since=None)


def test_unrunnable_docker_is_an_evidence_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no docker")))
    with pytest.raises(ViolationEvidenceError, match="could not run docker logs"):
        _default_log_fetcher(since=None)


@pytest.mark.parametrize("arm", ["A", "B", "C"])
@pytest.mark.parametrize(
    "reader",
    [
        lambda: (_ for _ in ()).throw(OSError("reader unavailable")),
        lambda: (_ for _ in ()).throw(TimeoutError("slow")),
        lambda: None,
        lambda: 42,
    ],
    ids=["oserror", "timeout", "none", "non-str"],
)
def test_injected_reader_failures_become_evidence_errors(arm, reader):
    """An injected reader must fail closed too, or run_cell would never persist infra_fail."""
    check = make_violation_check(
        arm=arm, chain="devnet", log_fetcher=reader, mcp_url=_MCP_URL,
        allowlist_path=Path("containers/proxy/allowlist.observe"),
    )
    with pytest.raises(ViolationEvidenceError):
        check(arm, Path("."))


@pytest.mark.parametrize("arm", ["A", "B", "C"])
def test_empty_log_is_valid_evidence_of_no_connection(arm):
    check = make_violation_check(
        arm=arm, chain="devnet", log_fetcher=lambda: "", mcp_url=_MCP_URL,
        allowlist_path=Path("containers/proxy/allowlist.observe"),
    )
    assert check(arm, Path(".")) is False


@pytest.mark.parametrize("arm", ["A", "B", "C"])
def test_unrelated_reader_bug_stays_visible(arm):
    check = make_violation_check(
        arm=arm, chain="devnet",
        log_fetcher=lambda: (_ for _ in ()).throw(KeyError("bug")), mcp_url=_MCP_URL,
        allowlist_path=Path("containers/proxy/allowlist.observe"),
    )
    with pytest.raises(KeyError):
        check(arm, Path("."))
