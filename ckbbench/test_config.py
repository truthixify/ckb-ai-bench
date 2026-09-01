"""Tests for the harness config / single-source-of-truth constants.

These encode design INTENT (Rule 9): the arm matrix IS the condition ladder, and the headline
C - B delta only means something if the arms differ in exactly the documented way. A test that
could not fail when someone redefines an arm, drops an env override, or silently changes a
pinned infra value is worthless, so we assert the specific semantics AND the live defaults.
"""

from __future__ import annotations

import importlib

import pytest

from ckbbench import config


# --- the condition ladder (the headline-bearing invariants) ---------------------------------

def test_arms_are_the_four_ladder_conditions():
    assert config.ARMS == ("A", "B", "C", "D")
    assert set(config.ARM_MATRIX) == set(config.ARMS)
    assert config.LADDER_ORDER == config.ARMS  # X-axis order for the ladder chart (ADR-0011)


def test_headline_delta_C_minus_B_isolates_mcp():
    # C - B is the headline: the ONLY difference between B and C must be MCP presence.
    # If web-research policy ever diverged between them, the delta would confound MCP with web.
    b_mcp, b_web = config.ARM_MATRIX["B"]
    c_mcp, c_web = config.ARM_MATRIX["C"]
    assert b_web == c_web, "B and C must share web policy so C-B isolates MCP"
    assert b_mcp is False and c_mcp is True, "C-B must vary exactly MCP presence"


def test_diagnostic_delta_D_minus_A_isolates_mcp():
    a_mcp, a_web = config.ARM_MATRIX["A"]
    d_mcp, d_web = config.ARM_MATRIX["D"]
    assert a_web == d_web, "A and D must share web policy so D-A isolates MCP"
    assert a_mcp is False and d_mcp is True


def test_delta_B_minus_A_isolates_web():
    # B - A is "what web research buys": the only difference must be the web flag.
    a_mcp, a_web = config.ARM_MATRIX["A"]
    b_mcp, b_web = config.ARM_MATRIX["B"]
    assert a_mcp == b_mcp, "A and B must share MCP policy so B-A isolates web"
    assert a_web is False and b_web is True


def test_arm_matrix_pairs_are_unique():
    # Four arms must occupy four distinct (mcp, web) cells; a collision would make a delta meaningless.
    pairs = list(config.ARM_MATRIX.values())
    assert len(set(pairs)) == 4


def test_arm_matrix_is_immutable():
    # A later phase must not be able to corrupt the ladder by reassigning an entry at run time.
    with pytest.raises(TypeError):
        config.ARM_MATRIX["C"] = (False, False)  # type: ignore[index]


# --- egress policy is an explicit invariant, not derived from the web flag (ADR-0006) -------

def test_egress_blocks_exactly_the_no_research_arms():
    # A/D are network-blocked to an allowlist; B/C observe web. This is the HARD control behind
    # the no-research arms; it must track the no-web arms exactly.
    no_web = {a for a, (_, web) in config.ARM_MATRIX.items() if not web}
    blocked = {a for a, mode in config.EGRESS_MODE_BY_ARM.items() if mode == "block"}
    assert blocked == no_web == {"A", "D"}
    assert set(config.EGRESS_MODE_BY_ARM.values()) == {"block", "observe"}


# --- chain profiles -------------------------------------------------------------------------

def test_chain_profiles_resolve_to_distinct_urls():
    assert config.CHAIN_PROFILES == ("devnet", "testnet")
    assert config.rpc_url_for("devnet") != config.rpc_url_for("testnet")


def test_unknown_chain_is_rejected_loudly():
    with pytest.raises(ValueError, match="unknown chain profile"):
        config.rpc_url_for("mainnet")  # never a profile; must fail loud, not default silently


# --- live infra defaults are pinned (so a silent edit to a garbage host is caught) ----------

def test_default_infra_pins_match_the_documented_values():
    # These are the live defaults documented in .env.example / docs. Changing one is a real
    # decision that should break this test, not slip through unnoticed.
    assert config.MCP_PINNED_VERSION == "1.6.13"
    assert config.MCP_URL == "https://mcp.ckbdev.com/ckbai"
    assert config.TESTNET_RPC == "http://192.168.0.73:18114"
    assert config.DEVNET_RPC == "http://127.0.0.1:8114"
    assert config.LLM_API_BASE == "http://localhost:18321/v1"


# --- env-override contract: the new name wins, and the legacy name is honored ----------------

def test_new_env_name_overrides_default(monkeypatch):
    monkeypatch.setenv("CKBBENCH_MCP_VERSION", "9.9.9")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.MCP_PINNED_VERSION == "9.9.9"
    finally:
        monkeypatch.delenv("CKBBENCH_MCP_VERSION", raising=False)
        importlib.reload(config)


def test_legacy_env_name_is_honored_as_fallback(monkeypatch):
    # The spikes/agent use MCP_URL (no CKBBENCH_ prefix); config must read it so Phase 4 does
    # not silently diverge from the rest of the codebase.
    monkeypatch.delenv("CKBBENCH_MCP_URL", raising=False)
    monkeypatch.setenv("MCP_URL", "http://legacy.example/mcp")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.MCP_URL == "http://legacy.example/mcp"
    finally:
        monkeypatch.delenv("MCP_URL", raising=False)
        importlib.reload(config)


AGENT_PIN = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
VERIFIER_PIN = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_role_pins_resolve_to_the_exact_local_image_id(monkeypatch):
    """A local image ID is passed to Docker verbatim, never rebuilt into name@sha256:..."""
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    monkeypatch.delenv("CKBBENCH_VERIFIER_IMAGE", raising=False)
    assert config.resolve_agent_image(agent_pin=AGENT_PIN) == AGENT_PIN
    assert config.resolve_verifier_image(verifier_pin=VERIFIER_PIN) == VERIFIER_PIN
    assert "@" not in config.resolve_agent_image(agent_pin=AGENT_PIN)
    assert "@" not in config.resolve_verifier_image(verifier_pin=VERIFIER_PIN)


