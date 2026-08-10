"""Tests for the operator CLI's run lock (scripts/ckbbench).

The lock is what keeps two benchmark operations off one DevNet, work volume, and results
directory, so it must hold on every supported operator host. `flock(1)` is util-linux and is not
installed on a stock macOS host, where the CLI used to report a phantom "another ckbbench
operation holds the lock" instead of taking a free lock; the python backend closes that gap with
the same `flock(2)` call. Both backends are exercised here, and a backend that could not fail if
mutual exclusion silently degraded to "trust the metadata file" would be worthless, so the cases
below drive the real script in real processes: the lock lives in the kernel's open file
description, which an in-process fake cannot model.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "scripts" / "ckbbench"

HAVE_FLOCK = shutil.which("flock") is not None
BACKENDS = ["auto", "python"] + (["flock"] if HAVE_FLOCK else [])
WAIT_TIMEOUT = 30.0


def _lock_dir(runtime_dir: Path) -> Path:
    return runtime_dir / f"ckbbench-{os.getuid()}"


def _meta_file(runtime_dir: Path) -> Path:
    return _lock_dir(runtime_dir) / "owner.meta"


def _env(runtime_dir: Path, backend: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env.pop("CKBBENCH_LOCK_BACKEND", None)
    if backend is not None and backend != "auto":
        env["CKBBENCH_LOCK_BACKEND"] = backend
    return env


def _bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=WAIT_TIMEOUT,
    )


def _take_lock(env: dict[str, str], name: str = "demo") -> subprocess.CompletedProcess[str]:
    return _bash(f'source "{CLI}"\nwith_lock {name}\necho "TOOK backend=$LOCK_BACKEND"', env)


def _dead_pid() -> int:
    """A pid that has exited and been reaped, so `kill -0` reports it dead."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _write_meta(runtime_dir: Path, pid: int) -> None:
    _meta_file(runtime_dir).write_text(f"pid={pid}\ncmd=stale\nstarted=1970-01-01T00:00:00+00:00\n")


@pytest.fixture
def holders():
    """Spawn lock holders that block until released, and never leak one out of a test."""
    started: list[subprocess.Popen[str]] = []

    def start(runtime_dir: Path, env: dict[str, str], name: str = "holder") -> subprocess.Popen[str]:
        ready = runtime_dir / f"{name}.ready"
        # `read` is a builtin, so no child of the holder inherits the lock FD and a killed
        # holder releases immediately; closing stdin releases it cleanly.
        proc = subprocess.Popen(
            ["bash", "-c", f'source "{CLI}"\nwith_lock {name}\n: >"{ready}"\nread -r _ || true'],
            cwd=REPO,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        started.append(proc)
        _wait_for(ready, proc)
        return proc

    yield start

    for proc in started:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=WAIT_TIMEOUT)


def _wait_for(path: Path, proc: subprocess.Popen[str] | None = None) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc is not None and proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(f"lock holder exited early (rc={proc.returncode}): {out}{err}")
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _release(proc: subprocess.Popen[str]) -> None:
    # communicate() closes the holder's stdin, so its blocking `read` sees EOF and it exits.
    out, err = proc.communicate(timeout=WAIT_TIMEOUT)
    assert proc.returncode == 0, f"{out}{err}"


@pytest.mark.parametrize("backend", BACKENDS)
def test_lock_is_taken_and_owner_recorded(tmp_path, backend):
    env = _env(tmp_path, backend)
    res = _take_lock(env, "up")

    assert res.returncode == 0, res.stderr
    assert "TOOK" in res.stdout
    if backend != "auto":
        assert f"backend={backend}" in res.stdout
    # The holder exited, so its trap cleared the metadata; the lock dir stays operator-private.
    assert not _meta_file(tmp_path).exists()
    assert oct(_lock_dir(tmp_path).stat().st_mode & 0o777) == "0o700"


def test_auto_backend_matches_host_flock_availability(tmp_path):
    res = _take_lock(_env(tmp_path, "auto"))

    assert res.returncode == 0, res.stderr
    assert f"backend={'flock' if HAVE_FLOCK else 'python'}" in res.stdout


@pytest.mark.parametrize("backend", BACKENDS)
def test_live_owner_refuses_a_second_operation(tmp_path, backend, holders):
    env = _env(tmp_path, backend)
    holder = holders(tmp_path, env)

    res = _take_lock(env, "run")

    assert res.returncode != 0
    assert "another ckbbench operation holds the lock" in res.stderr
    assert f"owner pid={holder.pid}" in res.stderr
    assert "TOOK" not in res.stdout
    # A refused operation must not clear the live owner's metadata.
    assert f"pid={holder.pid}" in _meta_file(tmp_path).read_text()
    _release(holder)


@pytest.mark.parametrize("backend", BACKENDS)
def test_stale_metadata_does_not_steal_a_live_lock(tmp_path, backend, holders):
    env = _env(tmp_path, backend)
    holder = holders(tmp_path, env)
    dead = _dead_pid()
    _write_meta(tmp_path, dead)

    res = _take_lock(env, "run")

    # Metadata is only a hint: it is reclaimed, but the kernel lock still refuses the operation.
    assert f"reclaiming stale lock metadata (dead pid {dead})" in res.stdout
    assert res.returncode != 0
    assert "another ckbbench operation holds the lock" in res.stderr
    _release(holder)


