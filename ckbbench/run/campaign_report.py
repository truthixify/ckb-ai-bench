"""Deterministic reports from accepted campaign evidence."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ckbbench.run.attempt_store import AttemptEnvelope, AttemptStore
from ckbbench.run.campaign import (
    REPORT_RESOLUTION_SCHEMA_VERSION,
    AcceptedReportResolution,
    CampaignManifest,
)
from ckbbench.run.chain_profile import ChainProfile
from ckbbench.run.task_attempt import (
    AttemptTimings,
    AttemptUsage,
    ExecutionSource,
    TaskBudget,
    artifact_sha256,
    canonical_json_bytes,
    validate_public_artifact_values,
)
from ckbbench.run.treatment_surface import TreatmentSurfaceProfile
from ckbbench.verify.diagnostics import (
    VerificationDiagnosticError,
    VerificationDiagnostics,
)

if TYPE_CHECKING:
    from ckbbench.run.suite_release import CampaignReleaseBinding


REPORT_DATASET_SCHEMA_VERSION = "ckbbench-campaign-report-dataset-v2"
REPORT_BUILDER_DIGEST_METHOD = "sha256-git-ls-tree-v1"
_MAX_DATASET_BYTES = 32 << 20
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")

METHODOLOGY = {
    "accepted_evidence": (
        "Every row comes from the frozen campaign manifest, the separately published accepted "
        "resolution, and the exact immutable attempt envelopes named by that resolution."
    ),
    "acquisition_usage": (
        "Acquisition usage sums the original attempt and its whole-task retry. Observed partial "
        "usage remains visible and is never labelled exact."
    ),
    "comparison": (
        "B and C are compared only inside the same trial, task, chain profile, model variant and "
        "thinking level. A group delta is withheld if any declared pair lacks correctness evidence."
    ),
    "correctness": (
        "Passes, verifier failures and protocol violations are correctness observations. "
        "Infrastructure failures are excluded rather than converted to zero scores."
    ),
    "diagnostics": (
        "Task rewards remain all-or-nothing. Verifier criterion counts are diagnostic only and "
        "never contribute partial benchmark credit."
    ),
    "health": (
        "Infrastructure outcomes, cleanup state and retry lineage are reported independently from "
        "correctness."
    ),
    "selection": (
        "The report never discovers accepted evidence by scanning a results directory and never "
        "changes inclusion after observing outcomes."
    ),
}


class CampaignReportError(ValueError):
    """Accepted evidence cannot produce a valid deterministic report."""


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CampaignReportError(f"{label} must contain exactly the reviewed fields")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CampaignReportError(f"{label} must be a bounded public identifier")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CampaignReportError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CampaignReportError(f"{label} must be an integer of at least {minimum}")
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampaignReportError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise CampaignReportError(f"{label} must be a non-negative finite number")
    return number


def _nullable_finite(value: Any, label: str) -> float | None:
    return None if value is None else _finite(value, label)


def _nullable_int(value: Any, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CampaignReportError(f"{label} must be an array")
    return value


def _canonical_equal(left: Any, right: Any) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


@dataclass(frozen=True)
class ReportBuilderSource:
    repository_revision: str
    source_tree_sha256: str
    digest_method: str = REPORT_BUILDER_DIGEST_METHOD

    def __post_init__(self) -> None:
        if not isinstance(self.repository_revision, str) or _REVISION.fullmatch(
            self.repository_revision
        ) is None:
            raise CampaignReportError("report builder needs a full repository revision")
        _sha(self.source_tree_sha256, "report builder source tree")
        if self.digest_method != REPORT_BUILDER_DIGEST_METHOD:
            raise CampaignReportError("report builder digest method is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest_method": self.digest_method,
            "repository_revision": self.repository_revision,
            "source_tree_sha256": self.source_tree_sha256,
        }

    @classmethod
    def from_dict(cls, document: Any) -> ReportBuilderSource:
        return cls(**_exact(document, {
            "digest_method", "repository_revision", "source_tree_sha256",
        }, "report builder source"))


def _git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ("git", "-C", str(repo), *arguments),
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CampaignReportError("report builder source cannot be resolved from Git") from exc


def resolve_report_builder_source(repository_root: Path | str) -> ReportBuilderSource:
    """Bind a report to the exact clean tracked tree that renders it."""
    root = Path(repository_root)
    try:
        if root.is_symlink() or not root.is_dir():
            raise CampaignReportError("report builder repository must be a real directory")
    except OSError as exc:
        raise CampaignReportError("report builder repository is inaccessible") from exc
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=no").stdout
    if status:
        raise CampaignReportError("report builder tracked tree must be clean")
    revision = _git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
    tree = _git(root, "ls-tree", "-r", "--full-tree", "-z", "HEAD").stdout
    return ReportBuilderSource(
        repository_revision=revision,
        source_tree_sha256=hashlib.sha256(tree).hexdigest(),
    )


@dataclass(frozen=True)
class CampaignReportDataset:
    """Strict immutable wrapper around the canonical public report dataset."""

    _payload: bytes

    def __post_init__(self) -> None:
        try:
            document = json.loads(self._payload.decode("ascii"), object_pairs_hook=_unique_object)
        except CampaignReportError:
            raise
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CampaignReportError("report dataset is not canonical JSON") from exc
        _validate_dataset(document)
        if self._payload != canonical_json_bytes(document):
            raise CampaignReportError("report dataset bytes are not canonical")

    @classmethod
    def from_dict(cls, document: Any) -> CampaignReportDataset:
        _validate_dataset(document)
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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise CampaignReportError("report dataset contains a duplicate JSON key")
        document[key] = value
    return document


def load_campaign_report_dataset(path: Path | str) -> CampaignReportDataset:
    source = Path(path)
    try:
        mode = source.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise CampaignReportError("report dataset must be a regular non-symlink file")
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            payload = handle.read(_MAX_DATASET_BYTES + 1)
    except CampaignReportError:
        raise
    except OSError as exc:
        raise CampaignReportError("report dataset cannot be read") from exc
    if len(payload) > _MAX_DATASET_BYTES:
        raise CampaignReportError("report dataset exceeds its byte limit")
    return CampaignReportDataset(payload)


_ATTEMPT_KEYS = {
    "agent_exit_status", "arm", "artifact_reference_sha256", "attempt_id", "batch_id",
    "budget", "chain_profile_id", "chain_profile_sha256", "chain_track", "cleanup_receipt_count",
    "cleanup_status", "correctness_eligible", "created_utc", "failure_category", "failure_stage",
    "grade_reason", "grade_status", "max_score", "model_profile_id", "model_profile_sha256",
    "model_variant_id", "outcome", "predecessor_attempt_id", "requested_model", "result_created_utc",
    "retry_ordinal", "score_awarded", "slot_id", "slot_order", "task_id", "terminal", "thinking_level",
    "timings", "treatment_profile_id", "treatment_profile_sha256", "trial_id", "usage",
    "preflight_status", "controller_request_count_status", "controller_request_count",
    "task_content_sha256", "verification_diagnostics",
}

_ACQUISITION_KEYS = {
    "arm", "attempt_ids", "chain_profile_id", "chain_profile_sha256", "chain_track", "completion_tokens",
    "cost_status", "failure_counts", "infrastructure_failure_attempts", "model_calls", "model_profile_id", "model_profile_sha256",
    "model_variant_id", "observed_cost_usd", "prompt_tokens", "provider_attempts", "provider_responses",
    "provider_retry_count", "provider_retry_delay_seconds", "requested_model", "retry_count", "slot_id",
    "slot_order", "task_id", "terminal_attempt_id", "terminal_correctness_eligible", "terminal_outcome",
    "thinking_level", "timing_status", "timings", "token_status", "total_tokens", "trial_id",
    "controller_request_count", "controller_request_count_status", "budget", "task_content_sha256",
}

_ARM_SUMMARY_KEYS = {
    "correctness_observations", "infra_failures", "score_awarded", "score_percent", "score_possible",
    "slots",
}

_MATCHED_KEYS = {
    "b_score_awarded", "c_minus_b_score_percent", "c_score_awarded", "comparison_status",
    "correctness_pairs", "pairs", "score_percent_b", "score_percent_c", "score_possible_per_arm",
}

_SUMMARY_KEYS = {
    "acquisition_cost_status", "acquisition_observed_cost_usd", "acquisition_token_status",
    "acquisition_total_tokens", "arms", "chain_profile_id", "chain_profile_sha256", "chain_track",
    "matched", "model_profile_id", "model_profile_sha256", "model_variant_id", "requested_model",
    "retry_count", "thinking_level", "variant_key", "attempt_ids", "slot_ids",
}

_TASK_SUMMARY_KEYS = _SUMMARY_KEYS | {"budget", "task_content_sha256", "task_id"}


def _validate_attempt(row: Any) -> dict[str, Any]:
    item = _exact(row, _ATTEMPT_KEYS, "report attempt")
    for field in (
        "attempt_id", "batch_id", "chain_profile_id", "chain_track", "cleanup_status", "created_utc",
        "grade_reason", "grade_status", "model_profile_id", "model_variant_id", "outcome",
        "requested_model", "result_created_utc", "slot_id", "task_id", "thinking_level",
        "treatment_profile_id", "trial_id",
    ):
        if not isinstance(item[field], str):
            raise CampaignReportError(f"report attempt {field} must be public text")
    for field in (
        "artifact_reference_sha256", "chain_profile_sha256", "model_profile_sha256",
        "task_content_sha256", "treatment_profile_sha256",
    ):
        _sha(item[field], f"report attempt {field}")
    for field in ("cleanup_receipt_count", "max_score", "retry_ordinal", "score_awarded", "slot_order"):
        _integer(item[field], f"report attempt {field}")
    if item["max_score"] <= 0 or item["score_awarded"] > item["max_score"]:
        raise CampaignReportError("report attempt score is invalid")
    if item["arm"] not in {"B", "C"} or item["chain_track"] not in {
        "testnet", "devnet", "local-hermetic",
    }:
        raise CampaignReportError("report attempt experimental identity is unsupported")
    if item["outcome"] not in {"pass", "agent_fail", "infra_fail", "protocol_violation"}:
        raise CampaignReportError("report attempt outcome is unsupported")
    if item["grade_status"] not in {"passed", "failed", "not_scored"}:
        raise CampaignReportError("report attempt grade status is unsupported")
    if item["cleanup_status"] != "complete" or item["cleanup_receipt_count"] < 1:
        raise CampaignReportError("accepted report attempt cleanup must be complete")
    if item["retry_ordinal"] not in {0, 1}:
        raise CampaignReportError("report attempt retry ordinal is unsupported")
    if item["preflight_status"] not in {"passed", "failed"}:
        raise CampaignReportError("report attempt preflight status is unsupported")
    if item["controller_request_count_status"] not in {"exact", "unknown"}:
        raise CampaignReportError("report attempt controller request status is unsupported")
    _nullable_int(item["controller_request_count"], "report attempt controller requests")
    if (item["controller_request_count_status"] == "exact") != (
        item["controller_request_count"] is not None
    ):
        raise CampaignReportError("report attempt controller request count is inconsistent")
    if type(item["terminal"]) is not bool or type(item["correctness_eligible"]) is not bool:
        raise CampaignReportError("report attempt eligibility fields must be boolean")
    if item["predecessor_attempt_id"] is not None and not isinstance(
        item["predecessor_attempt_id"], str
    ):
        raise CampaignReportError("report attempt predecessor must be null or an ID")
    for field in ("agent_exit_status", "failure_category", "failure_stage"):
        if item[field] is not None and not isinstance(item[field], str):
            raise CampaignReportError(f"report attempt {field} must be null or text")
    try:
        TaskBudget.from_dict(item["budget"])
        AttemptUsage.from_dict(item["usage"])
        AttemptTimings.from_dict(item["timings"])
        diagnostics = VerificationDiagnostics.from_dict(item["verification_diagnostics"])
    except VerificationDiagnosticError as exc:
        raise CampaignReportError("report attempt diagnostics are invalid") from exc
    except ValueError as exc:
        raise CampaignReportError("report attempt contains invalid typed metrics") from exc
    if item["correctness_eligible"] != (item["outcome"] != "infra_fail"):
        raise CampaignReportError("report attempt correctness contradicts its outcome")
    if item["correctness_eligible"] != (item["grade_status"] != "not_scored"):
        raise CampaignReportError("report attempt grade contradicts correctness eligibility")
    if diagnostics.status != "unavailable":
        if item["grade_status"] == "not_scored":
            raise CampaignReportError(
                "unscored report attempt cannot carry verifier diagnostics"
            )
        if item["grade_status"] == "passed" and (
            diagnostics.status != "complete" or diagnostics.criteria_failed != 0
        ):
            raise CampaignReportError(
                "passed report attempt contradicts its verifier diagnostics"
            )
        if (
            item["grade_status"] == "failed"
            and diagnostics.status == "complete"
            and diagnostics.criteria_failed == 0
        ):
            raise CampaignReportError(
                "failed report attempt contradicts its verifier diagnostics"
            )
    return item


def _lineage_token_status(usages: list[dict[str, Any]]) -> str:
    statuses = [usage["token_usage_status"] for usage in usages]
    if all(status == "complete" for status in statuses):
        return "complete"
    if all(status == "not_started" for status in statuses):
        return "not_started"
    if all(status == "unavailable" for status in statuses):
        return "unavailable"
    return "incomplete"


def _lineage_cost(usages: list[dict[str, Any]]) -> tuple[str, str | None]:
    values = [
        Decimal(usage["provider_reported_cost_usd"])
        for usage in usages
        if usage["provider_reported_cost_usd"] is not None
    ]
    if not values:
        return "unavailable", None
    exact = all(usage["cost_status"] == "complete" for usage in usages)
    value = format(sum(values, Decimal(0)), "f").rstrip("0").rstrip(".") or "0"
    return ("complete" if exact else "lower_bound"), value


def _sum_known(usages: list[dict[str, Any]], field: str) -> int | None:
    values = [usage[field] for usage in usages if usage[field] is not None]
    return None if not values else sum(values)


def _derive_acquisitions(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in attempts:
        grouped.setdefault(row["slot_id"], []).append(row)
    acquisitions = []
    for slot_id, rows in sorted(grouped.items(), key=lambda item: item[1][0]["slot_order"]):
        rows = sorted(rows, key=lambda row: row["retry_ordinal"])
        terminal = next((row for row in rows if row["terminal"]), None)
        if terminal is None or rows[0]["retry_ordinal"] != 0:
            raise CampaignReportError("report attempt lineage lacks one terminal original")
        if [row["retry_ordinal"] for row in rows] != list(range(len(rows))):
            raise CampaignReportError("report attempt lineage has invalid retry ordinals")
        usages = [row["usage"] for row in rows]
        timing_rows = [row["timings"] for row in rows]
        failure_counts: dict[str, int] = {}
        for usage in usages:
            for category, count in usage["provider_failure_counts"].items():
                failure_counts[category] = failure_counts.get(category, 0) + count
        cost_status, cost = _lineage_cost(usages)
        controller_counts = [row["controller_request_count"] for row in rows]
        known_controller_counts = [value for value in controller_counts if value is not None]
        acquisitions.append({
            "arm": terminal["arm"],
            "attempt_ids": [row["attempt_id"] for row in rows],
            "budget": terminal["budget"],
            "chain_profile_id": terminal["chain_profile_id"],
            "chain_profile_sha256": terminal["chain_profile_sha256"],
            "chain_track": terminal["chain_track"],
            "completion_tokens": _sum_known(usages, "completion_tokens"),
            "controller_request_count": (
                sum(known_controller_counts) if known_controller_counts else None
            ),
            "controller_request_count_status": (
                "exact" if len(known_controller_counts) == len(rows) else "incomplete"
            ),
            "cost_status": cost_status,
            "failure_counts": dict(sorted(failure_counts.items())),
            "infrastructure_failure_attempts": sum(
                row["outcome"] == "infra_fail" for row in rows
            ),
            "model_calls": sum(usage["model_calls"] for usage in usages),
            "model_profile_id": terminal["model_profile_id"],
            "model_profile_sha256": terminal["model_profile_sha256"],
            "model_variant_id": terminal["model_variant_id"],
            "observed_cost_usd": cost,
            "prompt_tokens": _sum_known(usages, "prompt_tokens"),
            "provider_attempts": sum(usage["provider_attempts"] for usage in usages),
            "provider_responses": sum(usage["provider_responses"] for usage in usages),
            "provider_retry_count": sum(usage["provider_retry_count"] for usage in usages),
            "provider_retry_delay_seconds": sum(
                usage["provider_retry_delay_seconds"] for usage in usages
            ),
            "requested_model": terminal["requested_model"],
            "retry_count": len(rows) - 1,
            "slot_id": slot_id,
            "slot_order": terminal["slot_order"],
            "task_id": terminal["task_id"],
            "task_content_sha256": terminal["task_content_sha256"],
            "terminal_attempt_id": terminal["attempt_id"],
            "terminal_correctness_eligible": terminal["correctness_eligible"],
            "terminal_outcome": terminal["outcome"],
            "thinking_level": terminal["thinking_level"],
            "timing_status": (
                "complete"
                if all(row["measurement_status"] == "complete" for row in timing_rows)
                else "incomplete"
            ),
            "timings": {
                field: float(sum(row[field] for row in timing_rows))
                for field in (
                    "reservation_seconds", "preflight_seconds", "setup_seconds", "agent_seconds",
                    "grading_seconds",
                )
            },
            "token_status": _lineage_token_status(usages),
            "total_tokens": _sum_known(usages, "total_tokens"),
            "trial_id": terminal["trial_id"],
        })
    return acquisitions


def _variant_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "chain_profile_id": row["chain_profile_id"],
        "chain_profile_sha256": row["chain_profile_sha256"],
        "chain_track": row["chain_track"],
        "model_profile_id": row["model_profile_id"],
        "model_profile_sha256": row["model_profile_sha256"],
        "model_variant_id": row["model_variant_id"],
        "requested_model": row["requested_model"],
        "thinking_level": row["thinking_level"],
    }


def _percent(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(100.0 * numerator / denominator, 6)


def _aggregate_cost_status(values: list[str]) -> str:
    if values and all(value == "complete" for value in values):
        return "complete"
    if not values or all(value == "unavailable" for value in values):
        return "unavailable"
    return "lower_bound"


def _aggregate_token_status(values: list[str]) -> str:
    if values and all(value == "complete" for value in values):
        return "complete"
    if values and all(value == "not_started" for value in values):
        return "not_started"
    if not values or all(value == "unavailable" for value in values):
        return "unavailable"
    return "incomplete"


def _summary(
    group: list[dict[str, Any]],
    attempt_source: dict[str, dict[str, Any]],
    *,
    task_identity: tuple[str, str, dict[str, Any]] | None,
) -> dict[str, Any]:
    identity = _variant_identity(group[0])
    arms: dict[str, dict[str, Any]] = {}
    for arm in ("B", "C"):
        rows = [row for row in group if row["arm"] == arm]
        scored = [row for row in rows if row["terminal_correctness_eligible"]]
        terminals = [attempt_source[row["terminal_attempt_id"]] for row in rows]
        awarded = sum(row["score_awarded"] for row in terminals if row["correctness_eligible"])
        possible = sum(row["max_score"] for row in terminals if row["correctness_eligible"])
        arms[arm] = {
            "correctness_observations": len(scored),
            "infra_failures": sum(row["infrastructure_failure_attempts"] for row in rows),
            "score_awarded": awarded,
            "score_percent": _percent(awarded, possible),
            "score_possible": possible,
            "slots": len(rows),
        }
    pairs: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for row in group:
        pair = pairs.setdefault((row["trial_id"], row["task_id"]), {})
        if row["arm"] in pair:
            raise CampaignReportError("report aggregation repeats an arm inside one matched pair")
        pair[row["arm"]] = row
    paired = [pair for pair in pairs.values() if set(pair) == {"B", "C"}]
    eligible = [
        pair for pair in paired
        if pair["B"]["terminal_correctness_eligible"]
        and pair["C"]["terminal_correctness_eligible"]
    ]
    terminal_by_slot = {
        row["slot_id"]: attempt_source[row["terminal_attempt_id"]] for row in group
    }
    b_awarded = sum(terminal_by_slot[pair["B"]["slot_id"]]["score_awarded"] for pair in eligible)
    c_awarded = sum(terminal_by_slot[pair["C"]["slot_id"]]["score_awarded"] for pair in eligible)
    possible = sum(terminal_by_slot[pair["B"]["slot_id"]]["max_score"] for pair in eligible)
    b_percent = _percent(b_awarded, possible)
    c_percent = _percent(c_awarded, possible)
    comparison_available = bool(paired) and len(eligible) == len(paired)
    matched = {
        "b_score_awarded": b_awarded,
        "c_minus_b_score_percent": (
            round(c_percent - b_percent, 6)
            if comparison_available and b_percent is not None and c_percent is not None
            else None
        ),
        "c_score_awarded": c_awarded,
        "comparison_status": "available" if comparison_available else "withheld",
        "correctness_pairs": len(eligible),
        "pairs": len(paired),
        "score_percent_b": b_percent if comparison_available else None,
        "score_percent_c": c_percent if comparison_available else None,
        "score_possible_per_arm": possible,
    }
    token_statuses = [row["token_status"] for row in group]
    cost_statuses = [row["cost_status"] for row in group]
    costs = [Decimal(row["observed_cost_usd"]) for row in group if row["observed_cost_usd"]]
    summary = {
        **identity,
        "attempt_ids": [
            attempt_id for row in group for attempt_id in row["attempt_ids"]
        ],
        "acquisition_cost_status": _aggregate_cost_status(cost_statuses),
        "acquisition_observed_cost_usd": (
            (format(sum(costs, Decimal(0)), "f").rstrip("0").rstrip(".") or "0")
            if costs else None
        ),
        "acquisition_token_status": _aggregate_token_status(token_statuses),
        "acquisition_total_tokens": (
            sum(row["total_tokens"] for row in group if row["total_tokens"] is not None)
            if any(row["total_tokens"] is not None for row in group) else None
        ),
        "arms": arms,
        "matched": matched,
        "retry_count": sum(row["retry_count"] for row in group),
        "slot_ids": [row["slot_id"] for row in group],
        "variant_key": artifact_sha256(identity),
    }
    if task_identity is not None:
        task_id, task_content_sha256, budget = task_identity
        summary["budget"] = budget
        summary["task_content_sha256"] = task_content_sha256
        summary["task_id"] = task_id
    return summary


def _derive_summaries(
    attempts: list[dict[str, Any]], acquisitions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attempt_source = {row["attempt_id"]: row for row in attempts}
    variants: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    tasks: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in acquisitions:
        key = tuple(_variant_identity(row).values())
        variants.setdefault(key, []).append(row)
        tasks.setdefault((
            *key,
            row["task_id"],
            row["task_content_sha256"],
            canonical_json_bytes(row["budget"]),
        ), []).append(row)
    variant_rows = [
        _summary(rows, attempt_source, task_identity=None)
        for _key, rows in sorted(variants.items())
    ]
    task_rows = [
        _summary(
            rows,
            attempt_source,
            task_identity=(key[-3], key[-2], rows[0]["budget"]),
        )
        for key, rows in sorted(tasks.items())
    ]
    return variant_rows, task_rows


def _validate_acquisition(row: Any) -> dict[str, Any]:
    item = _exact(row, _ACQUISITION_KEYS, "report acquisition")
    for field in (
        "arm", "chain_profile_id", "chain_track", "cost_status", "model_profile_id",
        "model_variant_id", "requested_model", "slot_id", "task_id", "terminal_attempt_id",
        "terminal_outcome", "thinking_level", "timing_status", "token_status", "trial_id",
    ):
        if not isinstance(item[field], str):
            raise CampaignReportError(f"report acquisition {field} must be text")
    for field in ("chain_profile_sha256", "model_profile_sha256"):
        _sha(item[field], f"report acquisition {field}")
    _sha(item["task_content_sha256"], "report acquisition task content")
    try:
        TaskBudget.from_dict(item["budget"])
    except ValueError as exc:
        raise CampaignReportError("report acquisition budget is invalid") from exc
    for field in (
        "model_calls", "provider_attempts", "provider_responses", "provider_retry_count",
        "provider_retry_delay_seconds", "retry_count", "slot_order",
        "infrastructure_failure_attempts",
    ):
        _integer(item[field], f"report acquisition {field}")
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        _nullable_int(item[field], f"report acquisition {field}")
    if type(item["terminal_correctness_eligible"]) is not bool:
        raise CampaignReportError("report acquisition correctness must be boolean")
    if item["arm"] not in {"B", "C"} or item["terminal_outcome"] not in {
        "pass", "agent_fail", "infra_fail", "protocol_violation",
    }:
        raise CampaignReportError("report acquisition outcome identity is unsupported")
    if item["token_status"] not in {
        "complete", "incomplete", "not_started", "unavailable",
    } or item["cost_status"] not in {"complete", "lower_bound", "unavailable"}:
        raise CampaignReportError("report acquisition usage status is unsupported")
    if item["timing_status"] not in {"complete", "incomplete"}:
        raise CampaignReportError("report acquisition timing status is unsupported")
    if item["controller_request_count_status"] not in {"exact", "incomplete"}:
        raise CampaignReportError("report acquisition controller request status is unsupported")
    _nullable_int(item["controller_request_count"], "report acquisition controller requests")
    if item["controller_request_count_status"] == "exact" and item[
        "controller_request_count"
    ] is None:
        raise CampaignReportError("exact controller request count cannot be null")
    attempt_ids = _list(item["attempt_ids"], "report acquisition attempt IDs")
    if not attempt_ids or not all(isinstance(value, str) for value in attempt_ids):
        raise CampaignReportError("report acquisition attempt IDs are invalid")
    if len(attempt_ids) != item["retry_count"] + 1 or item["terminal_attempt_id"] != attempt_ids[-1]:
        raise CampaignReportError("report acquisition lineage is inconsistent")
    failures = item["failure_counts"]
    if not isinstance(failures, dict) or not all(
        isinstance(key, str) and type(value) is int and value > 0
        for key, value in failures.items()
    ):
        raise CampaignReportError("report acquisition failure counts are invalid")
    if list(failures) != sorted(failures):
        raise CampaignReportError("report acquisition failure counts must be sorted")
    timings = _exact(item["timings"], {
        "agent_seconds", "grading_seconds", "preflight_seconds", "reservation_seconds",
        "setup_seconds",
    }, "report acquisition timings")
    for field, value in timings.items():
        _finite(value, f"report acquisition timings {field}")
    if item["observed_cost_usd"] is not None:
        try:
            if Decimal(item["observed_cost_usd"]) < 0:
                raise ValueError
            if not Decimal(item["observed_cost_usd"]).is_finite():
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            raise CampaignReportError("report acquisition observed cost is invalid") from None
    return item


def _validate_arm_summary(value: Any) -> None:
    row = _exact(value, _ARM_SUMMARY_KEYS, "report arm summary")
    for field in (
        "correctness_observations", "infra_failures", "score_awarded", "score_possible", "slots",
    ):
        _integer(row[field], f"report arm summary {field}")
    _nullable_finite(row["score_percent"], "report arm summary score percent")


def _validate_summary(value: Any, *, task: bool) -> None:
    row = _exact(value, _TASK_SUMMARY_KEYS if task else _SUMMARY_KEYS, "report summary")
    for field in (
        "acquisition_cost_status", "acquisition_token_status", "chain_profile_id", "chain_track",
        "model_profile_id", "model_variant_id", "requested_model", "thinking_level",
    ):
        if not isinstance(row[field], str):
            raise CampaignReportError(f"report summary {field} must be text")
    if row["acquisition_cost_status"] not in {"complete", "lower_bound", "unavailable"}:
        raise CampaignReportError("report summary cost status is unsupported")
    if row["acquisition_token_status"] not in {
        "complete", "incomplete", "not_started", "unavailable",
    }:
        raise CampaignReportError("report summary token status is unsupported")
    if task and not isinstance(row["task_id"], str):
        raise CampaignReportError("report task summary needs a task ID")
    if task:
        _sha(row["task_content_sha256"], "report task summary content")
        try:
            TaskBudget.from_dict(row["budget"])
        except ValueError as exc:
            raise CampaignReportError("report task summary budget is invalid") from exc
    for field in ("chain_profile_sha256", "model_profile_sha256", "variant_key"):
        _sha(row[field], f"report summary {field}")
    _integer(row["retry_count"], "report summary retry count")
    for field in ("attempt_ids", "slot_ids"):
        values = _list(row[field], f"report summary {field}")
        if not values or not all(isinstance(item, str) for item in values):
            raise CampaignReportError(f"report summary {field} is invalid")
        if len(values) != len(set(values)):
            raise CampaignReportError(f"report summary {field} must be unique")
    _nullable_int(row["acquisition_total_tokens"], "report summary tokens")
    if row["acquisition_observed_cost_usd"] is not None and not isinstance(
        row["acquisition_observed_cost_usd"], str
    ):
        raise CampaignReportError("report summary cost must be null or decimal text")
    arms = _exact(row["arms"], {"B", "C"}, "report summary arms")
    _validate_arm_summary(arms["B"])
    _validate_arm_summary(arms["C"])
    matched = _exact(row["matched"], _MATCHED_KEYS, "report matched summary")
    for field in (
        "b_score_awarded", "c_score_awarded", "correctness_pairs", "pairs",
        "score_possible_per_arm",
    ):
        _integer(matched[field], f"report matched {field}")
    for field in ("c_minus_b_score_percent", "score_percent_b", "score_percent_c"):
        _nullable_finite(matched[field], f"report matched {field}")
    if matched["comparison_status"] not in {"available", "withheld"}:
        raise CampaignReportError("report matched comparison status is invalid")
    if (matched["comparison_status"] == "available") != (
        matched["pairs"] > 0 and matched["correctness_pairs"] == matched["pairs"]
    ):
        raise CampaignReportError("report matched status contradicts its eligible pairs")


def _validate_dataset(document: Any) -> None:
    root = _exact(document, {
        "attempts", "campaign", "methodology", "profiles", "report_builder", "resolution",
        "schema_version", "slot_acquisitions", "task_comparisons", "variant_summaries",
    }, "campaign report dataset")
    if root["schema_version"] != REPORT_DATASET_SCHEMA_VERSION:
        raise CampaignReportError("report dataset schema version is unsupported")
    campaign = _exact(root["campaign"], {
        "campaign_id", "concurrency_contract", "created_utc", "execution_plan_id",
        "execution_plan_sha256", "execution_source", "manifest_sha256", "retry_policy_id",
        "retry_policy_sha256", "slot_count", "stopping_rule_id", "stopping_rule_sha256",
        "suite_freeze_sha256", "suite_semver",
    }, "report campaign")
    for field in (
        "campaign_id", "concurrency_contract", "created_utc", "execution_plan_id", "retry_policy_id",
        "stopping_rule_id", "suite_semver",
    ):
        if not isinstance(campaign[field], str):
            raise CampaignReportError(f"report campaign {field} must be text")
    for field in (
        "execution_plan_sha256", "manifest_sha256", "retry_policy_sha256", "stopping_rule_sha256",
        "suite_freeze_sha256",
    ):
        _sha(campaign[field], f"report campaign {field}")
    _integer(campaign["slot_count"], "report campaign slot count", minimum=1)
    try:
        ExecutionSource.from_dict(campaign["execution_source"])
    except ValueError as exc:
        raise CampaignReportError("report campaign execution source is invalid") from exc
    resolution = _exact(root["resolution"], {
        "attempt_count", "schema_version", "sha256", "slot_count",
    }, "report resolution")
    for field in ("attempt_count", "slot_count"):
        _integer(resolution[field], f"report resolution {field}", minimum=1)
    _sha(resolution["sha256"], "report resolution digest")
    if resolution["schema_version"] != REPORT_RESOLUTION_SCHEMA_VERSION:
        raise CampaignReportError("report resolution schema version is unsupported")
    ReportBuilderSource.from_dict(root["report_builder"])
    profiles = _exact(root["profiles"], {
        "chain_profiles", "model_variants", "release_validated", "treatment_profiles",
    }, "report profiles")
    if type(profiles["release_validated"]) is not bool:
        raise CampaignReportError("report release validation marker must be boolean")
    chain_rows = _list(profiles["chain_profiles"], "report chain profiles")
    chain_keys: list[tuple[str, str]] = []
    for item in chain_rows:
        row = _exact(item, {"profile", "sha256"}, "report chain profile")
        try:
            profile = ChainProfile.from_dict(row["profile"])
        except ValueError as exc:
            raise CampaignReportError("report chain profile is invalid") from exc
        if profile.sha256 != _sha(row["sha256"], "report chain profile digest"):
            raise CampaignReportError("report chain profile digest does not match")
        chain_keys.append((profile.profile_id, row["sha256"]))
    if chain_keys != sorted(set(chain_keys)):
        raise CampaignReportError("report chain profiles must be unique and sorted")
    treatment_rows = _list(profiles["treatment_profiles"], "report treatment profiles")
    treatment_keys: list[tuple[str, str]] = []
    for item in treatment_rows:
        row = _exact(item, {"profile", "sha256"}, "report treatment profile")
        try:
            profile = TreatmentSurfaceProfile.from_dict(row["profile"])
        except ValueError as exc:
            raise CampaignReportError("report treatment profile is invalid") from exc
        if profile.sha256 != _sha(row["sha256"], "report treatment profile digest"):
            raise CampaignReportError("report treatment profile digest does not match")
        treatment_keys.append((profile.profile_id, row["sha256"]))
    if treatment_keys != sorted(set(treatment_keys)):
        raise CampaignReportError("report treatment profiles must be unique and sorted")
    variants = _list(profiles["model_variants"], "report model variants")
    for item in variants:
        row = _exact(item, {
            "model_profile_id", "model_profile_sha256", "model_variant_id", "requested_model",
            "thinking_level",
        }, "report model variant")
        for value in row.values():
            if not isinstance(value, str):
                raise CampaignReportError("report model variant fields must be text")
        _sha(row["model_profile_sha256"], "report model profile digest")
    if variants != sorted(variants, key=lambda row: row["model_variant_id"]):
        raise CampaignReportError("report model variants must be sorted")
    if root["methodology"] != METHODOLOGY:
        raise CampaignReportError("report methodology differs from the reviewed rules")
    attempts = [_validate_attempt(row) for row in _list(root["attempts"], "report attempts")]
    if not attempts or len({row["attempt_id"] for row in attempts}) != len(attempts):
        raise CampaignReportError("report attempts must be non-empty and unique")
    if attempts != sorted(attempts, key=lambda row: (row["slot_order"], row["retry_ordinal"])):
        raise CampaignReportError("report attempts must follow campaign and retry order")
    acquisitions = [
        _validate_acquisition(row) for row in _list(root["slot_acquisitions"], "report acquisitions")
    ]
    expected_acquisitions = _derive_acquisitions(attempts)
    if not _canonical_equal(acquisitions, expected_acquisitions):
        raise CampaignReportError("report acquisitions do not derive from attempt rows")
    expected_variants, expected_tasks = _derive_summaries(attempts, acquisitions)
    summaries = _list(root["variant_summaries"], "report variant summaries")
    task_rows = _list(root["task_comparisons"], "report task comparisons")
    for row in summaries:
        _validate_summary(row, task=False)
    for row in task_rows:
        _validate_summary(row, task=True)
    if not _canonical_equal(summaries, expected_variants):
        raise CampaignReportError("report variant summaries do not derive from attempt rows")
    if not _canonical_equal(task_rows, expected_tasks):
        raise CampaignReportError("report task comparisons do not derive from attempt rows")
    if campaign["slot_count"] != len(acquisitions) or resolution["slot_count"] != len(acquisitions):
        raise CampaignReportError("report slot counts disagree")
    if resolution["attempt_count"] != len(attempts):
        raise CampaignReportError("report attempt count disagrees")
    expected_variants = sorted({
        (
            row["model_variant_id"], row["requested_model"], row["thinking_level"],
            row["model_profile_id"], row["model_profile_sha256"],
        )
        for row in attempts
    })
    observed_variants = [
        (
            row["model_variant_id"], row["requested_model"], row["thinking_level"],
            row["model_profile_id"], row["model_profile_sha256"],
        )
        for row in variants
    ]
    if observed_variants != expected_variants:
        raise CampaignReportError("report model profiles do not match its attempts")
    if profiles["release_validated"]:
        chain_identities = {
            (row["profile"]["profile_id"], row["sha256"]) for row in chain_rows
        }
        treatment_identities = {
            (row["profile"]["profile_id"], row["sha256"]) for row in treatment_rows
        }
        if not chain_identities or not treatment_identities or any(
            (row["chain_profile_id"], row["chain_profile_sha256"]) not in chain_identities
            or (row["treatment_profile_id"], row["treatment_profile_sha256"])
            not in treatment_identities
            for row in attempts
        ):
            raise CampaignReportError("report release profiles do not cover every attempt")
    elif chain_rows or treatment_rows:
        raise CampaignReportError("unvalidated release profiles cannot be published")
    try:
        validate_public_artifact_values(root)
    except ValueError as exc:
        raise CampaignReportError("report dataset contains a secret-shaped value") from exc


def _attempt_row(
    slot_order: int,
    slot_id: str,
    envelope: AttemptEnvelope,
    reference: Any,
    terminal_attempt_id: str,
) -> dict[str, Any]:
    identity = envelope.intent.identity
    result = envelope.result
    grade = result.grade
    return {
        "agent_exit_status": result.agent_exit_status,
        "arm": identity.arm,
        "artifact_reference_sha256": artifact_sha256(reference.to_dict()),
        "attempt_id": envelope.intent.attempt_id,
        "batch_id": identity.batch_id,
        "budget": identity.budget.to_dict(),
        "chain_profile_id": identity.chain_profile_id,
        "chain_profile_sha256": identity.chain_profile_sha256,
        "chain_track": identity.chain_track,
        "cleanup_receipt_count": len(envelope.receipts),
        "cleanup_status": envelope.receipts[-1].status,
        "controller_request_count": envelope.preflight_evidence.controller_request_count,
        "controller_request_count_status": (
            envelope.preflight_evidence.controller_request_count_status
        ),
        "correctness_eligible": result.correctness_eligible,
        "created_utc": envelope.intent.created_utc,
        "failure_category": result.failure_category,
        "failure_stage": result.failure_stage,
        "grade_reason": grade.reason,
        "grade_status": grade.status,
        "max_score": grade.max_score,
        "model_profile_id": identity.model_profile_id,
        "model_profile_sha256": identity.model_profile_sha256,
        "model_variant_id": identity.model_variant_id,
        "outcome": result.outcome,
        "predecessor_attempt_id": (
            None if envelope.intent.retry is None else envelope.intent.retry.predecessor_attempt_id
        ),
        "preflight_status": envelope.preflight_evidence.status,
        "requested_model": identity.requested_model,
        "result_created_utc": result.created_utc,
        "retry_ordinal": envelope.intent.retry_ordinal,
        "score_awarded": grade.score_awarded,
        "slot_id": slot_id,
        "slot_order": slot_order,
        "task_id": identity.task_id,
        "task_content_sha256": identity.task_content_sha256,
        "terminal": envelope.intent.attempt_id == terminal_attempt_id,
        "thinking_level": identity.thinking_level,
        "timings": result.timings.to_dict(),
        "treatment_profile_id": identity.treatment_profile_id,
        "treatment_profile_sha256": identity.treatment_profile_sha256,
        "trial_id": identity.trial_id,
        "usage": result.usage.to_dict(),
        "verification_diagnostics": grade.diagnostics.to_dict(),
    }


def build_campaign_report_dataset(
    manifest: CampaignManifest,
    resolution: AcceptedReportResolution,
    store: AttemptStore,
    builder_source: ReportBuilderSource,
    release_binding: CampaignReleaseBinding | None = None,
) -> CampaignReportDataset:
    """Build accepted report data from the resolution's exact envelopes only."""
    from ckbbench.run.campaign_operator import validate_report_resolution_evidence

    validate_report_resolution_evidence(manifest, resolution, store)
    if release_binding is not None:
        release_binding.validate_manifest(manifest)
    elif int(manifest.suite_semver.split(".", 1)[0]) >= 4:
        raise CampaignReportError("this suite needs its immutable release binding")
    resolved_by_slot = {row.slot_id: row for row in resolution.slots}
    attempts: list[dict[str, Any]] = []
    for slot_order, slot in enumerate(manifest.ordered_slots, start=1):
        resolved = resolved_by_slot[slot.slot_id]
        references = [resolved.original]
        if resolved.retry is not None:
            references.append(resolved.retry)
        for reference in references:
            envelope = store.load_envelope(reference.attempt_id)
            attempts.append(
                _attempt_row(
                    slot_order,
                    slot.slot_id,
                    envelope,
                    reference,
                    resolved.terminal_attempt_id,
                )
            )
    acquisitions = _derive_acquisitions(attempts)
    variants, tasks = _derive_summaries(attempts, acquisitions)
    model_variants = sorted({
        (
            slot.model_variant_id,
            slot.requested_model,
            slot.thinking_level,
            slot.model_profile_id,
            slot.model_profile_sha256,
        )
        for slot in manifest.slots
    })
    chain_profiles = []
    treatment_profiles = []
    if release_binding is not None:
        chain_profiles = [
            {"profile": profile.to_dict(), "sha256": profile.sha256}
            for profile in sorted(release_binding.chain_profiles, key=lambda row: row.profile_id)
        ]
        treatment_profiles = [
            {"profile": profile.to_dict(), "sha256": profile.sha256}
            for profile in sorted(release_binding.treatment_profiles, key=lambda row: row.profile_id)
        ]
    document = {
        "attempts": attempts,
        "campaign": {
            "campaign_id": manifest.campaign_id,
            "concurrency_contract": manifest.concurrency_contract,
            "created_utc": manifest.created_utc,
            "execution_plan_id": manifest.execution_plan_id,
            "execution_plan_sha256": manifest.execution_plan_sha256,
            "execution_source": manifest.execution_source.to_dict(),
            "manifest_sha256": manifest.sha256,
            "retry_policy_id": manifest.retry_policy_id,
            "retry_policy_sha256": manifest.retry_policy_sha256,
            "slot_count": len(manifest.slots),
            "stopping_rule_id": manifest.stopping_rule_id,
            "stopping_rule_sha256": manifest.stopping_rule_sha256,
            "suite_freeze_sha256": manifest.suite_freeze_sha256,
            "suite_semver": manifest.suite_semver,
        },
        "methodology": METHODOLOGY,
        "profiles": {
            "chain_profiles": chain_profiles,
            "model_variants": [
                {
                    "model_profile_id": profile_id,
                    "model_profile_sha256": profile_sha256,
                    "model_variant_id": variant_id,
                    "requested_model": model,
                    "thinking_level": thinking,
                }
                for variant_id, model, thinking, profile_id, profile_sha256 in model_variants
            ],
            "release_validated": release_binding is not None,
            "treatment_profiles": treatment_profiles,
        },
        "report_builder": builder_source.to_dict(),
        "resolution": {
            "attempt_count": len(attempts),
            "schema_version": resolution.schema_version,
            "sha256": resolution.sha256,
            "slot_count": len(resolution.slots),
        },
        "schema_version": REPORT_DATASET_SCHEMA_VERSION,
        "slot_acquisitions": acquisitions,
        "task_comparisons": tasks,
        "variant_summaries": variants,
    }
    return CampaignReportDataset.from_dict(document)


