"""Frozen Task-scoped CKB AI treatment surfaces."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from ckbbench.run.task_attempt import (
    AttemptSchemaError,
    artifact_sha256,
    canonical_json_bytes,
    validate_public_artifact_values,
)

SURFACE_SCHEMA_VERSION = "ckbbench-ckb-ai-surface-v1"
MAX_CATALOG_BYTES = 1 << 20
MAX_CATALOG_ENTRIES = 256

TESTNET_IDENTITY_TOOLS = (
    "dev_get_genesis_hash",
    "rpc_get_block_hash",
    "rpc_get_blockchain_info",
    "rpc_get_tip_block_number",
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]{0,127}$")
_SENSITIVE_SCHEMA_KEYS = frozenset({
    "api_key",
    "credential",
    "mnemonic",
    "password",
    "private_key",
    "privatekey",
    "secret",
    "seed_phrase",
    "signing_key",
})
_SENSITIVE_SCHEMA_TERMS = (
    "api key",
    "credential",
    "mnemonic",
    "password",
    "private key",
    "secret",
    "seed phrase",
    "signing key",
)
_SENSITIVE_SCHEMA_KEY_TOKENS = frozenset(
    re.sub(r"[^a-z0-9]", "", item) for item in _SENSITIVE_SCHEMA_KEYS
)
_PRIVILEGED_TOOL_NAMES = frozenset({
    "dev_deploy_cell_data",
    "dev_generate_lock_info",
    "dev_get_default_account_info",
    "dev_request_testnet_funds",
})
_PRIVILEGED_NAME_PARTS = (
    "broadcast_transaction",
    "custody",
    "derive_key",
    "deploy",
    "faucet",
    "generate_key",
    "generate_private",
    "keygen",
    "private_key",
    "request_funds",
    "send_transaction",
    "server_sign",
    "sign_tx",
    "sign_transaction",
    "submit_transaction",
    "tx_submit",
    "wallet",
)


class TreatmentSurfaceError(ValueError):
    """A surface or advertised catalog violates the frozen treatment contract."""


class McpCatalogClient(Protocol):
    def initialize(self) -> dict[str, Any]: ...

    def list_tools(self) -> list[dict[str, Any]]: ...

    def list_resources(self) -> list[dict[str, Any]]: ...

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def read_resource(self, uri: str) -> dict[str, Any]: ...


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise TreatmentSurfaceError(f"{label} must be a bounded public identifier")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TreatmentSurfaceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _tool_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or _TOOL_NAME.fullmatch(value) is None:
        raise TreatmentSurfaceError(f"{label} must be a bounded MCP tool name")
    return value


def _exact(document: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != fields:
        raise TreatmentSurfaceError(f"{label} must contain exactly the reviewed fields")
    return document


def _canonical_copy(value: Any, label: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError):
        raise TreatmentSurfaceError(f"{label} is not canonical JSON data") from None
    if len(encoded) > MAX_CATALOG_BYTES:
        raise TreatmentSurfaceError(f"{label} exceeds the catalog byte limit")
    return json.loads(encoded)


def _contains_sensitive_schema_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str):
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in _SENSITIVE_SCHEMA_KEY_TOKENS:
                    return True
                if normalized in {"description", "format", "title"} and isinstance(child, str):
                    lowered = child.lower().replace("_", "-").replace("-", " ")
                    if any(term in lowered for term in _SENSITIVE_SCHEMA_TERMS):
                        return True
            if _contains_sensitive_schema_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_schema_key(child) for child in value)
    return False


def _is_privileged_tool(name: str, schema: Any) -> bool:
    lowered = name.lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return (
        name in _PRIVILEGED_TOOL_NAMES
        or any(part in normalized for part in _PRIVILEGED_NAME_PARTS)
        or _contains_sensitive_schema_key(schema)
    )


def normalize_tool_catalog(catalog: Any) -> tuple[dict[str, Any], ...]:
    """Validate and canonicalize a complete tools/list response by exact tool name."""
    if not isinstance(catalog, list) or len(catalog) > MAX_CATALOG_ENTRIES:
        raise TreatmentSurfaceError("tools/list must be a bounded array")
    rows: dict[str, dict[str, Any]] = {}
    for position, raw in enumerate(catalog):
        row = _canonical_copy(raw, f"tools/list[{position}]")
        if not isinstance(row, dict):
            raise TreatmentSurfaceError(f"tools/list[{position}] must be an object")
        name = _tool_name(row.get("name"), f"tools/list[{position}].name")
        if name in rows:
            raise TreatmentSurfaceError("tools/list repeats a tool name")
        if not isinstance(row.get("description", ""), str):
            raise TreatmentSurfaceError(f"tools/list[{position}].description must be text")
        if not isinstance(row.get("inputSchema"), dict):
            raise TreatmentSurfaceError(f"tools/list[{position}].inputSchema must be an object")
        rows[name] = row
    return tuple(rows[name] for name in sorted(rows))


def _resource_uri(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TreatmentSurfaceError(f"{label} must be a bounded resource URI")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise TreatmentSurfaceError(f"{label} must be a canonical ASCII resource URI") from None
    if len(encoded) > 2048 or any(byte < 33 or byte == 127 for byte in encoded):
        raise TreatmentSurfaceError(f"{label} must be a bounded resource URI")
    parsed = urlsplit(value)
    percent_tokens = re.findall(r"%[^%]{0,2}", value)
    if any(re.fullmatch(r"%[0-9A-F]{2}", token) is None for token in percent_tokens):
        raise TreatmentSurfaceError(f"{label} must use canonical percent encoding")
    decoded_path = unquote(parsed.path)
    if (
        not parsed.scheme
        or value.split(":", 1)[0] != parsed.scheme
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in decoded_path
        or decoded_path.count("/") != parsed.path.count("/")
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
    ):
        raise TreatmentSurfaceError(f"{label} must be an absolute canonical resource URI")
    return value


def normalize_resource_catalog(catalog: Any) -> tuple[dict[str, Any], ...]:
    """Validate and canonicalize a complete resources/list response by exact URI."""
    if not isinstance(catalog, list) or len(catalog) > MAX_CATALOG_ENTRIES:
        raise TreatmentSurfaceError("resources/list must be a bounded array")
    rows: dict[str, dict[str, Any]] = {}
    for position, raw in enumerate(catalog):
        row = _canonical_copy(raw, f"resources/list[{position}]")
        if not isinstance(row, dict):
            raise TreatmentSurfaceError(f"resources/list[{position}] must be an object")
        uri = _resource_uri(row.get("uri"), f"resources/list[{position}].uri")
        if uri in rows:
            raise TreatmentSurfaceError("resources/list repeats a URI")
        if not isinstance(row.get("name", ""), str):
            raise TreatmentSurfaceError(f"resources/list[{position}].name must be text")
        rows[uri] = row
    return tuple(rows[uri] for uri in sorted(rows))


def tool_catalog_sha256(catalog: Any) -> str:
    return artifact_sha256({"tools": list(normalize_tool_catalog(catalog))})


def resource_catalog_sha256(catalog: Any) -> str:
    return artifact_sha256({"resources": list(normalize_resource_catalog(catalog))})


def combined_catalog_sha256(tool_sha256: str, resource_sha256: str) -> str:
    return artifact_sha256({
        "resource_catalog_sha256": _sha(resource_sha256, "resource catalog digest"),
        "tool_catalog_sha256": _sha(tool_sha256, "tool catalog digest"),
    })


def _prefix(value: Any, label: str) -> str:
    uri = _resource_uri(value, label)
    if not uri.endswith("/"):
        raise TreatmentSurfaceError(f"{label} must end with a slash")
    return uri


@dataclass(frozen=True)
class TreatmentSurfaceProfile:
    profile_id: str
    server_name: str
    server_version: str
    claims_live_chain: bool
    allowed_tools: tuple[str, ...]
    allowed_resource_prefixes: tuple[str, ...]
    controller_identity_tools: tuple[str, ...]
    tool_catalog_sha256: str
    resource_catalog_sha256: str
    catalog_sha256: str
    schema_version: str = SURFACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "surface.profile_id")
        _identifier(self.server_name, "surface.server_name")
        _identifier(self.server_version, "surface.server_version")
        if not isinstance(self.claims_live_chain, bool):
            raise TreatmentSurfaceError("surface.claims_live_chain must be boolean")
        if not isinstance(self.allowed_tools, tuple):
            raise TreatmentSurfaceError("surface.allowed_tools must be immutable")
        tools = tuple(_tool_name(value, "surface.allowed_tools item") for value in self.allowed_tools)
        if tools != tuple(sorted(set(tools))):
            raise TreatmentSurfaceError("surface.allowed_tools must be unique and sorted")
        if any(_is_privileged_tool(name, {}) for name in tools):
            raise TreatmentSurfaceError("surface exposes a privileged tool")
        if not isinstance(self.allowed_resource_prefixes, tuple):
            raise TreatmentSurfaceError("surface resource prefixes must be immutable")
        prefixes = tuple(
            _prefix(value, "surface resource prefix")
            for value in self.allowed_resource_prefixes
        )
        if prefixes != tuple(sorted(set(prefixes))):
            raise TreatmentSurfaceError("surface resource prefixes must be unique and sorted")
        if not isinstance(self.controller_identity_tools, tuple):
            raise TreatmentSurfaceError("surface identity tools must be immutable")
        identity_tools = tuple(
            _tool_name(value, "surface identity tool")
            for value in self.controller_identity_tools
        )
        expected_identity = TESTNET_IDENTITY_TOOLS if self.claims_live_chain else ()
        if identity_tools != expected_identity:
            raise TreatmentSurfaceError("surface uses an unsupported chain identity contract")
        if set(identity_tools) & set(tools):
            raise TreatmentSurfaceError("controller identity tools cannot be model-visible")
        _sha(self.tool_catalog_sha256, "surface.tool_catalog_sha256")
        _sha(self.resource_catalog_sha256, "surface.resource_catalog_sha256")
        _sha(self.catalog_sha256, "surface.catalog_sha256")
        expected_catalog = combined_catalog_sha256(
            self.tool_catalog_sha256,
            self.resource_catalog_sha256,
        )
        if self.catalog_sha256 != expected_catalog:
            raise TreatmentSurfaceError("surface combined catalog digest is invalid")
        if self.schema_version != SURFACE_SCHEMA_VERSION:
            raise TreatmentSurfaceError("surface schema version is unsupported")
        try:
            validate_public_artifact_values(self.to_dict())
        except AttemptSchemaError as exc:
            raise TreatmentSurfaceError("surface contains a secret-shaped value") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_resource_prefixes": list(self.allowed_resource_prefixes),
            "allowed_tools": list(self.allowed_tools),
            "catalog_sha256": self.catalog_sha256,
            "claims_live_chain": self.claims_live_chain,
            "controller_identity_tools": list(self.controller_identity_tools),
            "profile_id": self.profile_id,
            "resource_catalog_sha256": self.resource_catalog_sha256,
            "schema_version": self.schema_version,
            "server_name": self.server_name,
            "server_version": self.server_version,
            "tool_catalog_sha256": self.tool_catalog_sha256,
        }

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> TreatmentSurfaceProfile:
        raw = dict(_exact(document, {
            "allowed_resource_prefixes",
            "allowed_tools",
            "catalog_sha256",
            "claims_live_chain",
            "controller_identity_tools",
            "profile_id",
            "resource_catalog_sha256",
            "schema_version",
            "server_name",
            "server_version",
            "tool_catalog_sha256",
        }, "CKB AI surface"))
        for field_name in (
            "allowed_resource_prefixes",
            "allowed_tools",
            "controller_identity_tools",
        ):
            if not isinstance(raw[field_name], list):
                raise TreatmentSurfaceError(f"surface.{field_name} must be an array")
            raw[field_name] = tuple(raw[field_name])
        return cls(**raw)

    @classmethod
    def from_catalogs(
        cls,
        *,
        profile_id: str,
        server_name: str,
        server_version: str,
        claims_live_chain: bool,
        allowed_tools: tuple[str, ...],
        allowed_resource_prefixes: tuple[str, ...],
        tools: Any,
        resources: Any,
    ) -> TreatmentSurfaceProfile:
        if not isinstance(allowed_tools, tuple):
            raise TreatmentSurfaceError("surface.allowed_tools must be immutable")
        checked_tools = tuple(
            _tool_name(name, "surface.allowed_tools item") for name in allowed_tools
        )
        if not isinstance(allowed_resource_prefixes, tuple):
            raise TreatmentSurfaceError("surface resource prefixes must be immutable")
        checked_prefixes = tuple(
            _prefix(prefix, "surface resource prefix")
            for prefix in allowed_resource_prefixes
        )
        normalized_tools = normalize_tool_catalog(tools)
        normalized_resources = normalize_resource_catalog(resources)
        by_name = {row["name"]: row for row in normalized_tools}
        controller_tools = TESTNET_IDENTITY_TOOLS if claims_live_chain else ()
        required_names = set(checked_tools) | set(controller_tools)
        if not required_names <= set(by_name):
            raise TreatmentSurfaceError("catalog is missing a declared surface tool")
        if set(checked_tools) & set(controller_tools):
            raise TreatmentSurfaceError("controller identity tools cannot be model-visible")
        if any(
            _is_privileged_tool(name, by_name[name].get("inputSchema"))
            for name in checked_tools
        ):
            raise TreatmentSurfaceError("the declared model surface contains a privileged tool")
        resource_uris = tuple(row["uri"] for row in normalized_resources)
        if any(
            not any(uri.startswith(prefix) for uri in resource_uris)
            for prefix in checked_prefixes
        ):
            raise TreatmentSurfaceError("catalog is missing a declared resource prefix")
        tool_digest = artifact_sha256({"tools": list(normalized_tools)})
        resource_digest = artifact_sha256({"resources": list(normalized_resources)})
        return cls(
            profile_id=profile_id,
            server_name=server_name,
            server_version=server_version,
            claims_live_chain=claims_live_chain,
            allowed_tools=checked_tools,
            allowed_resource_prefixes=checked_prefixes,
            controller_identity_tools=controller_tools,
            tool_catalog_sha256=tool_digest,
            resource_catalog_sha256=resource_digest,
            catalog_sha256=combined_catalog_sha256(tool_digest, resource_digest),
        )


@dataclass
class TaskMcpSurfacePolicy:
    """One policy object controls discovery and dispatch for a frozen Task surface."""

    profile_record: TreatmentSurfaceProfile
    violation_count: int = field(default=0, init=False)

    @property
    def profile(self) -> str:
        return self.profile_record.profile_id

    @property
    def resource_prefix(self) -> str:
        prefixes = self.profile_record.allowed_resource_prefixes
        return prefixes[0] if len(prefixes) == 1 else "approved task resource prefixes"

    def allows_tool(self, name: Any) -> bool:
        return isinstance(name, str) and name in self.profile_record.allowed_tools

    def allows_resource(self, uri: Any) -> bool:
        if not isinstance(uri, str):
            return False
        try:
            _resource_uri(uri, "resource URI")
        except TreatmentSurfaceError:
            return False
        return any(uri.startswith(prefix) for prefix in self.profile_record.allowed_resource_prefixes)

    def filter_tools(self, catalog: Any) -> list[dict[str, Any]]:
        normalized = normalize_tool_catalog(catalog)
        if artifact_sha256({"tools": list(normalized)}) != self.profile_record.tool_catalog_sha256:
            raise TreatmentSurfaceError("tools/list differs from the frozen catalog")
        by_name = {row["name"]: row for row in normalized}
        missing = set(self.profile_record.allowed_tools) - set(by_name)
        if missing:
            raise TreatmentSurfaceError("tools/list is missing a required task tool")
        for name in self.profile_record.allowed_tools:
            if _is_privileged_tool(name, by_name[name].get("inputSchema")):
                raise TreatmentSurfaceError("the frozen task surface contains a privileged tool")
        return [deepcopy(by_name[name]) for name in self.profile_record.allowed_tools]

    def validate_resources(self, catalog: Any) -> None:
        normalized = normalize_resource_catalog(catalog)
        if (
            artifact_sha256({"resources": list(normalized)})
            != self.profile_record.resource_catalog_sha256
        ):
            raise TreatmentSurfaceError("resources/list differs from the frozen catalog")
        uris = tuple(row["uri"] for row in normalized)
        if any(
            not any(uri.startswith(prefix) for uri in uris)
            for prefix in self.profile_record.allowed_resource_prefixes
        ):
            raise TreatmentSurfaceError("resources/list is missing a required task resource prefix")

    def refuse(self) -> None:
        self.violation_count += 1


class ScopedMcpClient:
    """Defense-in-depth dispatch guard around the model-visible MCP client."""

    def __init__(self, client: McpCatalogClient, policy: TaskMcpSurfacePolicy) -> None:
        self._client = client
        self.policy = policy

    def initialize(self) -> dict[str, Any]:
        return self._client.initialize()

    def list_tools(self) -> list[dict[str, Any]]:
        return self.policy.filter_tools(self._client.list_tools())

    def list_resources(self) -> list[dict[str, Any]]:
        resources = self._client.list_resources()
        self.policy.validate_resources(resources)
        return resources

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.policy.allows_tool(name):
            self.policy.refuse()
            raise TreatmentSurfaceError("MCP tool is outside the frozen task surface")
        return self._client.call_tool(name, arguments)

    def read_resource(self, uri: str) -> dict[str, Any]:
        if not self.policy.allows_resource(uri):
            self.policy.refuse()
            raise TreatmentSurfaceError("MCP resource is outside the frozen task surface")
        return self._client.read_resource(uri)


def profile_bytes(profile: TreatmentSurfaceProfile) -> bytes:
    return canonical_json_bytes(profile.to_dict())
