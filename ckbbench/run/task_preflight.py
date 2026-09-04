"""Fail-closed readiness checks for one independent Task attempt."""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, TypeVar

from ckbbench.run.task_attempt import (
    AttemptSchemaError,
    ExecutionSource,
    OwnershipJournalEntry,
    PreflightBinding,
    TaskAttemptIntent,
    TaskAttemptResult,
    artifact_sha256,
    validate_journal,
)

REQUIREMENTS_SCHEMA_VERSION = "ckbbench-task-preflight-requirements-v1"
EVIDENCE_SCHEMA_VERSION = "ckbbench-task-preflight-evidence-v2"
READINESS_OPERATION = "authenticated-non-generation-v1"
QUALIFICATION_KIND = "bounded-generation-compatibility-v1"

MAX_MODEL_EVIDENCE_AGE_SECONDS = 31 * 24 * 60 * 60
MAX_PROVIDER_READINESS_REQUESTS = 4
MAX_CKB_AI_PREFLIGHT_REQUESTS = 8

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HASH32 = re.compile(r"^0x[0-9a-f]{64}$")
_EVIDENCE_ID = re.compile(r"^preflight-[0-9a-f]{32}$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SECRET_MARKERS = (
    "sk-", "api_key", "apikey", "authorization", "bearer", "password",
    "private_key", "private-key", "seed phrase", "mnemonic", "://",
)

_CHECK_NAMES = (
    "source", "provider", "ckb_ai", "rpc", "signer", "funding", "dependencies", "outputs",
)
_LOCAL_CHECK_SEQUENCE = ("source", "provider", "ckb_ai", "dependencies", "outputs")
_READ_ONLY_CHAIN_CHECK_SEQUENCE = (
    "source", "provider", "ckb_ai", "rpc", "dependencies", "outputs",
)
_SIGNED_CHAIN_CHECK_SEQUENCE = _CHECK_NAMES
_FAILURE_CATEGORIES = frozenset({
    "interrupted", "invalid-intent", "reservation-mismatch", "source-drift", "stale-model-evidence",
    "provider-unready", "ckb-ai-unready", "rpc-unready", "network-mismatch",
    "signer-unready", "funding-insufficient", "dependency-mismatch", "output-not-fresh",
    "adapter-error", "deadline-exceeded", "malformed-observation",
})
_FAILURE_CATEGORIES_BY_STAGE = {
    "intent": frozenset({"interrupted", "invalid-intent", "reservation-mismatch"}),
    "source": frozenset({
        "source-drift", "adapter-error", "deadline-exceeded", "malformed-observation",
    }),
    "provider": frozenset({
        "stale-model-evidence", "provider-unready", "adapter-error", "deadline-exceeded",
        "malformed-observation",
    }),
    "ckb_ai": frozenset({
        "ckb-ai-unready", "network-mismatch", "adapter-error", "deadline-exceeded",
        "malformed-observation",
    }),
    "rpc": frozenset({
        "rpc-unready", "network-mismatch", "adapter-error", "deadline-exceeded",
        "malformed-observation",
    }),
    "signer": frozenset({
        "signer-unready", "adapter-error", "deadline-exceeded", "malformed-observation",
    }),
    "funding": frozenset({
        "funding-insufficient", "adapter-error", "deadline-exceeded", "malformed-observation",
    }),
    "dependencies": frozenset({
        "dependency-mismatch", "adapter-error", "deadline-exceeded", "malformed-observation",
    }),
    "outputs": frozenset({
        "output-not-fresh", "adapter-error", "deadline-exceeded", "malformed-observation",
    }),
}


class TaskPreflightError(ValueError):
    """A requirements, observation, or evidence record violates the reviewed contract."""


class ProviderUnavailable(TaskPreflightError):
    """The provider gate could not prove readiness before an attempt was reserved."""


class _CheckFailure(Exception):
    def __init__(self, stage: str, category: str) -> None:
        super().__init__(stage, category)
        self.stage = stage
        self.category = category


def allocate_preflight_id() -> str:
    return f"preflight-{secrets.token_hex(16)}"


def _evidence_id(value: Any) -> str:
    if not isinstance(value, str) or not _EVIDENCE_ID.fullmatch(value):
        raise TaskPreflightError("preflight evidence ID is invalid")
    return value


