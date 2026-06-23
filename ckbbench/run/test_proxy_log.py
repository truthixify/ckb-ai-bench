"""Unit tests for tinyproxy log violation reader (ADR-0006, no docker/network)."""

from __future__ import annotations

from pathlib import Path

from ckbbench.run.proxy_log import (
    _default_allowlist_path,
    _default_log_fetcher,
    check_proxy_violation,
    host_matches_allowlist,
    make_violation_check,
    parse_established_hosts,
)

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


def test_make_violation_check_observe_arm_always_false(tmp_path: Path):
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