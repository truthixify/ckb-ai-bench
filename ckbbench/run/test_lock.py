"""Tests for the Python view of the shared project lock (ckbbench/run/lock.py, plan §9.1).

The point of the module is that it is not a second lock: a Python holder and a shell holder must
exclude each other, or a destructive proof could run beside `./bench up`.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from ckbbench.run.lock import ProjectLockBusy, lock_file, meta_file, owner_pid, project_lock

REPO = Path(__file__).resolve().parents[2]
LOCK_LIB = REPO / "scripts" / "lib" / "lock.sh"


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path


def test_the_lock_is_held_for_the_block_and_released_after(isolated_runtime):
    with project_lock("unit"):
        assert lock_file().is_file()
        assert owner_pid() == os.getpid()
        with pytest.raises(ProjectLockBusy, match="holds the project lock"):
            with project_lock("second"):
                pass
    assert not meta_file().exists(), "owner metadata must not outlive the operation"
    with project_lock("again"):
        pass


def test_a_shell_holder_excludes_the_python_holder(isolated_runtime):
    """Same flock(2) on the same file: the shell CLI and this module are one lock, not two."""
    script = textwrap.dedent(f"""\
        set -euo pipefail
        source "{LOCK_LIB}"
        with_lock "shell-side"
        echo READY
        sleep 30
    """)
    proc = subprocess.Popen(
        ["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "XDG_RUNTIME_DIR": str(isolated_runtime)},
    )
    try:
        assert proc.stdout.readline().strip() == "READY"
        with pytest.raises(ProjectLockBusy) as excinfo:
            with project_lock("python-side"):
                pass
        assert str(proc.pid) in str(excinfo.value)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_python_holder_excludes_the_shell_holder(isolated_runtime):
    with project_lock("python-side"):
        res = subprocess.run(
            ["bash", "-c", f'source "{LOCK_LIB}"\nwith_lock "shell-side"'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "XDG_RUNTIME_DIR": str(isolated_runtime)},
        )
    assert res.returncode != 0
    assert "another ckbbench operation holds the lock" in res.stderr


def test_metadata_from_a_dead_owner_is_reclaimed(isolated_runtime):
    """A crashed operation must not wedge the project; a LIVE one still must not be displaced."""
    dead = subprocess.Popen(["true"])
    dead.wait()
    meta_file().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta_file().write_text(f"pid={dead.pid}\ncmd=crashed\n")
    with project_lock("recovering"):
        assert owner_pid() == os.getpid()


def test_a_symlinked_lock_path_is_refused(tmp_path: Path, monkeypatch):
    """Matching the shell helper: a redirected lock path is never followed."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (runtime / f"ckbbench-{os.getuid()}").symlink_to(elsewhere)
    with pytest.raises(ProjectLockBusy, match="symlink"):
        with project_lock("unit"):
            pass
