"""Task and Suite data model (ADR-0003, ADR-0008).

A Task is a prompt fragment, a score amount, and a verifier spec. A Suite is a versioned,
ordered registry of Tasks plus suite-level pins loaded from ``manifest.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TaskKind = Literal["onchain", "code"]
ParamClass = Literal["prompt", "verifier"]
ParamGenerator = Literal[
    "harness_tip",
    "high_entropy_nonce_amount_shannons",
    "recipient_args",
    "static",
]


@dataclass(frozen=True)
class ParamSpec:
    """Per-run parameter schema entry (ADR-0009)."""

    name: str
    param_class: ParamClass
    generator: ParamGenerator
    static_value: str | None = None


@dataclass(frozen=True)
class OnchainVerifierSpec:
    """Verifier spec for an on-chain Task: direct-RPC checks."""

    check: str
    rpc_method: str
    rpc_params: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Task:
    """One atomic benchmark unit: prompt + score + verifier."""

    id: str
    prompt_fragment: str
    score: int
    proof_file: str
    kind: TaskKind
    verifier: OnchainVerifierSpec | str
    param_schema: tuple[ParamSpec, ...] = ()


@dataclass(frozen=True)
class SuitePins:
    """Suite-level pins from the manifest (image digests, toolchain versions, etc.)."""

    docker_image_digest: str | None = None
    mcp_tools_digest: str | None = None
    scoring_schema_version: str | None = None
    toolchain_versions: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Suite:
    """A versioned, immutable registry of Tasks plus suite-level pins."""

    suite_semver: str
    chain_profile: str
    mcp_server_version: str
    tasks: tuple[Task, ...]
    pins: SuitePins