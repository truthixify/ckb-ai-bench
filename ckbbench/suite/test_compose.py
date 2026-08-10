"""Composer tests: deterministic delivery shape and thin pointer (ADR-0008)."""

from __future__ import annotations

from pathlib import Path

from ckbbench.suite.compose import (
    chain_context_text,
    compose,
    pointer_prompt,
    write_instructions,
)
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


def test_chain_context_names_the_chain_and_the_env_var(tmp_path: Path):
    """The endpoint is NAMED, not inlined: the prompt and the agent environment must not be able
    to drift apart, and one cell's URL must never be frozen into the suite."""
    for chain in ("devnet", "testnet"):
        text = chain_context_text(chain)
        assert f"CKB {chain} chain" in text
        assert "CKB_RPC_URL" in text
        assert "CKBBENCH_CHAIN_PROFILE" in text
        assert "http://" not in text


def test_compose_places_chain_context_between_base_preamble_and_arm_slot(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    text = compose(
        suite, extra_preamble="ARM POLICY LINE", chain_context=chain_context_text("devnet")
    )
    assert text.index("numbered list of INDEPENDENT") < text.index("CKB devnet chain")
    assert text.index("CKB devnet chain") < text.index("ARM POLICY LINE")
    assert text.index("ARM POLICY LINE") < text.index("Write tip")


def test_compose_without_chain_context_is_unchanged(tmp_path: Path):
    """Composition stays usable (and hashable) without run-time chain facts."""
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    assert compose(suite) == compose(suite, chain_context="   ")
    assert "CKB_RPC_URL" not in compose(suite)


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