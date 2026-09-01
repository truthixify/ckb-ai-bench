"""Composer tests: deterministic delivery shape and thin pointer (ADR-0008)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from ckbbench.suite.compose import (
    chain_context_text,
    compose,
    compose_stage,
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
    assert "Canonical review view" in first
    assert "releases these tasks one at a time" in first
    assert "any order" not in first
    assert first.index("Write the constant") < first.index("Write epoch")


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


def test_local_hermetic_context_exposes_no_live_chain_capability():
    text = chain_context_text("local-hermetic")
    assert "local hermetic workspace" in text
    assert "CKB_RPC_URL" not in text
    assert "PRIVKEY" not in text
    assert "no live chain" in text


def test_broker_context_names_public_policy_but_never_a_key_variable():
    text = chain_context_text("testnet", broker_bound=True)
    assert "SIGNING_POLICY.json" in text
    assert "CKB_RPC_URL" in text
    assert "no private key" in text
    assert "PRIVKEY" not in text

    with pytest.raises(ValueError, match="cannot carry"):
        chain_context_text("local-hermetic", broker_bound=True)


def test_compose_places_chain_context_between_base_preamble_and_arm_slot(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    text = compose(
        suite, extra_preamble="ARM POLICY LINE", chain_context=chain_context_text("devnet")
    )
    assert text.index("Canonical review view") < text.index("CKB devnet chain")
    assert text.index("CKB devnet chain") < text.index("ARM POLICY LINE")
    assert text.index("ARM POLICY LINE") < text.index("Write the constant")


def _named_env_tokens(text: str) -> set[str]:
    """Exact UPPER_SNAKE tokens named in the prompt.

    Substring membership is unsafe here: BENCH_TESTNET_SENDER_PRIVKEY is a substring of
    CKBBENCH_TESTNET_SENDER_PRIVKEY, so `name in text` would report the legacy name as named
    whenever only the preferred one appears.
    """
    return set(re.findall(r"[A-Z][A-Z0-9_]{5,}", text))


@pytest.mark.parametrize(
    ("chain", "runtime_docker"),
    [("devnet", True), ("testnet", True), ("devnet", False), ("testnet", False)],
)
def test_chain_context_only_names_signers_the_chain_can_carry(monkeypatch, chain, runtime_docker):
    """The prompt must not point an agent at a variable its chain never uses: a TestNet cell has
    no CKB_SENDER_PRIVKEY, and a DevNet cell has no operator TestNet key."""
    from ckbbench.run import agent_factory

    monkeypatch.setattr(agent_factory, "use_docker", lambda: runtime_docker)
    may_carry = set(agent_factory.signer_env_for(chain)) | set(
        agent_factory.testnet_forward_env(chain)
    )

    named = _named_env_tokens(chain_context_text(chain)) & set(agent_factory.SIGNER_ENV_NAMES)

    assert named, "the context must name a signer variable so an agent can find one"
    assert named <= may_carry, f"{named - may_carry} is named for {chain} but that chain never sets it"


@pytest.mark.parametrize(
    ("exported", "label"),
    [
        (("CKBBENCH_TESTNET_SENDER_PRIVKEY",), "preferred-only"),
        (("BENCH_TESTNET_SENDER_PRIVKEY",), "legacy-only"),
        (("CKBBENCH_TESTNET_SENDER_PRIVKEY", "BENCH_TESTNET_SENDER_PRIVKEY"), "both"),
    ],
)
def test_testnet_context_finds_the_key_whichever_operator_name_is_set(monkeypatch, exported, label):
    """Both operator names are supported, and docker forwards only the ones actually exported.
    Naming just the preferred one left a legacy-only operator's agent hunting an unset variable.
    Sentinels only -- no real key is used."""
    from ckbbench.run import agent_factory

    for name in agent_factory.SIGNER_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name in exported:
        monkeypatch.setenv(name, f"SENTINEL_{label}")
    forwarded = {
        name for name in agent_factory.testnet_forward_env("testnet") if os.environ.get(name)
    }

    named = _named_env_tokens(chain_context_text("testnet")) & set(agent_factory.SIGNER_ENV_NAMES)

    assert named & forwarded, f"{label}: the prompt names {named}, none of which the host provides"


def test_sdk_home_is_named_only_as_a_container_facility():
    """CKB_SDK_HOME is an image contract; a local (non-container) cell never defines it, so the
    prompt must not assert that it exists."""
    from ckbbench.run import agent_factory

    for chain in ("devnet", "testnet"):
        text = chain_context_text(chain)
        if agent_factory.SDK_HOME_ENV in _named_env_tokens(text):
            assert "inside the benchmark container" in text


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
    assert "Write the constant" not in pointer
    assert "Write epoch" not in pointer
    assert "first task released" in pointer
    assert "replace the file" in pointer
    assert "same session" in pointer


def test_stage_composition_reveals_exactly_one_task(tmp_path: Path):
    suite = load_suite(build_registry(tmp_path / "reg"))
    first = compose_stage(suite, 0)
    second = compose_stage(suite, 1)

    assert "Task 1 of 2: task-a" in first
    assert "Write the constant" in first
    assert "Write epoch" not in first
    assert "Do not submit yet" in first
    assert "Task 2 of 2: task-b" in second
    assert "Write epoch" in second
    assert "Write the constant" not in second
    assert "This is the final task" in second


@pytest.mark.parametrize("stage_index", [-1, 2])
def test_stage_composition_refuses_an_out_of_range_index(tmp_path: Path, stage_index: int):
    suite = load_suite(build_registry(tmp_path / "reg"))
    with pytest.raises(IndexError):
        compose_stage(suite, stage_index)
