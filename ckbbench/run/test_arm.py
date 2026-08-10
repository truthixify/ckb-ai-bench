"""Arm resolution tests: ladder matrix + prompt preamble (RECOMMENDATION §2/§6)."""

from __future__ import annotations

import pytest

from ckbbench.config import ARM_MATRIX, EGRESS_MODE_BY_ARM
from ckbbench.run.arm import resolve_arm


@pytest.mark.parametrize(
    ("arm", "mcp", "web", "egress"),
    [
        ("A", False, False, "block"),
        ("B", False, True, "observe"),
        ("C", True, True, "observe"),
        ("D", True, False, "block"),
    ],
)
def test_arm_matrix_and_egress(arm, mcp, web, egress):
    cfg = resolve_arm(arm)
    assert cfg.arm == arm
    assert cfg.mcp_enabled is mcp
    assert cfg.web_research_allowed is web
    assert cfg.egress_mode == egress
    assert ARM_MATRIX[arm] == (mcp, web)
    assert EGRESS_MODE_BY_ARM[arm] == egress


def test_arm_A_and_D_forbid_web_research():
    for arm in ("A", "D"):
        preamble = resolve_arm(arm).prompt_preamble
        assert "must NOT use web research" in preamble


def test_arm_B_and_C_allow_web_research():
    for arm in ("B", "C"):
        preamble = resolve_arm(arm).prompt_preamble
        assert "may use web research" in preamble


def test_arm_C_and_D_carry_mcp_steering():
    for arm in ("C", "D"):
        preamble = resolve_arm(arm).prompt_preamble
        assert "prefer mcp_call" in preamble
        assert "FALLBACK_RPC" in preamble


def test_no_arm_preamble_names_a_chain():
    """The steering line used to hard-code "CKB/testnet", which handed C/D a chain fact A/B never
    saw and was simply wrong on a DevNet cell. The chain now reaches every arm through the composed
    chain context (plan §8.1), so no preamble may name one."""
    for arm in ("A", "B", "C", "D"):
        preamble = resolve_arm(arm).prompt_preamble.lower()
        assert "testnet" not in preamble
        assert "devnet" not in preamble
        assert "mainnet" not in preamble


def test_arm_A_and_B_have_no_mcp_steering():
    for arm in ("A", "B"):
        preamble = resolve_arm(arm).prompt_preamble
        assert "mcp_call" not in preamble


def test_unknown_arm_raises():
    with pytest.raises(ValueError, match="unknown arm"):
        resolve_arm("Z")