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
    stop_agent_checked,
)
from ckbbench.run.runner import PrepareError


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


def test_stop_agent_checked_removes_and_clears():
    """WHY: fail-open agent stop leaves processes holding the work volume."""
    recorded: list[list[str]] = []
    alive = {"cid-9": True}

    class Env:
        def __init__(self) -> None:
            self.container_id = "cid-9"

    class Agent:
        def __init__(self) -> None:
            self.env = Env()

    def seam(argv):
        recorded.append(list(argv))
        if len(argv) >= 2 and argv[0] == "docker" and argv[1] == "rm":
            alive[argv[-1]] = False
            return 0, ""
        if len(argv) >= 2 and argv[0] == "docker" and argv[1] == "inspect":
            return (0, "{}") if alive.get(argv[-1]) else (1, "Error: No such object: cid-9")
        return 0, ""

    agent = Agent()
    stop_agent_checked(agent, run=seam)
    assert ["docker", "rm", "-f", "cid-9"] in recorded
    assert agent.env.container_id is None


def test_stop_agent_checked_raises_if_still_present():
    class Env:
        container_id = "stuck"

    class Agent:
        env = Env()

    def seam(argv):
        if argv[:2] == ["docker", "inspect"]:
            return 0, "{}"
        return 0, ""

    with pytest.raises(PrepareError, match="still present"):
        stop_agent_checked(Agent(), run=seam)


def test_stop_agent_checked_fail_closed_on_daemon_error():
    """WHY: inspect exit 1 without 'No such' must not clear id and continue grade."""
    class Env:
        container_id = "cid-x"

    class Agent:
        env = Env()

    def seam(argv):
        if argv[:2] == ["docker", "inspect"]:
            return 1, "Cannot connect to the Docker daemon"
        return 0, ""

    agent = Agent()
    with pytest.raises(PrepareError, match="cannot verify"):
        stop_agent_checked(agent, run=seam)
    assert agent.env.container_id == "cid-x"


def test_stop_agent_checked_fail_closed_on_missing_docker_errno_text():
    class Env:
        container_id = "cid-live"

    class Agent:
        env = Env()

    def seam(argv):
        return 1, "[Errno 2] No such file or directory: 'docker'"

    agent = Agent()
    with pytest.raises(PrepareError, match="cannot verify|cannot run docker"):
        stop_agent_checked(agent, run=seam)
    assert agent.env.container_id == "cid-live"


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



class _StopEnv:
    def __init__(self, cid: str = "ckbbench-agent-1") -> None:
        self.container_id = cid


class _StopAgent:
    def __init__(self, cid: str = "ckbbench-agent-1") -> None:
        self.env = _StopEnv(cid)


def _stop_seam(inspect_code: int, inspect_out: str):
    def seam(argv):
        if argv[:3] == ["docker", "rm", "-f"]:
            return 0, ""
        return inspect_code, inspect_out
    return seam


def test_stop_agent_checked_accepts_an_exact_name_absence():
    agent = _StopAgent()
    stop_agent_checked(agent, run=_stop_seam(1, "Error: No such container: ckbbench-agent-1"))
    assert agent.env.container_id is None


@pytest.mark.parametrize(
    ("inspect_code", "inspect_out", "why"),
    [
        (1, "Error: No such container: ckbbench-agent-12", "adjacent suffix token"),
        (1, "Error: No such container: x-ckbbench-agent-1", "adjacent prefix token"),
        (1, "Error: No such container: some-other-container", "unrelated container"),
        (1, "Cannot connect to the Docker daemon", "daemon failure"),
        (1, "permission denied while trying to connect", "permission failure"),
        (0, "[{}]", "still present"),
    ],
)
def test_stop_agent_checked_refuses_ambiguous_absence(inspect_code, inspect_out, why):
    """An ambiguous stop must raise and leave the container id set for cleanup."""
    agent = _StopAgent()
    with pytest.raises(PrepareError):
        stop_agent_checked(agent, run=_stop_seam(inspect_code, inspect_out))
    assert agent.env.container_id == "ckbbench-agent-1", why


@pytest.mark.parametrize(
    ("inspect_out", "why"),
    [
        ("Error: No such volume: ckbbench-agent-1", "a volume absence must not clear a container"),
        ("Error: No such image: ckbbench-agent-1", "an image absence must not clear a container"),
    ],
)
def test_stop_agent_checked_refuses_a_wrong_object_kind(inspect_out, why):
    agent = _StopAgent()
    with pytest.raises(PrepareError):
        stop_agent_checked(agent, run=_stop_seam(1, inspect_out))
    assert agent.env.container_id == "ckbbench-agent-1", why


def test_stop_agent_checked_accepts_generic_no_such_object():
    """`docker inspect` (not `container inspect`) words absence as "No such object"."""
    agent = _StopAgent()
    stop_agent_checked(agent, run=_stop_seam(1, "Error: No such object: ckbbench-agent-1"))
    assert agent.env.container_id is None


def test_stop_agent_checked_is_not_confused_by_a_container_named_like_another_kind():
    """A name embedding "no-such-volume" still clears: Docker names cannot contain spaces.

    The wrong-kind guard matches the space-separated daemon phrase, so no container name can forge
    or trip it.
    """
    agent = _StopAgent()
    agent.env.container_id = "ckbbench-no-such-volume-1"
    stop_agent_checked(
        agent, run=_stop_seam(1, "Error: No such object: ckbbench-no-such-volume-1")
    )
    assert agent.env.container_id is None
