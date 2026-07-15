"""Post-run cleanup for docker instances, volumes, and host run dirs.

Default: delete ephemeral resources after each cell (agent container, work volume,
owned host mount tree, temp allowlists) and any leftover cargo volume after a matrix
launch. Set ``CKBBENCH_KEEP=1`` or pass ``keep=True`` / ``--keep`` to leave everything
for debugging.

Safety: only removes docker resources whose names start with ``ckbbench-``.
Grade prepare uses checked stop/remove (``stop_agent_checked``) — not fail-open.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ckbbench.run.runner import DEFAULT_CARGO_VOLUME, DEFAULT_WORK_VOLUME, PrepareError

# Injectable seam: argv -> (exit_code, combined output). Same shape as runner.SubprocessSeam.
SubprocessSeam = Callable[[Sequence[str]], tuple[int, str]]

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def keep_resources(*, keep: bool | None = None) -> bool:
    """Return True when cleanup must be skipped (debug leave-behind)."""
    if keep is not None:
        return keep
    return os.getenv("CKBBENCH_KEEP", "0").strip().lower() in _TRUTHY


def _default_run(argv: Sequence[str]) -> tuple[int, str]:
    """Best-effort cleanup seam: never raise (post-result finally must not hide outcomes)."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except (FileNotFoundError, OSError) as exc:
        return 1, str(exc)
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
    """Force-remove a container (sync). Best-effort if already gone (post-run cleanup)."""
    if not container_id:
        return
    seam = run or _default_run
    seam(["docker", "rm", "-f", container_id])


def docker_rm_volume(
    name: str,
    *,
    run: SubprocessSeam | None = None,
) -> None:
    """Remove a named docker volume (best-effort cleanup). Name must start with ``ckbbench-``."""
    assert_ckbbench_name(name, "volume")
    seam = run or _default_run
    seam(["docker", "volume", "rm", "-f", name])


def stop_agent_checked(
    agent: Any,
    *,
    run: SubprocessSeam | None = None,
) -> None:
    """Stop/remove the agent container before grade; raise PrepareError if still present.

    No-op when there is no container_id (unit tests / non-docker path).
    """
    env = getattr(agent, "env", None)
    if env is None:
        return
    container_id = getattr(env, "container_id", None)
    if not container_id:
        cleanup_fn = getattr(env, "cleanup", None)
        if callable(cleanup_fn):
            cleanup_fn()
        return
    seam = run or _default_run
    try:
        rm_code, rm_out = seam(["docker", "rm", "-f", container_id])
        inspect_code, inspect_out = seam(["docker", "inspect", container_id])
    except OSError as exc:
        raise PrepareError(f"agent stop OS error: {exc}") from exc
    if inspect_code == 0:
        raise PrepareError(
            f"agent container {container_id!r} still present after stop "
            f"(rm exit {rm_code}): {rm_out.strip()}"
        )
    # Fail closed: only treat as gone when inspect clearly says the object is missing.
    # Daemon/permission errors must not clear the id and continue to grade.
    from ckbbench.run.runner import _docker_resource_absent

    if not _docker_resource_absent(inspect_code, inspect_out):
        # Best-effort seam may return exit 1 with no "No such" text when docker is missing.
        if "No such" not in inspect_out and "not found" not in inspect_out.lower():
            if "failed to execute" in inspect_out.lower() or "no such file" in inspect_out.lower():
                raise PrepareError(f"agent stop cannot run docker: {inspect_out.strip()}")
        raise PrepareError(
            f"cannot verify agent container {container_id!r} stopped "
            f"(rm exit {rm_code}, inspect exit {inspect_code}): {inspect_out.strip()}"
        )
    try:
        env.container_id = None
    except Exception:
        pass


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
