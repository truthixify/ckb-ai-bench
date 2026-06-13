"""Tests for the harness config / single-source-of-truth constants.

These encode design INTENT (Rule 9): the arm matrix IS the condition ladder, and the headline
C - B delta only means something if the arms differ in exactly the documented way. A test that
could not fail when someone redefines an arm is worthless, so we assert the specific semantics.
"""

from __future__ import annotations

import pytest

from ckbbench import config


def test_arms_are_the_four_ladder_conditions():
    assert config.ARMS == ("A", "B", "C", "D")
    assert set(config.ARM_MATRIX) == set(config.ARMS)


def test_arm_matrix_encodes_the_ladder_semantics():
    # A floor: no MCP, no web. B: no MCP, web. C: MCP + web (headline). D: MCP, no web.
    assert config.ARM_MATRIX["A"] == (False, False)
    assert config.ARM_MATRIX["B"] == (False, True)
    assert config.ARM_MATRIX["C"] == (True, True)
    assert config.ARM_MATRIX["D"] == (True, False)


def test_headline_delta_arms_differ_only_in_mcp():
    # C - B is the headline: the ONLY difference between B and C must be MCP presence.
    # If web-research policy ever diverged between them, the delta would confound MCP with web.
    b_mcp, b_web = config.ARM_MATRIX["B"]
    c_mcp, c_web = config.ARM_MATRIX["C"]
    assert b_web == c_web, "B and C must share web policy so C-B isolates MCP"
    assert b_mcp is False and c_mcp is True, "C-B must vary exactly MCP presence"


def test_diagnostic_arms_differ_only_in_mcp():
    # D - A is the no-web diagnostic: the only difference must be MCP presence.
    a_mcp, a_web = config.ARM_MATRIX["A"]
    d_mcp, d_web = config.ARM_MATRIX["D"]
    assert a_web == d_web, "A and D must share web policy so D-A isolates MCP"
    assert a_mcp is False and d_mcp is True


def test_chain_profiles_resolve_to_distinct_urls():
    assert config.CHAIN_PROFILES == ("devnet", "testnet")
    assert config.rpc_url_for("devnet") != config.rpc_url_for("testnet")


def test_unknown_chain_is_rejected_loudly():
    with pytest.raises(ValueError):
        config.rpc_url_for("mainnet")  # never a profile; must fail loud, not default silently
