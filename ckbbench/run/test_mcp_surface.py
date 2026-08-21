"""Surface-policy tests: the exact allowlist must fail closed (ADR-0013).

The policy is the single source of truth for what a model may reach through the MCP controller, so
an unknown arm, profile, tool or resource must be a refusal rather than a permissive default.
"""

from __future__ import annotations

import pytest

from ckbbench.run.mcp_surface import (
    DOCS_ONLY_TOOLS,
    DOCS_RESOURCE_PREFIX,
    PROFILE_BY_ARM,
    PROFILE_DOCS_ONLY,
    PROFILE_OFF,
    SURFACE_PROFILES,
    McpSurfaceError,
    McpSurfacePolicy,
    normalize_catalog,
    policy_for_arm,
    policy_for_profile,
    profile_for_arm,
)

DOCS = policy_for_profile(PROFILE_DOCS_ONLY)
OFF = policy_for_profile(PROFILE_OFF)


def test_the_fixed_treatment_is_exactly_the_ladder_contract():
    assert dict(PROFILE_BY_ARM) == {
        "A": "off", "B": "off", "C": "docs-only-v1", "D": "docs-only-v1"
    }
    assert SURFACE_PROFILES == {"off", "docs-only-v1"}
    assert DOCS_ONLY_TOOLS == {"search_resources"}
    assert DOCS_RESOURCE_PREFIX == "ckb://docs/"


def test_the_docs_profile_allows_exactly_one_tool():
    assert DOCS.allows_tool("search_resources")
    for other in ("search_tools", "rpc_get_tip_block_number", "dev_faucet_claim",
                  "ckb_query_address", "a_tool_added_next_year", "", None, 7):
        assert not DOCS.allows_tool(other)


def test_the_off_profile_allows_nothing():
    assert not OFF.enabled
    assert not OFF.allows_tool("search_resources")
    assert not OFF.allows_resource("ckb://docs/reference/x")


@pytest.mark.parametrize("uri,allowed", [
    ("ckb://docs/reference/token-script-hashes", True),
    ("ckb://docs/x", True),
    ("ckb://docs/", False),
    ("ckb://docs", False),
    ("ckb://docsx/y", False),
    ("ckb://chain/tip", False),
    ("CKB://DOCS/x", False),
    ("https://docs.example/ckb", False),
    (" ckb://docs/x", False),
    ("", False),
    (None, False),
    (b"ckb://docs/x", False),
])
def test_resource_prefix_is_exact(uri, allowed):
    assert DOCS.allows_resource(uri) is allowed


def test_filter_keeps_only_the_allowed_tool_from_a_realistic_catalog():
    catalog = [
        {"name": "search_tools", "description": "deferred"},
        {"name": "search_resources", "description": "docs"},
        {"name": "rpc_send_transaction", "description": "broadcast"},
        {"name": "dev_faucet_claim", "description": "funds"},
        {"name": "ckb_query_address", "description": "address"},
        {"name": "a_tool_added_next_year", "description": "unknown"},
    ]
    assert [t["name"] for t in DOCS.filter_tools(catalog)] == ["search_resources"]


def test_filter_fails_closed_when_the_required_tool_is_absent():
    with pytest.raises(McpSurfaceError, match="search_resources"):
        DOCS.filter_tools([{"name": "search_tools", "description": "deferred"}])


def test_filter_fails_closed_on_a_malformed_allowed_entry():
    with pytest.raises(McpSurfaceError, match="malformed"):
        DOCS.filter_tools([{"name": "search_resources", "description": ["not", "a", "string"]}])


def test_the_off_profile_filters_an_empty_catalog_without_requiring_anything():
    assert OFF.filter_tools([{"name": "search_resources", "description": "docs"}]) == []


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
def test_each_arm_resolves_to_its_fixed_policy(arm):
    assert policy_for_arm(arm).profile == profile_for_arm(arm)


@pytest.mark.parametrize("arm", ["Z", "", "a", None, 1])
def test_an_unknown_arm_is_a_refusal(arm):
    with pytest.raises(McpSurfaceError, match="unknown arm"):
        profile_for_arm(arm)


@pytest.mark.parametrize("profile", ["docs-only", "DOCS-ONLY-V1", "full", "", None, 1])
def test_an_unknown_profile_is_a_refusal(profile):
    with pytest.raises(McpSurfaceError, match="unknown MCP surface profile"):
        policy_for_profile(profile)


