"""Deterministic publication from selected campaign reports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ckbbench.run.campaign_report import (
    SUPPORTED_METHODOLOGIES,
    CampaignReportDataset,
    CampaignReportError,
    ReportBuilderSource,
    publish_report_files,
)
from ckbbench.run.report_site import render_report_site
from ckbbench.run.task_attempt import canonical_json_bytes, validate_public_artifact_values


PUBLICATION_DATASET_SCHEMA_VERSION = "ckbbench-publication-dataset-v1"
_MAX_PUBLICATION_BYTES = 128 << 20
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CAMPAIGN_ID = re.compile(r"^campaign-[0-9a-f]{32}$")


class PublicationError(CampaignReportError):
    """Selected campaign reports cannot form one comparable publication."""


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PublicationError(f"{label} must contain exactly the reviewed fields")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise PublicationError("publication dataset contains a duplicate JSON key")
        document[key] = value
    return document


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PublicationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_equal(left: Any, right: Any) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _slot_design(document: dict[str, Any]) -> list[dict[str, Any]]:
    attempts_by_slot = {
        row["slot_id"]: row for row in document["attempts"] if row["terminal"]
    }
    design = []
    for acquisition in document["slot_acquisitions"]:
        terminal = attempts_by_slot.get(acquisition["slot_id"])
        if terminal is None:
            raise PublicationError("publication source lacks one terminal attempt per slot")
        design.append({
            "arm": acquisition["arm"],
            "budget": acquisition["budget"],
            "chain_profile_id": acquisition["chain_profile_id"],
            "chain_profile_sha256": acquisition["chain_profile_sha256"],
            "chain_track": acquisition["chain_track"],
            "max_score": terminal["max_score"],
            "slot_order": acquisition["slot_order"],
            "task_content_sha256": acquisition["task_content_sha256"],
            "task_id": acquisition["task_id"],
            "treatment_profile_id": terminal["treatment_profile_id"],
            "treatment_profile_sha256": terminal["treatment_profile_sha256"],
        })
    return design


def _compatibility_signature(document: dict[str, Any]) -> dict[str, Any]:
    campaign = document["campaign"]
    return {
        "chain_profiles": document["profiles"]["chain_profiles"],
        "concurrency_contract": campaign["concurrency_contract"],
        "execution_source": campaign["execution_source"],
        "retry_policy_id": campaign["retry_policy_id"],
        "retry_policy_sha256": campaign["retry_policy_sha256"],
        "slot_design": _slot_design(document),
        "stopping_rule_id": campaign["stopping_rule_id"],
        "stopping_rule_sha256": campaign["stopping_rule_sha256"],
        "suite_freeze_sha256": campaign["suite_freeze_sha256"],
        "suite_semver": campaign["suite_semver"],
        "treatment_profiles": document["profiles"]["treatment_profiles"],
    }


def _validate_publication(document: Any) -> None:
    root = _exact(document, {
        "campaigns", "methodology", "report_builder", "schema_version",
    }, "publication dataset")
    if root["schema_version"] != PUBLICATION_DATASET_SCHEMA_VERSION:
        raise PublicationError("publication dataset schema version is unsupported")
    if root["methodology"] not in SUPPORTED_METHODOLOGIES:
        raise PublicationError("publication methodology differs from the reviewed rules")
    try:
        builder = ReportBuilderSource.from_dict(root["report_builder"])
    except CampaignReportError as exc:
        raise PublicationError("publication report builder is invalid") from exc
    rows = root["campaigns"]
    if not isinstance(rows, list) or not rows:
        raise PublicationError("publication needs at least one campaign report")

    campaign_ids: list[str] = []
    dataset_hashes: list[str] = []
    model_variants: list[str] = []
    attempt_ids: list[str] = []
    slot_ids: list[str] = []
    signature: dict[str, Any] | None = None
    for item in rows:
        row = _exact(item, {
            "campaign_id", "dataset", "dataset_sha256",
        }, "publication campaign")
        try:
            source = CampaignReportDataset.from_dict(row["dataset"])
        except CampaignReportError as exc:
            raise PublicationError("publication source dataset is invalid") from exc
        source_document = source.to_dict()
        if not _canonical_equal(source_document["methodology"], root["methodology"]):
            raise PublicationError("publication source methodology does not match")
        campaign_id = source_document["campaign"]["campaign_id"]
        if not isinstance(row["campaign_id"], str) or _CAMPAIGN_ID.fullmatch(
            row["campaign_id"]
        ) is None:
            raise PublicationError("publication campaign ID is invalid")
        if row["campaign_id"] != campaign_id:
            raise PublicationError("publication campaign ID does not match its source dataset")
        if _sha(row["dataset_sha256"], "publication source dataset") != source.sha256:
            raise PublicationError("publication source dataset digest does not match")
        if ReportBuilderSource.from_dict(source_document["report_builder"]) != builder:
            raise PublicationError("publication sources need the same report builder")
        if not source_document["profiles"]["release_validated"]:
            raise PublicationError("publication sources need validated release profiles")

        selected_signature = _compatibility_signature(source_document)
        if signature is None:
            signature = selected_signature
        elif not _canonical_equal(selected_signature, signature):
            raise PublicationError("publication campaign designs are not comparable")

        campaign_ids.append(campaign_id)
        dataset_hashes.append(source.sha256)
        model_variants.extend(
            profile["model_variant_id"]
            for profile in source_document["profiles"]["model_variants"]
        )
        attempt_ids.extend(row["attempt_id"] for row in source_document["attempts"])
        slot_ids.extend(row["slot_id"] for row in source_document["slot_acquisitions"])

    if campaign_ids != sorted(campaign_ids) or len(set(campaign_ids)) != len(campaign_ids):
        raise PublicationError("publication campaigns must be unique and sorted")
    for values, label in (
        (dataset_hashes, "source datasets"),
        (model_variants, "model variants"),
        (attempt_ids, "attempt IDs"),
        (slot_ids, "slot IDs"),
    ):
        if len(values) != len(set(values)):
            raise PublicationError(f"publication {label} must be unique")
    try:
        validate_public_artifact_values(root)
    except ValueError as exc:
        raise PublicationError("publication dataset contains a secret-shaped value") from exc


@dataclass(frozen=True)
class CampaignPublicationDataset:
    """Strict immutable wrapper around independently attributable campaign reports."""

    _payload: bytes

    def __post_init__(self) -> None:
        try:
            document = json.loads(self._payload.decode("ascii"), object_pairs_hook=_unique_object)
        except PublicationError:
            raise
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PublicationError("publication dataset is not canonical JSON") from exc
        _validate_publication(document)
        if self._payload != canonical_json_bytes(document):
            raise PublicationError("publication dataset bytes are not canonical")

    @classmethod
    def from_dict(cls, document: Any) -> CampaignPublicationDataset:
        _validate_publication(document)
        return cls(canonical_json_bytes(document))

    def to_dict(self) -> dict[str, Any]:
        document = json.loads(self._payload.decode("ascii"))
        assert isinstance(document, dict)
        return document

    @property
    def canonical_bytes(self) -> bytes:
        return self._payload

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self._payload).hexdigest()

    @property
    def campaign_reports(self) -> tuple[CampaignReportDataset, ...]:
        try:
            return tuple(
                CampaignReportDataset.from_dict(row["dataset"])
                for row in self.to_dict()["campaigns"]
            )
        except CampaignReportError as exc:
            raise PublicationError("publication source dataset is invalid") from exc


def build_campaign_publication_dataset(
    datasets: Iterable[CampaignReportDataset],
    builder_source: ReportBuilderSource,
) -> CampaignPublicationDataset:
    """Build a canonical publication without discovering or pooling campaigns."""
    selected = sorted(datasets, key=lambda dataset: dataset.to_dict()["campaign"]["campaign_id"])
    if not selected:
        raise PublicationError("publication needs at least one campaign report")
    methodology = selected[0].to_dict()["methodology"]
    if any(
        not _canonical_equal(dataset.to_dict()["methodology"], methodology)
        for dataset in selected[1:]
    ):
        raise PublicationError("publication sources need the same methodology")
    document = {
        "campaigns": [
            {
                "campaign_id": dataset.to_dict()["campaign"]["campaign_id"],
                "dataset": dataset.to_dict(),
                "dataset_sha256": dataset.sha256,
            }
            for dataset in selected
        ],
        "methodology": methodology,
        "report_builder": builder_source.to_dict(),
        "schema_version": PUBLICATION_DATASET_SCHEMA_VERSION,
    }
    return CampaignPublicationDataset.from_dict(document)


def load_campaign_publication_dataset(path: Path | str) -> CampaignPublicationDataset:
    source = Path(path)
    try:
        mode = source.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise PublicationError("publication dataset must be a regular non-symlink file")
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            payload = handle.read(_MAX_PUBLICATION_BYTES + 1)
    except PublicationError:
        raise
    except OSError as exc:
        raise PublicationError("publication dataset cannot be read") from exc
    if len(payload) > _MAX_PUBLICATION_BYTES:
        raise PublicationError("publication dataset exceeds its byte limit")
    return CampaignPublicationDataset(payload)


def render_campaign_publication(dataset: CampaignPublicationDataset) -> bytes:
    return render_report_site(
        [
            (report.to_dict(), report.sha256)
            for report in dataset.campaign_reports
        ],
        publication_dataset_sha256=dataset.sha256,
    )


def publish_campaign_publication(
    output: Path | str,
    dataset: CampaignPublicationDataset,
) -> tuple[str, str]:
    try:
        return publish_report_files(
            output,
            dataset.canonical_bytes,
            render_campaign_publication(dataset),
        )
    except PublicationError:
        raise
    except CampaignReportError as exc:
        raise PublicationError(str(exc)) from exc
