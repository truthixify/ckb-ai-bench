"""Unit tests for post-run docker/host cleanup (no real docker)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ckbbench.run.cleanup import (
    CellCleanupTargets,
    assert_ckbbench_name,
    cleanup_agent,
    cleanup_cell,
    cleanup_matrix_volumes,
    docker_rm_container,
    docker_rm_volume,
    keep_resources,
    resolve_work_volume,
    rm_host_path,
)


def test_keep_resources_env_and_explicit(monkeypatch):
    monkeypatch.delenv("CKBBENCH_KEEP", raising=False)
    assert keep_resources() is False
    assert keep_resources(keep=True) is True
    assert keep_resources(keep=False) is False
    monkeypatch.setenv("CKBBENCH_KEEP", "1")
    assert keep_resources() is True
    monkeypatch.setenv("CKBBENCH_KEEP", "yes")
    assert keep_resources() is True
    # Explicit keep=False wins over env (caller override).
    assert keep_resources(keep=False) is False


def test_assert_ckbbench_name_rejects_foreign():
    with pytest.raises(ValueError, match="refusing"):
        assert_ckbbench_name("other-volume", "volume")
    with pytest.raises(ValueError, match="refusing"):
        assert_ckbbench_name("", "volume")
    assert_ckbbench_name("ckbbench-work", "volume")


def test_docker_rm_container_and_volume_argv():
    recorded: list[list[str]] = []

    def seam(argv):
        recorded.append(list(argv))
        return 0, ""

    docker_rm_container("abc123", run=seam)
    docker_rm_volume("ckbbench-work", run=seam)
    assert recorded == [
        ["docker", "rm", "-f", "abc123"],
        ["docker", "volume", "rm", "-f", "ckbbench-work"],
    ]


def test_docker_rm_volume_rejects_non_ckbbench():
    with pytest.raises(ValueError, match="refusing"):
        docker_rm_volume("redis-data", run=lambda argv: (0, ""))


def test_cleanup_agent_uses_container_id_and_clears_it():
    recorded: list[list[str]] = []

    class Env:
        def __init__(self) -> None:
            self.container_id = "cid-9"

    class Agent:
        def __init__(self) -> None:
            self.env = Env()

    def seam(argv):
        recorded.append(list(argv))
        return 0, ""

    agent = Agent()
    cleanup_agent(agent, run=seam)
    assert recorded == [["docker", "rm", "-f", "cid-9"]]
    assert agent.env.container_id is None


def test_cleanup_agent_falls_back_to_cleanup_method():
    called = {"n": 0}

    class Env:
        def cleanup(self):
            called["n"] += 1

    class Agent:
        env = Env()

    cleanup_agent(Agent())
    assert called["n"] == 1


def test_cleanup_cell_default_removes_all(tmp_path: Path):
    host = tmp_path / "ckbbench-runs" / "run1"
    host.mkdir(parents=True)
    (host / "mount").mkdir()
    allow = tmp_path / "allowlist.built"
    allow.write_text("x")
    recorded: list[list[str]] = []

    class Env:
        def __init__(self) -> None:
            self.container_id = "c1"

    class Agent:
        def __init__(self) -> None:
            self.env = Env()

    def seam(argv):
        recorded.append(list(argv))
        return 0, ""

    cleanup_cell(
        CellCleanupTargets(
            agent=Agent(),
            work_volume="ckbbench-work",
            host_run_dir=host,
            extra_paths=(allow,),
        ),
        keep=False,
        run=seam,
    )
    assert not host.exists()
    assert not allow.exists()
    assert ["docker", "rm", "-f", "c1"] in recorded
    assert ["docker", "volume", "rm", "-f", "ckbbench-work"] in recorded


def test_cleanup_cell_keep_skips_everything(tmp_path: Path):
    host = tmp_path / "run"
    host.mkdir()
    allow = tmp_path / "a.built"
    allow.write_text("x")
    recorded: list[list[str]] = []

    class Env:
        def __init__(self) -> None:
            self.container_id = "c1"

    class Agent:
        def __init__(self) -> None:
            self.env = Env()

    agent = Agent()
    cleanup_cell(
        CellCleanupTargets(
            agent=agent,
            work_volume="ckbbench-work",
            host_run_dir=host,
            extra_paths=(allow,),
        ),
        keep=True,
        run=lambda argv: recorded.append(list(argv)) or (0, ""),
    )
    assert host.exists()
    assert allow.exists()
    assert recorded == []
    assert agent.env.container_id == "c1"


def test_cleanup_matrix_volumes_cargo(monkeypatch):
    recorded: list[list[str]] = []
    cleanup_matrix_volumes(
        cargo_volume="ckbbench-cargo-cache",
        keep=False,
        run=lambda argv: recorded.append(list(argv)) or (0, ""),
    )
    assert recorded == [["docker", "volume", "rm", "-f", "ckbbench-cargo-cache"]]
    recorded.clear()
    cleanup_matrix_volumes(keep=True, run=lambda argv: recorded.append(list(argv)) or (0, ""))
    assert recorded == []


def test_resolve_work_volume(monkeypatch):
    monkeypatch.delenv("CKBBENCH_DOCKER", raising=False)
    monkeypatch.delenv("CKBBENCH_WORK_VOLUME", raising=False)
    assert resolve_work_volume() is None
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    assert resolve_work_volume() == "ckbbench-work"
    assert resolve_work_volume(explicit="ckbbench-work-custom") == "ckbbench-work-custom"


def test_rm_host_path_file_and_dir(tmp_path: Path):
    f = tmp_path / "f.txt"
    f.write_text("hi")
    d = tmp_path / "d"
    d.mkdir()
    (d / "x").write_text("y")
    rm_host_path(f)
    rm_host_path(d)
    assert not f.exists()
    assert not d.exists()
    rm_host_path(tmp_path / "missing")  # no raise