def test_the_policy_is_immutable_and_shared():
    """A later phase must not be able to widen the surface by mutating the policy in place."""
    assert policy_for_profile(PROFILE_DOCS_ONLY) is policy_for_profile(PROFILE_DOCS_ONLY)
    with pytest.raises(Exception):
        DOCS.allowed_tools.add("rpc_send_transaction")  # type: ignore[attr-defined]
    with pytest.raises(Exception):
        DOCS.profile = "full"  # type: ignore[misc]
    with pytest.raises(Exception):
        PROFILE_BY_ARM["B"] = PROFILE_DOCS_ONLY  # type: ignore[index]


def test_resolving_a_policy_performs_no_external_action(monkeypatch):
    """Policy resolution is pure data: no socket, no subprocess, no client construction."""
    import subprocess

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("the surface policy performed an external action")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    for arm in ("A", "B", "C", "D"):
        assert policy_for_arm(arm).profile in SURFACE_PROFILES


# --- canonical policies cannot be forged ----------------------------------------------------------

@pytest.mark.parametrize("tools,prefix", [
    (frozenset({"search_resources", "search_tools"}), "ckb://docs/"),
    (frozenset({"search_resources"}), "ckb://"),
    (frozenset({"search_resources"}), ""),
    (frozenset(), "ckb://docs/"),
    (frozenset({"rpc_send_transaction"}), "ckb://docs/"),
])
def test_a_widened_policy_cannot_wear_the_canonical_name(tools, prefix):
    """A profile name is a claim about an exact treatment; the stored label must not be forgeable."""
    with pytest.raises(McpSurfaceError, match="canonical"):
        McpSurfacePolicy(
            profile=PROFILE_DOCS_ONLY, allowed_tools=tools, resource_prefix=prefix
        )


def test_the_off_profile_is_equally_canonical():
    with pytest.raises(McpSurfaceError, match="canonical"):
        McpSurfacePolicy(
            profile=PROFILE_OFF,
            allowed_tools=frozenset({"search_resources"}),
            resource_prefix="ckb://docs/",
        )


@pytest.mark.parametrize("profile", ["full", "docs-only", "", None, 7])
def test_an_unknown_profile_cannot_be_constructed_at_all(profile):
    with pytest.raises(McpSurfaceError, match="unknown MCP surface profile"):
        McpSurfacePolicy(profile=profile, allowed_tools=frozenset(), resource_prefix="")


def test_the_canonical_construction_still_equals_the_shared_instance():
    rebuilt = McpSurfacePolicy(
        profile=PROFILE_DOCS_ONLY,
        allowed_tools=DOCS_ONLY_TOOLS,
        resource_prefix=DOCS_RESOURCE_PREFIX,
    )
    assert rebuilt == policy_for_profile(PROFILE_DOCS_ONLY)


# --- catalog shape validation ---------------------------------------------------------------------

@pytest.mark.parametrize("catalog,match", [
    (None, "must be a list"),
    ("search_resources", "must be a list"),
    ({"name": "search_resources"}, "must be a list"),
    (iter([{"name": "search_resources"}]), "must be a list"),
    ([None], "must be an object"),
    (["search_resources"], "must be an object"),
    ([{"no_name": 1}], "no usable name"),
    ([{"name": ""}], "no usable name"),
    ([{"name": "   "}], "no usable name"),
    ([{"name": 7}], "no usable name"),
    ([{"name": []}], "no usable name"),
    ([{"name": "search_resources", "description": 42}], "malformed description"),
    ([{"name": "a"}, {"name": "a"}], "repeats an earlier tool name"),
])
def test_a_malformed_catalog_is_refused_rather_than_coerced(catalog, match):
    """An unhashable name or a non-list body must not escape as a raw TypeError."""
    with pytest.raises(McpSurfaceError, match=match):
        normalize_catalog(catalog)


def test_a_valid_catalog_normalizes_to_a_name_keyed_mapping():
    entries = normalize_catalog([
        {"name": "search_resources", "description": "docs"},
        {"name": "rpc_get_tip_block_number", "description": "tip"},
    ])
    assert list(entries) == ["search_resources", "rpc_get_tip_block_number"]


def test_the_required_tool_plus_a_malformed_extra_entry_now_fails_closed():
    """A malformed extra entry fails closed instead of being silently skipped."""
    with pytest.raises(McpSurfaceError, match="no usable name"):
        DOCS.filter_tools([{"name": "search_resources", "description": "docs"}, {"name": None}])
