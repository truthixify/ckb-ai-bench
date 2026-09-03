"""Immutable Task-attempt evidence schema.

The legacy matrix keeps ``RunResult`` and schema 1.8.0. Campaign execution records one Task per
envelope: an intent, hash-chained ownership entries, one result, and a cleanup or reconciliation
receipt chain. This module defines the public documents and their cross-document invariants;
orchestration lives in the separate single-Task supervisor.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from ckbbench.run.model_profile import ModelProfileError, model_variant_id
from ckbbench.verify.diagnostics import (
    VerificationDiagnosticError,
    VerificationDiagnostics,
)

INTENT_SCHEMA_VERSION = "ckbbench-task-attempt-intent-v1"
JOURNAL_SCHEMA_VERSION = "ckbbench-ownership-journal-entry-v1"
LEGACY_RESULT_SCHEMA_VERSION = "ckbbench-task-attempt-result-v2"
RESULT_SCHEMA_VERSION = "ckbbench-task-attempt-result-v3"
SUPPORTED_RESULT_SCHEMA_VERSIONS = frozenset({
    LEGACY_RESULT_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
})
RECEIPT_SCHEMA_VERSION = "ckbbench-cleanup-receipt-v1"
CANONICAL_JSON_VERSION = "canonical-json-sha256-v1"
CONCURRENCY_CONTRACT = "serialized-one-attempt-v1"
VERIFIER_PRIVATE_COMMITMENT_SCHEME = "sha256-canonical-blinded-256-v1"

Arm = Literal["B", "C"]
ChainTrack = Literal["testnet", "devnet", "local-hermetic"]
AttemptOutcome = Literal["pass", "agent_fail", "infra_fail", "protocol_violation"]

_ARMS = frozenset({"B", "C"})
_CHAIN_TRACKS = frozenset({"testnet", "devnet", "local-hermetic"})
_OUTCOMES = frozenset({"pass", "agent_fail", "infra_fail", "protocol_violation"})
_GRADE_STATUSES = frozenset({"passed", "failed", "not_scored"})
_PREFLIGHT_STATUSES = frozenset({"passed", "failed"})
_TOKEN_STATUSES = frozenset({"complete", "incomplete", "not_started", "unavailable"})
_TIMING_STATUSES = frozenset({"complete", "unavailable"})
_COST_STATUSES = frozenset({"complete", "lower_bound", "unavailable"})
_JOURNAL_PHASES = frozenset({"reserve", "preflight", "setup", "teardown", "reconcile"})
_JOURNAL_ACTIONS = frozenset({
    "claim", "mutation-intent", "acquired", "observed", "release-intent", "released",
    "retired", "permanent", "absent", "cleanup-failed",
})
_FINAL_ACTIONS = frozenset({"released", "retired", "permanent", "absent"})
_CLEANUP_ACTIONS = frozenset({
    "release-intent", "released", "retired", "absent", "cleanup-failed",
})
_RECEIPT_KINDS = frozenset({"cleanup", "reconciliation"})
_RECEIPT_STATUSES = frozenset({"complete", "incomplete"})
_FINAL_STATES = frozenset({"released", "retired", "permanent", "absent", "failed"})
_PHASE_ORDER = {
    phase: index
    for index, phase in enumerate(("reserve", "preflight", "setup", "teardown", "reconcile"))
}
_ACTION_TO_FINAL_STATE = {
    "released": "released",
    "retired": "retired",
    "permanent": "permanent",
    "absent": "absent",
}

_ATTEMPT_ID = re.compile(r"^attempt-[0-9a-f]{32}$")
_RECEIPT_ID = re.compile(r"^receipt-[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_PLAIN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?$")
_SEMVER = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_SECRET_VALUE_MARKERS = (
    "bearer ", "api_key=", "api-key=", "password=", "begin rsa", "begin openssh",
    "begin private key", "begin ec private key",
)
_SECRET_TOKEN = re.compile(r"(?:^|[^A-Za-z0-9])sk-[A-Za-z0-9]")


class AttemptSchemaError(ValueError):
    """An attempt document is malformed or contradicts its bindings."""


def canonical_json_bytes(document: dict[str, Any]) -> bytes:
    """Exact bytes used for every public artifact and digest."""
    try:
        payload = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise AttemptSchemaError("artifact is not canonical JSON data") from None
    return (payload + "\n").encode("ascii")


def artifact_sha256(document: dict[str, Any]) -> str:
    """Digest the exact canonical bytes written to disk."""
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def allocate_attempt_id() -> str:
    return f"attempt-{secrets.token_hex(16)}"


def allocate_receipt_id() -> str:
    return f"receipt-{secrets.token_hex(16)}"


def _exact(document: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != keys:
        raise AttemptSchemaError(f"{label} must contain exactly the reviewed fields")
    return document


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _PLAIN_ID.fullmatch(value):
        raise AttemptSchemaError(f"{field} must be a plain public identifier")
    return value


def _attempt_id(value: Any) -> str:
    if not isinstance(value, str) or not _ATTEMPT_ID.fullmatch(value):
        raise AttemptSchemaError("attempt_id must be an opaque attempt identifier")
    return value


def _receipt_id(value: Any) -> str:
    if not isinstance(value, str) or not _RECEIPT_ID.fullmatch(value):
        raise AttemptSchemaError("receipt_id must be an opaque receipt identifier")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise AttemptSchemaError(f"{field} must be 64 lowercase hex characters")
    return value


def _optional_sha(value: Any, field: str) -> str | None:
    return None if value is None else _sha(value, field)


def _image(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IMAGE_DIGEST.fullmatch(value):
        raise AttemptSchemaError(f"{field} must be a sha256 image digest")
    return value


def _utc(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        raise AttemptSchemaError(f"{field} must be a whole-second RFC3339 UTC timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AttemptSchemaError(f"{field} must be a valid UTC timestamp") from None
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AttemptSchemaError(f"{field} must be a non-negative integer")
    return value


def _sequence(value: Any, field: str) -> int:
    value = _nonnegative_int(value, field)
    if value > 999_999:
        raise AttemptSchemaError(f"{field} exceeds the six-digit artifact sequence limit")
    return value


def _positive_int(value: Any, field: str) -> int:
    value = _nonnegative_int(value, field)
    if value == 0:
        raise AttemptSchemaError(f"{field} must be greater than zero")
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    return None if value is None else _positive_int(value, field)


def _duration(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AttemptSchemaError(f"{field} must be a non-negative finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise AttemptSchemaError(f"{field} must be a non-negative finite number")
    return number


def _public_text(value: Any, field: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise AttemptSchemaError(f"{field} must be bounded public text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise AttemptSchemaError(f"{field} must be valid Unicode") from None
    if len(encoded) > maximum:
        raise AttemptSchemaError(f"{field} must be bounded public text")
    if any(ord(char) < 32 and char not in "\n\t" for char in value) or "\x7f" in value:
        raise AttemptSchemaError(f"{field} contains a control character")
    return value


def _reject_secret_values(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_secret_values(key)
            _reject_secret_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secret_values(child)
    elif isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in _SECRET_VALUE_MARKERS) or _SECRET_TOKEN.search(
            lowered
        ):
            raise AttemptSchemaError("public artifact contains a secret-shaped value")


def validate_public_artifact_values(value: Any) -> None:
    """Refuse secret-shaped values before adapter output reaches an immutable artifact."""
    _reject_secret_values(value)


@dataclass(frozen=True)
class TaskBudget:
    profile_id: str
    profile_sha256: str
    step_limit: int
    wall_time_limit_seconds: int
    provider_call_limit: int | None
    output_token_limit: int | None

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "budget.profile_id")
        _sha(self.profile_sha256, "budget.profile_sha256")
        _positive_int(self.step_limit, "budget.step_limit")
        _positive_int(self.wall_time_limit_seconds, "budget.wall_time_limit_seconds")
        _optional_positive_int(self.provider_call_limit, "budget.provider_call_limit")
        _optional_positive_int(self.output_token_limit, "budget.output_token_limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_token_limit": self.output_token_limit,
            "profile_id": self.profile_id,
            "profile_sha256": self.profile_sha256,
            "provider_call_limit": self.provider_call_limit,
            "step_limit": self.step_limit,
            "wall_time_limit_seconds": self.wall_time_limit_seconds,
        }

    @classmethod
    def from_dict(cls, document: Any) -> TaskBudget:
        raw = _exact(document, {
            "output_token_limit", "profile_id", "profile_sha256", "provider_call_limit",
            "step_limit", "wall_time_limit_seconds",
        }, "budget")
        return cls(**raw)


@dataclass(frozen=True)
class ExecutionSource:
    repository_revision: str
    source_tree_sha256: str
    agent_image_digest: str
    verifier_image_digest: str
    toolchain_sha256: str
    concurrency_contract: str = CONCURRENCY_CONTRACT

    def __post_init__(self) -> None:
        if not isinstance(self.repository_revision, str) or not _REVISION.fullmatch(
            self.repository_revision
        ):
            raise AttemptSchemaError("repository_revision must be a full lowercase revision")
        _sha(self.source_tree_sha256, "execution_source.source_tree_sha256")
        _image(self.agent_image_digest, "execution_source.agent_image_digest")
        _image(self.verifier_image_digest, "execution_source.verifier_image_digest")
        _sha(self.toolchain_sha256, "execution_source.toolchain_sha256")
        if self.concurrency_contract != CONCURRENCY_CONTRACT:
            raise AttemptSchemaError("execution source uses an unsupported concurrency contract")

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_image_digest": self.agent_image_digest,
            "concurrency_contract": self.concurrency_contract,
            "repository_revision": self.repository_revision,
            "source_tree_sha256": self.source_tree_sha256,
            "toolchain_sha256": self.toolchain_sha256,
            "verifier_image_digest": self.verifier_image_digest,
        }

    @classmethod
    def from_dict(cls, document: Any) -> ExecutionSource:
        raw = _exact(document, {
            "agent_image_digest", "concurrency_contract", "repository_revision",
            "source_tree_sha256", "toolchain_sha256", "verifier_image_digest",
        }, "execution_source")
        return cls(**raw)


@dataclass(frozen=True)
class AttemptIdentity:
    campaign_id: str
    campaign_manifest_sha256: str
    batch_id: str
    execution_plan_id: str
    execution_plan_sha256: str
    trial_id: str
    suite_semver: str
    suite_freeze_sha256: str
    task_id: str
    task_content_sha256: str
    arm: Arm
    treatment_profile_id: str
    treatment_profile_sha256: str
    chain_track: ChainTrack
    chain_profile_id: str
    chain_profile_sha256: str
    requested_model: str
    thinking_level: str
    model_variant_id: str
    model_profile_id: str
    model_profile_sha256: str
    budget: TaskBudget
    trial_challenge_id: str
    trial_challenge_sha256: str
    run_params_derivation: str
    prompt_params_sha256: str
    verifier_private_commitment_scheme: str
    verifier_private_commitment_sha256: str
    resource_equivalence_policy_id: str
    resource_equivalence_policy_sha256: str
    retry_policy_id: str
    retry_policy_sha256: str
    execution_source: ExecutionSource

    def __post_init__(self) -> None:
        if not isinstance(self.budget, TaskBudget) or not isinstance(
            self.execution_source, ExecutionSource
        ):
            raise AttemptSchemaError("identity needs typed budget and execution source records")
        for field in (
            "campaign_id", "batch_id", "execution_plan_id", "trial_id", "suite_semver",
            "task_id", "treatment_profile_id", "chain_profile_id", "model_profile_id",
            "trial_challenge_id", "run_params_derivation", "resource_equivalence_policy_id",
            "retry_policy_id",
        ):
            _identifier(getattr(self, field), f"identity.{field}")
        for field in (
            "campaign_manifest_sha256", "execution_plan_sha256", "suite_freeze_sha256",
            "task_content_sha256", "treatment_profile_sha256", "chain_profile_sha256",
            "model_profile_sha256", "trial_challenge_sha256", "prompt_params_sha256",
            "verifier_private_commitment_sha256", "resource_equivalence_policy_sha256",
            "retry_policy_sha256",
        ):
            _sha(getattr(self, field), f"identity.{field}")
        if not isinstance(self.arm, str) or self.arm not in _ARMS:
            raise AttemptSchemaError("identity.arm must be B or C")
        if not _SEMVER.fullmatch(self.suite_semver):
            raise AttemptSchemaError("identity.suite_semver must be semantic version x.y.z")
        if not isinstance(self.chain_track, str) or self.chain_track not in _CHAIN_TRACKS:
            raise AttemptSchemaError("identity.chain_track is unsupported")
        if self.verifier_private_commitment_scheme != VERIFIER_PRIVATE_COMMITMENT_SCHEME:
            raise AttemptSchemaError("identity uses an unsupported private commitment scheme")
        try:
            expected_variant = model_variant_id(
                requested_model=self.requested_model,
                thinking_level=self.thinking_level,
                profile_id=self.model_profile_id,
                profile_sha256=self.model_profile_sha256,
            )
        except ModelProfileError as exc:
            raise AttemptSchemaError("identity has invalid model-variant fields") from exc
        if self.model_variant_id != expected_variant:
            raise AttemptSchemaError("identity.model_variant_id does not match its profile")

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "batch_id": self.batch_id,
            "budget": self.budget.to_dict(),
            "campaign_id": self.campaign_id,
            "campaign_manifest_sha256": self.campaign_manifest_sha256,
            "chain_profile_id": self.chain_profile_id,
            "chain_profile_sha256": self.chain_profile_sha256,
            "chain_track": self.chain_track,
            "execution_plan_id": self.execution_plan_id,
            "execution_plan_sha256": self.execution_plan_sha256,
            "execution_source": self.execution_source.to_dict(),
            "model_profile_id": self.model_profile_id,
            "model_profile_sha256": self.model_profile_sha256,
            "model_variant_id": self.model_variant_id,
            "prompt_params_sha256": self.prompt_params_sha256,
            "requested_model": self.requested_model,
            "resource_equivalence_policy_id": self.resource_equivalence_policy_id,
            "resource_equivalence_policy_sha256": self.resource_equivalence_policy_sha256,
            "retry_policy_id": self.retry_policy_id,
            "retry_policy_sha256": self.retry_policy_sha256,
            "run_params_derivation": self.run_params_derivation,
            "suite_freeze_sha256": self.suite_freeze_sha256,
            "suite_semver": self.suite_semver,
            "task_content_sha256": self.task_content_sha256,
            "task_id": self.task_id,
            "thinking_level": self.thinking_level,
            "treatment_profile_id": self.treatment_profile_id,
            "treatment_profile_sha256": self.treatment_profile_sha256,
            "trial_challenge_id": self.trial_challenge_id,
            "trial_challenge_sha256": self.trial_challenge_sha256,
            "trial_id": self.trial_id,
            "verifier_private_commitment_scheme": self.verifier_private_commitment_scheme,
            "verifier_private_commitment_sha256": self.verifier_private_commitment_sha256,
        }

    @classmethod
    def from_dict(cls, document: Any) -> AttemptIdentity:
        keys = {
            "arm", "batch_id", "budget", "campaign_id", "campaign_manifest_sha256",
            "chain_profile_id", "chain_profile_sha256", "chain_track", "execution_plan_id",
            "execution_plan_sha256", "execution_source", "model_profile_id",
            "model_profile_sha256", "model_variant_id", "prompt_params_sha256",
            "requested_model", "resource_equivalence_policy_id",
            "resource_equivalence_policy_sha256", "retry_policy_id", "retry_policy_sha256",
            "run_params_derivation", "suite_freeze_sha256", "suite_semver",
            "task_content_sha256", "task_id", "thinking_level", "treatment_profile_id",
            "treatment_profile_sha256", "trial_challenge_id", "trial_challenge_sha256",
            "trial_id", "verifier_private_commitment_scheme",
            "verifier_private_commitment_sha256",
        }
        raw = dict(_exact(document, keys, "identity"))
        raw["budget"] = TaskBudget.from_dict(raw["budget"])
        raw["execution_source"] = ExecutionSource.from_dict(raw["execution_source"])
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True)
class RetryReference:
    predecessor_attempt_id: str
    predecessor_intent_sha256: str
    predecessor_result_sha256: str
    predecessor_cleanup_receipt_sha256: str

    def __post_init__(self) -> None:
        _attempt_id(self.predecessor_attempt_id)
        _sha(self.predecessor_intent_sha256, "retry.predecessor_intent_sha256")
        _sha(self.predecessor_result_sha256, "retry.predecessor_result_sha256")
        _sha(
            self.predecessor_cleanup_receipt_sha256,
            "retry.predecessor_cleanup_receipt_sha256",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "predecessor_attempt_id": self.predecessor_attempt_id,
            "predecessor_cleanup_receipt_sha256": self.predecessor_cleanup_receipt_sha256,
            "predecessor_intent_sha256": self.predecessor_intent_sha256,
            "predecessor_result_sha256": self.predecessor_result_sha256,
        }

    @classmethod
    def from_dict(cls, document: Any) -> RetryReference:
        return cls(**_exact(document, {
            "predecessor_attempt_id", "predecessor_cleanup_receipt_sha256",
            "predecessor_intent_sha256", "predecessor_result_sha256",
        }, "retry"))


@dataclass(frozen=True)
class TaskAttemptIntent:
    attempt_id: str
    created_utc: str
    identity: AttemptIdentity
    retry_ordinal: int = 0
    retry: RetryReference | None = None
    schema_version: str = INTENT_SCHEMA_VERSION
    canonical_json: str = CANONICAL_JSON_VERSION

    def __post_init__(self) -> None:
        _attempt_id(self.attempt_id)
        _utc(self.created_utc, "intent.created_utc")
        if not isinstance(self.identity, AttemptIdentity):
            raise AttemptSchemaError("intent identity must be a typed record")
        if self.schema_version != INTENT_SCHEMA_VERSION:
            raise AttemptSchemaError("intent schema version is unsupported")
        if self.canonical_json != CANONICAL_JSON_VERSION:
            raise AttemptSchemaError("intent canonical JSON contract is unsupported")
        _nonnegative_int(self.retry_ordinal, "intent.retry_ordinal")
        if self.retry_ordinal not in (0, 1):
            raise AttemptSchemaError("retry_ordinal must be zero or one")
        if (self.retry_ordinal == 0) != (self.retry is None):
            raise AttemptSchemaError("retry reference must exist exactly for retry ordinal one")
        if self.retry is not None and not isinstance(self.retry, RetryReference):
            raise AttemptSchemaError("intent retry must be a typed reference")

    def to_dict(self) -> dict[str, Any]:
        document = {
            "attempt_id": self.attempt_id,
            "canonical_json": self.canonical_json,
            "created_utc": self.created_utc,
            "identity": self.identity.to_dict(),
            "retry": None if self.retry is None else self.retry.to_dict(),
            "retry_ordinal": self.retry_ordinal,
            "schema_version": self.schema_version,
        }
        _reject_secret_values(document)
        return document

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> TaskAttemptIntent:
        raw = dict(_exact(document, {
            "attempt_id", "canonical_json", "created_utc", "identity", "retry",
            "retry_ordinal", "schema_version",
        }, "intent"))
        raw["identity"] = AttemptIdentity.from_dict(raw["identity"])
        raw["retry"] = None if raw["retry"] is None else RetryReference.from_dict(raw["retry"])
        return cls(**raw)


@dataclass(frozen=True)
class OwnershipJournalEntry:
    attempt_id: str
    intent_sha256: str
    sequence: int
    created_utc: str
    phase: str
    action: str
    resource_kind: str
    resource_id: str
    details_sha256: str | None
    previous_entry_sha256: str | None
    schema_version: str = JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _attempt_id(self.attempt_id)
        _sha(self.intent_sha256, "journal.intent_sha256")
        _sequence(self.sequence, "journal.sequence")
        _utc(self.created_utc, "journal.created_utc")
        if not isinstance(self.phase, str) or self.phase not in _JOURNAL_PHASES:
            raise AttemptSchemaError("journal phase is unsupported")
        if not isinstance(self.action, str) or self.action not in _JOURNAL_ACTIONS:
            raise AttemptSchemaError("journal action is unsupported")
        if self.action == "claim" and self.phase in {"teardown", "reconcile"}:
            raise AttemptSchemaError("a resource claim must precede teardown")
        if self.action in _CLEANUP_ACTIONS and self.phase not in {"teardown", "reconcile"}:
            raise AttemptSchemaError("a cleanup action must occur in teardown or reconciliation")
        _identifier(self.resource_kind, "journal.resource_kind")
        _identifier(self.resource_id, "journal.resource_id")
        _optional_sha(self.details_sha256, "journal.details_sha256")
        _optional_sha(self.previous_entry_sha256, "journal.previous_entry_sha256")
        if self.sequence == 0 and self.previous_entry_sha256 is not None:
            raise AttemptSchemaError("first journal entry cannot name a predecessor")
        if self.sequence > 0 and self.previous_entry_sha256 is None:
            raise AttemptSchemaError("later journal entry must name its predecessor")
        if self.schema_version != JOURNAL_SCHEMA_VERSION:
            raise AttemptSchemaError("journal schema version is unsupported")

    def to_dict(self) -> dict[str, Any]:
        document = {
            "action": self.action,
            "attempt_id": self.attempt_id,
            "created_utc": self.created_utc,
            "details_sha256": self.details_sha256,
            "intent_sha256": self.intent_sha256,
            "phase": self.phase,
            "previous_entry_sha256": self.previous_entry_sha256,
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
        }
        _reject_secret_values(document)
        return document

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> OwnershipJournalEntry:
        return cls(**_exact(document, {
            "action", "attempt_id", "created_utc", "details_sha256", "intent_sha256", "phase",
            "previous_entry_sha256", "resource_id", "resource_kind", "schema_version", "sequence",
        }, "journal entry"))


@dataclass(frozen=True)
class PreflightBinding:
    evidence_id: str
    evidence_sha256: str
    status: str

    def __post_init__(self) -> None:
        _identifier(self.evidence_id, "preflight.evidence_id")
        _sha(self.evidence_sha256, "preflight.evidence_sha256")
        if not isinstance(self.status, str) or self.status not in _PREFLIGHT_STATUSES:
            raise AttemptSchemaError("preflight status is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, document: Any) -> PreflightBinding:
        return cls(**_exact(document, {"evidence_id", "evidence_sha256", "status"}, "preflight"))


@dataclass(frozen=True)
class TaskGrade:
    status: str
    verifier_score: int
    score_awarded: int
    max_score: int
    reason: str
    proof: str
    diagnostics: VerificationDiagnostics = field(
        default_factory=VerificationDiagnostics.unavailable
    )

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in _GRADE_STATUSES:
            raise AttemptSchemaError("grade status is unsupported")
        _nonnegative_int(self.verifier_score, "grade.verifier_score")
        _nonnegative_int(self.score_awarded, "grade.score_awarded")
        _positive_int(self.max_score, "grade.max_score")
        if self.verifier_score > self.max_score or self.score_awarded > self.max_score:
            raise AttemptSchemaError("grade score exceeds its maximum")
        _public_text(self.reason, "grade.reason")
        _public_text(self.proof, "grade.proof", maximum=16384)
        if not isinstance(self.diagnostics, VerificationDiagnostics):
            raise AttemptSchemaError("grade diagnostics must be a typed record")
        if self.status == "passed" and self.verifier_score != self.max_score:
            raise AttemptSchemaError("a passed grade must carry the full verifier score")
        if self.status == "failed" and self.verifier_score != 0:
            raise AttemptSchemaError("a failed grade must carry zero verifier score")
        if self.status == "not_scored" and (self.verifier_score or self.score_awarded):
            raise AttemptSchemaError("an unscored grade must carry zero scores")
        if self.diagnostics.status != "unavailable":
            if self.status == "not_scored":
                raise AttemptSchemaError(
                    "an unscored grade cannot carry verifier diagnostics"
                )
            if self.status == "passed" and (
                self.diagnostics.status != "complete"
                or self.diagnostics.criteria_failed != 0
            ):
                raise AttemptSchemaError(
                    "a passed grade cannot carry failed or incomplete verifier diagnostics"
                )
            if (
                self.status == "failed"
                and self.diagnostics.status == "complete"
                and self.diagnostics.criteria_failed == 0
            ):
                raise AttemptSchemaError(
                    "a failed grade cannot carry an all-passing verifier diagnostic"
                )

    def to_dict(self, *, include_diagnostics: bool = True) -> dict[str, Any]:
        document = {
            "max_score": self.max_score,
            "proof": self.proof,
            "reason": self.reason,
            "score_awarded": self.score_awarded,
            "status": self.status,
            "verifier_score": self.verifier_score,
        }
        if include_diagnostics:
            document["diagnostics"] = self.diagnostics.to_dict()
        return document

    @classmethod
    def from_dict(cls, document: Any, *, include_diagnostics: bool = True) -> TaskGrade:
        keys = {
            "max_score", "proof", "reason", "score_awarded", "status", "verifier_score",
        }
        if include_diagnostics:
            keys.add("diagnostics")
        raw = dict(_exact(document, keys, "grade"))
        if include_diagnostics:
            try:
                raw["diagnostics"] = VerificationDiagnostics.from_dict(raw["diagnostics"])
            except VerificationDiagnosticError as exc:
                raise AttemptSchemaError(f"grade diagnostics are invalid: {exc}") from exc
        return cls(**raw)


def _cost(value: Any, status: str) -> str | None:
    if status == "unavailable":
        if value is not None:
            raise AttemptSchemaError("unavailable cost must be null")
        return None
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise AttemptSchemaError("reported cost must be a canonical non-negative decimal string")
    try:
        if Decimal(value) < 0:
            raise AttemptSchemaError("reported cost must be non-negative")
    except InvalidOperation:
        raise AttemptSchemaError("reported cost must be a canonical decimal") from None
    return value


@dataclass(frozen=True)
class AttemptUsage:
    token_usage_status: str
    cost_status: str
    provider_reported_cost_usd: str | None
    model_calls: int
    provider_attempts: int
    provider_responses: int
    provider_retry_count: int
    provider_retry_delay_seconds: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    provider_failure_category: str | None
    provider_failure_counts: tuple[tuple[str, int], ...] = ()
    provider_response_model_counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.token_usage_status, str)
            or self.token_usage_status not in _TOKEN_STATUSES
        ):
            raise AttemptSchemaError("token usage status is unsupported")
        if not isinstance(self.cost_status, str) or self.cost_status not in _COST_STATUSES:
            raise AttemptSchemaError("cost status is unsupported")
        _cost(self.provider_reported_cost_usd, self.cost_status)
        counts = {
            name: _nonnegative_int(getattr(self, name), f"usage.{name}")
            for name in (
                "model_calls", "provider_attempts", "provider_responses",
                "provider_retry_count", "provider_retry_delay_seconds",
            )
        }
        if counts["provider_responses"] > counts["provider_attempts"]:
            raise AttemptSchemaError("provider responses cannot exceed attempts")
        if counts["provider_responses"] > counts["model_calls"]:
            raise AttemptSchemaError("provider responses cannot exceed logical model calls")
        if counts["provider_attempts"] and not counts["model_calls"]:
            raise AttemptSchemaError("provider attempts require a logical model call")
        if counts["provider_retry_count"] > counts["provider_attempts"]:
            raise AttemptSchemaError("provider retries cannot exceed attempts")
        tokens = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        if any(value is not None for value in tokens):
            if not all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in tokens
            ):
                raise AttemptSchemaError("token counts must be one complete non-negative triple")
            if (
                self.prompt_tokens + self.completion_tokens != self.total_tokens
            ):  # type: ignore[operator]
                raise AttemptSchemaError("token counts do not satisfy prompt + completion = total")
        if not isinstance(self.provider_failure_counts, tuple) or not all(
            isinstance(item, tuple) and len(item) == 2 for item in self.provider_failure_counts
        ):
            raise AttemptSchemaError("provider failure counts must be immutable key/count pairs")
        failures: set[str] = set()
        for category, count in self.provider_failure_counts:
            _identifier(category, "usage.provider_failure_counts category")
            _positive_int(count, "usage.provider_failure_counts count")
            if category in failures:
                raise AttemptSchemaError("provider failure categories must be unique")
            failures.add(category)
        if tuple(sorted(self.provider_failure_counts)) != self.provider_failure_counts:
            raise AttemptSchemaError("provider failure counts must be sorted")
        if self.provider_failure_category is not None:
            _identifier(self.provider_failure_category, "usage.provider_failure_category")
            if self.provider_failure_category == "multiple" and len(failures) < 2:
                raise AttemptSchemaError(
                    "multiple provider failure category needs multiple failures"
                )
            if (
                self.provider_failure_category != "multiple"
                and self.provider_failure_category not in failures
            ):
                raise AttemptSchemaError("provider failure category must summarize failure counts")
        elif failures:
            raise AttemptSchemaError("provider failure counts need a summary category")
        failure_total = sum(count for _category, count in self.provider_failure_counts)
        if failure_total > counts["provider_attempts"] - counts["provider_responses"]:
            raise AttemptSchemaError("provider failure counts exceed unanswered attempts")
        if counts["provider_retry_count"] > failure_total:
            raise AttemptSchemaError("provider retries must be backed by classified failures")
        if not isinstance(self.provider_response_model_counts, tuple) or not all(
            isinstance(item, tuple) and len(item) == 2
            for item in self.provider_response_model_counts
        ):
            raise AttemptSchemaError(
                "provider response model counts must be immutable key/count pairs"
            )
        response_models: set[str] = set()
        for model, count in self.provider_response_model_counts:
            _identifier(model, "usage.provider_response_model_counts model")
            _positive_int(count, "usage.provider_response_model_counts count")
            if model in response_models:
                raise AttemptSchemaError("provider response models must be unique")
            response_models.add(model)
        if (
            tuple(sorted(self.provider_response_model_counts))
            != self.provider_response_model_counts
        ):
            raise AttemptSchemaError("provider response model counts must be sorted")
        if sum(count for _model, count in self.provider_response_model_counts) != counts[
            "provider_responses"
        ]:
            raise AttemptSchemaError("every provider response must carry a model identity")
        if self.token_usage_status == "complete":
            if None in tokens or counts["model_calls"] == 0:
                raise AttemptSchemaError("complete usage needs calls and exact tokens")
            if not (
                counts["model_calls"] == counts["provider_attempts"]
                == counts["provider_responses"]
            ):
                raise AttemptSchemaError("complete usage needs one answered attempt per model call")
            if failures or counts["provider_retry_count"]:
                raise AttemptSchemaError("complete usage cannot claim failed provider attempts")
        elif self.token_usage_status in {"not_started", "unavailable"}:
            if any(counts.values()) or any(value is not None for value in tokens) or failures:
                raise AttemptSchemaError(
                    f"{self.token_usage_status} usage cannot carry provider activity"
                )
            if self.cost_status != "unavailable":
                raise AttemptSchemaError(
                    f"{self.token_usage_status} usage cannot carry provider cost"
                )
        elif counts["provider_attempts"] == 0:
            raise AttemptSchemaError("incomplete usage needs at least one provider attempt")

    def to_dict(self) -> dict[str, Any]:
        return {
            "completion_tokens": self.completion_tokens,
            "cost_status": self.cost_status,
            "model_calls": self.model_calls,
            "prompt_tokens": self.prompt_tokens,
            "provider_attempts": self.provider_attempts,
            "provider_failure_category": self.provider_failure_category,
            "provider_failure_counts": {
                category: count for category, count in self.provider_failure_counts
            },
            "provider_reported_cost_usd": self.provider_reported_cost_usd,
            "provider_response_model_counts": {
                model: count for model, count in self.provider_response_model_counts
            },
            "provider_responses": self.provider_responses,
            "provider_retry_count": self.provider_retry_count,
            "provider_retry_delay_seconds": self.provider_retry_delay_seconds,
            "token_usage_status": self.token_usage_status,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_dict(cls, document: Any) -> AttemptUsage:
        raw = dict(_exact(document, {
            "completion_tokens", "cost_status", "model_calls", "prompt_tokens",
            "provider_attempts", "provider_failure_category", "provider_failure_counts",
            "provider_reported_cost_usd", "provider_response_model_counts",
            "provider_responses", "provider_retry_count", "provider_retry_delay_seconds",
            "token_usage_status", "total_tokens",
        }, "usage"))
        failures = raw["provider_failure_counts"]
        if not isinstance(failures, dict):
            raise AttemptSchemaError("provider_failure_counts must be an object")
        raw["provider_failure_counts"] = tuple(sorted(failures.items()))
        response_models = raw["provider_response_model_counts"]
        if not isinstance(response_models, dict):
            raise AttemptSchemaError("provider_response_model_counts must be an object")
        raw["provider_response_model_counts"] = tuple(sorted(response_models.items()))
        return cls(**raw)


@dataclass(frozen=True)
class AttemptTimings:
    reservation_seconds: float
    preflight_seconds: float
    setup_seconds: float
    agent_seconds: float
    grading_seconds: float
    measurement_status: str = "complete"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.measurement_status, str)
            or self.measurement_status not in _TIMING_STATUSES
        ):
            raise AttemptSchemaError("timing measurement status is unsupported")
        for field in (
            "reservation_seconds", "preflight_seconds", "setup_seconds", "agent_seconds",
            "grading_seconds",
        ):
            _duration(getattr(self, field), f"timings.{field}")
        if self.measurement_status == "unavailable" and any(
            getattr(self, field) != 0.0
            for field in (
                "reservation_seconds", "preflight_seconds", "setup_seconds", "agent_seconds",
                "grading_seconds",
            )
        ):
            raise AttemptSchemaError("unavailable timings must use structural zero values")

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_seconds": float(self.agent_seconds),
            "grading_seconds": float(self.grading_seconds),
            "measurement_status": self.measurement_status,
            "preflight_seconds": float(self.preflight_seconds),
            "reservation_seconds": float(self.reservation_seconds),
            "setup_seconds": float(self.setup_seconds),
        }

    @classmethod
    def from_dict(cls, document: Any) -> AttemptTimings:
        return cls(**_exact(document, {
            "agent_seconds", "grading_seconds", "measurement_status", "preflight_seconds",
            "reservation_seconds", "setup_seconds",
        }, "timings"))


@dataclass(frozen=True)
class TaskAttemptResult:
    attempt_id: str
    created_utc: str
    intent_sha256: str
    identity: AttemptIdentity
    pre_teardown_journal_sha256: str
    preflight: PreflightBinding
    outcome: AttemptOutcome
    correctness_eligible: bool
    grade: TaskGrade
    usage: AttemptUsage
    timings: AttemptTimings
    initial_resource_equivalence_sha256: str
    agent_exit_status: str | None
    failure_stage: str | None
    failure_category: str | None
    schema_version: str = RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _attempt_id(self.attempt_id)
        _utc(self.created_utc, "result.created_utc")
        _sha(self.intent_sha256, "result.intent_sha256")
        _sha(self.pre_teardown_journal_sha256, "result.pre_teardown_journal_sha256")
        _sha(
            self.initial_resource_equivalence_sha256,
            "result.initial_resource_equivalence_sha256",
        )
        if not all(
            (
                isinstance(self.identity, AttemptIdentity),
                isinstance(self.preflight, PreflightBinding),
                isinstance(self.grade, TaskGrade),
                isinstance(self.usage, AttemptUsage),
                isinstance(self.timings, AttemptTimings),
            )
        ):
            raise AttemptSchemaError("result contains an untyped nested record")
        if not isinstance(self.outcome, str) or self.outcome not in _OUTCOMES:
            raise AttemptSchemaError("result outcome is unsupported")
        if not isinstance(self.correctness_eligible, bool):
            raise AttemptSchemaError("correctness_eligible must be boolean")
        if self.agent_exit_status is not None:
            _identifier(self.agent_exit_status, "result.agent_exit_status")
        if self.failure_stage is not None:
            _identifier(self.failure_stage, "result.failure_stage")
        if self.failure_category is not None:
            _identifier(self.failure_category, "result.failure_category")
        if self.schema_version not in SUPPORTED_RESULT_SCHEMA_VERSIONS:
            raise AttemptSchemaError("result schema version is unsupported")
        if (
            self.schema_version == LEGACY_RESULT_SCHEMA_VERSION
            and self.grade.diagnostics != VerificationDiagnostics.unavailable()
        ):
            raise AttemptSchemaError("legacy results cannot carry verifier diagnostics")
        if self.outcome == "infra_fail":
            if self.correctness_eligible or self.grade.status != "not_scored":
                raise AttemptSchemaError("infrastructure failure cannot carry correctness evidence")
            if self.failure_stage is None or self.failure_category is None:
                raise AttemptSchemaError(
                    "infrastructure failure needs a sanitized stage and category"
                )
        else:
            if not self.correctness_eligible or self.grade.status == "not_scored":
                raise AttemptSchemaError("scored outcome must carry correctness evidence")
            if self.agent_exit_status is None or self.usage.token_usage_status in {
                "not_started", "unavailable",
            }:
                raise AttemptSchemaError("scored outcome needs agent and provider evidence")
            if self.outcome == "pass" and (
                self.grade.status != "passed" or self.grade.score_awarded != self.grade.max_score
            ):
                raise AttemptSchemaError("pass outcome needs a full awarded verifier pass")
            if self.outcome == "agent_fail" and (
                self.grade.status != "failed" or self.grade.score_awarded != 0
            ):
                raise AttemptSchemaError("agent failure needs a zero-score verifier failure")
            if self.outcome == "protocol_violation" and self.grade.score_awarded != 0:
                raise AttemptSchemaError("protocol violation must award zero")
            expected_failure = {
                "pass": (None, None),
                "agent_fail": ("grading", "verifier-failed"),
                "protocol_violation": ("protocol", "treatment-violation"),
            }[self.outcome]
            if (self.failure_stage, self.failure_category) != expected_failure:
                raise AttemptSchemaError("scored outcome carries contradictory failure metadata")
        if self.outcome != "infra_fail" and self.timings.measurement_status != "complete":
            raise AttemptSchemaError("scored outcome needs measured timings")

    def to_dict(self) -> dict[str, Any]:
        document = {
            "agent_exit_status": self.agent_exit_status,
            "attempt_id": self.attempt_id,
            "correctness_eligible": self.correctness_eligible,
            "created_utc": self.created_utc,
            "failure_category": self.failure_category,
            "failure_stage": self.failure_stage,
            "grade": self.grade.to_dict(
                include_diagnostics=self.schema_version == RESULT_SCHEMA_VERSION
            ),
            "identity": self.identity.to_dict(),
            "initial_resource_equivalence_sha256": self.initial_resource_equivalence_sha256,
            "intent_sha256": self.intent_sha256,
            "outcome": self.outcome,
            "pre_teardown_journal_sha256": self.pre_teardown_journal_sha256,
            "preflight": self.preflight.to_dict(),
            "schema_version": self.schema_version,
            "timings": self.timings.to_dict(),
            "usage": self.usage.to_dict(),
        }
        _reject_secret_values(document)
        return document

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> TaskAttemptResult:
        raw = dict(_exact(document, {
            "agent_exit_status", "attempt_id", "correctness_eligible", "created_utc",
            "failure_category", "failure_stage", "grade", "identity",
            "initial_resource_equivalence_sha256", "intent_sha256", "outcome",
            "pre_teardown_journal_sha256", "preflight", "schema_version", "timings", "usage",
        }, "result"))
        schema_version = raw["schema_version"]
        if schema_version not in SUPPORTED_RESULT_SCHEMA_VERSIONS:
            raise AttemptSchemaError("result schema version is unsupported")
        raw["identity"] = AttemptIdentity.from_dict(raw["identity"])
        raw["preflight"] = PreflightBinding.from_dict(raw["preflight"])
        raw["grade"] = TaskGrade.from_dict(
            raw["grade"],
            include_diagnostics=schema_version == RESULT_SCHEMA_VERSION,
        )
        raw["usage"] = AttemptUsage.from_dict(raw["usage"])
        raw["timings"] = AttemptTimings.from_dict(raw["timings"])
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ResourceDisposition:
    resource_kind: str
    resource_id: str
    final_state: str
    journal_entry_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.resource_kind, "disposition.resource_kind")
        _identifier(self.resource_id, "disposition.resource_id")
        if not isinstance(self.final_state, str) or self.final_state not in _FINAL_STATES:
            raise AttemptSchemaError("resource disposition final state is unsupported")
        _sha(self.journal_entry_sha256, "disposition.journal_entry_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_state": self.final_state,
            "journal_entry_sha256": self.journal_entry_sha256,
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind,
        }

    @classmethod
    def from_dict(cls, document: Any) -> ResourceDisposition:
        return cls(**_exact(document, {
            "final_state", "journal_entry_sha256", "resource_id", "resource_kind",
        }, "resource disposition"))


@dataclass(frozen=True)
class CleanupReceipt:
    receipt_id: str
    attempt_id: str
    created_utc: str
    sequence: int
    kind: str
    status: str
    intent_sha256: str
    result_sha256: str
    pre_teardown_journal_sha256: str
    terminal_journal_sha256: str
    prior_receipt_sha256: str | None
    dispositions: tuple[ResourceDisposition, ...]
    schema_version: str = RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _receipt_id(self.receipt_id)
        _attempt_id(self.attempt_id)
        _utc(self.created_utc, "receipt.created_utc")
        _sequence(self.sequence, "receipt.sequence")
        if not isinstance(self.kind, str) or self.kind not in _RECEIPT_KINDS:
            raise AttemptSchemaError("receipt kind is unsupported")
        if not isinstance(self.status, str) or self.status not in _RECEIPT_STATUSES:
            raise AttemptSchemaError("receipt status is unsupported")
        for field in (
            "intent_sha256", "result_sha256", "pre_teardown_journal_sha256",
            "terminal_journal_sha256",
        ):
            _sha(getattr(self, field), f"receipt.{field}")
        _optional_sha(self.prior_receipt_sha256, "receipt.prior_receipt_sha256")
        if self.sequence == 0:
            if self.kind != "cleanup" or self.prior_receipt_sha256 is not None:
                raise AttemptSchemaError("first receipt must be cleanup with no predecessor")
        elif self.kind != "reconciliation" or self.prior_receipt_sha256 is None:
            raise AttemptSchemaError("later receipt must reconcile its predecessor")
        if not isinstance(self.dispositions, tuple) or not all(
            isinstance(item, ResourceDisposition) for item in self.dispositions
        ):
            raise AttemptSchemaError("receipt dispositions must be immutable typed records")
        keys = [(item.resource_kind, item.resource_id) for item in self.dispositions]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise AttemptSchemaError("receipt dispositions must be unique and sorted")
        if self.status == "complete" and any(
            item.final_state == "failed" for item in self.dispositions
        ):
            raise AttemptSchemaError("complete receipt cannot contain a failed disposition")
        if self.status == "incomplete" and not any(
            item.final_state == "failed" for item in self.dispositions
        ):
            raise AttemptSchemaError("incomplete receipt must name a failed disposition")
        if self.schema_version != RECEIPT_SCHEMA_VERSION:
            raise AttemptSchemaError("receipt schema version is unsupported")

    def to_dict(self) -> dict[str, Any]:
        document = {
            "attempt_id": self.attempt_id,
            "created_utc": self.created_utc,
            "dispositions": [item.to_dict() for item in self.dispositions],
            "intent_sha256": self.intent_sha256,
            "kind": self.kind,
            "pre_teardown_journal_sha256": self.pre_teardown_journal_sha256,
            "prior_receipt_sha256": self.prior_receipt_sha256,
            "receipt_id": self.receipt_id,
            "result_sha256": self.result_sha256,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "status": self.status,
            "terminal_journal_sha256": self.terminal_journal_sha256,
        }
        _reject_secret_values(document)
        return document

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> CleanupReceipt:
        raw = dict(_exact(document, {
            "attempt_id", "created_utc", "dispositions", "intent_sha256", "kind",
            "pre_teardown_journal_sha256", "prior_receipt_sha256", "receipt_id",
            "result_sha256", "schema_version", "sequence", "status",
            "terminal_journal_sha256",
        }, "receipt"))
        if not isinstance(raw["dispositions"], list):
            raise AttemptSchemaError("receipt dispositions must be a list")
        raw["dispositions"] = tuple(
            ResourceDisposition.from_dict(item) for item in raw["dispositions"]
        )
        return cls(**raw)


@dataclass(frozen=True)
class JournalState:
    terminal_sha256: str
    resources: tuple[tuple[str, str], ...]
    final_states: tuple[tuple[tuple[str, str], str, str], ...]


def validate_journal(
    intent: TaskAttemptIntent,
    entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
) -> JournalState:
    """Validate one contiguous hash chain and its per-resource transitions."""
    if not entries:
        raise AttemptSchemaError("an attempt journal must contain at least one ownership entry")
    previous: str | None = None
    previous_utc = intent.created_utc
    previous_phase = -1
    state: dict[tuple[str, str], tuple[str, str]] = {}
    final: dict[tuple[str, str], tuple[str, str]] = {}
    for expected_sequence, entry in enumerate(entries):
        if entry.attempt_id != intent.attempt_id or entry.intent_sha256 != intent.sha256:
            raise AttemptSchemaError("journal entry does not bind the attempt intent")
        if entry.sequence != expected_sequence or entry.previous_entry_sha256 != previous:
            raise AttemptSchemaError("journal chain is gapped, reordered or forked")
        phase = _PHASE_ORDER[entry.phase]
        if entry.created_utc < previous_utc or phase < previous_phase:
            raise AttemptSchemaError("journal chronology moves backwards")
        if expected_sequence == 0 and (entry.phase != "reserve" or entry.action != "claim"):
            raise AttemptSchemaError("journal must begin with a reserve claim")
        key = (entry.resource_kind, entry.resource_id)
        if entry.action == "claim":
            if key in state:
                raise AttemptSchemaError("a journal resource may be claimed only once")
            state[key] = (entry.action, entry.sha256)
        else:
            if key not in state:
                raise AttemptSchemaError("resource action occurred before its durable claim")
            if key in final:
                raise AttemptSchemaError("resource action occurred after final disposition")
            state[key] = (entry.action, entry.sha256)
            if entry.action in _FINAL_ACTIONS:
                final[key] = (entry.action, entry.sha256)
        previous = entry.sha256
        previous_utc = entry.created_utc
        previous_phase = phase
    final_rows = tuple(
        (key, action, digest) for key, (action, digest) in sorted(final.items())
    )
    return JournalState(
        terminal_sha256=previous or "",
        resources=tuple(sorted(state)),
        final_states=final_rows,
    )


def _journal_prefix(
    entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry], digest: str
) -> tuple[OwnershipJournalEntry, ...]:
    for index, entry in enumerate(entries):
        if entry.sha256 == digest:
            return tuple(entries[: index + 1])
    raise AttemptSchemaError("artifact references an unknown journal prefix")


def validate_result_binding(
    intent: TaskAttemptIntent,
    entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
    result: TaskAttemptResult,
) -> None:
    if result.attempt_id != intent.attempt_id or result.intent_sha256 != intent.sha256:
        raise AttemptSchemaError("result does not bind its attempt intent")
    if result.identity != intent.identity:
        raise AttemptSchemaError("result experimental identity differs from its intent")
    prefix = _journal_prefix(entries, result.pre_teardown_journal_sha256)
    validate_journal(intent, prefix)
    if any(entry.phase in {"teardown", "reconcile"} for entry in prefix):
        raise AttemptSchemaError("result journal prefix already contains teardown activity")
    if result.created_utc < prefix[-1].created_utc:
        raise AttemptSchemaError("result predates its journal prefix")
    suffix = tuple(entries[len(prefix):])
    if any(entry.phase not in {"teardown", "reconcile"} for entry in suffix):
        raise AttemptSchemaError("post-result journal activity must be teardown or reconciliation")
    if any(entry.action == "claim" for entry in suffix):
        raise AttemptSchemaError("post-result journal activity cannot claim a new resource")
    if result.preflight.status == "failed" and result.outcome != "infra_fail":
        raise AttemptSchemaError("failed preflight cannot produce a scored result")
    if result.outcome != "infra_fail" and any(
        action != "acquired" for action, _digest in state_for_entries(prefix).values()
    ):
        raise AttemptSchemaError("scored result requires every resource to be acquired")


def _validate_receipt_dispositions(
    intent: TaskAttemptIntent,
    entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
    receipt: CleanupReceipt,
) -> None:
    prefix = _journal_prefix(entries, receipt.terminal_journal_sha256)
    state = validate_journal(intent, prefix)
    supplied = {
        (item.resource_kind, item.resource_id): item for item in receipt.dispositions
    }
    if set(supplied) != set(state.resources):
        raise AttemptSchemaError("receipt must account for every owned resource exactly once")
    final = {key: (action, digest) for key, action, digest in state.final_states}
    for key, item in supplied.items():
        observed = final.get(key)
        if observed is None:
            if item.final_state != "failed":
                raise AttemptSchemaError("active resource must have a failed disposition")
            latest_action, latest_digest = state_for_entries(prefix)[key]
            if latest_action != "cleanup-failed":
                raise AttemptSchemaError("failed disposition needs a cleanup-failed journal entry")
            if item.journal_entry_sha256 != latest_digest:
                raise AttemptSchemaError(
                    "receipt disposition does not bind the latest journal entry"
                )
        else:
            action, digest = observed
            if (
                item.final_state != _ACTION_TO_FINAL_STATE[action]
                or item.journal_entry_sha256 != digest
            ):
                raise AttemptSchemaError("receipt disposition contradicts the ownership journal")


def state_for_entries(
    entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Latest action and digest per resource for an already validated journal prefix."""
    return {
        (entry.resource_kind, entry.resource_id): (entry.action, entry.sha256)
        for entry in entries
    }


