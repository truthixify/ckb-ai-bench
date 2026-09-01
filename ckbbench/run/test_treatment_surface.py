from __future__ import annotations

from dataclasses import replace

import pytest

from ckbbench.run.treatment_surface import (
    TESTNET_IDENTITY_TOOLS,
    TreatmentSurfaceError,
    TreatmentSurfaceProfile,
    ScopedMcpClient,
    TaskMcpSurfacePolicy,
    combined_catalog_sha256,
    normalize_resource_catalog,
    normalize_tool_catalog,
    profile_bytes,
    resource_catalog_sha256,
    tool_catalog_sha256,
)


def _tools() -> list[dict]:
    return [
        {
            "name": "search_resources",
            "description": "Search documentation",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "rpc_get_tip_block_number",
            "description": "Read the current tip",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rpc_get_block_hash",
            "description": "Read a block hash",
            "inputSchema": {
                "type": "object",
                "properties": {"block_number": {"type": "integer"}},
                "required": ["block_number"],
            },
        },
        {
            "name": "rpc_get_blockchain_info",
            "description": "Read chain identity",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "dev_get_genesis_hash",
            "description": "Read genesis",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "dev_request_testnet_funds",
            "description": "Request faucet funds",
            "inputSchema": {
                "type": "object",
                "properties": {"address": {"type": "string"}},
            },
        },
        {
            "name": "dev_generate_lock_info",
            "description": "Generate lock information",
            "inputSchema": {
                "type": "object",
                "properties": {"private_key": {"type": "string"}},
            },
        },
    ]


def _resources() -> list[dict]:
    return [
        {
            "uri": "ckb://docs/reference/token-script-hashes",
            "name": "Token script hashes",
            "mimeType": "text/markdown",
        },
        {
            "uri": "ckb://docs/rfcs/rfc-0022-transaction-structure",
            "name": "Transaction structure",
            "mimeType": "text/markdown",
        },
        {
            "uri": "ckb://internal/operator-state",
            "name": "Operator state",
        },
    ]


def _profile(
    *,
    allowed_tools: tuple[str, ...] = ("search_resources",),
    prefixes: tuple[str, ...] = ("ckb://docs/",),
    live: bool = True,
) -> TreatmentSurfaceProfile:
    return TreatmentSurfaceProfile.from_catalogs(
        profile_id="ckb-ai-testnet-read-v1" if live else "ckb-ai-docs-v1",
        server_name="ckb-ai-mcp",
        server_version="1.6.13",
        claims_live_chain=live,
        allowed_tools=allowed_tools,
        allowed_resource_prefixes=prefixes,
        tools=_tools(),
        resources=_resources(),
    )


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def initialize(self) -> dict:
        self.calls.append(("initialize", None))
        return {"serverInfo": {"name": "ckb-ai-mcp", "version": "1.6.13"}}

    def list_tools(self) -> list[dict]:
        self.calls.append(("list_tools", None))
        return _tools()

    def list_resources(self) -> list[dict]:
        self.calls.append(("list_resources", None))
        return _resources()

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}

    def read_resource(self, uri: str) -> dict:
        self.calls.append(("resources/read", uri))
        return {"contents": [{"uri": uri, "text": "body"}]}


def test_catalog_normalization_is_order_independent_and_does_not_mutate_inputs():
    tools = _tools()
    resources = _resources()
    tools_before = repr(tools)
    resources_before = repr(resources)

    assert tool_catalog_sha256(tools) == tool_catalog_sha256(list(reversed(tools)))
    assert resource_catalog_sha256(resources) == resource_catalog_sha256(
        list(reversed(resources))
    )
    assert repr(tools) == tools_before
    assert repr(resources) == resources_before
    assert tuple(row["name"] for row in normalize_tool_catalog(tools)) == tuple(
        sorted(row["name"] for row in tools)
    )
    assert tuple(row["uri"] for row in normalize_resource_catalog(resources)) == tuple(
        sorted(row["uri"] for row in resources)
    )


def test_surface_round_trip_bytes_and_digest_are_stable():
    profile = _profile()

    assert profile.controller_identity_tools == TESTNET_IDENTITY_TOOLS
    assert TreatmentSurfaceProfile.from_dict(profile.to_dict()) == profile
    assert profile_bytes(profile).endswith(b"\n")
    assert profile.catalog_sha256 == combined_catalog_sha256(
        profile.tool_catalog_sha256,
        profile.resource_catalog_sha256,
    )
    assert len(profile.sha256) == 64


