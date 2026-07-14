"""Post-run cleanup for docker instances, volumes, and host run dirs.

Default: delete ephemeral resources after each cell (agent container, work volume,
owned host mount tree, temp allowlists) and the shared cargo volume after a matrix
launch. Set ``CKBBENCH_KEEP=1`` or pass ``keep=True`` / ``--keep`` to leave everything
for debugging.

Safety: only removes docker resources whose names start with ``ckbbench-``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ckbbench.run.runner import DEFAULT_CARGO_VOLUME, DEFAULT_WORK_VOLUME

# Injectable seam: argv -> (exit_code, combined output). Same shape as runner.SubprocessSeam.
SubprocessSeam = Callable[[Sequence[str]], tuple[int, str]]

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def keep_resources(*, keep: bool | None = None) -> bool:
    """Return True when cleanup must be skipped (debug leave-behind)."""
    if keep is not None:
        return keep
    return os.getenv("CKBBENCH_KEEP", "0").strip().lower() in _TRUTHY


def _default_run(argv: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def assert_ckbbench_name(name: str, kind: str) -> None:
    """Refuse to remove non-ckbbench resources (containers/README safety rule)."""
    if not name or not name.startswith("ckbbench-"):
        raise ValueError(f"refusing to remove non-ckbbench {kind}: {name!r}")


def docker_rm_container(
    container_id: str,
    *,
    run: SubprocessSeam | None = None,
) -> None:
    """Force-remove a container (sync). Best-effort if already gone."""
    if not container_id:
        return
    seam = run or _default_run
    seam(["docker", "rm", "-f", container_id])


def docker_rm_volume(
    name: str,
    *,
    run: SubprocessSeam | None = None,
) -> None:
    """Remove a named docker volume. Name must start with ``ckbbench-``."""
    assert_ckbbench_name(name, "volume")
    seam = run or _default_run
    seam(["docker", "volume", "rm", "-f", name])


def cleanup_agent(
    agent: Any,
    *,
    run: SubprocessSeam | None = None,
) -> None:
    """Stop/remove the agent docker container if present; clear id to avoid double __del__."""
    env = getattr(agent, "env", None)
    if env is None:
        return
    container_id = getattr(env, "container_id", None)
    if container_id:
        docker_rm_container(container_id, run=run)
        try:
            env.container_id = None
        except Exception:
            pass
        return
    cleanup_fn = getattr(env, "cleanup", None)
    if callable(cleanup_fn):
        cleanup_fn()


def rm_host_path(path: Path | str) -> None:
    """Remove a file or directory tree; ignore missing paths."""
    p = Path(path)
    if not p.exists():
        return
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass
class CellCleanupTargets:
    """Resources owned by one matrix cell that cleanup may delete."""

    agent: Any | None = None
    work_volume: str | None = None
    host_run_dir: Path | None = None
    extra_paths: tuple[Path, ...] = field(default_factory=tuple)


def cleanup_cell(
    targets: CellCleanupTargets,
    *,
    keep: bool | None = None,
    run: SubprocessSeam | None = None,
) -> None:
    """Tear down per-cell resources unless ``keep`` / ``CKBBENCH_KEEP`` is set."""
    if keep_resources(keep=keep):
        return
    if targets.agent is not None:
        cleanup_agent(targets.agent, run=run)
    if targets.work_volume:
        try:
            docker_rm_volume(targets.work_volume, run=run)
        except ValueError:
            # Misconfigured non-ckbbench name: skip rather than raise mid-finally.
            pass
    if targets.host_run_dir is not None:
        rm_host_path(targets.host_run_dir)
    for path in targets.extra_paths:
        rm_host_path(path)


def cleanup_matrix_volumes(
    *,
    cargo_volume: str | None = None,
    keep: bool | None = None,
    run: SubprocessSeam | None = None,
) -> None:
    """Remove shared volumes after a matrix launch (cargo cache). Work volume is per-cell."""
    if keep_resources(keep=keep):
        return
    name = cargo_volume or os.getenv("CKBBENCH_CARGO_VOLUME", DEFAULT_CARGO_VOLUME)
    try:
        docker_rm_volume(name, run=run)
    except ValueError:
        pass


def resolve_work_volume(*, explicit: str | None = None) -> str | None:
    """Work volume to clean after a docker cell; None when docker path is unused."""
    if explicit is not None:
        return explicit
    if os.getenv("CKBBENCH_DOCKER", "0") != "1":
        return None
    return os.getenv("CKBBENCH_WORK_VOLUME", DEFAULT_WORK_VOLUME)