def test_role_pins_stay_distinct(monkeypatch):
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    monkeypatch.delenv("CKBBENCH_VERIFIER_IMAGE", raising=False)
    assert config.resolve_agent_image(agent_pin=AGENT_PIN) != config.resolve_verifier_image(
        verifier_pin=VERIFIER_PIN
    )


def test_each_role_env_override_wins_independently(monkeypatch):
    monkeypatch.delenv("CKBBENCH_VERIFIER_IMAGE", raising=False)
    monkeypatch.setenv("CKBBENCH_AGENT_IMAGE", "override-agent:1")
    assert config.resolve_agent_image(agent_pin=AGENT_PIN) == "override-agent:1"
    assert config.resolve_verifier_image(verifier_pin=VERIFIER_PIN) == VERIFIER_PIN
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    monkeypatch.setenv("CKBBENCH_VERIFIER_IMAGE", "override-verifier:1")
    assert config.resolve_agent_image(agent_pin=AGENT_PIN) == AGENT_PIN
    assert config.resolve_verifier_image(verifier_pin=VERIFIER_PIN) == "override-verifier:1"


def test_absent_pins_keep_the_development_defaults(monkeypatch):
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    monkeypatch.delenv("CKBBENCH_VERIFIER_IMAGE", raising=False)
    assert config.resolve_agent_image() == "ckbbench-agent:latest"
    assert config.resolve_verifier_image() == "ckbbench-verifier:latest"


@pytest.mark.parametrize(
    "bad",
    [
        "TO_BE_FILLED",
        "latest",
        "ckbbench-agent:latest",
        "sha256:abc123",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "ckbbench-agent@sha256:" + "a" * 64,
        "sha256:" + "0" * 64 + "extra",
    ],
)
def test_a_malformed_role_pin_fails_closed(monkeypatch, bad):
    """Never silently fall back to `latest`: the freeze would claim an immutable image."""
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    monkeypatch.delenv("CKBBENCH_VERIFIER_IMAGE", raising=False)
    with pytest.raises(ValueError):
        config.resolve_agent_image(agent_pin=bad)
    with pytest.raises(ValueError):
        config.resolve_verifier_image(verifier_pin=bad)


def test_a_non_string_role_pin_fails_closed(monkeypatch):
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    with pytest.raises(ValueError):
        config.resolve_agent_image(agent_pin=123)


def test_image_and_testnet_privkey_env_overrides(monkeypatch):
    monkeypatch.setenv("CKBBENCH_AGENT_IMAGE", "my-agent@sha256:abc")
    monkeypatch.setenv("CKBBENCH_VERIFIER_IMAGE", "my-verifier@sha256:def")
    monkeypatch.setenv("CKBBENCH_TESTNET_SENDER_PRIVKEY", "0xdeadbeef")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.AGENT_IMAGE == "my-agent@sha256:abc"
        assert reloaded.VERIFIER_IMAGE == "my-verifier@sha256:def"
        assert reloaded.TESTNET_SENDER_PRIVKEY == "0xdeadbeef"
    finally:
        monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
        monkeypatch.delenv("CKBBENCH_VERIFIER_IMAGE", raising=False)
        monkeypatch.delenv("CKBBENCH_TESTNET_SENDER_PRIVKEY", raising=False)
        importlib.reload(config)


def test_new_name_wins_over_legacy_name(monkeypatch):
    monkeypatch.setenv("MCP_URL", "http://legacy.example/mcp")
    monkeypatch.setenv("CKBBENCH_MCP_URL", "http://preferred.example/mcp")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.MCP_URL == "http://preferred.example/mcp"
    finally:
        monkeypatch.delenv("MCP_URL", raising=False)
        monkeypatch.delenv("CKBBENCH_MCP_URL", raising=False)
        importlib.reload(config)


def test_a_profile_resolves_the_declared_generic_credential(monkeypatch):
    monkeypatch.setenv("CKBBENCH_LLM_API_KEY", "generic-key")
    assert config.resolve_llm_api_key("CKBBENCH_LLM_API_KEY") == "generic-key"


def test_a_profile_never_falls_back_to_the_legacy_development_key(monkeypatch):
    monkeypatch.delenv("CKBBENCH_LLM_API_KEY", raising=False)
    monkeypatch.setenv("BENCH_API_KEY", "legacy-key")
    assert config.resolve_llm_api_key("CKBBENCH_LLM_API_KEY", default="missing") == "missing"


def test_a_profile_cannot_select_an_arbitrary_environment_secret(monkeypatch):
    monkeypatch.setenv("OTHER_API_KEY", "other-key")
    with pytest.raises(ValueError, match="unsupported credential channel"):
        config.resolve_llm_api_key("OTHER_API_KEY", default="missing")


def test_the_unprofiled_development_resolver_keeps_the_legacy_fallback(monkeypatch):
    monkeypatch.delenv("CKBBENCH_LLM_API_KEY", raising=False)
    monkeypatch.setenv("BENCH_API_KEY", "legacy-key")
    assert config.resolve_llm_api_key() == "legacy-key"


def test_all_zero_placeholder_pin_fails_closed_at_the_resolver(monkeypatch):
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    monkeypatch.delenv("CKBBENCH_VERIFIER_IMAGE", raising=False)
    null_pin = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="all-zero"):
        config.resolve_agent_image(agent_pin=null_pin)
    with pytest.raises(ValueError, match="all-zero"):
        config.resolve_verifier_image(verifier_pin=null_pin)