def test_neutral_surface_has_no_controller_chain_contract():
    profile = _profile(live=False)
    assert profile.controller_identity_tools == ()
    assert not profile.claims_live_chain


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "wrong"},
        {"catalog_sha256": "0" * 64},
        {"controller_identity_tools": ()},
        {"allowed_tools": ("search_resources", "search_resources")},
        {"allowed_tools": ("dev_request_testnet_funds",)},
        {"allowed_resource_prefixes": ("ckb://docs",)},
        {"allowed_resource_prefixes": ("ckb://docs/../private/",)},
    ],
)
def test_surface_contract_refuses_invalid_or_privileged_mutations(change: dict):
    with pytest.raises(TreatmentSurfaceError):
        replace(_profile(), **change)


def test_catalog_digest_detects_schema_description_name_and_membership_drift():
    baseline = tool_catalog_sha256(_tools())
    mutations = []
    for field, value in (
        ("description", "Changed"),
        ("name", "search_resources_v2"),
        ("inputSchema", {"type": "object", "properties": {}}),
    ):
        rows = _tools()
        rows[0][field] = value
        mutations.append(rows)
    mutations.append(_tools()[:-1])

    assert all(tool_catalog_sha256(rows) != baseline for rows in mutations)


@pytest.mark.parametrize(
    "catalog",
    [
        {},
        [{"name": "x", "description": "", "inputSchema": {}}] * 257,
        [{"name": "x", "description": "", "inputSchema": {}},
         {"name": "x", "description": "", "inputSchema": {}}],
        [{"name": "x", "description": 1, "inputSchema": {}}],
        [{"name": "x", "description": "", "inputSchema": []}],
        [{"name": "x", "description": "", "inputSchema": {"const": float("nan")}}],
    ],
)
def test_malformed_tool_catalogs_fail_closed(catalog: object):
    with pytest.raises(TreatmentSurfaceError):
        normalize_tool_catalog(catalog)


@pytest.mark.parametrize(
    "catalog",
    [
        {},
        [{"uri": "relative", "name": "x"}],
        [{"uri": "ckb://docs/../secret", "name": "x"}],
        [{"uri": "ckb://docs/%2E%2E/secret", "name": "x"}],
        [{"uri": "ckb://docs/%2fsecret", "name": "x"}],
        [{"uri": "CKB://docs/reference", "name": "x"}],
        [{"uri": "ckb://user:pass@docs/reference", "name": "x"}],
        [{"uri": "ckb://docs/référence", "name": "x"}],
        [{"uri": "ckb://docs/x?credential=value", "name": "x"}],
        [{"uri": "ckb://docs/x", "name": "x"}, {"uri": "ckb://docs/x", "name": "y"}],
        [{"uri": "ckb://docs/x", "name": 3}],
    ],
)
def test_malformed_resource_catalogs_fail_closed(catalog: object):
    with pytest.raises(TreatmentSurfaceError):
        normalize_resource_catalog(catalog)


def test_discovery_returns_only_allowed_tools_after_full_catalog_validation():
    policy = TaskMcpSurfacePolicy(_profile())
    visible = policy.filter_tools(_tools())
    assert [row["name"] for row in visible] == ["search_resources"]
    assert all(row is not source for row, source in zip(visible, _tools(), strict=False))


def test_catalog_drift_fails_before_a_surface_is_returned():
    policy = TaskMcpSurfacePolicy(_profile())
    drifted = _tools()
    drifted[0]["description"] = "drifted"
    with pytest.raises(TreatmentSurfaceError, match="frozen catalog"):
        policy.filter_tools(drifted)


def test_sensitive_schema_is_refused_even_when_a_profile_digest_was_built_from_it():
    tools = _tools()
    tools[0]["inputSchema"]["properties"]["private_key"] = {"type": "string"}
    with pytest.raises(TreatmentSurfaceError, match="privileged"):
        TreatmentSurfaceProfile.from_catalogs(
            profile_id="unsafe-synthetic-v1",
            server_name="ckb-ai-mcp",
            server_version="1.6.13",
            claims_live_chain=False,
            allowed_tools=("search_resources",),
            allowed_resource_prefixes=("ckb://docs/",),
            tools=tools,
            resources=_resources(),
        )