def _exact(document: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != fields:
        raise TaskPreflightError(f"{label} must contain exactly the reviewed fields")
    return document


def _public_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _PUBLIC_ID.fullmatch(value):
        raise TaskPreflightError(f"{field} must be a plain public identifier")
    lowered = value.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise TaskPreflightError(f"{field} contains a secret-shaped value")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise TaskPreflightError(f"{field} must be 64 lowercase hex characters")
    return value


def _hash32(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HASH32.fullmatch(value):
        raise TaskPreflightError(f"{field} must be a 0x-prefixed 32-byte hash")
    return value


def _nonnegative(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TaskPreflightError(f"{field} must be a non-negative integer")
    return value


def _positive(value: Any, field: str) -> int:
    value = _nonnegative(value, field)
    if value == 0:
        raise TaskPreflightError(f"{field} must be greater than zero")
    return value


def _utc(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise TaskPreflightError(f"{field} must be whole-second RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TaskPreflightError(f"{field} must be a valid UTC timestamp") from None
    if parsed.tzinfo != timezone.utc:
        raise TaskPreflightError(f"{field} must be UTC")
    return value


def _pairs(value: Any, field: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, tuple) or not all(
        isinstance(item, tuple) and len(item) == 2 for item in value
    ):
        raise TaskPreflightError(f"{field} must be immutable identifier/digest pairs")
    checked = tuple((_public_id(key, field), _sha(digest, field)) for key, digest in value)
    if len(checked) > 256:
        raise TaskPreflightError(f"{field} exceeds the record limit")
    if checked != tuple(sorted(checked)) or len(checked) != len(set(checked)):
        raise TaskPreflightError(f"{field} must be unique and sorted")
    return checked


def _claims(value: Any, field: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, tuple) or not all(
        isinstance(item, tuple) and len(item) == 2 for item in value
    ):
        raise TaskPreflightError(f"{field} must be immutable resource claims")
    checked = tuple((_public_id(kind, field), _public_id(name, field)) for kind, name in value)
    if len(checked) > 256:
        raise TaskPreflightError(f"{field} exceeds the record limit")
    if checked != tuple(sorted(checked)) or len(checked) != len(set(checked)):
        raise TaskPreflightError(f"{field} must be unique and sorted")
    return checked


@dataclass(frozen=True)
class FundingRequirement:
    maximum_transfer_shannons: int
    fee_reserve_shannons: int
    safety_margin_shannons: int
    minimum_cell_count: int
    minimum_confirmations: int

    def __post_init__(self) -> None:
        for field in (
            "maximum_transfer_shannons", "fee_reserve_shannons", "safety_margin_shannons",
            "minimum_cell_count", "minimum_confirmations",
        ):
            _nonnegative(getattr(self, field), f"funding_requirement.{field}")

    @property
    def required_capacity_shannons(self) -> int:
        return (
            self.maximum_transfer_shannons
            + self.fee_reserve_shannons
            + self.safety_margin_shannons
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fee_reserve_shannons": self.fee_reserve_shannons,
            "maximum_transfer_shannons": self.maximum_transfer_shannons,
            "minimum_cell_count": self.minimum_cell_count,
            "minimum_confirmations": self.minimum_confirmations,
            "safety_margin_shannons": self.safety_margin_shannons,
        }

    @classmethod
    def from_dict(cls, document: Any) -> FundingRequirement:
        return cls(**_exact(document, {
            "fee_reserve_shannons", "maximum_transfer_shannons", "minimum_cell_count",
            "minimum_confirmations", "safety_margin_shannons",
        }, "funding requirement"))


@dataclass(frozen=True)
class TaskPreflightRequirements:
    requirements_id: str
    intent_sha256: str
    model_qualification_kind: str
    model_qualification_evidence_sha256: str
    model_qualification_utc: str
    model_evidence_max_age_seconds: int
    provider_readiness_operation: str
    provider_readiness_request_limit: int
    ckb_ai_surface_id: str
    ckb_ai_surface_sha256: str
    ckb_ai_server_version: str
    ckb_ai_catalog_sha256: str
    ckb_ai_request_limit: int
    ckb_ai_claims_live_chain: bool
    expected_chain_id: str | None
    expected_genesis_hash: str | None
    signer_required: bool
    expected_signer_handle: str | None
    expected_signer_address: str | None
    signing_policy_id: str | None
    signing_policy_sha256: str | None
    funding: FundingRequirement | None
    required_dependencies: tuple[tuple[str, str], ...]
    required_resource_claims: tuple[tuple[str, str], ...]
    expected_output_resources: tuple[tuple[str, str], ...]
    schema_version: str = REQUIREMENTS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _public_id(self.requirements_id, "requirements_id")
        _sha(self.intent_sha256, "requirements.intent_sha256")
        if self.model_qualification_kind != QUALIFICATION_KIND:
            raise TaskPreflightError("requirements use an unsupported model qualification kind")
        _sha(
            self.model_qualification_evidence_sha256,
            "requirements.model_qualification_evidence_sha256",
        )
        _utc(self.model_qualification_utc, "requirements.model_qualification_utc")
        evidence_age = _positive(
            self.model_evidence_max_age_seconds,
            "requirements.model_evidence_max_age_seconds",
        )
        if evidence_age > MAX_MODEL_EVIDENCE_AGE_SECONDS:
            raise TaskPreflightError("model qualification evidence age exceeds the hard ceiling")
        if self.provider_readiness_operation != READINESS_OPERATION:
            raise TaskPreflightError(
                "requirements use an unsupported provider readiness operation"
            )
        provider_limit = _positive(
            self.provider_readiness_request_limit,
            "requirements.provider_readiness_request_limit",
        )
        if provider_limit > MAX_PROVIDER_READINESS_REQUESTS:
            raise TaskPreflightError("provider readiness request limit exceeds the hard ceiling")
        _public_id(self.ckb_ai_surface_id, "requirements.ckb_ai_surface_id")
        _sha(self.ckb_ai_surface_sha256, "requirements.ckb_ai_surface_sha256")
        _public_id(self.ckb_ai_server_version, "requirements.ckb_ai_server_version")
        _sha(self.ckb_ai_catalog_sha256, "requirements.ckb_ai_catalog_sha256")
        ckb_ai_limit = _positive(self.ckb_ai_request_limit, "requirements.ckb_ai_request_limit")
        if ckb_ai_limit > MAX_CKB_AI_PREFLIGHT_REQUESTS:
            raise TaskPreflightError("CKB AI request limit exceeds the hard ceiling")
        if not isinstance(self.ckb_ai_claims_live_chain, bool):
            raise TaskPreflightError("requirements.ckb_ai_claims_live_chain must be boolean")
        if (self.expected_chain_id is None) != (self.expected_genesis_hash is None):
            raise TaskPreflightError("expected chain ID and genesis must be present together")
        if self.expected_chain_id is not None:
            _public_id(self.expected_chain_id, "requirements.expected_chain_id")
            _hash32(self.expected_genesis_hash, "requirements.expected_genesis_hash")
        if not isinstance(self.signer_required, bool):
            raise TaskPreflightError("requirements.signer_required must be boolean")
        signer_fields = (
            self.expected_signer_handle,
            self.expected_signer_address,
            self.signing_policy_id,
            self.signing_policy_sha256,
            self.funding,
        )
        if self.signer_required:
            if self.expected_chain_id is None:
                raise TaskPreflightError("a signer requirement needs an expected chain")
            if any(value is None for value in signer_fields):
                raise TaskPreflightError("on-chain requirements need signer and funding policy")
            _public_id(self.expected_signer_handle, "requirements.expected_signer_handle")
            _public_id(self.expected_signer_address, "requirements.expected_signer_address")
            _public_id(self.signing_policy_id, "requirements.signing_policy_id")
            _sha(self.signing_policy_sha256, "requirements.signing_policy_sha256")
            if not isinstance(self.funding, FundingRequirement):
                raise TaskPreflightError("requirements funding must be typed")
            if (
                self.funding.required_capacity_shannons == 0
                or self.funding.minimum_cell_count == 0
                or self.funding.minimum_confirmations == 0
            ):
                raise TaskPreflightError("on-chain funding requirements must reserve capacity")
        elif any(value is not None for value in signer_fields):
            raise TaskPreflightError("unsigned requirements cannot carry signer fields")
        if self.ckb_ai_claims_live_chain and self.expected_chain_id is None:
            raise TaskPreflightError("a live-chain CKB AI claim needs an expected chain")
        _pairs(self.required_dependencies, "requirements.required_dependencies")
        _claims(self.required_resource_claims, "requirements.required_resource_claims")
        _claims(self.expected_output_resources, "requirements.expected_output_resources")
        claims = set(self.required_resource_claims)
        if not any(kind == "workspace" for kind, _ in claims):
            raise TaskPreflightError("preflight requirements need a reserved workspace")
        if not any(kind == "runtime-name" for kind, _ in claims):
            raise TaskPreflightError("preflight requirements need a reserved runtime name")
        if self.signer_required:
            if ("signer", self.expected_signer_handle) not in claims:
                raise TaskPreflightError("the expected signer must be reserved")
            if not any(kind == "spendable-input" for kind, _ in claims):
                raise TaskPreflightError("on-chain requirements need reserved spendable inputs")
        if not set(self.expected_output_resources) <= set(self.required_resource_claims):
            raise TaskPreflightError("every output resource must be reserved in the journal")
        if self.schema_version != REQUIREMENTS_SCHEMA_VERSION:
            raise TaskPreflightError("preflight requirements schema is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ckb_ai_catalog_sha256": self.ckb_ai_catalog_sha256,
            "ckb_ai_claims_live_chain": self.ckb_ai_claims_live_chain,
            "ckb_ai_request_limit": self.ckb_ai_request_limit,
            "ckb_ai_server_version": self.ckb_ai_server_version,
            "ckb_ai_surface_id": self.ckb_ai_surface_id,
            "ckb_ai_surface_sha256": self.ckb_ai_surface_sha256,
            "expected_chain_id": self.expected_chain_id,
            "expected_genesis_hash": self.expected_genesis_hash,
            "expected_output_resources": [list(item) for item in self.expected_output_resources],
            "expected_signer_address": self.expected_signer_address,
            "expected_signer_handle": self.expected_signer_handle,
            "funding": None if self.funding is None else self.funding.to_dict(),
            "intent_sha256": self.intent_sha256,
            "model_evidence_max_age_seconds": self.model_evidence_max_age_seconds,
            "model_qualification_evidence_sha256": self.model_qualification_evidence_sha256,
            "model_qualification_kind": self.model_qualification_kind,
            "model_qualification_utc": self.model_qualification_utc,
            "provider_readiness_operation": self.provider_readiness_operation,
            "provider_readiness_request_limit": self.provider_readiness_request_limit,
            "required_dependencies": [list(item) for item in self.required_dependencies],
            "required_resource_claims": [list(item) for item in self.required_resource_claims],
            "requirements_id": self.requirements_id,
            "schema_version": self.schema_version,
            "signer_required": self.signer_required,
            "signing_policy_id": self.signing_policy_id,
            "signing_policy_sha256": self.signing_policy_sha256,
        }

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> TaskPreflightRequirements:
        raw = dict(_exact(document, {
            "ckb_ai_catalog_sha256", "ckb_ai_claims_live_chain", "ckb_ai_request_limit",
            "ckb_ai_server_version", "ckb_ai_surface_id", "ckb_ai_surface_sha256",
            "expected_chain_id", "expected_genesis_hash", "expected_output_resources",
            "expected_signer_address", "expected_signer_handle", "funding", "intent_sha256",
            "model_evidence_max_age_seconds", "model_qualification_evidence_sha256",
            "model_qualification_kind", "model_qualification_utc", "provider_readiness_operation",
            "provider_readiness_request_limit", "required_dependencies",
            "required_resource_claims", "requirements_id", "schema_version", "signer_required",
            "signing_policy_id", "signing_policy_sha256",
        }, "preflight requirements"))
        for field in (
            "expected_output_resources", "required_dependencies", "required_resource_claims",
        ):
            value = raw[field]
            if not isinstance(value, list) or not all(
                isinstance(item, list) and len(item) == 2 for item in value
            ):
                raise TaskPreflightError(f"{field} must be a list of pairs")
            raw[field] = tuple(tuple(item) for item in value)
        raw["funding"] = (
            None if raw["funding"] is None else FundingRequirement.from_dict(raw["funding"])
        )
        return cls(**raw)


@dataclass(frozen=True)
class SourceObservation:
    execution_source: ExecutionSource
    staged_change_count: int
    tracked_change_count: int
    untracked_execution_input_count: int
    untracked_execution_inputs_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution_source, ExecutionSource):
            raise TaskPreflightError("source observation needs a typed execution source")
        for field in (
            "staged_change_count", "tracked_change_count", "untracked_execution_input_count",
        ):
            _nonnegative(getattr(self, field), f"source.{field}")
        _sha(
            self.untracked_execution_inputs_sha256,
            "source.untracked_execution_inputs_sha256",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_source": self.execution_source.to_dict(),
            "staged_change_count": self.staged_change_count,
            "tracked_change_count": self.tracked_change_count,
            "untracked_execution_input_count": self.untracked_execution_input_count,
            "untracked_execution_inputs_sha256": self.untracked_execution_inputs_sha256,
        }


@dataclass(frozen=True)
class ProviderObservation:
    model_profile_id: str
    model_profile_sha256: str
    qualification_kind: str
    qualification_evidence_sha256: str
    qualification_utc: str
    operation: str
    authenticated: bool
    credential_present: bool
    ready: bool
    request_count: int
    generation_request_count: int
    body_sent: bool
    redirect_followed: bool

    def __post_init__(self) -> None:
        _public_id(self.model_profile_id, "provider.model_profile_id")
        _sha(self.model_profile_sha256, "provider.model_profile_sha256")
        _public_id(self.qualification_kind, "provider.qualification_kind")
        _sha(self.qualification_evidence_sha256, "provider.qualification_evidence_sha256")
        _utc(self.qualification_utc, "provider.qualification_utc")
        _public_id(self.operation, "provider.operation")
        for field in (
            "authenticated", "credential_present", "ready", "body_sent", "redirect_followed",
        ):
            if not isinstance(getattr(self, field), bool):
                raise TaskPreflightError(f"provider.{field} must be boolean")
        _nonnegative(self.request_count, "provider.request_count")
        _nonnegative(self.generation_request_count, "provider.generation_request_count")
        if self.generation_request_count > self.request_count:
            raise TaskPreflightError("provider generation count exceeds total requests")

    def to_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "body_sent": self.body_sent,
            "credential_present": self.credential_present,
            "generation_request_count": self.generation_request_count,
            "model_profile_id": self.model_profile_id,
            "model_profile_sha256": self.model_profile_sha256,
            "operation": self.operation,
            "qualification_evidence_sha256": self.qualification_evidence_sha256,
            "qualification_kind": self.qualification_kind,
            "qualification_utc": self.qualification_utc,
            "ready": self.ready,
            "redirect_followed": self.redirect_followed,
            "request_count": self.request_count,
        }


@dataclass(frozen=True)
class ChainIdentityObservation:
    chain_id: str
    genesis_hash: str
    tip_number: int
    tip_hash: str
    request_count: int

    def __post_init__(self) -> None:
        _public_id(self.chain_id, "chain.chain_id")
        _hash32(self.genesis_hash, "chain.genesis_hash")
        _nonnegative(self.tip_number, "chain.tip_number")
        _hash32(self.tip_hash, "chain.tip_hash")
        _positive(self.request_count, "chain.request_count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "genesis_hash": self.genesis_hash,
            "request_count": self.request_count,
            "tip_hash": self.tip_hash,
            "tip_number": self.tip_number,
        }

    def stable_identity(self) -> tuple[str, str]:
        return self.chain_id, self.genesis_hash

    @property
    def stable_identity_sha256(self) -> str:
        return artifact_sha256({
            "chain_id": self.chain_id,
            "genesis_hash": self.genesis_hash,
        })


@dataclass(frozen=True)
class CkbAiObservation:
    surface_id: str
    surface_sha256: str
    server_version: str
    catalog_sha256: str
    ready: bool
    request_count: int
    chain_identity: ChainIdentityObservation | None

    def __post_init__(self) -> None:
        _public_id(self.surface_id, "ckb_ai.surface_id")
        _sha(self.surface_sha256, "ckb_ai.surface_sha256")
        _public_id(self.server_version, "ckb_ai.server_version")
        _sha(self.catalog_sha256, "ckb_ai.catalog_sha256")
        if not isinstance(self.ready, bool):
            raise TaskPreflightError("ckb_ai.ready must be boolean")
        _positive(self.request_count, "ckb_ai.request_count")
        if self.chain_identity is not None and not isinstance(
            self.chain_identity, ChainIdentityObservation
        ):
            raise TaskPreflightError("CKB AI chain identity must be typed")
        if (
            self.chain_identity is not None
            and self.chain_identity.request_count > self.request_count
        ):
            raise TaskPreflightError("CKB AI chain request count exceeds its total")

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_sha256": self.catalog_sha256,
            "chain_identity": (
                None if self.chain_identity is None else self.chain_identity.to_dict()
            ),
            "ready": self.ready,
            "request_count": self.request_count,
            "server_version": self.server_version,
            "surface_id": self.surface_id,
            "surface_sha256": self.surface_sha256,
        }


@dataclass(frozen=True)
class SignerObservation:
    signer_handle: str
    public_address: str
    signing_policy_id: str
    signing_policy_sha256: str
    chain_identity_sha256: str
    single_assignment: bool
    agent_accessible: bool
    check_count: int

    def __post_init__(self) -> None:
        _public_id(self.signer_handle, "signer.signer_handle")
        _public_id(self.public_address, "signer.public_address")
        _public_id(self.signing_policy_id, "signer.signing_policy_id")
        _sha(self.signing_policy_sha256, "signer.signing_policy_sha256")
        _sha(self.chain_identity_sha256, "signer.chain_identity_sha256")
        if not isinstance(self.single_assignment, bool) or not isinstance(
            self.agent_accessible, bool
        ):
            raise TaskPreflightError("signer state flags must be boolean")
        _positive(self.check_count, "signer.check_count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_accessible": self.agent_accessible,
            "chain_identity_sha256": self.chain_identity_sha256,
            "check_count": self.check_count,
            "public_address": self.public_address,
            "signer_handle": self.signer_handle,
            "signing_policy_id": self.signing_policy_id,
            "signing_policy_sha256": self.signing_policy_sha256,
            "single_assignment": self.single_assignment,
        }


@dataclass(frozen=True)
class FundingObservation:
    signer_handle: str
    lease_resource_id: str
    chain_identity_sha256: str
    spendable_capacity_shannons: int
    cell_count: int
    minimum_confirmations: int
    cells_sha256: str
    request_count: int

    def __post_init__(self) -> None:
        _public_id(self.signer_handle, "funding.signer_handle")
        _public_id(self.lease_resource_id, "funding.lease_resource_id")
        _sha(self.chain_identity_sha256, "funding.chain_identity_sha256")
        _nonnegative(self.spendable_capacity_shannons, "funding.spendable_capacity_shannons")
        _nonnegative(self.cell_count, "funding.cell_count")
        _nonnegative(self.minimum_confirmations, "funding.minimum_confirmations")
        _sha(self.cells_sha256, "funding.cells_sha256")
        _positive(self.request_count, "funding.request_count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_count": self.cell_count,
            "cells_sha256": self.cells_sha256,
            "chain_identity_sha256": self.chain_identity_sha256,
            "lease_resource_id": self.lease_resource_id,
            "minimum_confirmations": self.minimum_confirmations,
            "request_count": self.request_count,
            "signer_handle": self.signer_handle,
            "spendable_capacity_shannons": self.spendable_capacity_shannons,
        }


@dataclass(frozen=True)
class DependencyObservation:
    dependencies: tuple[tuple[str, str], ...]
    chain_identity_sha256: str | None
    request_count: int

    def __post_init__(self) -> None:
        _pairs(self.dependencies, "dependencies")
        if self.chain_identity_sha256 is not None:
            _sha(self.chain_identity_sha256, "dependencies.chain_identity_sha256")
        _nonnegative(self.request_count, "dependencies.request_count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_identity_sha256": self.chain_identity_sha256,
            "dependencies": [list(item) for item in self.dependencies],
            "request_count": self.request_count,
        }


@dataclass(frozen=True)
class OutputObservation:
    resources: tuple[tuple[str, str], ...]
    fresh: bool
    symlink_count: int
    foreign_owner_count: int
    check_count: int

    def __post_init__(self) -> None:
        _claims(self.resources, "outputs.resources")
        if not isinstance(self.fresh, bool):
            raise TaskPreflightError("outputs.fresh must be boolean")
        for field in ("symlink_count", "foreign_owner_count"):
            _nonnegative(getattr(self, field), f"outputs.{field}")
        _positive(self.check_count, "outputs.check_count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_count": self.check_count,
            "foreign_owner_count": self.foreign_owner_count,
            "fresh": self.fresh,
            "resources": [list(item) for item in self.resources],
            "symlink_count": self.symlink_count,
        }


@dataclass(frozen=True)
class CheckEvidence:
    name: str
    status: str
    observation_sha256: str | None
    request_count: int | None

    def __post_init__(self) -> None:
        if self.name not in _CHECK_NAMES:
            raise TaskPreflightError("preflight check name is unsupported")
        if self.status not in {"passed", "failed"}:
            raise TaskPreflightError("preflight check status is unsupported")
        if self.observation_sha256 is not None:
            _sha(self.observation_sha256, "check.observation_sha256")
        if self.request_count is not None:
            _nonnegative(self.request_count, "check.request_count")
        if self.status == "passed" and (
            self.observation_sha256 is None or self.request_count is None
        ):
            raise TaskPreflightError("passed check needs exact observation and request count")
        if self.status == "failed" and (
            (self.observation_sha256 is None) != (self.request_count is None)
        ):
            raise TaskPreflightError("failed check evidence and request count must agree")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "observation_sha256": self.observation_sha256,
            "request_count": self.request_count,
            "status": self.status,
        }


@dataclass(frozen=True)
class TaskPreflightEvidence:
    evidence_id: str
    attempt_id: str
    intent_sha256: str
    requirements_sha256: str
    created_utc: str
    status: str
    failure_stage: str | None
    failure_category: str | None
    checks: tuple[CheckEvidence, ...]
    controller_request_count_status: str
    controller_request_count: int | None
    direct_chain_identity_sha256: str | None
    ckb_ai_chain_identity_sha256: str | None
    signer_observation_sha256: str | None
    funding_observation_sha256: str | None
    required_capacity_shannons: int | None
    spendable_capacity_shannons: int | None
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _evidence_id(self.evidence_id)
        _public_id(self.attempt_id, "evidence.attempt_id")
        _sha(self.intent_sha256, "evidence.intent_sha256")
        _sha(self.requirements_sha256, "evidence.requirements_sha256")
        _utc(self.created_utc, "evidence.created_utc")
        if self.status not in {"passed", "failed"}:
            raise TaskPreflightError("preflight evidence status is unsupported")
        if self.status == "passed":
            if self.failure_stage is not None or self.failure_category is not None:
                raise TaskPreflightError("passed preflight cannot carry a failure")
        else:
            if self.failure_stage not in _CHECK_NAMES and self.failure_stage != "intent":
                raise TaskPreflightError("failed preflight stage is unsupported")
            if self.failure_category not in _FAILURE_CATEGORIES:
                raise TaskPreflightError("failed preflight category is unsupported")
            if self.failure_category not in _FAILURE_CATEGORIES_BY_STAGE[self.failure_stage]:
                raise TaskPreflightError("failure category does not match its stage")
        if not isinstance(self.checks, tuple) or not all(
            isinstance(check, CheckEvidence) for check in self.checks
        ):
            raise TaskPreflightError("checks must be immutable typed records")
        positions = [_CHECK_NAMES.index(check.name) for check in self.checks]
        if positions != sorted(positions) or len(positions) != len(set(positions)):
            raise TaskPreflightError("preflight checks must be unique and ordered")
        names = tuple(check.name for check in self.checks)
        if not any(
            names == sequence[: len(names)]
            for sequence in (
                _LOCAL_CHECK_SEQUENCE,
                _READ_ONLY_CHAIN_CHECK_SEQUENCE,
                _SIGNED_CHAIN_CHECK_SEQUENCE,
            )
        ):
            raise TaskPreflightError("preflight checks do not form a supported sequence")
        if any(check.status == "failed" for check in self.checks[:-1]):
            raise TaskPreflightError("no check may run after the first failure")
        if self.status == "passed":
            if names not in {
                _LOCAL_CHECK_SEQUENCE,
                _READ_ONLY_CHAIN_CHECK_SEQUENCE,
                _SIGNED_CHAIN_CHECK_SEQUENCE,
            }:
                raise TaskPreflightError("passed preflight must contain every required check")
            if any(check.status != "passed" for check in self.checks):
                raise TaskPreflightError("passed preflight cannot contain a failed check")
        elif self.failure_stage == "intent":
            if self.checks:
                raise TaskPreflightError("intent failure cannot carry dependency checks")
        elif (
            not self.checks
            or self.checks[-1].status != "failed"
            or self.failure_stage != self.checks[-1].name
        ):
            raise TaskPreflightError("failure stage must match the terminal failed check")
        if self.status == "failed" and self.failure_stage != "intent":
            unknown_failure = self.failure_category in {
                "adapter-error",
                "deadline-exceeded",
                "malformed-observation",
            }
            terminal_unknown = self.checks[-1].request_count is None
            if unknown_failure != terminal_unknown:
                raise TaskPreflightError(
                    "adapter failure must carry unknown observation evidence"
                )
        if self.controller_request_count_status not in {"exact", "unknown"}:
            raise TaskPreflightError("controller request-count status is unsupported")
        has_unknown_count = bool(self.checks and self.checks[-1].request_count is None)
        if self.controller_request_count_status == "exact":
            if has_unknown_count:
                raise TaskPreflightError("exact request count cannot include an unknown check")
            if self.controller_request_count is None:
                raise TaskPreflightError("exact request count cannot be null")
            expected = sum(check.request_count or 0 for check in self.checks)
            if self.controller_request_count != expected:
                raise TaskPreflightError("controller request count does not match its checks")
        else:
            if self.controller_request_count is not None:
                raise TaskPreflightError("unknown controller request count must be null")
            if not has_unknown_count:
                raise TaskPreflightError(
                    "request count may be unknown only after adapter failure"
                )
        for field in (
            "direct_chain_identity_sha256", "ckb_ai_chain_identity_sha256",
            "signer_observation_sha256", "funding_observation_sha256",
        ):
            value = getattr(self, field)
            if value is not None:
                _sha(value, f"evidence.{field}")
        if self.spendable_capacity_shannons is not None and self.required_capacity_shannons is None:
            raise TaskPreflightError("spendable capacity needs a required-capacity binding")
        for value in (self.required_capacity_shannons, self.spendable_capacity_shannons):
            if value is not None:
                _nonnegative(value, "evidence funding capacity")
        if self.required_capacity_shannons == 0:
            raise TaskPreflightError("required on-chain capacity must be positive")
        if (
            self.spendable_capacity_shannons is not None
            and self.required_capacity_shannons is not None
            and self.spendable_capacity_shannons < self.required_capacity_shannons
        ):
            raise TaskPreflightError("passed funding evidence is below the required capacity")
        passed_names = {check.name for check in self.checks if check.status == "passed"}
        if (self.direct_chain_identity_sha256 is not None) != ("rpc" in passed_names):
            raise TaskPreflightError("direct chain evidence must match the RPC check")
        if (self.signer_observation_sha256 is not None) != ("signer" in passed_names):
            raise TaskPreflightError("signer evidence must match the signer check")
        if (self.funding_observation_sha256 is not None) != ("funding" in passed_names):
            raise TaskPreflightError("funding evidence must match the funding check")
        if (self.spendable_capacity_shannons is not None) != ("funding" in passed_names):
            raise TaskPreflightError("spendable capacity must match the funding check")
        if self.ckb_ai_chain_identity_sha256 is not None and (
            "ckb_ai" not in passed_names or "rpc" not in names
        ):
            raise TaskPreflightError("CKB AI chain evidence needs CKB AI identity and an RPC check")
        if "dependencies" in names and "rpc" not in names and any((
            self.direct_chain_identity_sha256,
            self.ckb_ai_chain_identity_sha256,
            self.signer_observation_sha256,
            self.funding_observation_sha256,
            self.required_capacity_shannons,
            self.spendable_capacity_shannons,
        )):
            raise TaskPreflightError("local dependency checks cannot carry chain evidence")
        if self.status == "passed" and names == _LOCAL_CHECK_SEQUENCE:
            if any((
                self.ckb_ai_chain_identity_sha256,
                self.required_capacity_shannons,
            )):
                raise TaskPreflightError("local preflight cannot carry live-chain evidence")
        if self.status == "passed" and names == _READ_ONLY_CHAIN_CHECK_SEQUENCE:
            if any((
                self.signer_observation_sha256,
                self.funding_observation_sha256,
                self.required_capacity_shannons,
                self.spendable_capacity_shannons,
            )):
                raise TaskPreflightError("read-only chain preflight cannot carry signer evidence")
        if self.status == "passed" and names == _SIGNED_CHAIN_CHECK_SEQUENCE:
            if self.required_capacity_shannons is None:
                raise TaskPreflightError("on-chain preflight needs required capacity")
        if self.schema_version != EVIDENCE_SCHEMA_VERSION:
            raise TaskPreflightError("preflight evidence schema is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "checks": [check.to_dict() for check in self.checks],
            "ckb_ai_chain_identity_sha256": self.ckb_ai_chain_identity_sha256,
            "controller_request_count": self.controller_request_count,
            "controller_request_count_status": self.controller_request_count_status,
            "created_utc": self.created_utc,
            "direct_chain_identity_sha256": self.direct_chain_identity_sha256,
            "evidence_id": self.evidence_id,
            "failure_category": self.failure_category,
            "failure_stage": self.failure_stage,
            "funding_observation_sha256": self.funding_observation_sha256,
            "intent_sha256": self.intent_sha256,
            "required_capacity_shannons": self.required_capacity_shannons,
            "requirements_sha256": self.requirements_sha256,
            "schema_version": self.schema_version,
            "signer_observation_sha256": self.signer_observation_sha256,
            "spendable_capacity_shannons": self.spendable_capacity_shannons,
            "status": self.status,
        }

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    def binding(self) -> PreflightBinding:
        return PreflightBinding(
            evidence_id=self.evidence_id,
            evidence_sha256=self.sha256,
            status=self.status,
        )

    @classmethod
    def from_dict(cls, document: Any) -> TaskPreflightEvidence:
        raw = dict(_exact(document, {
            "attempt_id", "checks", "ckb_ai_chain_identity_sha256",
            "controller_request_count", "controller_request_count_status", "created_utc",
            "direct_chain_identity_sha256", "evidence_id", "failure_category", "failure_stage",
            "funding_observation_sha256", "intent_sha256", "required_capacity_shannons",
            "requirements_sha256", "schema_version", "signer_observation_sha256",
            "spendable_capacity_shannons", "status",
        }, "preflight evidence"))
        checks = raw["checks"]
        if not isinstance(checks, list):
            raise TaskPreflightError("evidence checks must be a list")
        raw["checks"] = tuple(
            CheckEvidence(**_exact(check, {
                "name", "observation_sha256", "request_count", "status",
            }, "preflight check"))
            for check in checks
        )
        return cls(**raw)


class TaskPreflightProbe(Protocol):
    def source(self, *, timeout_seconds: float | None) -> SourceObservation: ...

    def provider(self, *, timeout_seconds: float | None) -> ProviderObservation: ...

    def ckb_ai(self, *, timeout_seconds: float | None) -> CkbAiObservation: ...

    def rpc(self, *, timeout_seconds: float | None) -> ChainIdentityObservation: ...

    def signer(self, *, timeout_seconds: float | None) -> SignerObservation: ...

    def funding(self, *, timeout_seconds: float | None) -> FundingObservation: ...

    def dependencies(self, *, timeout_seconds: float | None) -> DependencyObservation: ...

    def outputs(self, *, timeout_seconds: float | None) -> OutputObservation: ...


def validate_task_preflight_evidence(
    intent: TaskAttemptIntent,
    requirements: TaskPreflightRequirements,
    evidence: TaskPreflightEvidence,
) -> None:
    """Validate bindings that span the intent, requirements, and public evidence."""
    if not isinstance(intent, TaskAttemptIntent):
        raise TaskPreflightError("preflight evidence needs a typed attempt intent")
    if not isinstance(requirements, TaskPreflightRequirements):
        raise TaskPreflightError("preflight evidence needs typed requirements")
    if not isinstance(evidence, TaskPreflightEvidence):
        raise TaskPreflightError("preflight evidence must be a typed record")
    if (
        evidence.attempt_id != intent.attempt_id
        or evidence.intent_sha256 != intent.sha256
        or evidence.requirements_sha256 != requirements.sha256
    ):
        raise TaskPreflightError("preflight evidence does not bind its inputs")
    if evidence.created_utc < intent.created_utc:
        raise TaskPreflightError("preflight evidence predates its attempt")

    intent_mismatch = requirements.intent_sha256 != intent.sha256
    reported_intent_mismatch = (
        evidence.status == "failed"
        and evidence.failure_stage == "intent"
        and evidence.failure_category == "invalid-intent"
    )
    if intent_mismatch and not reported_intent_mismatch:
        raise TaskPreflightError("requirements-to-intent mismatch is reported incorrectly")
    on_chain_intent = intent.identity.chain_track != "local-hermetic"
    on_chain_requirements = requirements.expected_chain_id is not None
    if on_chain_intent != on_chain_requirements and not reported_intent_mismatch:
        raise TaskPreflightError("preflight requirements contradict the intent chain track")

    expected_capacity = (
        None if requirements.funding is None else requirements.funding.required_capacity_shannons
    )
    if evidence.required_capacity_shannons != expected_capacity:
        raise TaskPreflightError("preflight evidence has the wrong required capacity")
    ckb_ai_passed = any(
        check.name == "ckb_ai" and check.status == "passed" for check in evidence.checks
    )
    expected_ckb_ai_chain_evidence = requirements.ckb_ai_claims_live_chain and ckb_ai_passed
    if (evidence.ckb_ai_chain_identity_sha256 is not None) != expected_ckb_ai_chain_evidence:
        raise TaskPreflightError("CKB AI chain evidence contradicts the requirements")

    if requirements.expected_chain_id is None:
        expected_sequence = _LOCAL_CHECK_SEQUENCE
    elif requirements.signer_required:
        expected_sequence = _SIGNED_CHAIN_CHECK_SEQUENCE
    else:
        expected_sequence = _READ_ONLY_CHAIN_CHECK_SEQUENCE
    names = tuple(check.name for check in evidence.checks)
    if evidence.failure_stage != "intent" and names != expected_sequence[: len(names)]:
        raise TaskPreflightError("preflight check sequence contradicts the requirements")


def validate_preflight_result_binding(
    intent: TaskAttemptIntent,
    requirements: TaskPreflightRequirements,
    evidence: TaskPreflightEvidence,
    result: TaskAttemptResult,
) -> None:
    """Validate that a Task result reports the stored preflight outcome exactly."""
    validate_task_preflight_evidence(intent, requirements, evidence)
    if not isinstance(result, TaskAttemptResult):
        raise TaskPreflightError("preflight result binding needs a typed Task result")
    if result.preflight != evidence.binding():
        raise TaskPreflightError("Task result does not bind the stored preflight evidence")
    if evidence.status == "failed" and (
        result.outcome != "infra_fail"
        or result.correctness_eligible
        or result.grade.status != "not_scored"
        or result.usage.token_usage_status != "not_started"
        or result.agent_exit_status is not None
        or result.failure_stage != evidence.failure_stage
        or result.failure_category != evidence.failure_category
    ):
        raise TaskPreflightError("failed preflight is misclassified by the Task result")


_T = TypeVar("_T")


def _observe(
    stage: str,
    expected: type[_T],
    call: Callable[[float | None], _T],
    timeout_seconds: float | None,
    started: float,
    monotonic: Callable[[], float],
) -> _T:
    try:
        remaining = None
        if timeout_seconds is not None:
            remaining = timeout_seconds - (float(monotonic()) - started)
            if remaining <= 0:
                raise TimeoutError
        observation = call(remaining)
        if (
            timeout_seconds is not None
            and float(monotonic()) - started > timeout_seconds
        ):
            raise TimeoutError
    except Exception as exc:
        category = "deadline-exceeded" if isinstance(exc, TimeoutError) else "adapter-error"
        raise _CheckFailure(stage, category) from None
    if type(observation) is not expected:
        raise _CheckFailure(stage, "malformed-observation")
    return observation


def _age_seconds(earlier: str, later: str) -> int:
    first = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
    second = datetime.fromisoformat(later.replace("Z", "+00:00"))
    return int((second - first).total_seconds())


def _request_count(observation: Any) -> int:
    if isinstance(observation, SourceObservation | SignerObservation | OutputObservation):
        return 0
    return int(observation.request_count)


def _check_source(
    intent: TaskAttemptIntent,
    observation: SourceObservation,
) -> None:
    if observation.execution_source != intent.identity.execution_source:
        raise _CheckFailure("source", "source-drift")
    if (
        observation.staged_change_count
        or observation.tracked_change_count
        or observation.untracked_execution_input_count
    ):
        raise _CheckFailure("source", "source-drift")
    empty_digest = artifact_sha256({"execution_inputs": []})
    if observation.untracked_execution_inputs_sha256 != empty_digest:
        raise _CheckFailure("source", "source-drift")


def _check_provider(
    intent: TaskAttemptIntent,
    requirements: TaskPreflightRequirements,
    observation: ProviderObservation,
    checked_utc: str,
) -> None:
    if (
        observation.model_profile_id != intent.identity.model_profile_id
        or observation.model_profile_sha256 != intent.identity.model_profile_sha256
        or observation.qualification_kind != requirements.model_qualification_kind
        or observation.qualification_evidence_sha256
        != requirements.model_qualification_evidence_sha256
        or observation.qualification_utc != requirements.model_qualification_utc
    ):
        raise _CheckFailure("provider", "provider-unready")
    age = _age_seconds(observation.qualification_utc, checked_utc)
    if age < 0 or age > requirements.model_evidence_max_age_seconds:
        raise _CheckFailure("provider", "stale-model-evidence")
    if (
        observation.operation != requirements.provider_readiness_operation
        or not observation.authenticated
        or not observation.credential_present
        or not observation.ready
        or observation.request_count == 0
        or observation.request_count > requirements.provider_readiness_request_limit
        or observation.generation_request_count != 0
        or observation.body_sent
        or observation.redirect_followed
    ):
        raise _CheckFailure("provider", "provider-unready")


def _check_ckb_ai(
    requirements: TaskPreflightRequirements,
    observation: CkbAiObservation,
) -> None:
    if (
        observation.surface_id != requirements.ckb_ai_surface_id
        or observation.surface_sha256 != requirements.ckb_ai_surface_sha256
        or observation.server_version != requirements.ckb_ai_server_version
        or observation.catalog_sha256 != requirements.ckb_ai_catalog_sha256
        or not observation.ready
        or observation.request_count > requirements.ckb_ai_request_limit
    ):
        raise _CheckFailure("ckb_ai", "ckb-ai-unready")
    if requirements.ckb_ai_claims_live_chain:
        if observation.chain_identity is None:
            raise _CheckFailure("ckb_ai", "network-mismatch")
        expected = requirements.expected_chain_id, requirements.expected_genesis_hash
        if observation.chain_identity.stable_identity() != expected:
            raise _CheckFailure("ckb_ai", "network-mismatch")
    elif observation.chain_identity is not None:
        raise _CheckFailure("ckb_ai", "network-mismatch")


def _check_rpc(
    requirements: TaskPreflightRequirements,
    observation: ChainIdentityObservation,
) -> None:
    expected = requirements.expected_chain_id, requirements.expected_genesis_hash
    if observation.request_count > 4:
        raise _CheckFailure("rpc", "rpc-unready")
    if observation.stable_identity() != expected:
        raise _CheckFailure("rpc", "network-mismatch")


def _check_signer(
    requirements: TaskPreflightRequirements,
    journal_resources: set[tuple[str, str]],
    direct_chain: ChainIdentityObservation,
    observation: SignerObservation,
) -> None:
    if (
        observation.signer_handle != requirements.expected_signer_handle
        or observation.public_address != requirements.expected_signer_address
        or observation.signing_policy_id != requirements.signing_policy_id
        or observation.signing_policy_sha256 != requirements.signing_policy_sha256
        or observation.chain_identity_sha256 != direct_chain.stable_identity_sha256
        or not observation.single_assignment
        or observation.agent_accessible
        or observation.check_count > 4
        or ("signer", observation.signer_handle) not in journal_resources
    ):
        raise _CheckFailure("signer", "signer-unready")


def _check_funding(
    requirements: TaskPreflightRequirements,
    journal_resources: set[tuple[str, str]],
    direct_chain: ChainIdentityObservation,
    observation: FundingObservation,
) -> None:
    funding = requirements.funding
    if funding is None:
        raise _CheckFailure("funding", "funding-insufficient")
    if (
        observation.signer_handle != requirements.expected_signer_handle
        or observation.chain_identity_sha256 != direct_chain.stable_identity_sha256
        or ("spendable-input", observation.lease_resource_id) not in journal_resources
        or observation.spendable_capacity_shannons < funding.required_capacity_shannons
        or observation.cell_count < funding.minimum_cell_count
        or observation.minimum_confirmations < funding.minimum_confirmations
        or observation.request_count > 8
    ):
        raise _CheckFailure("funding", "funding-insufficient")


def _check_dependencies(
    requirements: TaskPreflightRequirements,
    direct_chain: ChainIdentityObservation | None,
    observation: DependencyObservation,
) -> None:
    expected_chain_sha256 = (
        None if direct_chain is None else direct_chain.stable_identity_sha256
    )
    if (
        observation.dependencies != requirements.required_dependencies
        or observation.chain_identity_sha256 != expected_chain_sha256
        or observation.request_count > max(1, len(requirements.required_dependencies) * 2)
    ):
        raise _CheckFailure("dependencies", "dependency-mismatch")


def _check_outputs(
    requirements: TaskPreflightRequirements,
    observation: OutputObservation,
) -> None:
    if (
        observation.resources != requirements.expected_output_resources
        or not observation.fresh
        or observation.symlink_count
        or observation.foreign_owner_count
        or observation.check_count > max(1, len(requirements.expected_output_resources) * 2)
    ):
        raise _CheckFailure("outputs", "output-not-fresh")


def require_provider_available_before_attempt(
    intent: TaskAttemptIntent,
    requirements: TaskPreflightRequirements,
    probe: TaskPreflightProbe,
    *,
    checked_utc: str,
    deadline_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProviderObservation:
    """Prove source identity and provider readiness without reserving a Task attempt."""
    _utc(checked_utc, "checked_utc")
    if deadline_seconds is not None and (
        isinstance(deadline_seconds, bool)
        or not isinstance(deadline_seconds, (int, float))
        or deadline_seconds <= 0
    ):
        raise TaskPreflightError("provider gate deadline must be positive")
    if requirements.intent_sha256 != intent.sha256:
        raise TaskPreflightError("provider gate requirements do not bind the prepared intent")
    if requirements.provider_readiness_request_limit != 1:
        raise TaskPreflightError("provider gate requires exactly one readiness request")

    started = float(monotonic())
    try:
        source = _observe(
            "source",
            SourceObservation,
            lambda remaining: probe.source(timeout_seconds=remaining),
            deadline_seconds,
            started,
            monotonic,
        )
        _check_source(intent, source)
    except _CheckFailure as failure:
        raise TaskPreflightError(
            f"pre-attempt {failure.stage} check failed before provider readiness"
        ) from None

    try:
        provider = _observe(
            "provider",
            ProviderObservation,
            lambda remaining: probe.provider(timeout_seconds=remaining),
            deadline_seconds,
            started,
            monotonic,
        )
    except _CheckFailure as failure:
        if failure.category in {"adapter-error", "deadline-exceeded"}:
            raise ProviderUnavailable(
                "provider unavailable; campaign paused before reserving an attempt"
            ) from None
        raise TaskPreflightError("pre-attempt provider observation is malformed") from None

    if (
        provider.model_profile_id != intent.identity.model_profile_id
        or provider.model_profile_sha256 != intent.identity.model_profile_sha256
        or provider.qualification_kind != requirements.model_qualification_kind
        or provider.qualification_evidence_sha256
        != requirements.model_qualification_evidence_sha256
        or provider.qualification_utc != requirements.model_qualification_utc
    ):
        raise TaskPreflightError("pre-attempt provider identity differs from the frozen slot")
    age = _age_seconds(provider.qualification_utc, checked_utc)
    if age < 0 or age > requirements.model_evidence_max_age_seconds:
        raise TaskPreflightError("pre-attempt model qualification is stale")
    if (
        provider.operation != requirements.provider_readiness_operation
        or provider.request_count != requirements.provider_readiness_request_limit
        or provider.generation_request_count != 0
        or provider.body_sent
        or provider.redirect_followed
    ):
        raise TaskPreflightError("pre-attempt provider check violated its readiness contract")
    if not provider.authenticated or not provider.credential_present or not provider.ready:
        raise ProviderUnavailable(
            "provider unavailable; campaign paused before reserving an attempt"
        )
    return provider


def _evidence(
    *,
    evidence_id: str,
    intent: TaskAttemptIntent,
    requirements: TaskPreflightRequirements,
    created_utc: str,
    status: str,
    checks: list[CheckEvidence],
    failure: _CheckFailure | None,
    request_count_known: bool,
    direct_chain: ChainIdentityObservation | None,
    ckb_ai: CkbAiObservation | None,
    signer: SignerObservation | None,
    funding: FundingObservation | None,
) -> TaskPreflightEvidence:
    evidence = TaskPreflightEvidence(
        evidence_id=evidence_id,
        attempt_id=intent.attempt_id,
        intent_sha256=intent.sha256,
        requirements_sha256=requirements.sha256,
        created_utc=created_utc,
        status=status,
        failure_stage=None if failure is None else failure.stage,
        failure_category=None if failure is None else failure.category,
        checks=tuple(checks),
        controller_request_count_status="exact" if request_count_known else "unknown",
        controller_request_count=(
            sum(check.request_count or 0 for check in checks) if request_count_known else None
        ),
        direct_chain_identity_sha256=(
            None if direct_chain is None else artifact_sha256(direct_chain.to_dict())
        ),
        ckb_ai_chain_identity_sha256=(
            None
            if ckb_ai is None or ckb_ai.chain_identity is None
            else artifact_sha256(ckb_ai.chain_identity.to_dict())
        ),
        signer_observation_sha256=(
            None if signer is None else artifact_sha256(signer.to_dict())
        ),
        funding_observation_sha256=(
            None if funding is None else artifact_sha256(funding.to_dict())
        ),
        required_capacity_shannons=(
            None
            if requirements.funding is None
            else requirements.funding.required_capacity_shannons
        ),
        spendable_capacity_shannons=(
            None if funding is None else funding.spendable_capacity_shannons
        ),
    )
    validate_task_preflight_evidence(intent, requirements, evidence)
    return evidence


def run_task_preflight(
    intent: TaskAttemptIntent,
    journal: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
    requirements: TaskPreflightRequirements,
    probe: TaskPreflightProbe,
    *,
    checked_utc: str,
    evidence_id: str | None = None,
    deadline_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> TaskPreflightEvidence:
    """Run the exact ordered preflight and stop at the first unsafe observation."""
    _utc(checked_utc, "checked_utc")
    if deadline_seconds is not None and (
        isinstance(deadline_seconds, bool)
        or not isinstance(deadline_seconds, (int, float))
        or deadline_seconds <= 0
    ):
        raise TaskPreflightError("preflight deadline must be positive")
    started = float(monotonic())
    selected_evidence_id = evidence_id or allocate_preflight_id()
    _evidence_id(selected_evidence_id)
    checks: list[CheckEvidence] = []
    direct_chain: ChainIdentityObservation | None = None
    ckb_ai: CkbAiObservation | None = None
    signer: SignerObservation | None = None
    funding: FundingObservation | None = None

    try:
        if requirements.intent_sha256 != intent.sha256:
            raise _CheckFailure("intent", "invalid-intent")
        state = validate_journal(intent, journal)
        if checked_utc < journal[-1].created_utc:
            raise _CheckFailure("intent", "invalid-intent")
        if any(entry.phase != "reserve" or entry.action != "claim" for entry in journal):
            raise _CheckFailure("intent", "reservation-mismatch")
        journal_resources = set(state.resources)
        if journal_resources != set(requirements.required_resource_claims):
            raise _CheckFailure("intent", "reservation-mismatch")
        if intent.identity.chain_track == "local-hermetic":
            if requirements.expected_chain_id is not None or requirements.signer_required:
                raise _CheckFailure("intent", "invalid-intent")
        elif requirements.expected_chain_id is None:
            raise _CheckFailure("intent", "invalid-intent")
    except (AttemptSchemaError, TaskPreflightError):
        failure = _CheckFailure("intent", "invalid-intent")
        return _evidence(
            evidence_id=selected_evidence_id,
            intent=intent,
            requirements=requirements,
            created_utc=checked_utc,
            status="failed",
            checks=checks,
            failure=failure,
            request_count_known=True,
            direct_chain=None,
            ckb_ai=None,
            signer=None,
            funding=None,
        )
    except _CheckFailure as failure:
        return _evidence(
            evidence_id=selected_evidence_id,
            intent=intent,
            requirements=requirements,
            created_utc=checked_utc,
            status="failed",
            checks=checks,
            failure=failure,
            request_count_known=True,
            direct_chain=None,
            ckb_ai=None,
            signer=None,
            funding=None,
        )

    def execute(
        stage: str,
        expected: type[_T],
        call: Callable[[float | None], _T],
        validate: Callable[[_T], None],
    ) -> _T:
        try:
            observation = _observe(
                stage,
                expected,
                call,
                None if deadline_seconds is None else float(deadline_seconds),
                started,
                monotonic,
            )
        except _CheckFailure:
            checks.append(CheckEvidence(stage, "failed", None, None))
            raise
        count = _request_count(observation)
        try:
            validate(observation)
        except _CheckFailure:
            checks.append(
                CheckEvidence(stage, "failed", artifact_sha256(observation.to_dict()), count)
            )
            raise
        checks.append(
            CheckEvidence(stage, "passed", artifact_sha256(observation.to_dict()), count)
        )
        return observation

    try:
        execute(
            "source", SourceObservation,
            lambda timeout: probe.source(timeout_seconds=timeout),
            lambda observation: _check_source(intent, observation),
        )
        execute(
            "provider", ProviderObservation,
            lambda timeout: probe.provider(timeout_seconds=timeout),
            lambda observation: _check_provider(intent, requirements, observation, checked_utc),
        )
        ckb_ai = execute(
            "ckb_ai", CkbAiObservation,
            lambda timeout: probe.ckb_ai(timeout_seconds=timeout),
            lambda observation: _check_ckb_ai(requirements, observation),
        )
        if requirements.expected_chain_id is not None:
            direct_chain = execute(
                "rpc", ChainIdentityObservation,
                lambda timeout: probe.rpc(timeout_seconds=timeout),
                lambda observation: _check_rpc(requirements, observation),
            )
            if requirements.signer_required:
                signer = execute(
                    "signer", SignerObservation,
                    lambda timeout: probe.signer(timeout_seconds=timeout),
                    lambda observation: _check_signer(
                        requirements, journal_resources, direct_chain, observation
                    ),
                )
                funding = execute(
                    "funding", FundingObservation,
                    lambda timeout: probe.funding(timeout_seconds=timeout),
                    lambda observation: _check_funding(
                        requirements, journal_resources, direct_chain, observation
                    ),
                )
        execute(
            "dependencies", DependencyObservation,
            lambda timeout: probe.dependencies(timeout_seconds=timeout),
            lambda observation: _check_dependencies(requirements, direct_chain, observation),
        )
        execute(
            "outputs", OutputObservation,
            lambda timeout: probe.outputs(timeout_seconds=timeout),
            lambda observation: _check_outputs(requirements, observation),
        )
    except _CheckFailure as failure:
        return _evidence(
            evidence_id=selected_evidence_id,
            intent=intent,
            requirements=requirements,
            created_utc=checked_utc,
            status="failed",
            checks=checks,
            failure=failure,
            request_count_known=checks[-1].request_count is not None,
            direct_chain=direct_chain,
            ckb_ai=ckb_ai,
            signer=signer,
            funding=funding,
        )

    return _evidence(
        evidence_id=selected_evidence_id,
        intent=intent,
        requirements=requirements,
        created_utc=checked_utc,
        status="passed",
        checks=checks,
        failure=None,
        request_count_known=True,
        direct_chain=direct_chain,
        ckb_ai=ckb_ai,
        signer=signer,
        funding=funding,
    )