def _reject_unsealed_cleanup_retries(
    entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
) -> None:
    failed_resources: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.resource_kind, entry.resource_id)
        if key in failed_resources:
            raise AttemptSchemaError(
                "cleanup failure must be sealed before retrying that resource"
            )
        if entry.action == "cleanup-failed":
            failed_resources.add(key)


def validate_receipt_chain(
    intent: TaskAttemptIntent,
    entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
    result: TaskAttemptResult,
    receipts: tuple[CleanupReceipt, ...] | list[CleanupReceipt],
    *,
    require_complete: bool = True,
    allow_pending_reconciliation: bool = False,
) -> None:
    validate_result_binding(intent, entries, result)
    result_prefix_length = len(
        _journal_prefix(entries, result.pre_teardown_journal_sha256)
    )
    if not receipts:
        _reject_unsealed_cleanup_retries(entries[result_prefix_length:])
        if any(entry.phase == "reconcile" for entry in entries):
            raise AttemptSchemaError("reconciliation journal has no failed cleanup receipt")
        if require_complete:
            raise AttemptSchemaError("attempt has no cleanup receipt")
        return
    prior: CleanupReceipt | None = None
    prior_prefix_length = result_prefix_length
    receipt_ids: set[str] = set()
    for expected_sequence, receipt in enumerate(receipts):
        if receipt.sequence != expected_sequence:
            raise AttemptSchemaError("receipt chain is gapped or reordered")
        if receipt.receipt_id in receipt_ids:
            raise AttemptSchemaError("receipt IDs must be unique within an attempt")
        receipt_ids.add(receipt.receipt_id)
        if receipt.attempt_id != intent.attempt_id or receipt.intent_sha256 != intent.sha256:
            raise AttemptSchemaError("receipt does not bind its attempt intent")
        if receipt.result_sha256 != result.sha256:
            raise AttemptSchemaError("receipt does not bind its Task result")
        if receipt.pre_teardown_journal_sha256 != result.pre_teardown_journal_sha256:
            raise AttemptSchemaError("receipt does not bind the result journal prefix")
        expected_prior = None if prior is None else prior.sha256
        if receipt.prior_receipt_sha256 != expected_prior:
            raise AttemptSchemaError("receipt chain predecessor digest is invalid")
        prefix = _journal_prefix(entries, receipt.terminal_journal_sha256)
        minimum_utc = max(result.created_utc, prefix[-1].created_utc)
        if prior is not None:
            minimum_utc = max(minimum_utc, prior.created_utc)
        if receipt.created_utc < minimum_utc:
            raise AttemptSchemaError("receipt predates the evidence it seals")
        if len(prefix) < prior_prefix_length:
            raise AttemptSchemaError("receipt journal prefixes cannot move backwards")
        if prior is not None and len(prefix) == prior_prefix_length:
            raise AttemptSchemaError("reconciliation receipt needs new journal evidence")
        if prior is None and any(entry.phase == "reconcile" for entry in prefix):
            raise AttemptSchemaError("initial cleanup receipt cannot contain reconciliation")
        if prior is not None and any(
            entry.phase != "reconcile" for entry in prefix[prior_prefix_length:]
        ):
            raise AttemptSchemaError("reconciliation receipt must bind reconciliation entries")
        if prior is not None and prior.status != "incomplete":
            raise AttemptSchemaError("a complete cleanup receipt cannot be reconciled")
        sealed_entries = prefix[prior_prefix_length:]
        _reject_unsealed_cleanup_retries(sealed_entries)
        if any(entry.action == "cleanup-failed" for entry in sealed_entries):
            if receipt.status != "incomplete":
                raise AttemptSchemaError(
                    "a cleanup failure must be sealed by an incomplete receipt"
                )
        _validate_receipt_dispositions(intent, entries, receipt)
        prior = receipt
        prior_prefix_length = len(prefix)
    if receipts[-1].terminal_journal_sha256 != entries[-1].sha256:
        latest_prefix = _journal_prefix(entries, receipts[-1].terminal_journal_sha256)
        trailing = entries[len(latest_prefix):]
        _reject_unsealed_cleanup_retries(trailing)
        if not (
            allow_pending_reconciliation
            and receipts[-1].status == "incomplete"
            and trailing
            and all(entry.phase == "reconcile" and entry.action != "claim" for entry in trailing)
        ):
            raise AttemptSchemaError("latest receipt does not bind the terminal journal")
    if require_complete and receipts[-1].status != "complete":
        raise AttemptSchemaError("attempt cleanup is not complete")