@pytest.mark.parametrize("backend", BACKENDS)
def test_stale_metadata_alone_does_not_block_a_free_lock(tmp_path, backend):
    env = _env(tmp_path, backend)
    _take_lock(env)  # create the lock dir/file, then release
    dead = _dead_pid()
    _write_meta(tmp_path, dead)

    res = _take_lock(env, "up")

    assert res.returncode == 0, res.stderr
    assert "TOOK" in res.stdout


@pytest.mark.parametrize("backend", BACKENDS)
def test_lock_is_reusable_after_the_holder_exits(tmp_path, backend, holders):
    env = _env(tmp_path, backend)
    holder = holders(tmp_path, env)
    _release(holder)

    assert not _meta_file(tmp_path).exists()
    res = _take_lock(env, "up")
    assert res.returncode == 0, res.stderr
    assert "TOOK" in res.stdout


@pytest.mark.parametrize("backend", BACKENDS)
def test_lock_is_reusable_after_the_holder_is_killed(tmp_path, backend, holders):
    env = _env(tmp_path, backend)
    holder = holders(tmp_path, env)
    holder.kill()
    holder.wait(timeout=WAIT_TIMEOUT)

    # The trap never ran, so the dead owner's metadata is still on disk.
    assert f"pid={holder.pid}" in _meta_file(tmp_path).read_text()
    res = _take_lock(env, "up")
    assert res.returncode == 0, res.stderr
    assert "TOOK" in res.stdout


@pytest.mark.skipif(not HAVE_FLOCK, reason="host has no flock(1) to mix with the python backend")
@pytest.mark.parametrize(("owner", "other"), [("flock", "python"), ("python", "flock")])
def test_the_two_backends_exclude_each_other(tmp_path, owner, other, holders):
    # Both backends call flock(2) on the same file, so a mixed pair of operators must still
    # serialize — otherwise one host's default would silently disable the other's lock.
    holder = holders(tmp_path, _env(tmp_path, owner))

    res = _take_lock(_env(tmp_path, other), "run")

    assert res.returncode != 0
    assert "another ckbbench operation holds the lock" in res.stderr
    _release(holder)


def test_unlock_refuses_a_live_owner(tmp_path, holders):
    env = _env(tmp_path)
    holder = holders(tmp_path, env)

    res = _bash(f'bash "{CLI}" unlock', env)

    assert res.returncode != 0
    assert f"lock owner pid {holder.pid} is still alive" in res.stderr
    assert f"pid={holder.pid}" in _meta_file(tmp_path).read_text()
    _release(holder)


def test_unlock_clears_dead_owner_metadata(tmp_path):
    env = _env(tmp_path)
    _take_lock(env)
    dead = _dead_pid()
    _write_meta(tmp_path, dead)

    res = _bash(f'bash "{CLI}" unlock', env)

    assert res.returncode == 0, res.stderr
    assert f"clearing stale metadata for dead pid {dead}" in res.stdout
    assert not _meta_file(tmp_path).exists()


def test_unlock_reports_a_free_lock(tmp_path):
    res = _bash(f'bash "{CLI}" unlock', _env(tmp_path))

    assert res.returncode == 0, res.stderr
    assert "lock is free" in res.stdout


def test_unlock_refuses_to_release_an_unowned_held_lock(tmp_path, holders):
    env = _env(tmp_path)
    holder = holders(tmp_path, env)
    _meta_file(tmp_path).unlink()

    res = _bash(f'bash "{CLI}" unlock', env)

    assert res.returncode != 0
    assert "refuse to force-release another holder" in res.stdout
    _release(holder)


def test_lock_dir_symlink_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _lock_dir(tmp_path).symlink_to(elsewhere)

    res = _take_lock(_env(tmp_path))

    assert res.returncode != 0
    assert "lock dir is a symlink" in res.stderr


def test_lock_file_symlink_is_refused_without_touching_its_target(tmp_path):
    lock_dir = _lock_dir(tmp_path)
    lock_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    (lock_dir / "project.lock").symlink_to(victim)

    res = _take_lock(_env(tmp_path))

    assert res.returncode != 0
    assert "lock file is a symlink" in res.stderr
    assert not victim.exists()


def test_unknown_backend_is_refused(tmp_path):
    res = _take_lock(_env(tmp_path, "bogus"))

    assert res.returncode != 0
    assert "unknown CKBBENCH_LOCK_BACKEND: bogus" in res.stderr


@pytest.mark.skipif(HAVE_FLOCK, reason="host has flock(1) installed")
def test_forcing_the_flock_backend_fails_loudly_without_flock(tmp_path):
    res = _take_lock(_env(tmp_path, "flock"))

    assert res.returncode != 0
    assert "flock(1) is not installed" in res.stderr
