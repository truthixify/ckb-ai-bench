"""Python view of the project-wide advisory lock used by `scripts/lib/lock.sh`.

Same flock(2), same file, so a Python holder and a shell holder exclude each other. Destructive
DevNet work decides that state is disposable by observing its absence; that decision only stays
true while no other project operation can create state behind it, so anything that inventories and
later removes must hold this lock across both.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class ProjectLockBusy(RuntimeError):
    """Another project operation holds the lock."""


def lock_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(runtime) / f"ckbbench-{os.getuid()}"


def lock_file() -> Path:
    return lock_dir() / "project.lock"


def meta_file() -> Path:
    return lock_dir() / "owner.meta"


def owner_pid() -> int | None:
    try:
        for line in meta_file().read_text().splitlines():
            if line.startswith("pid="):
                return int(line[4:].strip())
    except (OSError, ValueError):
        return None
    return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ensure_lock_dir() -> Path:
    directory = lock_dir()
    if directory.is_symlink():
        raise ProjectLockBusy(f"lock dir is a symlink: {directory}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid():
        raise ProjectLockBusy(f"lock dir not owned by current user: {directory}")
    path = lock_file()
    if path.is_symlink():
        raise ProjectLockBusy(f"lock file is a symlink: {path}")
    return path


@contextmanager
def project_lock(label: str = "ckbbench"):
    """Hold the exclusive project lock for the whole block.

    Stale metadata from a dead owner is reclaimed, matching the shell helper. A live owner is never
    displaced: the caller is expected to fail rather than proceed.
    """
    path = _ensure_lock_dir()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pid = owner_pid()
            if pid is not None and not _pid_alive(pid):
                meta_file().unlink(missing_ok=True)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ProjectLockBusy(
                        f"another ckbbench operation holds the project lock (owner pid={pid})"
                    ) from exc
            else:
                raise ProjectLockBusy(
                    "another ckbbench operation holds the project lock "
                    f"(owner pid={pid if pid is not None else 'unknown'}). Try: ./bench unlock"
                ) from None
        meta = meta_file()
        meta.write_text(f"pid={os.getpid()}\ncmd={label}\n")
        os.chmod(meta, 0o600)
        try:
            yield
        finally:
            meta.unlink(missing_ok=True)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
