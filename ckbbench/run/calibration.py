"""Bounded non-accepted calibration for one released Task slot."""

from __future__ import annotations

import json
import math
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from ckbbench.run.attempt_store import AttemptEnvelope, AttemptStore
from ckbbench.run.campaign import (
    CampaignError,
    CampaignManifest,
    CampaignSlot,
    publish_document,
    validate_intent_for_slot,
)
from ckbbench.run.single_task import SingleTaskBackend, execute_single_task
from ckbbench.run.suite_release import CampaignReleaseBinding, SuiteReleaseError
from ckbbench.run.task_attempt import (
    AttemptSchemaError,
    TaskAttemptIntent,
    artifact_sha256,
    canonical_json_bytes,
    validate_public_artifact_values,
)
from ckbbench.run.task_preflight import TaskPreflightProbe, TaskPreflightRequirements

CALIBRATION_EVIDENCE_SCHEMA_VERSION = "ckbbench-calibration-evidence-v1"

_CALIBRATION_ID = re.compile(r"^calibration-[0-9a-f]{32}$")
_ATTEMPT_ID = re.compile(r"^attempt-[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_OUTCOMES = frozenset({"pass", "agent_fail", "infra_fail", "protocol_violation"})
_USAGE_STATUSES = frozenset({"complete", "incomplete", "not_started", "unavailable"})
_MAX_DOCUMENT_BYTES = 1 << 20


class CalibrationError(ValueError):
    """A calibration request or retained artifact violates its isolation contract."""


def _exact(document: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != fields:
        raise CalibrationError(f"{label} must contain exactly the reviewed fields")
    return document


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CalibrationError(f"{label} must be a bounded public identifier")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CalibrationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CalibrationError(f"{label} must be a non-negative integer")
    return value


def _duration(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalibrationError(f"{label} must be a finite non-negative duration")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise CalibrationError(f"{label} must be a finite non-negative duration")
    return result


@dataclass(frozen=True)
class PreparedCalibrationAttempt:
    intent: TaskAttemptIntent
    requirements: TaskPreflightRequirements
    preflight_probe: TaskPreflightProbe
    backend: SingleTaskBackend
    max_score: int

    def __post_init__(self) -> None:
        if not isinstance(self.intent, TaskAttemptIntent):
            raise CalibrationError("calibration runtime returned an untyped intent")
        if not isinstance(self.requirements, TaskPreflightRequirements):
            raise CalibrationError("calibration runtime returned untyped requirements")
        if self.requirements.intent_sha256 != self.intent.sha256:
            raise CalibrationError("calibration requirements do not bind their intent")
        if isinstance(self.max_score, bool) or not isinstance(self.max_score, int):
            raise CalibrationError("calibration runtime returned an invalid maximum score")
        if self.max_score <= 0:
            raise CalibrationError("calibration maximum score must be positive")


class CalibrationRuntimeFactory(Protocol):
    """Prepare private adapters without performing external activity."""

    def prepare_calibration(
        self,
        manifest: CampaignManifest,
        slot: CampaignSlot,
        calibration_id: str,
    ) -> PreparedCalibrationAttempt: ...


@dataclass(frozen=True)
class CalibrationEvidence:
    calibration_id: str
    accepted_campaign_eligible: bool
    source_campaign_manifest_sha256: str
    suite_freeze_sha256: str
    slot_id: str
    task_id: str
    task_content_sha256: str
    arm: str
    model_variant_id: str
    execution_contract_sha256: str
    budget_profile_sha256: str
    attempt_id: str
    intent_sha256: str
    preflight_requirements_sha256: str
    preflight_evidence_sha256: str
    result_sha256: str
    cleanup_receipt_sha256s: tuple[str, ...]
    cleanup_complete: bool
    terminal_outcome: str
    agent_exit_status: str | None
    usage_status: str
    observed_steps: int
    observed_agent_wall_seconds: float
    observed_provider_calls: int
    recorded_utc: str
    schema_version: str = CALIBRATION_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.calibration_id, str) or _CALIBRATION_ID.fullmatch(
            self.calibration_id
        ) is None:
            raise CalibrationError("calibration ID must be an opaque calibration identifier")
        if self.accepted_campaign_eligible is not False:
            raise CalibrationError("calibration evidence can never be campaign-eligible")
        for field in (
            "source_campaign_manifest_sha256",
            "suite_freeze_sha256",
            "task_content_sha256",
            "execution_contract_sha256",
            "budget_profile_sha256",
            "intent_sha256",
            "preflight_requirements_sha256",
            "preflight_evidence_sha256",
            "result_sha256",
        ):
            _sha(getattr(self, field), f"calibration.{field}")
        for field in ("slot_id", "task_id", "model_variant_id"):
            _identifier(getattr(self, field), f"calibration.{field}")
        if self.arm not in {"B", "C"}:
            raise CalibrationError("calibration arm must be B or C")
        if not isinstance(self.attempt_id, str) or _ATTEMPT_ID.fullmatch(self.attempt_id) is None:
            raise CalibrationError("calibration attempt ID is invalid")
        if not isinstance(self.cleanup_receipt_sha256s, tuple) or not self.cleanup_receipt_sha256s:
            raise CalibrationError("calibration needs retained cleanup evidence")
        for value in self.cleanup_receipt_sha256s:
            _sha(value, "calibration cleanup receipt digest")
        if not isinstance(self.cleanup_complete, bool):
            raise CalibrationError("calibration cleanup status must be boolean")
        if self.terminal_outcome not in _OUTCOMES:
            raise CalibrationError("calibration terminal outcome is unsupported")
        if self.agent_exit_status is not None:
            _identifier(self.agent_exit_status, "calibration agent exit status")
        if self.usage_status not in _USAGE_STATUSES:
            raise CalibrationError("calibration usage status is unsupported")
        _nonnegative(self.observed_steps, "calibration observed steps")
        _duration(self.observed_agent_wall_seconds, "calibration observed agent wall time")
        _nonnegative(self.observed_provider_calls, "calibration observed provider calls")
        if not isinstance(self.recorded_utc, str) or _UTC.fullmatch(self.recorded_utc) is None:
            raise CalibrationError("calibration recorded time must be UTC")
        if self.schema_version != CALIBRATION_EVIDENCE_SCHEMA_VERSION:
            raise CalibrationError("calibration evidence schema version is unsupported")
        try:
            validate_public_artifact_values(self.to_dict())
        except AttemptSchemaError as exc:
            raise CalibrationError("calibration evidence contains a secret-shaped value") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_campaign_eligible": self.accepted_campaign_eligible,
            "agent_exit_status": self.agent_exit_status,
            "arm": self.arm,
            "attempt_id": self.attempt_id,
            "budget_profile_sha256": self.budget_profile_sha256,
            "calibration_id": self.calibration_id,
            "cleanup_complete": self.cleanup_complete,
            "cleanup_receipt_sha256s": list(self.cleanup_receipt_sha256s),
            "execution_contract_sha256": self.execution_contract_sha256,
            "intent_sha256": self.intent_sha256,
            "model_variant_id": self.model_variant_id,
            "observed_agent_wall_seconds": float(self.observed_agent_wall_seconds),
            "observed_provider_calls": self.observed_provider_calls,
            "observed_steps": self.observed_steps,
            "preflight_evidence_sha256": self.preflight_evidence_sha256,
            "preflight_requirements_sha256": self.preflight_requirements_sha256,
            "recorded_utc": self.recorded_utc,
            "result_sha256": self.result_sha256,
            "schema_version": self.schema_version,
            "slot_id": self.slot_id,
            "source_campaign_manifest_sha256": self.source_campaign_manifest_sha256,
            "suite_freeze_sha256": self.suite_freeze_sha256,
            "task_content_sha256": self.task_content_sha256,
            "task_id": self.task_id,
            "terminal_outcome": self.terminal_outcome,
            "usage_status": self.usage_status,
        }

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> CalibrationEvidence:
        raw = dict(_exact(document, {
            "accepted_campaign_eligible",
            "agent_exit_status",
            "arm",
            "attempt_id",
            "budget_profile_sha256",
            "calibration_id",
            "cleanup_complete",
            "cleanup_receipt_sha256s",
            "execution_contract_sha256",
            "intent_sha256",
            "model_variant_id",
            "observed_agent_wall_seconds",
            "observed_provider_calls",
            "observed_steps",
            "preflight_evidence_sha256",
            "preflight_requirements_sha256",
            "recorded_utc",
            "result_sha256",
            "schema_version",
            "slot_id",
            "source_campaign_manifest_sha256",
            "suite_freeze_sha256",
            "task_content_sha256",
            "task_id",
            "terminal_outcome",
            "usage_status",
        }, "calibration evidence"))
        receipts = raw["cleanup_receipt_sha256s"]
        if not isinstance(receipts, list):
            raise CalibrationError("calibration cleanup receipts must be an array")
        raw["cleanup_receipt_sha256s"] = tuple(receipts)
        return cls(**raw)


def validate_calibration_intent(
    manifest: CampaignManifest,
    slot: CampaignSlot,
    calibration_id: str,
    intent: TaskAttemptIntent,
) -> None:
    if not isinstance(calibration_id, str) or _CALIBRATION_ID.fullmatch(calibration_id) is None:
        raise CalibrationError("calibration ID must be an opaque calibration identifier")
    if intent.identity.campaign_id != calibration_id:
        raise CalibrationError("calibration intent does not bind its pilot identity")
    if intent.retry_ordinal != 0 or intent.retry is not None:
        raise CalibrationError("calibration cannot carry whole-Task retry provenance")
    normalized = replace(
        intent,
        identity=replace(intent.identity, campaign_id=manifest.campaign_id),
    )
    try:
        validate_intent_for_slot(manifest, slot, normalized)
    except CampaignError as exc:
        raise CalibrationError("calibration intent differs from its released source slot") from exc
    try:
        validate_intent_for_slot(manifest, slot, intent)
    except CampaignError:
        return
    raise CalibrationError("calibration intent is eligible for the accepted source campaign")


def calibration_evidence(
    calibration_id: str,
    manifest: CampaignManifest,
    slot: CampaignSlot,
    execution_contract_sha256: str,
    envelope: AttemptEnvelope,
) -> CalibrationEvidence:
    result = envelope.result
    return CalibrationEvidence(
        calibration_id=calibration_id,
        accepted_campaign_eligible=False,
        source_campaign_manifest_sha256=manifest.sha256,
        suite_freeze_sha256=manifest.suite_freeze_sha256,
        slot_id=slot.slot_id,
        task_id=slot.task_id,
        task_content_sha256=slot.task_content_sha256,
        arm=slot.arm,
        model_variant_id=slot.model_variant_id,
        execution_contract_sha256=execution_contract_sha256,
        budget_profile_sha256=slot.budget.profile_sha256,
        attempt_id=envelope.intent.attempt_id,
        intent_sha256=envelope.intent.sha256,
        preflight_requirements_sha256=envelope.preflight_requirements.sha256,
        preflight_evidence_sha256=envelope.preflight_evidence.sha256,
        result_sha256=result.sha256,
        cleanup_receipt_sha256s=tuple(receipt.sha256 for receipt in envelope.receipts),
        cleanup_complete=envelope.receipts[-1].status == "complete",
        terminal_outcome=result.outcome,
        agent_exit_status=result.agent_exit_status,
        usage_status=result.usage.token_usage_status,
        observed_steps=result.usage.model_calls,
        observed_agent_wall_seconds=result.timings.agent_seconds,
        observed_provider_calls=result.usage.provider_attempts,
        recorded_utc=envelope.receipts[-1].created_utc,
    )


def run_calibration(
    manifest: CampaignManifest,
    slot_id: str,
    calibration_id: str,
    store: AttemptStore,
    output_path: Path | str,
    binding: CampaignReleaseBinding,
    runtime: CalibrationRuntimeFactory,
) -> tuple[CalibrationEvidence, AttemptEnvelope]:
    binding.validate_manifest(manifest)
    matches = tuple(slot for slot in manifest.slots if slot.slot_id == slot_id)
    if len(matches) != 1:
        raise CalibrationError("calibration must select exactly one released slot")
    slot = matches[0]
    if store.root.exists():
        raise CalibrationError("calibration attempt root must be absent before execution")
    destination = Path(output_path)
    try:
        if destination.resolve(strict=False).is_relative_to(store.root.resolve(strict=False)):
            raise CalibrationError("calibration summary must be outside its attempt store")
        if destination.is_symlink() or destination.exists():
            raise CalibrationError("calibration summary must be absent before execution")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not stat.S_ISDIR(destination.parent.lstat().st_mode):
            raise CalibrationError("calibration summary parent must be a real directory")
    except OSError as exc:
        raise CalibrationError("cannot resolve calibration output paths") from exc

    prepared = runtime.prepare_calibration(manifest, slot, calibration_id)
    validate_calibration_intent(manifest, slot, calibration_id, prepared.intent)
    if prepared.max_score != slot.max_score:
        raise CalibrationError("calibration maximum score differs from the released Task")
    try:
        binding.validate_calibration_preflight(
            manifest,
            slot,
            prepared.intent,
            prepared.requirements,
        )
    except SuiteReleaseError as exc:
        raise CalibrationError("calibration preparation differs from the suite release") from exc
    contract = binding.execution_contract_for(slot)
    envelope = execute_single_task(
        store,
        prepared.intent,
        prepared.requirements,
        prepared.preflight_probe,
        prepared.backend,
        max_score=prepared.max_score,
        execution_contract=contract,
    )
    evidence = calibration_evidence(
        calibration_id,
        manifest,
        slot,
        contract.sha256,
        envelope,
    )
    try:
        publish_document(destination, evidence.to_dict(), "calibration evidence")
    except CampaignError as exc:
        raise CalibrationError("cannot retain calibration evidence") from exc
    return evidence, envelope


def load_calibration_evidence(path: Path | str) -> CalibrationEvidence:
    source = Path(path)
    try:
        if source.is_symlink() or not source.is_file():
            raise CalibrationError("calibration evidence must be a regular file")
        payload = source.read_bytes()
    except CalibrationError:
        raise
    except OSError as exc:
        raise CalibrationError("calibration evidence is unreadable") from exc
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise CalibrationError("calibration evidence exceeds its byte limit")
    try:
        document = json.loads(payload.decode("ascii"))
        evidence = CalibrationEvidence.from_dict(document)
    except (UnicodeError, json.JSONDecodeError, TypeError, CalibrationError) as exc:
        raise CalibrationError("calibration evidence is invalid") from exc
    if payload != canonical_json_bytes(evidence.to_dict()):
        raise CalibrationError("calibration evidence bytes are not canonical")
    return evidence
