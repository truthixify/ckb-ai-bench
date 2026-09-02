"""Immutable campaign plans and report resolutions."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ckbbench.run.model_profile import ModelProfileError, model_variant_id
from ckbbench.run.retry_policy import RETRY_POLICY_ID, RETRY_POLICY_SHA256
from ckbbench.run.task_preflight import MAX_MODEL_EVIDENCE_AGE_SECONDS, QUALIFICATION_KIND
from ckbbench.run.task_attempt import (
    CONCURRENCY_CONTRACT,
    AttemptSchemaError,
    ExecutionSource,
    TaskAttemptIntent,
    TaskBudget,
    artifact_sha256,
    canonical_json_bytes,
    validate_public_artifact_values,
)

CAMPAIGN_SCHEMA_VERSION = "ckbbench-campaign-manifest-v1"
QUALIFIED_CAMPAIGN_SCHEMA_VERSION = "ckbbench-campaign-manifest-v2"
MODEL_QUALIFICATION_SCHEMA_VERSION = "ckbbench-model-qualification-v1"
REPORT_RESOLUTION_SCHEMA_VERSION = "ckbbench-report-resolution-v1"
EXPLORATORY_PREVIEW_SCHEMA_VERSION = "ckbbench-exploratory-preview-v1"
STOPPING_RULE_ID = "serialized-evidence-stop-v1"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_CAMPAIGN_ID = re.compile(r"^campaign-[0-9a-f]{32}$")
_QUALIFICATION_ID = re.compile(r"^qualification-[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_ARMS = frozenset({"B", "C"})
_CHAIN_TRACKS = frozenset({"testnet", "devnet", "local-hermetic"})
_OUTCOMES = frozenset({"pass", "agent_fail", "infra_fail", "protocol_violation"})
_PREVIEW_STATES = frozenset({"active", "cleanup-pending", "cleanup-incomplete", "complete"})
_ATTEMPT_ID = re.compile(r"^attempt-[0-9a-f]{32}$")
_MAX_DOCUMENT_BYTES = 1 << 20

STOPPING_RULE = {
    "continue_after_exhausted_infrastructure_retry": True,
    "continue_after_scored_outcome": True,
    "id": STOPPING_RULE_ID,
    "pause_on_corrupt_evidence": True,
    "pause_on_incomplete_cleanup": True,
    "score_adaptive": False,
}
STOPPING_RULE_SHA256 = artifact_sha256(STOPPING_RULE)

Arm = Literal["B", "C"]
ChainTrack = Literal["testnet", "devnet", "local-hermetic"]


class CampaignError(ValueError):
    """A campaign plan or report resolution violates its immutable contract."""


@dataclass(frozen=True)
class CampaignQualification:
    qualification_id: str
    qualification_kind: str
    qualification_schema_version: str
    qualification_sha256: str
    completed_utc: str
    model_profile_id: str
    model_profile_sha256: str
    model_variant_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.qualification_id, str)
            or _QUALIFICATION_ID.fullmatch(self.qualification_id) is None
        ):
            raise CampaignError("qualification.qualification_id is invalid")
        for field in (
            "qualification_kind",
            "qualification_schema_version",
            "model_profile_id",
            "model_variant_id",
        ):
            _identifier(getattr(self, field), f"qualification.{field}")
        _sha(self.qualification_sha256, "qualification.qualification_sha256")
        _sha(self.model_profile_sha256, "qualification.model_profile_sha256")
        _utc(self.completed_utc, "qualification.completed_utc")
        if self.qualification_kind != QUALIFICATION_KIND:
            raise CampaignError("campaign model qualification kind is unsupported")
        if self.qualification_schema_version != MODEL_QUALIFICATION_SCHEMA_VERSION:
            raise CampaignError("campaign model qualification schema is unsupported")

    @property
    def profile_key(self) -> tuple[str, str]:
        return self.model_profile_id, self.model_profile_sha256

    def to_dict(self) -> dict[str, Any]:
        return _public({
            "completed_utc": self.completed_utc,
            "model_profile_id": self.model_profile_id,
            "model_profile_sha256": self.model_profile_sha256,
            "model_variant_id": self.model_variant_id,
            "qualification_id": self.qualification_id,
            "qualification_kind": self.qualification_kind,
            "qualification_schema_version": self.qualification_schema_version,
            "qualification_sha256": self.qualification_sha256,
        }, "campaign qualification")

    @classmethod
    def from_dict(cls, document: Any) -> CampaignQualification:
        return cls(**_exact(document, {
            "completed_utc",
            "model_profile_id",
            "model_profile_sha256",
            "model_variant_id",
            "qualification_id",
            "qualification_kind",
            "qualification_schema_version",
            "qualification_sha256",
        }, "campaign qualification"))


def _exact(document: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != keys:
        raise CampaignError(f"{label} must contain exactly the reviewed fields")
    return document


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CampaignError(f"{label} must be a bounded public identifier")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CampaignError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _utc(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CampaignError(f"{label} must be UTC with a Z suffix")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CampaignError(f"{label} must be an ISO-8601 UTC timestamp") from None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise CampaignError(f"{label} must be UTC")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CampaignError(f"{label} must be a positive integer")
    return value


def _tuple_of(value: Any, expected: type, label: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple) or not all(type(item) is expected for item in value):
        raise CampaignError(f"{label} must contain immutable typed records")
    return value


def _public(document: dict[str, Any], label: str) -> dict[str, Any]:
    try:
        validate_public_artifact_values(document)
    except AttemptSchemaError as exc:
        raise CampaignError(f"{label} contains a secret-shaped value") from exc
    return document


@dataclass(frozen=True)
class CampaignBatch:
    batch_id: str
    slot_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.batch_id, "batch.batch_id")
        if not isinstance(self.slot_ids, tuple) or not self.slot_ids:
            raise CampaignError("batch.slot_ids must be a non-empty immutable sequence")
        for value in self.slot_ids:
            _identifier(value, "batch.slot_ids item")
        if len(set(self.slot_ids)) != len(self.slot_ids):
            raise CampaignError("a batch cannot repeat a slot")

    def to_dict(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "slot_ids": list(self.slot_ids)}

    @classmethod
    def from_dict(cls, document: Any) -> CampaignBatch:
        raw = dict(_exact(document, {"batch_id", "slot_ids"}, "campaign batch"))
        if not isinstance(raw["slot_ids"], list):
            raise CampaignError("batch.slot_ids must be an array")
        raw["slot_ids"] = tuple(raw["slot_ids"])
        return cls(**raw)


@dataclass(frozen=True)
class CampaignSlot:
    slot_id: str
    batch_id: str
    trial_id: str
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
    max_score: int
    trial_challenge_id: str
    trial_challenge_sha256: str
    run_params_derivation: str
    resource_equivalence_policy_id: str
    resource_equivalence_policy_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "slot_id",
            "batch_id",
            "trial_id",
            "task_id",
            "treatment_profile_id",
            "chain_profile_id",
            "requested_model",
            "thinking_level",
            "model_variant_id",
            "model_profile_id",
            "trial_challenge_id",
            "run_params_derivation",
            "resource_equivalence_policy_id",
        ):
            _identifier(getattr(self, field), f"slot.{field}")
        for field in (
            "task_content_sha256",
            "treatment_profile_sha256",
            "chain_profile_sha256",
            "model_profile_sha256",
            "trial_challenge_sha256",
            "resource_equivalence_policy_sha256",
        ):
            _sha(getattr(self, field), f"slot.{field}")
        if not isinstance(self.arm, str) or self.arm not in _ARMS:
            raise CampaignError("slot.arm must be B or C")
        if not isinstance(self.chain_track, str) or self.chain_track not in _CHAIN_TRACKS:
            raise CampaignError("slot.chain_track is unsupported")
        if not isinstance(self.budget, TaskBudget):
            raise CampaignError("slot.budget must be a typed Task budget")
        _positive_int(self.max_score, "slot.max_score")
        try:
            expected_variant = model_variant_id(
                requested_model=self.requested_model,
                thinking_level=self.thinking_level,
                profile_id=self.model_profile_id,
                profile_sha256=self.model_profile_sha256,
            )
        except ModelProfileError as exc:
            raise CampaignError("slot model-variant fields are invalid") from exc
        if self.model_variant_id != expected_variant:
            raise CampaignError("slot model variant does not match its profile")

    def to_dict(self) -> dict[str, Any]:
        document = {
            "arm": self.arm,
            "batch_id": self.batch_id,
            "budget": self.budget.to_dict(),
            "chain_profile_id": self.chain_profile_id,
            "chain_profile_sha256": self.chain_profile_sha256,
            "chain_track": self.chain_track,
            "max_score": self.max_score,
            "model_profile_id": self.model_profile_id,
            "model_profile_sha256": self.model_profile_sha256,
            "model_variant_id": self.model_variant_id,
            "requested_model": self.requested_model,
            "resource_equivalence_policy_id": self.resource_equivalence_policy_id,
            "resource_equivalence_policy_sha256": self.resource_equivalence_policy_sha256,
            "run_params_derivation": self.run_params_derivation,
            "slot_id": self.slot_id,
            "task_content_sha256": self.task_content_sha256,
            "task_id": self.task_id,
            "thinking_level": self.thinking_level,
            "treatment_profile_id": self.treatment_profile_id,
            "treatment_profile_sha256": self.treatment_profile_sha256,
            "trial_challenge_id": self.trial_challenge_id,
            "trial_challenge_sha256": self.trial_challenge_sha256,
            "trial_id": self.trial_id,
        }
        return _public(document, "campaign slot")

    @classmethod
    def from_dict(cls, document: Any) -> CampaignSlot:
        keys = {
            "arm",
            "batch_id",
            "budget",
            "chain_profile_id",
            "chain_profile_sha256",
            "chain_track",
            "max_score",
            "model_profile_id",
            "model_profile_sha256",
            "model_variant_id",
            "requested_model",
            "resource_equivalence_policy_id",
            "resource_equivalence_policy_sha256",
            "run_params_derivation",
            "slot_id",
            "task_content_sha256",
            "task_id",
            "thinking_level",
            "treatment_profile_id",
            "treatment_profile_sha256",
            "trial_challenge_id",
            "trial_challenge_sha256",
            "trial_id",
        }
        raw = dict(_exact(document, keys, "campaign slot"))
        try:
            raw["budget"] = TaskBudget.from_dict(raw["budget"])
        except AttemptSchemaError as exc:
            raise CampaignError("campaign slot contains an invalid Task budget") from exc
        return cls(**raw)


def execution_plan_sha256(
    batches: tuple[CampaignBatch, ...],
    slots: tuple[CampaignSlot, ...],
) -> str:
    return artifact_sha256({
        "batches": [batch.to_dict() for batch in batches],
        "slots": [slot.to_dict() for slot in slots],
    })


def _pair_key(slot: CampaignSlot) -> tuple[str, str, str]:
    return slot.trial_id, slot.task_id, slot.model_variant_id


def _matched_fields(slot: CampaignSlot) -> tuple[Any, ...]:
    return (
        slot.batch_id,
        slot.task_content_sha256,
        slot.chain_track,
        slot.chain_profile_id,
        slot.chain_profile_sha256,
        slot.requested_model,
        slot.thinking_level,
        slot.model_variant_id,
        slot.model_profile_id,
        slot.model_profile_sha256,
        slot.budget,
        slot.max_score,
        slot.trial_challenge_id,
        slot.trial_challenge_sha256,
        slot.run_params_derivation,
        slot.resource_equivalence_policy_id,
        slot.resource_equivalence_policy_sha256,
    )


@dataclass(frozen=True)
class CampaignManifest:
    campaign_id: str
    created_utc: str
    suite_semver: str
    suite_freeze_sha256: str
    execution_plan_id: str
    execution_plan_sha256: str
    retry_policy_id: str
    retry_policy_sha256: str
    retry_limit: int
    stopping_rule_id: str
    stopping_rule_sha256: str
    concurrency_contract: str
    execution_source: ExecutionSource
    batches: tuple[CampaignBatch, ...]
    slots: tuple[CampaignSlot, ...]
    model_qualifications: tuple[CampaignQualification, ...] = ()
    schema_version: str = CAMPAIGN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_id, str) or _CAMPAIGN_ID.fullmatch(self.campaign_id) is None:
            raise CampaignError("campaign_id must be an opaque 128-bit identifier")
        _utc(self.created_utc, "campaign.created_utc")
        if not isinstance(self.suite_semver, str) or _SEMVER.fullmatch(self.suite_semver) is None:
            raise CampaignError("campaign.suite_semver must be semantic version x.y.z")
        suite_major = int(self.suite_semver.split(".", 1)[0])
        for field in (
            "execution_plan_id",
            "retry_policy_id",
            "stopping_rule_id",
        ):
            _identifier(getattr(self, field), f"campaign.{field}")
        for field in (
            "suite_freeze_sha256",
            "execution_plan_sha256",
            "retry_policy_sha256",
            "stopping_rule_sha256",
        ):
            _sha(getattr(self, field), f"campaign.{field}")
        if type(self.retry_limit) is not int or self.retry_limit != 1:
            raise CampaignError("campaign.retry_limit must be exactly one")
        if self.retry_policy_id != RETRY_POLICY_ID:
            raise CampaignError("campaign retry policy is unsupported")
        if self.retry_policy_sha256 != RETRY_POLICY_SHA256:
            raise CampaignError("campaign retry policy digest is unsupported")
        if self.stopping_rule_id != STOPPING_RULE_ID:
            raise CampaignError("campaign stopping rule is unsupported")
        if self.stopping_rule_sha256 != STOPPING_RULE_SHA256:
            raise CampaignError("campaign stopping rule digest is unsupported")
        if self.concurrency_contract != CONCURRENCY_CONTRACT:
            raise CampaignError("campaign concurrency contract is unsupported")
        if not isinstance(self.execution_source, ExecutionSource):
            raise CampaignError("campaign execution source must be a typed record")
        _tuple_of(self.batches, CampaignBatch, "campaign.batches")
        _tuple_of(self.slots, CampaignSlot, "campaign.slots")
        if not self.batches or not self.slots:
            raise CampaignError("campaign needs at least one batch and one slot")
        if len({batch.batch_id for batch in self.batches}) != len(self.batches):
            raise CampaignError("campaign batch IDs must be unique")
        if len({slot.slot_id for slot in self.slots}) != len(self.slots):
            raise CampaignError("campaign slot IDs must be unique")
        slot_by_id = {slot.slot_id: slot for slot in self.slots}
        flattened = tuple(slot_id for batch in self.batches for slot_id in batch.slot_ids)
        if len(flattened) != len(set(flattened)) or set(flattened) != set(slot_by_id):
            raise CampaignError("batches must schedule every campaign slot exactly once")
        batch_by_slot = {
            slot_id: batch.batch_id for batch in self.batches for slot_id in batch.slot_ids
        }
        if any(slot.batch_id != batch_by_slot[slot.slot_id] for slot in self.slots):
            raise CampaignError("campaign slot names the wrong batch")
        if self.execution_plan_sha256 != execution_plan_sha256(self.batches, self.slots):
            raise CampaignError("campaign execution plan digest does not match its schedule")

        positions = {slot_id: index for index, slot_id in enumerate(flattened)}
        pairs: dict[tuple[str, str, str], list[CampaignSlot]] = {}
        for slot in self.slots:
            pairs.setdefault(_pair_key(slot), []).append(slot)
        ordered_pairs = sorted(
            pairs.values(),
            key=lambda pair: min(positions[slot.slot_id] for slot in pair),
        )
        for pair_index, pair in enumerate(ordered_pairs):
            if len(pair) != 2 or {slot.arm for slot in pair} != _ARMS:
                raise CampaignError("every campaign trial slot needs exactly one B and one C arm")
            b = next(slot for slot in pair if slot.arm == "B")
            c = next(slot for slot in pair if slot.arm == "C")
            if _matched_fields(b) != _matched_fields(c):
                raise CampaignError("matched B and C slots differ outside treatment")
            if (
                b.treatment_profile_id == c.treatment_profile_id
                or b.treatment_profile_sha256 == c.treatment_profile_sha256
            ):
                raise CampaignError("matched B and C slots need distinct treatment profiles")
            pair_positions = sorted((positions[b.slot_id], positions[c.slot_id]))
            if pair_positions[1] != pair_positions[0] + 1:
                raise CampaignError("matched B and C slots must be adjacent")
            ordered_arms = tuple(slot_by_id[flattened[index]].arm for index in pair_positions)
            expected = ("B", "C") if pair_index % 2 == 0 else ("C", "B")
            if ordered_arms != expected:
                raise CampaignError("matched arm order must be counterbalanced")

        if self.schema_version == CAMPAIGN_SCHEMA_VERSION:
            if suite_major >= 5:
                raise CampaignError("this suite requires qualified campaign manifests")
            if self.model_qualifications:
                raise CampaignError("legacy campaigns cannot carry model qualifications")
        elif self.schema_version == QUALIFIED_CAMPAIGN_SCHEMA_VERSION:
            if suite_major < 5:
                raise CampaignError("legacy suites cannot use qualified campaign manifests")
            _tuple_of(
                self.model_qualifications,
                CampaignQualification,
                "campaign.model_qualifications",
            )
            if not self.model_qualifications:
                raise CampaignError("qualified campaigns need model qualification evidence")
            qualification_by_profile = {
                binding.profile_key: binding for binding in self.model_qualifications
            }
            if len(qualification_by_profile) != len(self.model_qualifications):
                raise CampaignError("campaign model qualifications must be unique per profile")
            if (
                len({row.qualification_id for row in self.model_qualifications})
                != len(self.model_qualifications)
                or len({row.qualification_sha256 for row in self.model_qualifications})
                != len(self.model_qualifications)
            ):
                raise CampaignError("campaign model qualification evidence must be unique")
            if self.model_qualifications != tuple(
                sorted(self.model_qualifications, key=lambda binding: binding.profile_key)
            ):
                raise CampaignError("campaign model qualifications must use canonical profile order")
            created = datetime.fromisoformat(self.created_utc.replace("Z", "+00:00"))
            for binding in self.model_qualifications:
                completed = datetime.fromisoformat(
                    binding.completed_utc.replace("Z", "+00:00")
                )
                age = int((created - completed).total_seconds())
                if age < 0 or age > MAX_MODEL_EVIDENCE_AGE_SECONDS:
                    raise CampaignError("campaign model qualification evidence is stale")
            variants_by_profile: dict[tuple[str, str], set[str]] = {}
            for slot in self.slots:
                variants_by_profile.setdefault(
                    (slot.model_profile_id, slot.model_profile_sha256), set()
                ).add(slot.model_variant_id)
            if any(len(variants) != 1 for variants in variants_by_profile.values()):
                raise CampaignError("one model profile cannot name multiple campaign variants")
            slot_profiles = {
                key: next(iter(variants)) for key, variants in variants_by_profile.items()
            }
            if set(qualification_by_profile) != set(slot_profiles):
                raise CampaignError("campaign qualifications do not exactly cover slot profiles")
            if any(
                qualification_by_profile[key].model_variant_id != variant
                for key, variant in slot_profiles.items()
            ):
                raise CampaignError("campaign qualification names the wrong model variant")
        else:
            raise CampaignError("campaign schema version is unsupported")

    @property
    def ordered_slots(self) -> tuple[CampaignSlot, ...]:
        by_id = {slot.slot_id: slot for slot in self.slots}
        return tuple(by_id[slot_id] for batch in self.batches for slot_id in batch.slot_ids)

    def qualification_for_profile(
        self,
        profile_id: str,
        profile_sha256: str,
    ) -> CampaignQualification | None:
        if self.schema_version == CAMPAIGN_SCHEMA_VERSION:
            return None
        matches = tuple(
            binding
            for binding in self.model_qualifications
            if binding.profile_key == (profile_id, profile_sha256)
        )
        if len(matches) != 1:
            raise CampaignError("campaign lacks one exact model qualification")
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        document = {
            "batches": [batch.to_dict() for batch in self.batches],
            "campaign_id": self.campaign_id,
            "concurrency_contract": self.concurrency_contract,
            "created_utc": self.created_utc,
            "execution_plan_id": self.execution_plan_id,
            "execution_plan_sha256": self.execution_plan_sha256,
            "execution_source": self.execution_source.to_dict(),
            "retry_limit": self.retry_limit,
            "retry_policy_id": self.retry_policy_id,
            "retry_policy_sha256": self.retry_policy_sha256,
            "schema_version": self.schema_version,
            "slots": [slot.to_dict() for slot in self.slots],
            "stopping_rule_id": self.stopping_rule_id,
            "stopping_rule_sha256": self.stopping_rule_sha256,
            "suite_freeze_sha256": self.suite_freeze_sha256,
            "suite_semver": self.suite_semver,
        }
        if self.schema_version == QUALIFIED_CAMPAIGN_SCHEMA_VERSION:
            document["model_qualifications"] = [
                binding.to_dict() for binding in self.model_qualifications
            ]
        return _public(document, "campaign manifest")

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> CampaignManifest:
        keys = {
            "batches",
            "campaign_id",
            "concurrency_contract",
            "created_utc",
            "execution_plan_id",
            "execution_plan_sha256",
            "execution_source",
            "retry_limit",
            "retry_policy_id",
            "retry_policy_sha256",
            "schema_version",
            "slots",
            "stopping_rule_id",
            "stopping_rule_sha256",
            "suite_freeze_sha256",
            "suite_semver",
        }
        if isinstance(document, dict) and document.get("schema_version") == (
            QUALIFIED_CAMPAIGN_SCHEMA_VERSION
        ):
            keys.add("model_qualifications")
        raw = dict(_exact(document, keys, "campaign manifest"))
        if not isinstance(raw["batches"], list) or not isinstance(raw["slots"], list):
            raise CampaignError("campaign batches and slots must be arrays")
        raw["batches"] = tuple(CampaignBatch.from_dict(item) for item in raw["batches"])
        raw["slots"] = tuple(CampaignSlot.from_dict(item) for item in raw["slots"])
        if "model_qualifications" in raw:
            if not isinstance(raw["model_qualifications"], list):
                raise CampaignError("campaign model qualifications must be an array")
            raw["model_qualifications"] = tuple(
                CampaignQualification.from_dict(item)
                for item in raw["model_qualifications"]
            )
        try:
            raw["execution_source"] = ExecutionSource.from_dict(raw["execution_source"])
        except AttemptSchemaError as exc:
            raise CampaignError("campaign execution source is invalid") from exc
        return cls(**raw)


def validate_intent_for_slot(
    manifest: CampaignManifest,
    slot: CampaignSlot,
    intent: TaskAttemptIntent,
) -> None:
    if slot not in manifest.slots:
        raise CampaignError("attempt slot does not belong to the campaign")
    identity = intent.identity
    expected = (
        manifest.campaign_id,
        manifest.sha256,
        slot.batch_id,
        manifest.execution_plan_id,
        manifest.execution_plan_sha256,
        slot.trial_id,
        manifest.suite_semver,
        manifest.suite_freeze_sha256,
        slot.task_id,
        slot.task_content_sha256,
        slot.arm,
        slot.treatment_profile_id,
        slot.treatment_profile_sha256,
        slot.chain_track,
        slot.chain_profile_id,
        slot.chain_profile_sha256,
        slot.requested_model,
        slot.thinking_level,
        slot.model_variant_id,
        slot.model_profile_id,
        slot.model_profile_sha256,
        slot.budget,
        slot.trial_challenge_id,
        slot.trial_challenge_sha256,
        slot.run_params_derivation,
        slot.resource_equivalence_policy_id,
        slot.resource_equivalence_policy_sha256,
        manifest.retry_policy_id,
        manifest.retry_policy_sha256,
        manifest.execution_source,
    )
    observed = (
        identity.campaign_id,
        identity.campaign_manifest_sha256,
        identity.batch_id,
        identity.execution_plan_id,
        identity.execution_plan_sha256,
        identity.trial_id,
        identity.suite_semver,
        identity.suite_freeze_sha256,
        identity.task_id,
        identity.task_content_sha256,
        identity.arm,
        identity.treatment_profile_id,
        identity.treatment_profile_sha256,
        identity.chain_track,
        identity.chain_profile_id,
        identity.chain_profile_sha256,
        identity.requested_model,
        identity.thinking_level,
        identity.model_variant_id,
        identity.model_profile_id,
        identity.model_profile_sha256,
        identity.budget,
        identity.trial_challenge_id,
        identity.trial_challenge_sha256,
        identity.run_params_derivation,
        identity.resource_equivalence_policy_id,
        identity.resource_equivalence_policy_sha256,
        identity.retry_policy_id,
        identity.retry_policy_sha256,
        identity.execution_source,
    )
    if observed != expected:
        raise CampaignError("attempt identity does not match its declared campaign slot")


@dataclass(frozen=True)
class AttemptArtifactReference:
    attempt_id: str
    intent_sha256: str
    preflight_requirements_sha256: str
    journal_entry_sha256s: tuple[str, ...]
    preflight_evidence_sha256: str
    result_sha256: str
    cleanup_receipt_sha256s: tuple[str, ...]
    retry_ordinal: int
    outcome: str

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or _ATTEMPT_ID.fullmatch(self.attempt_id) is None:
            raise CampaignError("attempt reference attempt_id is invalid")
        for field in (
            "intent_sha256",
            "preflight_requirements_sha256",
            "preflight_evidence_sha256",
            "result_sha256",
        ):
            _sha(getattr(self, field), f"attempt reference {field}")
        for field in ("journal_entry_sha256s", "cleanup_receipt_sha256s"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or not values:
                raise CampaignError(f"attempt reference {field} must be a non-empty sequence")
            for value in values:
                _sha(value, f"attempt reference {field} item")
            if len(set(values)) != len(values):
                raise CampaignError(f"attempt reference {field} must not repeat a digest")
        if type(self.retry_ordinal) is not int or self.retry_ordinal not in (0, 1):
            raise CampaignError("attempt reference retry ordinal is unsupported")
        if not isinstance(self.outcome, str) or self.outcome not in _OUTCOMES:
            raise CampaignError("attempt reference outcome is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "cleanup_receipt_sha256s": list(self.cleanup_receipt_sha256s),
            "intent_sha256": self.intent_sha256,
            "journal_entry_sha256s": list(self.journal_entry_sha256s),
            "outcome": self.outcome,
            "preflight_evidence_sha256": self.preflight_evidence_sha256,
            "preflight_requirements_sha256": self.preflight_requirements_sha256,
            "result_sha256": self.result_sha256,
            "retry_ordinal": self.retry_ordinal,
        }

    @classmethod
    def from_dict(cls, document: Any) -> AttemptArtifactReference:
        raw = dict(_exact(document, {
            "attempt_id",
            "cleanup_receipt_sha256s",
            "intent_sha256",
            "journal_entry_sha256s",
            "outcome",
            "preflight_evidence_sha256",
            "preflight_requirements_sha256",
            "result_sha256",
            "retry_ordinal",
        }, "attempt artifact reference"))
        for field in ("journal_entry_sha256s", "cleanup_receipt_sha256s"):
            if not isinstance(raw[field], list):
                raise CampaignError(f"attempt artifact reference {field} must be an array")
            raw[field] = tuple(raw[field])
        return cls(**raw)


@dataclass(frozen=True)
class ResolvedCampaignSlot:
    slot_id: str
    original: AttemptArtifactReference
    retry: AttemptArtifactReference | None
    terminal_attempt_id: str

    def __post_init__(self) -> None:
        _identifier(self.slot_id, "resolved slot slot_id")
        if not isinstance(self.original, AttemptArtifactReference):
            raise CampaignError("resolved slot needs a typed original attempt")
        if self.retry is not None and not isinstance(self.retry, AttemptArtifactReference):
            raise CampaignError("resolved slot retry must be a typed attempt reference")
        expected = self.original.attempt_id if self.retry is None else self.retry.attempt_id
        if self.terminal_attempt_id != expected:
            raise CampaignError("resolved slot terminal attempt contradicts its lineage")
        if self.original.retry_ordinal != 0 or (
            self.retry is not None and self.retry.retry_ordinal != 1
        ):
            raise CampaignError("resolved slot retry ordinals are invalid")
        if self.retry is not None and self.original.outcome != "infra_fail":
            raise CampaignError("a scored predecessor cannot have a retry")
        if self.retry is not None and self.retry.attempt_id == self.original.attempt_id:
            raise CampaignError("a retry must use a distinct attempt ID")

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": self.original.to_dict(),
            "retry": None if self.retry is None else self.retry.to_dict(),
            "slot_id": self.slot_id,
            "terminal_attempt_id": self.terminal_attempt_id,
        }

    @classmethod
    def from_dict(cls, document: Any) -> ResolvedCampaignSlot:
        raw = dict(_exact(document, {
            "original", "retry", "slot_id", "terminal_attempt_id",
        }, "resolved campaign slot"))
        raw["original"] = AttemptArtifactReference.from_dict(raw["original"])
        raw["retry"] = (
            None
            if raw["retry"] is None
            else AttemptArtifactReference.from_dict(raw["retry"])
        )
        return cls(**raw)


@dataclass(frozen=True)
class AcceptedReportResolution:
    campaign_id: str
    campaign_manifest_sha256: str
    slots: tuple[ResolvedCampaignSlot, ...]
    kind: str = "accepted"
    schema_version: str = REPORT_RESOLUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_id, str) or _CAMPAIGN_ID.fullmatch(self.campaign_id) is None:
            raise CampaignError("report resolution campaign ID is invalid")
        _sha(self.campaign_manifest_sha256, "report resolution manifest digest")
        _tuple_of(self.slots, ResolvedCampaignSlot, "report resolution slots")
        if not self.slots or len({slot.slot_id for slot in self.slots}) != len(self.slots):
            raise CampaignError("report resolution needs unique slots")
        attempt_ids = [
            reference.attempt_id
            for slot in self.slots
            for reference in (slot.original, slot.retry)
            if reference is not None
        ]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise CampaignError("report resolution cannot reuse an attempt across slots")
        if self.kind != "accepted" or self.schema_version != REPORT_RESOLUTION_SCHEMA_VERSION:
            raise CampaignError("accepted report resolution identity is unsupported")

    def to_dict(self) -> dict[str, Any]:
        document = {
            "campaign_id": self.campaign_id,
            "campaign_manifest_sha256": self.campaign_manifest_sha256,
            "kind": self.kind,
            "schema_version": self.schema_version,
            "slots": [slot.to_dict() for slot in self.slots],
        }
        return _public(document, "accepted report resolution")

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> AcceptedReportResolution:
        raw = dict(_exact(document, {
            "campaign_id", "campaign_manifest_sha256", "kind", "schema_version", "slots",
        }, "accepted report resolution"))
        if not isinstance(raw["slots"], list):
            raise CampaignError("report resolution slots must be an array")
        raw["slots"] = tuple(ResolvedCampaignSlot.from_dict(item) for item in raw["slots"])
        return cls(**raw)


@dataclass(frozen=True)
class ExploratoryAttemptSummary:
    attempt_id: str
    campaign_id: str
    task_id: str
    arm: Arm
    model_variant_id: str
    retry_ordinal: int
    state: str
    outcome: str | None

    def __post_init__(self) -> None:
        for field in ("attempt_id", "campaign_id", "task_id", "model_variant_id", "state"):
            _identifier(getattr(self, field), f"exploratory attempt {field}")
        if (
            self.arm not in _ARMS
            or type(self.retry_ordinal) is not int
            or self.retry_ordinal not in (0, 1)
        ):
            raise CampaignError("exploratory attempt identity is invalid")
        if _ATTEMPT_ID.fullmatch(self.attempt_id) is None:
            raise CampaignError("exploratory attempt ID is invalid")
        if self.state not in _PREVIEW_STATES:
            raise CampaignError("exploratory attempt state is unsupported")
        if self.outcome is not None and self.outcome not in _OUTCOMES:
            raise CampaignError("exploratory attempt outcome is unsupported")
        if (self.state == "active") != (self.outcome is None):
            raise CampaignError("exploratory attempt state contradicts its outcome")

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "attempt_id": self.attempt_id,
            "campaign_id": self.campaign_id,
            "model_variant_id": self.model_variant_id,
            "outcome": self.outcome,
            "retry_ordinal": self.retry_ordinal,
            "state": self.state,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, document: Any) -> ExploratoryAttemptSummary:
        return cls(**_exact(document, {
            "arm", "attempt_id", "campaign_id", "model_variant_id", "outcome",
            "retry_ordinal", "state", "task_id",
        }, "exploratory attempt summary"))


@dataclass(frozen=True)
class ExploratoryPreview:
    attempts: tuple[ExploratoryAttemptSummary, ...]
    accepted: bool = False
    kind: str = "exploratory"
    schema_version: str = EXPLORATORY_PREVIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _tuple_of(self.attempts, ExploratoryAttemptSummary, "exploratory attempts")
        if self.accepted is not False or self.kind != "exploratory":
            raise CampaignError("exploratory preview cannot claim accepted evidence")
        if self.schema_version != EXPLORATORY_PREVIEW_SCHEMA_VERSION:
            raise CampaignError("exploratory preview schema is unsupported")
        if tuple(sorted(self.attempts, key=lambda row: row.attempt_id)) != self.attempts:
            raise CampaignError("exploratory attempts must use deterministic order")

    def to_dict(self) -> dict[str, Any]:
        document = {
            "accepted": self.accepted,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "kind": self.kind,
            "schema_version": self.schema_version,
        }
        return _public(document, "exploratory preview")

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> ExploratoryPreview:
        raw = dict(_exact(document, {
            "accepted", "attempts", "kind", "schema_version",
        }, "exploratory preview"))
        if not isinstance(raw["attempts"], list):
            raise CampaignError("exploratory attempts must be an array")
        raw["attempts"] = tuple(
            ExploratoryAttemptSummary.from_dict(item) for item in raw["attempts"]
        )
        return cls(**raw)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise CampaignError("document contains a duplicate JSON key")
        document[key] = value
    return document


def _read_document(path: Path | str, *, canonical: bool) -> tuple[dict[str, Any], bytes]:
    source = Path(path)
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError:
        raise CampaignError("document is missing") from None
    if not stat.S_ISREG(mode):
        raise CampaignError("document must be a regular non-symlink file")
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise CampaignError("document must be a regular file")
            payload = handle.read(_MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise CampaignError("document is not readable") from exc
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise CampaignError("document exceeds the size limit")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CampaignError("document is not valid UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise CampaignError("document must be a JSON object")
    try:
        normalized = canonical_json_bytes(document)
    except AttemptSchemaError as exc:
        raise CampaignError("document is not canonical JSON data") from exc
    if canonical and payload != normalized:
        raise CampaignError("document bytes are not canonical")
    return document, payload


def load_campaign(path: Path | str) -> CampaignManifest:
    document, payload = _read_document(path, canonical=True)
    manifest = CampaignManifest.from_dict(document)
    if canonical_json_bytes(manifest.to_dict()) != payload:
        raise CampaignError("campaign does not use its canonical schema representation")
    return manifest


def load_report_resolution(path: Path | str) -> AcceptedReportResolution:
    document, payload = _read_document(path, canonical=True)
    resolution = AcceptedReportResolution.from_dict(document)
    if canonical_json_bytes(resolution.to_dict()) != payload:
        raise CampaignError("report resolution does not use its canonical schema representation")
    return resolution


def load_exploratory_preview(path: Path | str) -> ExploratoryPreview:
    document, payload = _read_document(path, canonical=True)
    preview = ExploratoryPreview.from_dict(document)
    if canonical_json_bytes(preview.to_dict()) != payload:
        raise CampaignError("exploratory preview does not use its canonical schema representation")
    return preview


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_document(path: Path | str, document: dict[str, Any], label: str) -> Path:
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CampaignError(f"cannot create {label} parent") from exc
    try:
        parent_mode = destination.parent.lstat().st_mode
    except OSError as exc:
        raise CampaignError(f"cannot inspect {label} parent") from exc
    if not stat.S_ISDIR(parent_mode):
        raise CampaignError(f"{label} parent must be a real directory")
    try:
        payload = canonical_json_bytes(_public(document, label))
    except AttemptSchemaError as exc:
        raise CampaignError(f"{label} is not canonical JSON data") from exc
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise CampaignError(f"{label} exceeds the size limit")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            raise CampaignError(f"{label} already exists and cannot be replaced") from None
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def freeze_campaign(draft_path: Path | str, output_path: Path | str) -> CampaignManifest:
    document, _payload = _read_document(draft_path, canonical=False)
    manifest = CampaignManifest.from_dict(document)
    if int(manifest.suite_semver.split(".", 1)[0]) >= 4:
        raise CampaignError("this suite requires release-derived campaign freezing")
    publish_document(output_path, manifest.to_dict(), "campaign manifest")
    return manifest


def validate_report_resolution(
    manifest: CampaignManifest,
    resolution: AcceptedReportResolution,
) -> None:
    if (
        resolution.campaign_id != manifest.campaign_id
        or resolution.campaign_manifest_sha256 != manifest.sha256
    ):
        raise CampaignError("report resolution does not bind its campaign manifest")
    if tuple(slot.slot_id for slot in resolution.slots) != tuple(
        slot.slot_id for slot in manifest.ordered_slots
    ):
        raise CampaignError("report resolution does not cover the frozen slot order")
