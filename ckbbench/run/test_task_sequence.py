from __future__ import annotations

import json
from pathlib import Path

import pytest

from ckbbench.run.task_sequence import (
    INSTRUCTIONS_FILE,
    TaskOrderViolation,
    TaskSequenceController,
    TaskSequenceError,
    TaskStage,
)


def _stages() -> tuple[TaskStage, ...]:
    return (
        TaskStage(
            task_id="task-a",
            proof_file="proof-a.txt",
            param_filename="task-a.json",
            prompt_injected={"value": "a"},
            instructions="FIRST ONLY\n",
        ),
        TaskStage(
            task_id="task-b",
            proof_file="build/release/proof-b",
            param_filename="task-b.json",
            prompt_injected={"value": "b"},
            instructions="SECOND ONLY\n",
        ),
    )


def _controller(tmp_path: Path) -> TaskSequenceController:
    return TaskSequenceController(tmp_path / "mount", _stages())


def test_start_exposes_only_the_first_stage(tmp_path: Path):
    controller = _controller(tmp_path)
    assert controller.start() == INSTRUCTIONS_FILE
    mount = controller.mount

    assert (mount / INSTRUCTIONS_FILE).read_text() == "FIRST ONLY\n"
    assert json.loads((mount / "task-a.json").read_text()) == {"value": "a"}
    assert not (mount / "task-b.json").exists()
    assert not (mount / "build/release/proof-b").exists()
    assert controller.current_task_id == "task-a"
    assert controller.released_task_ids == ("task-a",)


def test_each_regular_proof_releases_exactly_one_following_stage(tmp_path: Path):
    controller = _controller(tmp_path)
    controller.start()
    mount = controller.mount

    (mount / "proof-a.txt").write_text("proof")
    first = controller.after_action()
    assert first.advanced is True and first.complete is False
    assert controller.current_task_id == "task-b"
    assert controller.released_task_ids == ("task-a", "task-b")
    assert (mount / INSTRUCTIONS_FILE).read_text() == "SECOND ONLY\n"
    assert json.loads((mount / "task-b.json").read_text()) == {"value": "b"}

    proof = mount / "build/release/proof-b"
    proof.parent.mkdir(parents=True)
    proof.write_bytes(b"binary")
    final = controller.after_action()
    assert final.advanced is True and final.complete is True
    assert controller.current_task_id is None
    assert controller.current_proof_file is None


@pytest.mark.parametrize("reserved", ["task-b.json", "build/release/proof-b"])
def test_an_unreleased_reserved_artifact_fails_the_sequence(tmp_path: Path, reserved: str):
    controller = _controller(tmp_path)
    controller.start()
    planted = controller.mount / reserved
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("early")

    with pytest.raises(TaskOrderViolation, match="has not been released"):
        controller.before_action()


def test_one_action_cannot_skip_across_two_proofs(tmp_path: Path):
    controller = _controller(tmp_path)
    controller.start()
    (controller.mount / "proof-a.txt").write_text("first")
    second = controller.mount / "build/release/proof-b"
    second.parent.mkdir(parents=True)
    second.write_text("second")

    with pytest.raises(TaskOrderViolation):
        controller.after_action()
    assert controller.released_task_ids == ("task-a",)


def test_a_symlink_cannot_satisfy_the_current_proof(tmp_path: Path):
    controller = _controller(tmp_path)
    controller.start()
    target = controller.mount / "other.txt"
    target.write_text("not the proof")
    (controller.mount / "proof-a.txt").symlink_to(target)

    with pytest.raises(TaskOrderViolation, match="regular workspace file"):
        controller.after_action()


def test_start_refuses_preexisting_managed_state(tmp_path: Path):
    controller = _controller(tmp_path)
    controller.mount.mkdir(parents=True)
    (controller.mount / "task-b.json").write_text("planted")

    with pytest.raises(TaskSequenceError, match="reserved task artifact"):
        controller.start()


def test_start_is_one_use_and_instruction_replacements_leave_no_temp_files(tmp_path: Path):
    controller = _controller(tmp_path)
    controller.start()
    with pytest.raises(TaskSequenceError, match="already started"):
        controller.start()

    (controller.mount / "proof-a.txt").write_text("proof")
    controller.after_action()
    assert list(controller.mount.glob(".instructions-*")) == []


@pytest.mark.parametrize(
    "stage",
    [
        TaskStage("a", "../proof", "a.json", {}, "x"),
        TaskStage("a", "proof", "nested/a.json", {}, "x"),
    ],
)
def test_unsafe_managed_paths_are_refused_before_publication(tmp_path: Path, stage: TaskStage):
    with pytest.raises(TaskSequenceError, match="safe relative path"):
        TaskSequenceController(tmp_path / "mount", (stage,))