_RETRY_STABLE_IDENTITY_FIELDS = (
    "campaign_id", "campaign_manifest_sha256", "batch_id", "execution_plan_id",
    "execution_plan_sha256", "trial_id", "suite_semver", "suite_freeze_sha256", "task_id",
    "task_content_sha256", "arm", "treatment_profile_id", "treatment_profile_sha256",
    "chain_track", "chain_profile_id", "chain_profile_sha256", "requested_model",
    "thinking_level", "model_variant_id", "model_profile_id", "model_profile_sha256", "budget",
    "trial_challenge_id", "trial_challenge_sha256", "run_params_derivation",
    "resource_equivalence_policy_id", "resource_equivalence_policy_sha256", "retry_policy_id",
    "retry_policy_sha256", "execution_source",
)


def validate_retry_link(
    retry_intent: TaskAttemptIntent,
    predecessor_intent: TaskAttemptIntent,
    predecessor_entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
    predecessor_result: TaskAttemptResult,
    predecessor_receipts: tuple[CleanupReceipt, ...] | list[CleanupReceipt],
) -> None:
    """Validate the only accepted whole-Task retry transition."""
    validate_attempt_envelope(
        predecessor_intent,
        predecessor_entries,
        predecessor_result,
        predecessor_receipts,
    )
    if retry_intent.retry_ordinal != 1 or retry_intent.retry is None:
        raise AttemptSchemaError("retry attempt must have ordinal one and a predecessor")
    if predecessor_intent.retry_ordinal != 0:
        raise AttemptSchemaError("a whole-Task retry cannot itself be retried")
    if retry_intent.attempt_id == predecessor_intent.attempt_id:
        raise AttemptSchemaError("retry must allocate a fresh attempt ID")
    reference = retry_intent.retry
    if (
        reference.predecessor_attempt_id != predecessor_intent.attempt_id
        or reference.predecessor_intent_sha256 != predecessor_intent.sha256
        or reference.predecessor_result_sha256 != predecessor_result.sha256
        or not predecessor_receipts
        or reference.predecessor_cleanup_receipt_sha256 != predecessor_receipts[-1].sha256
    ):
        raise AttemptSchemaError("retry predecessor references do not match immutable artifacts")
    if predecessor_result.outcome != "infra_fail" or predecessor_result.correctness_eligible:
        raise AttemptSchemaError("only an unscored infrastructure result may be retried")
    if predecessor_receipts[-1].status != "complete":
        raise AttemptSchemaError("retry requires complete predecessor cleanup")
    if retry_intent.created_utc < predecessor_receipts[-1].created_utc:
        raise AttemptSchemaError("retry intent predates predecessor cleanup")
    for field in _RETRY_STABLE_IDENTITY_FIELDS:
        if getattr(retry_intent.identity, field) != getattr(predecessor_intent.identity, field):
            raise AttemptSchemaError("retry crosses its frozen planned-slot identity")
    if (
        retry_intent.identity.prompt_params_sha256
        == predecessor_intent.identity.prompt_params_sha256
        or retry_intent.identity.verifier_private_commitment_sha256
        == predecessor_intent.identity.verifier_private_commitment_sha256
    ):
        raise AttemptSchemaError(
            "retry must use fresh prompt and verifier-private integrity material"
        )


def validate_retry_resource_freshness(
    retry_intent: TaskAttemptIntent,
    retry_entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
    predecessor_intent: TaskAttemptIntent,
    predecessor_entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
) -> None:
    """Prove that a retry did not reclaim any predecessor-owned resource identity."""
    retry_resources = set(validate_journal(retry_intent, retry_entries).resources)
    predecessor_resources = set(
        validate_journal(predecessor_intent, predecessor_entries).resources
    )
    if retry_resources & predecessor_resources:
        raise AttemptSchemaError("retry must claim fresh resource identities")


def validate_attempt_envelope(
    intent: TaskAttemptIntent,
    entries: tuple[OwnershipJournalEntry, ...] | list[OwnershipJournalEntry],
    result: TaskAttemptResult,
    receipts: tuple[CleanupReceipt, ...] | list[CleanupReceipt],
    *,
    require_complete: bool = True,
) -> None:
    validate_journal(intent, entries)
    validate_result_binding(intent, entries, result)
    validate_receipt_chain(
        intent,
        entries,
        result,
        receipts,
        require_complete=require_complete,
        allow_pending_reconciliation=not require_complete,
    )
