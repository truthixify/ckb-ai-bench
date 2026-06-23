"""Tests for production docker run kwargs (CKBBENCH_DOCKER seam)."""

from __future__ import annotations

from ckbbench.run.defaults import (
    build_cell_allowlist,
    internal_rpc_for,
    production_run_kwargs,
    use_docker,
)


def test_use_docker_false_by_default(monkeypatch):
    monkeypatch.delenv("CKBBENCH_DOCKER", raising=False)
    assert use_docker() is False


def test_use_docker_true_when_set(monkeypatch):
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    assert use_docker() is True


def test_production_run_kwargs_empty_without_docker(monkeypatch):
    monkeypatch.delenv("CKBBENCH_DOCKER", raising=False)
    assert production_run_kwargs(arm="A", chain="devnet") == {}


def test_internal_rpc_for_devnet():
    assert internal_rpc_for("devnet") == "http://ckbbench-devnet-node:8114"


def test_internal_rpc_for_testnet_parses_host(monkeypatch):
    monkeypatch.setattr("ckbbench.run.defaults.TESTNET_RPC", "http://192.168.0.73:18114")
    assert internal_rpc_for("testnet") == "http://192.168.0.73:18114"


def test_internal_rpc_for_testnet_without_port(monkeypatch):
    monkeypatch.setattr("ckbbench.run.defaults.TESTNET_RPC", "http://192.168.0.73")
    assert internal_rpc_for("testnet") == "http://192.168.0.73"


def test_internal_rpc_for_rejects_bad_testnet_rpc(monkeypatch):
    monkeypatch.setattr("ckbbench.run.defaults.TESTNET_RPC", "http://:badport")
    try:
        internal_rpc_for("testnet")
    except ValueError as exc:
        assert "TESTNET_RPC" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_internal_rpc_for_unknown_chain():
    try:
        internal_rpc_for("mainnet")
    except ValueError as exc:
        assert "mainnet" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_build_cell_allowlist_block_arm_writes_chain_and_proxy(tmp_path, monkeypatch):
    proxy_dir = tmp_path / "containers" / "proxy"
    proxy_dir.mkdir(parents=True)
    monkeypatch.setattr("ckbbench.run.defaults._REPO_ROOT", tmp_path)
    path = build_cell_allowlist("A", "devnet")
    assert path.parent == proxy_dir
    assert path.name.startswith("allowlist.A.devnet.")
    lines = [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines == [r"^ckbbench-devnet-node$", r"^ckbbench-proxy$"]


def test_build_cell_allowlist_mcp_arm_includes_mcp_host(tmp_path, monkeypatch):
    proxy_dir = tmp_path / "containers" / "proxy"
    proxy_dir.mkdir(parents=True)
    monkeypatch.setattr("ckbbench.run.defaults._REPO_ROOT", tmp_path)
    monkeypatch.setattr("ckbbench.run.defaults.MCP_URL", "https://mcp.example/ckbai")
    path = build_cell_allowlist("D", "testnet")
    lines = [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines == [
        r"^192\.168\.0\.73$",
        r"^ckbbench-proxy$",
        r"^mcp\.example$",
    ]


def test_production_run_kwargs_includes_runner_and_violation_check(monkeypatch):
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    kwargs = production_run_kwargs(arm="A", chain="devnet", log_since=1718188800.0)
    assert set(kwargs) == {"runner", "violation_check"}
    assert callable(kwargs["runner"])
    assert callable(kwargs["violation_check"])


def test_production_run_kwargs_violation_check_uses_built_allowlist(monkeypatch, tmp_path):
    proxy_dir = tmp_path / "containers" / "proxy"
    proxy_dir.mkdir(parents=True)
    monkeypatch.setattr("ckbbench.run.defaults._REPO_ROOT", tmp_path)
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")

    recorded: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = (
            'INFO     Jun 12 10:00:00.123 tinyproxy[1]: Established connection to host "evil.com"\n'
        )
        stderr = ""

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return FakeProc()

    monkeypatch.setattr("ckbbench.run.proxy_log.subprocess.run", fake_run)
    kwargs = production_run_kwargs(arm="A", chain="devnet", log_since=1718188800.5)
    assert kwargs["violation_check"]("A", tmp_path) is True
    assert recorded == [["docker", "logs", "--since", "1718188800.5", "ckbbench-proxy"]]