def _fmt_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _fmt_delta(value: float | None) -> str:
    return "withheld" if value is None else f"{value:+.1f} pp"


def _fmt_int(value: int | None) -> str:
    return "n/a" if value is None else f"{value:,}"


def _fmt_cost(value: str | None, status: str) -> str:
    if value is None:
        return "n/a"
    prefix = ">= " if status != "complete" else ""
    return f"{prefix}${Decimal(value):,.6f}".rstrip("0").rstrip(".")


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _failure_label(row: dict[str, Any]) -> str:
    parts = [value for value in (row["failure_stage"], row["failure_category"]) if value]
    provider = row["usage"]["provider_failure_category"]
    if provider:
        parts.append(f"provider:{provider}")
    return " / ".join(parts) if parts else "none"


def _failure_counts(row: dict[str, Any]) -> str:
    counts = row["failure_counts"]
    return ", ".join(f"{key}:{value}" for key, value in counts.items()) if counts else "none"


def _diagnostic_label(row: dict[str, Any]) -> str:
    diagnostics = row["verification_diagnostics"]
    if diagnostics["status"] == "unavailable":
        return "unavailable"
    if diagnostics["status"] == "not_evaluated":
        return f"0 / {diagnostics['criteria_total']} evaluated"
    label = (
        f"{diagnostics['criteria_passed']} passed, "
        f"{diagnostics['criteria_failed']} failed"
    )
    if diagnostics["criteria_not_evaluated"]:
        label += f"; {diagnostics['criteria_not_evaluated']} not reached"
    return label


