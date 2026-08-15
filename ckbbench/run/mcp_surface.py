"""The model-visible MCP surface for one arm (ADR-0013, RD3).

The suite scores a fresh local ``ckb_dev`` chain, but the pinned CKB AI endpoint is bound to public
TestNet. Its chain-bound tools would hand C/D live state from a chain the verifier never grades, so
phase one keeps only the endpoint's chain-neutral documentation surface and reaches the selected
chain through ``CKB_RPC_URL`` in every arm alike.

This module is the single source of truth for that boundary. The policy is an exact allowlist, not
a prefix rule or a denylist of today's chain tools: an unknown future tool must default to denied.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable

PROFILE_OFF = "off"
PROFILE_DOCS_ONLY = "docs-only-v1"
SURFACE_PROFILES: frozenset[str] = frozenset({PROFILE_OFF, PROFILE_DOCS_ONLY})

DOCS_ONLY_TOOLS: frozenset[str] = frozenset({"search_resources"})
DOCS_RESOURCE_PREFIX = "ckb://docs/"

# The ladder's fixed treatment. A/B have no MCP action surface at all; C/D differ from them only by
# the documentation surface and its steering.
PROFILE_BY_ARM = MappingProxyType({
    "A": PROFILE_OFF,
    "B": PROFILE_OFF,
    "C": PROFILE_DOCS_ONLY,
    "D": PROFILE_DOCS_ONLY,
})


class McpSurfaceError(ValueError):
    """Raised when a surface profile, arm mapping, or advertised catalog is unusable."""


class McpSurfaceSetupError(McpSurfaceError):
    """The MCP controller could not be established as the approved surface for this cell.

    Carries only a sanitized reason. Classified as a pre-agent infrastructure failure, like a
    failed preflight, so one unreachable or drifted server does not abort the matrix.
    """


# The only combinations that may exist. A profile name is a claim about an exact treatment, so a
# policy that pairs a canonical name with different tools or a wider prefix must not be
# constructible: the stored `mcp_surface_profile` would then describe a treatment the agent never
# ran under.
_CANONICAL_FIELDS = MappingProxyType({
    PROFILE_OFF: (frozenset(), ""),
    PROFILE_DOCS_ONLY: (DOCS_ONLY_TOOLS, DOCS_RESOURCE_PREFIX),
})


@dataclass(frozen=True)
class McpSurfacePolicy:
    """What one arm's model may reach through the MCP controller.

    Consumed by both the visible tool list and the dispatch guard, so a tool cannot be hidden from
    the prompt while remaining callable, or callable while hidden.

    Only the canonical combinations are constructible; use ``policy_for_profile`` or
    ``policy_for_arm`` rather than building one directly.
    """

    profile: str
    allowed_tools: frozenset[str]
    resource_prefix: str

    def __post_init__(self) -> None:
        canonical = _CANONICAL_FIELDS.get(self.profile) if isinstance(self.profile, str) else None
        if canonical is None:
            raise McpSurfaceError(
                f"unknown MCP surface profile {self.profile!r}; expected one of "
                f"{sorted(SURFACE_PROFILES)}"
            )
        try:
            tools = frozenset(self.allowed_tools)
        except TypeError as exc:
            raise McpSurfaceError(
                f"profile {self.profile!r} needs a collection of tool names, got "
                f"{type(self.allowed_tools).__name__}"
            ) from exc
        if not isinstance(self.resource_prefix, str):
            raise McpSurfaceError(
                f"profile {self.profile!r} needs a string resource prefix, got "
                f"{type(self.resource_prefix).__name__}"
            )
        if (tools, self.resource_prefix) != canonical:
            raise McpSurfaceError(
                f"profile {self.profile!r} is canonical: it allows {sorted(canonical[0])} and the "
                f"prefix {canonical[1]!r}, not {len(tools)} tool name(s) and "
                f"{self.resource_prefix!r}"
            )
        # Freeze what is stored, not only what was compared: a caller-supplied mutable set would
        # otherwise pass validation and the pre-run provenance check, then be widened before the
        # agent dispatches, while the result still carried the accepted label.
        object.__setattr__(self, "allowed_tools", tools)

    @property
    def enabled(self) -> bool:
        return bool(self.allowed_tools)

    def allows_tool(self, name: Any) -> bool:
        """Exact membership. No case folding, no trimming: a near-miss name is a different tool."""
        return isinstance(name, str) and name in self.allowed_tools

    def allows_resource(self, uri: Any) -> bool:
        return (
            isinstance(uri, str)
            and bool(self.resource_prefix)
            and uri.startswith(self.resource_prefix)
            and len(uri) > len(self.resource_prefix)
        )

    def filter_tools(self, catalog: Any) -> list[dict[str, Any]]:
        """The advertised catalog reduced to exactly the allowed tools.

        The whole catalog is validated first: a malformed shape anywhere is a server the harness
        cannot reason about, so it is refused rather than silently skipped. A missing or malformed
        required tool fails here, before a model call, rather than mid-run.
        """
        entries = normalize_catalog(catalog)
        kept = {name: entry for name, entry in entries.items() if self.allows_tool(name)}
        missing = sorted(self.allowed_tools - set(kept))
        if missing:
            raise McpSurfaceError(
                f"profile {self.profile!r} requires {missing} in tools/list; the server did not "
                "advertise them"
            )
        return [kept[name] for name in sorted(kept)]


_OFF_POLICY = McpSurfacePolicy(
    profile=PROFILE_OFF, allowed_tools=frozenset(), resource_prefix=""
)
_DOCS_ONLY_POLICY = McpSurfacePolicy(
    profile=PROFILE_DOCS_ONLY,
    allowed_tools=DOCS_ONLY_TOOLS,
    resource_prefix=DOCS_RESOURCE_PREFIX,
)
_POLICY_BY_PROFILE = MappingProxyType({
    PROFILE_OFF: _OFF_POLICY,
    PROFILE_DOCS_ONLY: _DOCS_ONLY_POLICY,
})


def normalize_catalog(catalog: Any) -> dict[str, dict[str, Any]]:
    """`tools/list` as a name-keyed mapping, or a refusal.

    Every shape the harness cannot interpret is rejected here rather than coerced, so a malformed
    or drifted server response becomes one classified failure instead of a raw ``TypeError`` from
    somewhere downstream.
    """
    if not isinstance(catalog, list):
        raise McpSurfaceError(f"tools/list must be a list, got {type(catalog).__name__}")
    entries: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(catalog):
        # Diagnostics name the position and the expected shape only. A server-supplied tool name or
        # body could carry a token or a raw response, and this text reaches operator logs.
        where = f"tools/list[{position}]"
        if not isinstance(entry, dict):
            raise McpSurfaceError(f"{where} must be an object, got {type(entry).__name__}")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise McpSurfaceError(f"{where} has no usable name")
        if not isinstance(entry.get("description", ""), str):
            raise McpSurfaceError(f"{where} has a malformed description")
        if name in entries:
            raise McpSurfaceError(f"{where} repeats an earlier tool name")
        entries[name] = entry
    return entries


def policy_for_profile(profile: str) -> McpSurfacePolicy:
    """The policy for an exact profile name. An unknown profile is a refusal, not a default."""
    try:
        return _POLICY_BY_PROFILE[profile]
    except (KeyError, TypeError) as exc:
        raise McpSurfaceError(
            f"unknown MCP surface profile {profile!r}; expected one of {sorted(SURFACE_PROFILES)}"
        ) from exc


def profile_for_arm(arm: str) -> str:
    """The fixed profile this arm must run under."""
    try:
        return PROFILE_BY_ARM[arm]
    except (KeyError, TypeError) as exc:
        raise McpSurfaceError(
            f"unknown arm {arm!r}; expected one of {sorted(PROFILE_BY_ARM)}"
        ) from exc


def policy_for_arm(arm: str) -> McpSurfacePolicy:
    return policy_for_profile(profile_for_arm(arm))
