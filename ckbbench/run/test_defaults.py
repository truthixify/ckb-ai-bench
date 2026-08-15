"""Tests for production docker run kwargs (CKBBENCH_DOCKER seam)."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def _isolate_proxy_dir(monkeypatch, tmp_path):
    """production_run_kwargs writes a per-cell allowlist beside the proxy config by design."""
    proxy = tmp_path / "containers" / "proxy"
    proxy.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("ckbbench.run.defaults._REPO_ROOT", tmp_path)


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


def test_production_run_kwargs_includes_runner_and_violation_check(monkeypatch, tmp_path):
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    _isolate_proxy_dir(monkeypatch, tmp_path)
    kwargs = production_run_kwargs(arm="A", chain="devnet", log_since=1718188800.0)
    assert set(kwargs) == {
        "runner",
        "violation_check",
        "cleanup_extra_paths",
        "mcp_url",
        "work_volume",
        "prepare_chain",
    }
    assert callable(kwargs["runner"])
    assert callable(kwargs["violation_check"])
    assert callable(kwargs["prepare_chain"])
    assert len(kwargs["cleanup_extra_paths"]) == 1
    assert kwargs["work_volume"] == "ckbbench-work"


def test_production_run_kwargs_only_resets_docker_devnet(monkeypatch, tmp_path):
    """TestNet is a live chain the harness does not own, and a local run has no managed sidecar:
    neither may be handed a reset seam (plan §9.1)."""
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    _isolate_proxy_dir(monkeypatch, tmp_path)
    assert "prepare_chain" not in production_run_kwargs(arm="A", chain="testnet")

    monkeypatch.setenv("CKBBENCH_DOCKER", "0")
    assert production_run_kwargs(arm="A", chain="devnet") == {}


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


def test_production_kwargs_thread_the_configured_mcp_url_into_b_detection(monkeypatch, tmp_path):
    """An overridden endpoint must control B's product-host policy, not a module-level literal."""
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    _isolate_proxy_dir(monkeypatch, tmp_path)
    kwargs = production_run_kwargs(arm="B", chain="devnet", mcp_url="https://other.example.com/x")
    check = kwargs["violation_check"]

    def log(host: str) -> str:
        return f'tinyproxy[1]: Established connection to host "{host}"\n'

    import ckbbench.run.proxy_log as pl

    monkeypatch.setattr(pl, "_default_log_fetcher", lambda *, since=None: log("other.example.com"))
    assert check("B", tmp_path) is True

    monkeypatch.setattr(pl, "_default_log_fetcher", lambda *, since=None: log("mcp.ckbdev.com"))
    assert check("B", tmp_path) is False, "the default endpoint must not be used after an override"


def test_one_effective_mcp_url_reaches_kwargs_checker_and_d_allowlist(monkeypatch, tmp_path):
    """The agent, B's checker and D's allowlist must all describe the same host."""
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    _isolate_proxy_dir(monkeypatch, tmp_path)
    override = "https://override.example/mcp"
    kwargs = production_run_kwargs(arm="D", chain="devnet", mcp_url=override)
    assert kwargs["mcp_url"] == override, "run_cell must receive the same endpoint"
    allowlist = kwargs["cleanup_extra_paths"][0].read_text()
    assert "override" in allowlist and "ckbdev" not in allowlist


def test_matrix_wrapper_forwards_an_overridden_endpoint_to_production_kwargs(monkeypatch, tmp_path):
    from ckbbench.matrix.launch import make_production_run_cell

    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    _isolate_proxy_dir(monkeypatch, tmp_path)
    seen = {}

    def fake_run_cell(suite_obj, chain, arm, model, seed, **kwargs):
        seen.update(kwargs)
        raise SystemExit(0)

    runner = make_production_run_cell(
        suite=None, results_dir=tmp_path, run_cell_fn=fake_run_cell
    )
    with pytest.raises(SystemExit):
        runner(None, "devnet", "B", "m", 1, mcp_url="https://override.example/mcp")
    assert seen["mcp_url"] == "https://override.example/mcp"
    check = seen["violation_check"]

    def log(host: str) -> str:
        return f'tinyproxy[1]: Established connection to host "{host}"\n'

    import ckbbench.run.proxy_log as pl

    monkeypatch.setattr(pl, "_default_log_fetcher", lambda *, since=None: log("override.example"))
    assert check("B", tmp_path) is True, "B must watch the endpoint the agent actually uses"
    # No production-path unlink: every generated artifact must already be under the temporary root.
    for path in seen.get("cleanup_extra_paths", ()):
        assert tmp_path in Path(path).parents, (
            f"the wrapper wrote outside the temporary root: {path}"
        )


@pytest.mark.parametrize("bad", ["", "   ", "/no/host", "not-a-url"])
def test_an_explicit_unusable_endpoint_is_rejected_not_defaulted(monkeypatch, bad):
    """Only None means "no override"; an empty string must not silently become the default."""
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    proxy_dir = Path("containers/proxy")
    before = set(proxy_dir.glob("allowlist.*.built"))
    with pytest.raises(ValueError, match="unusable MCP endpoint"):
        production_run_kwargs(arm="B", chain="devnet", mcp_url=bad)
    assert set(proxy_dir.glob("allowlist.*.built")) == before, "no allowlist may be left behind"


def test_matrix_wrapper_rejects_an_explicit_empty_endpoint(monkeypatch, tmp_path):
    from ckbbench.matrix.launch import make_production_run_cell

    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    runner = make_production_run_cell(
        suite=None, results_dir=tmp_path, run_cell_fn=lambda *a, **k: None
    )
    with pytest.raises(ValueError, match="unusable MCP endpoint"):
        runner(None, "devnet", "B", "m", 1, mcp_url="")
