"""Deterministic reporting build step (ADR-0012).

Load flat JSON results, validate invariants, aggregate Pass@1, render static HTML to ``site/``.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ckbbench.matrix.metrics import build_dataset
from ckbbench.matrix.render import write_site
from ckbbench.matrix.store import load_results, validate_results
from ckbbench.run.model_profile import (
    REPO_ROOT,
    ModelProfileError,
    ReportModelProfile,
    load_report_profile,
)
from ckbbench.run.result import RESULT_SCHEMA_VERSION


_CANONICAL_RUN_TIMESTAMP = re.compile(r"-(\d{10})$")
REPORT_MANIFEST_SCHEMA = "ckbbench-report-manifest-v1"
LEGACY_RESULT_ADAPTER = "result-1.4.0-to-1.7.0-v1"
RETRY_RESULT_ADAPTER = "result-1.6.0-to-1.7.0-v1"
_LEGACY_METRIC_FIELDS = frozenset({
    "total_wall_seconds", "model_calls", "provider_attempts", "provider_responses",
    "prompt_tokens", "completion_tokens", "total_tokens", "token_usage_status",
    "provider_failure_category",
})
_RETRY_METRIC_FIELDS = frozenset({
    *_LEGACY_METRIC_FIELDS,
    "provider_retry_count", "provider_retry_delay_seconds", "provider_failure_counts",
})


class ReportManifestError(ValueError):
    """A report manifest or its explicit legacy adapter is invalid."""


def _repo_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ReportManifestError(f"{field} must be a non-empty repository-relative path")
    candidate = (REPO_ROOT / value).resolve()
    try:
        candidate.relative_to(REPO_ROOT.resolve())
    except ValueError:
        raise ReportManifestError(f"{field} must remain inside the repository") from None
    return candidate


def adapt_legacy_result(row: dict[str, Any], adapter: str | None) -> dict[str, Any]:
    """Adapt one explicitly declared legacy result in memory without changing its source file."""
    if adapter is None:
        return copy.deepcopy(row)
    expected_schema = {
        LEGACY_RESULT_ADAPTER: "1.4.0",
        RETRY_RESULT_ADAPTER: "1.6.0",
    }.get(adapter)
    if expected_schema is None or row.get("schema_version") != expected_schema:
        raise ReportManifestError("the declared result schema adapter does not match its row")
    metrics = row.get("metrics")
    expected_metrics = (
        _LEGACY_METRIC_FIELDS if adapter == LEGACY_RESULT_ADAPTER else _RETRY_METRIC_FIELDS
    )
    if not isinstance(metrics, dict) or set(metrics) != expected_metrics:
        raise ReportManifestError(
            f"a legacy result does not match the exact {expected_schema} metric shape"
        )
    attempts = metrics.get("provider_attempts")
    responses = metrics.get("provider_responses")
    category = metrics.get("provider_failure_category")
    if (
        isinstance(attempts, bool) or not isinstance(attempts, int)
        or isinstance(responses, bool) or not isinstance(responses, int)
        or attempts < 0 or responses < 0 or responses > attempts
    ):
        raise ReportManifestError("a legacy result carries invalid provider counts")
    unanswered = attempts - responses
    if (unanswered == 0) != (category is None):
        raise ReportManifestError("a legacy result's failure category does not explain its counts")
    if unanswered and (not isinstance(category, str) or not category):
        raise ReportManifestError("a legacy result carries an invalid failure category")
    adapted = copy.deepcopy(row)
    adapted["schema_version"] = RESULT_SCHEMA_VERSION
    if adapter == LEGACY_RESULT_ADAPTER:
        adapted["metrics"].update({
            "provider_retry_count": 0,
            "provider_retry_delay_seconds": 0,
            "provider_failure_counts": {} if unanswered == 0 else {category: unanswered},
        })
    adapted["metrics"].update({
        "history_compaction_count": 0,
        "history_dropped_groups": 0,
        "history_dropped_items": 0,
        "history_max_prepared_bytes": 0,
    })
    return adapted


def load_report_manifest(
    manifest_path: Path | str,
) -> tuple[list[dict[str, Any]], tuple[ReportModelProfile, ...], list[dict[str, Any]]]:
    """Load exact model profiles and result cohorts named by one report manifest."""
    path = Path(manifest_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ReportManifestError("the report manifest is not readable UTF-8 JSON") from None
    if not isinstance(document, dict) or set(document) != {"schema_version", "cohorts"}:
        raise ReportManifestError("the report manifest must contain only schema_version and cohorts")
    cohorts = document.get("cohorts")
    if document.get("schema_version") != REPORT_MANIFEST_SCHEMA or not isinstance(cohorts, list) \
            or not cohorts:
        raise ReportManifestError("the report manifest schema or cohort list is invalid")

    results: list[dict[str, Any]] = []
    profiles: dict[tuple[str, str], ReportModelProfile] = {}
    sources: list[dict[str, Any]] = []
    seen_dirs: set[Path] = set()
    for index, cohort in enumerate(cohorts):
        if not isinstance(cohort, dict) or set(cohort) != {
            "results_dir", "model_profile", "schema_adapter"
        }:
            raise ReportManifestError("each report cohort must use the exact reviewed keys")
        results_dir = _repo_path(cohort["results_dir"], field="results_dir")
        profile_path = _repo_path(cohort["model_profile"], field="model_profile")
        if results_dir in seen_dirs:
            raise ReportManifestError("a results directory may appear only once in a report")
        seen_dirs.add(results_dir)
        adapter = cohort["schema_adapter"]
        if adapter not in (None, LEGACY_RESULT_ADAPTER, RETRY_RESULT_ADAPTER):
            raise ReportManifestError("the report manifest names an unsupported schema adapter")
        try:
            profile = load_report_profile(profile_path)
        except ModelProfileError as exc:
            raise ReportManifestError(str(exc)) from None
        rows = [adapt_legacy_result(row, adapter) for row in load_results(results_dir)]
        if not rows:
            raise ReportManifestError("a report cohort contains no result rows")
        profiles[(profile.profile_id, profile.sha256)] = profile
        results.extend(rows)
        sources.append({
            "cohort": index + 1,
            "model": profile.requested_model,
            "profile_id": profile.profile_id,
            "profile_sha256": profile.sha256,
            "model_stability": profile.model_stability,
            "schema_adapter": adapter,
            "rows": len(rows),
        })
    return results, tuple(profiles[key] for key in sorted(profiles)), sources


def results_through_utc(results: list[dict[str, object]]) -> str:
    """Return a deterministic UTC data timestamp from canonical production run IDs.

    Production rows end their run ID with the Unix time captured when the cell starts. Using the
    newest such value gives the report a meaningful data vintage without making identical rebuilds
    differ by wall clock. Synthetic or legacy rows without that suffix remain explicit.
    """
    timestamps: list[int] = []
    for row in results:
        match = _CANONICAL_RUN_TIMESTAMP.search(str(row.get("run_id", "")))
        if match is None:
            continue
        value = int(match.group(1))
        try:
            datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        timestamps.append(value)
    if not timestamps:
        return "timestamp unavailable"
    return (
        datetime.fromtimestamp(max(timestamps), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_site_from_results_dir(
    results_dir: Path | str,
    site_dir: Path | str,
    *,
    synthetic: bool = False,
    generated_at: str | None = None,
) -> Path:
    """Load, validate, aggregate, and render the ladder chart site."""
    results = load_results(results_dir)
    validate_results(results)
    dataset = build_dataset(
        results,
        synthetic=synthetic,
        generated_at=(
            results_through_utc(results) if generated_at is None else generated_at
        ),
    )
    return write_site(site_dir, dataset)


def build_site_from_manifest(
    manifest_path: Path | str,
    site_dir: Path | str,
    *,
    synthetic: bool = False,
    generated_at: str | None = None,
) -> Path:
    """Build one report from multiple explicitly pinned model cohorts."""
    results, profiles, sources = load_report_manifest(manifest_path)
    validate_results(results, profiles=profiles)
    dataset = build_dataset(
        results,
        synthetic=synthetic,
        generated_at=(results_through_utc(results) if generated_at is None else generated_at),
        report_sources=sources,
    )
    return write_site(site_dir, dataset)


def build_site(
    results_dir: Path | str,
    site_dir: Path | str,
    *,
    synthetic: bool = False,
    generated_at: str | None = None,
) -> Path:
    """Public alias for the reporting build entry point."""
    return build_site_from_results_dir(
        results_dir,
        site_dir,
        synthetic=synthetic,
        generated_at=generated_at,
    )


def main() -> None:
    """CLI entry for one results directory or one multi-cohort manifest."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m ckbbench.matrix.build_site")
    parser.add_argument("--manifest")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    if args.manifest:
        if len(args.paths) != 1:
            parser.error("--manifest needs exactly one site_dir")
        path = build_site_from_manifest(args.manifest, args.paths[0])
    else:
        if len(args.paths) != 2:
            parser.error("expected results_dir and site_dir")
        path = build_site(args.paths[0], args.paths[1])
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