def render_campaign_report(dataset: CampaignReportDataset) -> bytes:
    data = dataset.to_dict()
    summaries = data["variant_summaries"]
    tasks = data["task_comparisons"]
    attempts = data["attempts"]
    acquisitions = data["slot_acquisitions"]

    overview_rows = "".join(
        "<tr>"
        f"<td><strong>{_e(row['requested_model'])}</strong><span>{_e(row['thinking_level'])}</span></td>"
        f"<td>{_e(row['chain_track'])}<span>{_e(row['chain_profile_id'])}</span></td>"
        f"<td>{_fmt_percent(row['matched']['score_percent_b'])}</td>"
        f"<td>{_fmt_percent(row['matched']['score_percent_c'])}</td>"
        f"<td class='delta'>{_fmt_delta(row['matched']['c_minus_b_score_percent'])}</td>"
        f"<td>{row['matched']['correctness_pairs']} / {row['matched']['pairs']}</td>"
        f"<td>{row['arms']['B']['infra_failures']} / {row['arms']['C']['infra_failures']}</td>"
        f"<td>{row['retry_count']}</td>"
        f"<td>{_fmt_int(row['acquisition_total_tokens'])}<span>{_e(row['acquisition_token_status'])}</span></td>"
        f"<td>{_fmt_cost(row['acquisition_observed_cost_usd'], row['acquisition_cost_status'])}</td>"
        "</tr>"
        for row in summaries
    )
    task_rows = "".join(
        "<tr>"
        f"<td>{_e(row['task_id'])}<span>{_e(row['task_content_sha256'][:12])} / "
        f"{_e(row['budget']['profile_id'])}</span></td>"
        f"<td><strong>{_e(row['requested_model'])}</strong><span>{_e(row['thinking_level'])}</span></td>"
        f"<td>{_e(row['chain_track'])}</td>"
        f"<td>{_fmt_percent(row['matched']['score_percent_b'])}</td>"
        f"<td>{_fmt_percent(row['matched']['score_percent_c'])}</td>"
        f"<td>{_fmt_delta(row['matched']['c_minus_b_score_percent'])}</td>"
        f"<td>{row['matched']['correctness_pairs']} / {row['matched']['pairs']}</td>"
        f"<td>{row['arms']['B']['infra_failures']} / {row['arms']['C']['infra_failures']}</td>"
        f"<td>{row['retry_count']}</td>"
        "</tr>"
        for row in tasks
    )
    attempt_rows = "".join(
        "<tr "
        f"data-model='{_e(row['model_variant_id'])}' data-arm='{_e(row['arm'])}' "
        f"data-outcome='{_e(row['outcome'])}'>"
        f"<td>{row['slot_order']}</td><td>{_e(row['task_id'])}</td><td>{_e(row['arm'])}</td>"
        f"<td>{_e(row['requested_model'])}<span>{_e(row['thinking_level'])}</span></td>"
        f"<td>{row['retry_ordinal']}</td><td><span class='outcome {_e(row['outcome'])}'>{_e(row['outcome'])}</span></td>"
        f"<td>{row['score_awarded']} / {row['max_score']}</td>"
        f"<td>{_e(_diagnostic_label(row))}</td>"
        f"<td>{_e(_failure_label(row))}</td>"
        f"<td>{_fmt_int(row['usage']['total_tokens'])}<span>{_e(row['usage']['token_usage_status'])}</span></td>"
        f"<td>{sum(value for key, value in row['timings'].items() if key != 'measurement_status'):.2f}s"
        f"<span>{_e(row['timings']['measurement_status'])}</span></td>"
        f"<td>{_e(row['cleanup_status'])}</td><td><code>{_e(row['attempt_id'])}</code></td></tr>"
        for row in attempts
    )
    acquisition_rows = "".join(
        "<tr>"
        f"<td>{row['slot_order']}</td><td>{_e(row['task_id'])}</td><td>{_e(row['arm'])}</td>"
        f"<td>{len(row['attempt_ids'])}</td><td>{row['model_calls']}</td>"
        f"<td>{_fmt_int(row['controller_request_count'])}"
        f"<span>{_e(row['controller_request_count_status'])}</span></td>"
        f"<td>{row['provider_attempts']} / {row['provider_responses']}</td>"
        f"<td>{row['provider_retry_count']}<span>{row['provider_retry_delay_seconds']}s wait</span></td>"
        f"<td>{_e(_failure_counts(row))}</td>"
        f"<td>{_fmt_int(row['total_tokens'])}<span>{_e(row['token_status'])}</span></td>"
        f"<td>{_fmt_cost(row['observed_cost_usd'], row['cost_status'])}</td>"
        f"<td>{sum(row['timings'].values()):.2f}s<span>{_e(row['timing_status'])}</span></td></tr>"
        for row in acquisitions
    )
    model_options = "".join(
        f"<option value='{_e(row['model_variant_id'])}'>{_e(row['requested_model'])} / {_e(row['thinking_level'])}</option>"
        for row in data["profiles"]["model_variants"]
    )
    methodology = "".join(
        f"<article><h3>{_e(key.replace('_', ' ').title())}</h3><p>{_e(value)}</p></article>"
        for key, value in data["methodology"].items()
    )
    campaign = data["campaign"]
    provenance = "".join(
        f"<tr><th>{_e(label)}</th><td><code>{_e(value)}</code></td></tr>"
        for label, value in (
            ("Campaign", campaign["campaign_id"]),
            ("Manifest", campaign["manifest_sha256"]),
            ("Accepted resolution", data["resolution"]["sha256"]),
            ("Suite", campaign["suite_semver"]),
            ("Suite freeze", campaign["suite_freeze_sha256"]),
            ("Execution revision", campaign["execution_source"]["repository_revision"]),
            ("Execution tree", campaign["execution_source"]["source_tree_sha256"]),
            ("Report revision", data["report_builder"]["repository_revision"]),
            ("Report tree", data["report_builder"]["source_tree_sha256"]),
            ("Dataset", dataset.sha256),
        )
    )
    provenance += "".join(
        f"<tr><th>Model / {_e(row['thinking_level'])}</th><td><code>"
        f"{_e(row['requested_model'])} | {_e(row['model_variant_id'])} | "
        f"{_e(row['model_profile_id'])}@{_e(row['model_profile_sha256'])}</code></td></tr>"
        for row in data["profiles"]["model_variants"]
    )
    provenance += "".join(
        f"<tr><th>Chain profile</th><td><code>{_e(row['profile']['profile_id'])}@"
        f"{_e(row['sha256'])}</code></td></tr>"
        for row in data["profiles"]["chain_profiles"]
    )
    provenance += "".join(
        f"<tr><th>Treatment profile</th><td><code>{_e(row['profile']['profile_id'])}@"
        f"{_e(row['sha256'])}</code></td></tr>"
        for row in data["profiles"]["treatment_profiles"]
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CKB AI Bench</title><style>
:root{{--bg:#080a0d;--panel:#101419;--line:#27313a;--text:#f3f6f8;--muted:#9aa7b2;--b:#ffb454;--c:#54d6b3;--danger:#ff6b72;--accent:#70a7ff}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:0}}
nav{{position:sticky;top:0;z-index:5;display:flex;gap:20px;align-items:center;padding:14px max(24px,calc((100vw - 1240px)/2));background:rgba(8,10,13,.94);border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}}nav strong{{margin-right:auto}}nav a{{color:var(--muted);text-decoration:none}}nav a:hover{{color:var(--text)}}
main{{max-width:1240px;margin:auto;padding:52px 24px 80px}}header{{display:grid;grid-template-columns:1fr auto;gap:32px;align-items:end;margin-bottom:58px}}h1{{font:700 clamp(38px,6vw,76px)/.95 ui-sans-serif,system-ui;letter-spacing:0;margin:0}}header p{{max-width:540px;color:var(--muted);margin:18px 0 0}}.stamp{{border-left:3px solid var(--c);padding:8px 0 8px 18px;color:var(--muted)}}section{{padding:40px 0;border-top:1px solid var(--line)}}h2{{font:650 26px/1.2 ui-sans-serif,system-ui;letter-spacing:0;margin:0 0 20px}}h3{{letter-spacing:0}}.eyebrow{{color:var(--c);text-transform:uppercase;font-size:12px;margin-bottom:8px}}
.table-wrap{{overflow:auto;border:1px solid var(--line);background:var(--panel)}}table{{width:100%;border-collapse:collapse;min-width:900px}}th,td{{padding:14px 16px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}thead th{{color:var(--muted);font-size:11px;text-transform:uppercase;background:#0d1115;position:sticky;top:49px}}tbody tr:last-child td{{border-bottom:0}}td span{{display:block;color:var(--muted);font-size:11px;margin-top:3px}}td.delta{{color:var(--c)}}code{{font-size:11px;color:var(--muted)}}
.outcome{{display:inline-block!important;margin:0!important;color:var(--text)!important}}.outcome.pass:before{{content:'\\25cf  ';color:var(--c)}}.outcome.agent_fail:before,.outcome.protocol_violation:before{{content:'\\25c6  ';color:var(--b)}}.outcome.infra_fail:before{{content:'\\25a0  ';color:var(--danger)}}
.filters{{display:flex;gap:10px;margin:0 0 14px;flex-wrap:wrap}}select{{appearance:none;background:var(--panel);color:var(--text);border:1px solid var(--line);padding:10px 36px 10px 12px;border-radius:2px}}
.method{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line)}}.method article{{background:var(--panel);padding:22px}}.method h3{{font:650 15px/1.2 ui-sans-serif,system-ui;margin:0 0 8px}}.method p{{color:var(--muted);margin:0}}.provenance th{{width:220px;color:var(--muted)}}
@media(max-width:760px){{nav a{{display:none}}header{{grid-template-columns:1fr}}.method{{grid-template-columns:1fr}}main{{padding-inline:16px}}}}
</style></head><body><nav><strong>CKB AI Bench</strong><a href="#overview">Overview</a><a href="#tasks">Tasks</a><a href="#attempts">Attempts</a><a href="#acquisition">Acquisition</a><a href="#methodology">Methodology</a><a href="#provenance">Provenance</a></nav>
<main><header><div><div class="eyebrow">Accepted campaign evidence</div><h1>CKB AI Bench</h1><p>Task-level results with model thinking, infrastructure health, retry lineage and acquisition usage kept visible as separate evidence.</p></div><div class="stamp">{len(acquisitions)} slots<br>{len(attempts)} attempts<br>{len(summaries)} model variants</div></header>
<section id="overview"><div class="eyebrow">01 / Overview</div><h2>Matched B and C evidence</h2><div class="table-wrap"><table><thead><tr><th>Model / thinking</th><th>Chain</th><th>Matched B</th><th>Matched C</th><th>C - B</th><th>Eligible pairs</th><th>Infra B / C</th><th>Retries</th><th>Acquisition tokens</th><th>Reported cost</th></tr></thead><tbody>{overview_rows}</tbody></table></div></section>
<section id="tasks"><div class="eyebrow">02 / Tasks</div><h2>Task comparisons</h2><div class="table-wrap"><table><thead><tr><th>Task</th><th>Model / thinking</th><th>Chain</th><th>B</th><th>C</th><th>C - B</th><th>Eligible pairs</th><th>Infra B / C</th><th>Retries</th></tr></thead><tbody>{task_rows}</tbody></table></div></section>
<section id="attempts"><div class="eyebrow">03 / Attempts</div><h2>Originals and retries</h2><div class="filters"><select id="model-filter"><option value="">All model variants</option>{model_options}</select><select id="arm-filter"><option value="">Both arms</option><option>B</option><option>C</option></select><select id="outcome-filter"><option value="">All outcomes</option><option>pass</option><option>agent_fail</option><option>infra_fail</option><option>protocol_violation</option></select></div><div class="table-wrap"><table><thead><tr><th>Slot</th><th>Task</th><th>Arm</th><th>Model</th><th>Retry</th><th>Outcome</th><th>Score</th><th>Verifier criteria</th><th>Failure</th><th>Tokens</th><th>Measured stages</th><th>Cleanup</th><th>Attempt</th></tr></thead><tbody id="attempt-body">{attempt_rows}</tbody></table></div></section>
<section id="acquisition"><div class="eyebrow">04 / Acquisition</div><h2>Full evidence cost by slot</h2><div class="table-wrap"><table><thead><tr><th>Slot</th><th>Task</th><th>Arm</th><th>Attempts</th><th>Model calls</th><th>Controller requests</th><th>Provider attempts / responses</th><th>Provider retries</th><th>Provider failures</th><th>Tokens</th><th>Reported cost</th><th>Measured time</th></tr></thead><tbody>{acquisition_rows}</tbody></table></div></section>
<section id="methodology"><div class="eyebrow">05 / Methodology</div><h2>Rules that shape the report</h2><div class="method">{methodology}</div></section>
<section id="provenance"><div class="eyebrow">06 / Provenance</div><h2>Pinned evidence sources</h2><div class="table-wrap"><table class="provenance"><tbody>{provenance}</tbody></table></div></section></main>
<script>(()=>{{const f=[document.querySelector('#model-filter'),document.querySelector('#arm-filter'),document.querySelector('#outcome-filter')];const rows=[...document.querySelectorAll('#attempt-body tr')];const apply=()=>rows.forEach(r=>{{r.hidden=!!((f[0].value&&r.dataset.model!==f[0].value)||(f[1].value&&r.dataset.arm!==f[1].value)||(f[2].value&&r.dataset.outcome!==f[2].value))}});f.forEach(x=>x.addEventListener('change',apply))}})();</script></body></html>
"""
    return document.encode("utf-8")


def _check_output_path(destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise CampaignReportError("report output must not already exist")
    parent = destination.parent
    while not parent.exists() and not parent.is_symlink() and parent != parent.parent:
        parent = parent.parent
    if parent.is_symlink() or not parent.is_dir():
        raise CampaignReportError("report output parent must be a real directory")


def publish_campaign_report(
    output: Path | str,
    dataset: CampaignReportDataset,
) -> tuple[str, str]:
    """Publish canonical data and self-contained HTML into one fresh directory."""
    destination = Path(output)
    _check_output_path(destination)
    created = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _check_output_path(destination)
        destination.mkdir(mode=0o700)
        created = True
        dataset_path = destination / "dataset.json"
        site_path = destination / "index.html"
        site_bytes = render_campaign_report(dataset)
        for path, payload in (
            (dataset_path, dataset.canonical_bytes),
            (site_path, site_bytes),
        ):
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(0o644)
        destination.chmod(0o755)
        directory_descriptor = os.open(
            destination,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        parent_descriptor = os.open(
            destination.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except CampaignReportError:
        raise
    except OSError as exc:
        if created:
            for path in (destination / "dataset.json", destination / "index.html"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                destination.rmdir()
            except OSError:
                pass
        raise CampaignReportError("report output could not be published") from exc
    return dataset.sha256, hashlib.sha256(site_bytes).hexdigest()
