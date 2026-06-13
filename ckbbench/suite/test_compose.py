"""Composer tests: deterministic delivery shape and thin pointer (ADR-0008)."""

from __future__ import annotations

from pathlib import Path

from ckbbench.suite.compose import compose, pointer_prompt, write_instructions
from ckbbench.suite.registry import load_suite
from ckbbench.suite.test_registry import build_registry


def test_compose_is_deterministic_and_ordered(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    first = compose(suite)
    second = compose(suite)
    assert first == second
    assert "numbered list of INDEPENDENT" in first
    assert first.index("Write tip") < first.index("Write epoch")


def test_write_instructions_stable_sha256(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    composed = compose(suite)
    mount = tmp_path / "mount"
    path1, digest1 = write_instructions(composed, mount)
    path2, digest2 = write_instructions(composed, mount)
    assert path1 == mount / "INSTRUCTIONS.md"
    assert path1.read_text() == composed
    assert digest1 == digest2
    assert len(digest1) == 64


def test_pointer_does_not_inline_task_fragments(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    composed = compose(suite)
    inst, _ = write_instructions(composed, tmp_path / "mount")
    pointer = pointer_prompt(inst)
    assert "INSTRUCTIONS.md" in pointer
    assert "Write tip" not in pointer
    assert "Write epoch" not in pointer
    assert "numbered list of independent tasks" in pointer.lower()