@pytest.mark.parametrize(
    "name",
    [
        "broadcast-transaction",
        "custody_wallet",
        "derive_key",
        "deploy_contract",
        "faucet_request",
        "generate.key",
        "keygen",
        "send_transaction",
        "sign-tx",
        "sign_transaction",
        "submit_transaction",
        "tx/submit",
    ],
)
def test_privileged_capability_names_cannot_enter_a_model_surface(name: str):
    tools = _tools() + [{
        "name": name,
        "description": "Privileged operation",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    with pytest.raises(TreatmentSurfaceError, match="privileged"):
        TreatmentSurfaceProfile.from_catalogs(
            profile_id="unsafe-synthetic-v1",
            server_name="ckb-ai-mcp",
            server_version="1.6.13",
            claims_live_chain=False,
            allowed_tools=(name,),
            allowed_resource_prefixes=("ckb://docs/",),
            tools=tools,
            resources=_resources(),
        )


@pytest.mark.parametrize(
    "property_name",
    ["privateKey", "signing-key", "seedPhrase", "apiKey", "credential"],
)
def test_secret_accepting_schema_variants_cannot_enter_a_model_surface(property_name: str):
    tools = _tools()
    tools[0]["inputSchema"]["properties"][property_name] = {"type": "string"}
    with pytest.raises(TreatmentSurfaceError, match="privileged"):
        TreatmentSurfaceProfile.from_catalogs(
            profile_id="unsafe-synthetic-v1",
            server_name="ckb-ai-mcp",
            server_version="1.6.13",
            claims_live_chain=False,
            allowed_tools=("search_resources",),
            allowed_resource_prefixes=("ckb://docs/",),
            tools=tools,
            resources=_resources(),
        )


@pytest.mark.parametrize("metadata_key", ["Description", "FORMAT", "TiTlE"])
def test_sensitive_schema_metadata_is_case_insensitive(metadata_key: str):
    tools = _tools()
    tools[0]["inputSchema"][metadata_key] = "Accepts a private key"
    with pytest.raises(TreatmentSurfaceError, match="privileged"):
        TreatmentSurfaceProfile.from_catalogs(
            profile_id="unsafe-synthetic-v1",
            server_name="ckb-ai-mcp",
            server_version="1.6.13",
            claims_live_chain=False,
            allowed_tools=("search_resources",),
            allowed_resource_prefixes=("ckb://docs/",),
            tools=tools,
            resources=_resources(),
        )


def test_live_identity_tools_are_required_and_never_model_visible():
    missing = [row for row in _tools() if row["name"] != "dev_get_genesis_hash"]
    with pytest.raises(TreatmentSurfaceError, match="missing"):
        TreatmentSurfaceProfile.from_catalogs(
            profile_id="missing-identity-v1",
            server_name="ckb-ai-mcp",
            server_version="1.6.13",
            claims_live_chain=True,
            allowed_tools=("search_resources",),
            allowed_resource_prefixes=("ckb://docs/",),
            tools=missing,
            resources=_resources(),
        )

    with pytest.raises(TreatmentSurfaceError, match="controller identity"):
        _profile(allowed_tools=("rpc_get_tip_block_number", "search_resources"))


def test_each_public_profile_field_is_identity_bound_or_refused():
    baseline = _profile()
    mutations = {
        "profile_id": "ckb-ai-testnet-read-v2",
        "server_name": "ckb-ai-mcp-next",
        "server_version": "1.6.14",
        "claims_live_chain": False,
        "allowed_tools": (),
        "allowed_resource_prefixes": (),
        "tool_catalog_sha256": "1" * 64,
        "resource_catalog_sha256": "2" * 64,
    }
    for field_name, value in mutations.items():
        try:
            changed = replace(baseline, **{field_name: value})
        except TreatmentSurfaceError:
            continue
        assert changed.sha256 != baseline.sha256


def test_resource_catalog_and_dispatch_share_the_same_prefix_policy():
    policy = TaskMcpSurfacePolicy(_profile())
    policy.validate_resources(_resources())
    assert policy.allows_resource("ckb://docs/reference/token-script-hashes")
    assert not policy.allows_resource("ckb://internal/operator-state")
    assert not policy.allows_resource("ckb://docs/../internal/operator-state")


def test_missing_resource_prefix_is_refused():
    with pytest.raises(TreatmentSurfaceError, match="missing"):
        _profile(prefixes=("ckb://missing/",))


def test_scoped_client_guards_dispatch_even_if_the_agent_side_check_is_bypassed():
    raw = _Client()
    policy = TaskMcpSurfacePolicy(_profile())
    client = ScopedMcpClient(raw, policy)

    assert [row["name"] for row in client.list_tools()] == [
        "search_resources",
    ]
    client.list_resources()
    client.call_tool("search_resources", {"query": "type id"})
    client.read_resource("ckb://docs/reference/token-script-hashes")
    before = list(raw.calls)

    with pytest.raises(TreatmentSurfaceError):
        client.call_tool("dev_request_testnet_funds", {})
    with pytest.raises(TreatmentSurfaceError):
        client.read_resource("ckb://internal/operator-state")

    assert raw.calls == before
    assert policy.violation_count == 2


def test_profile_parser_refuses_extra_fields_and_non_array_collections():
    document = _profile().to_dict()
    document["extra"] = True
    with pytest.raises(TreatmentSurfaceError):
        TreatmentSurfaceProfile.from_dict(document)

    document = _profile().to_dict()
    document["allowed_tools"] = "search_resources"
    with pytest.raises(TreatmentSurfaceError):
        TreatmentSurfaceProfile.from_dict(document)
