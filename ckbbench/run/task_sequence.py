"""Controller-owned, one-at-a-time task delivery for a composed benchmark cell."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SUBMISSION_COMMAND = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
INSTRUCTIONS_FILE = "INSTRUCTIONS.md"
SIGNING_REQUEST_FILE = "SIGNING_REQUEST.json"


class TaskSequenceError(RuntimeError):
    """The task-delivery controller could not preserve its runtime contract."""


class TaskOrderViolation(TaskSequenceError):
    """The agent created a reserved artifact before its task was released."""


@dataclass(frozen=True)
class TaskStage:
    task_id: str
    proof_file: str
    param_filename: str
    prompt_injected: dict[str, Any]
    instructions: str


@dataclass(frozen=True)
class TaskSequenceUpdate:
    advanced: bool
    complete: bool
    message: str = ""


def _relative_path(value: str, *, label: str, flat: bool = False) -> Path:
    pure = PurePosixPath(value)
    if not value or value != value.strip() or pure.is_absolute() or ".." in pure.parts:
        raise TaskSequenceError(f"{label} must be a safe relative path")
    if str(pure) in ("", ".") or (flat and len(pure.parts) != 1):
        raise TaskSequenceError(f"{label} must be a safe relative path")
    return Path(*pure.parts)


class TaskSequenceController:
    """Release one task after each preceding proof path becomes a regular workspace file."""

    def __init__(self, mount_dir: Path | str, stages: tuple[TaskStage, ...]) -> None:
        if not stages:
            raise TaskSequenceError("a task sequence needs at least one stage")
        self.mount = Path(mount_dir).resolve()
        self.stages = stages
        self._proof_paths = tuple(
            _relative_path(stage.proof_file, label=f"{stage.task_id} proof_file")
            for stage in stages
        )
        self._param_paths = tuple(
            _relative_path(stage.param_filename, label=f"{stage.task_id} parameter file", flat=True)
            for stage in stages
        )
        self._validate_targets()
        self._index = 0
        self._started = False

    @property
    def complete(self) -> bool:
        return self._started and self._index == len(self.stages)

    @property
    def current_task_id(self) -> str | None:
        return None if self.complete else self.stages[self._index].task_id

    @property
    def current_proof_file(self) -> str | None:
        return None if self.complete else self.stages[self._index].proof_file

    @property
    def released_task_ids(self) -> tuple[str, ...]:
        released = self._index if self.complete else self._index + int(self._started)
        return tuple(stage.task_id for stage in self.stages[:released])

    def start(self) -> str:
        if self._started:
            raise TaskSequenceError("the task sequence has already started")
        self.mount.mkdir(parents=True, exist_ok=True)
        for path in (
            Path(INSTRUCTIONS_FILE),
            Path(SIGNING_REQUEST_FILE),
            *self._proof_paths,
            *self._param_paths,
        ):
            if self._lexists(path):
                raise TaskSequenceError("the agent workspace contains a reserved task artifact")
        self._publish_stage(0)
        self._started = True
        return INSTRUCTIONS_FILE

    def before_action(self) -> None:
        self._require_started()
        self._refuse_unreleased_artifacts()

    def after_action(self) -> TaskSequenceUpdate:
        self._require_started()
        self._refuse_unreleased_artifacts()
        if self.complete:
            return TaskSequenceUpdate(advanced=False, complete=True)

        proof = self._proof_paths[self._index]
        if not self._lexists(proof):
            return TaskSequenceUpdate(advanced=False, complete=False)
        if not self._is_regular_workspace_file(proof):
            raise TaskOrderViolation("the current proof path is not a regular workspace file")

        completed = self.stages[self._index]
        self._index += 1
        if self.complete:
            return TaskSequenceUpdate(
                advanced=True,
                complete=True,
                message=(
                    f"[benchmark] Observed the Proof file for {completed.task_id}. "
                    "All tasks have been released; submit when ready."
                ),
            )

        self._publish_stage(self._index)
        following = self.stages[self._index]
        return TaskSequenceUpdate(
            advanced=True,
            complete=False,
            message=(
                f"[benchmark] Observed the Proof file for {completed.task_id}. "
                f"The next task is {following.task_id}; read {INSTRUCTIONS_FILE} before acting."
            ),
        )

    def _validate_targets(self) -> None:
        targets = (
            Path(INSTRUCTIONS_FILE),
            Path(SIGNING_REQUEST_FILE),
            *self._proof_paths,
            *self._param_paths,
        )
        if len(set(targets)) != len(targets):
            raise TaskSequenceError("task delivery paths must be unique")
        for index, left in enumerate(targets):
            for right in targets[index + 1 :]:
                if left in right.parents or right in left.parents:
                    raise TaskSequenceError("task delivery paths must not contain one another")

    def _require_started(self) -> None:
        if not self._started:
            raise TaskSequenceError("the task sequence has not started")

    def _lexists(self, relative: Path) -> bool:
        return os.path.lexists(self.mount / relative)

    def _refuse_unreleased_artifacts(self) -> None:
        if self.complete:
            return
        for path in (*self._proof_paths[self._index + 1 :], *self._param_paths[self._index + 1 :]):
            if self._lexists(path):
                raise TaskOrderViolation("an artifact exists for a task that has not been released")

    def _is_regular_workspace_file(self, relative: Path) -> bool:
        path = self.mount / relative
        current = self.mount
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink():
                return False
        try:
            mode = path.lstat().st_mode
            resolved = path.resolve(strict=True)
        except OSError:
            return False
        return stat.S_ISREG(mode) and (resolved == self.mount or self.mount in resolved.parents)

    def _publish_stage(self, index: int) -> None:
        stage = self.stages[index]
        self._write_exclusive_json(self._param_paths[index], stage.prompt_injected)
        self._replace_instructions(stage.instructions)

    def _write_exclusive_json(self, relative: Path, value: dict[str, Any]) -> None:
        path = self.mount / relative
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _replace_instructions(self, text: str) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".instructions-", dir=self.mount)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
            temp.chmod(0o644)
            os.replace(temp, self.mount / INSTRUCTIONS_FILE)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
