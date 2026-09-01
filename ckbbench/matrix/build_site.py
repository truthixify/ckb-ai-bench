"""Deterministic reporting build step (ADR-0012).

Load flat JSON results, validate invariants, aggregate Pass@1, and render static HTML under the
configured benchmark output directory.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ckbbench.matrix.metrics import build_dataset
from ckbbench.matrix.render import write_site
from ckbbench.matrix.store import (
    ResultSuiteContract,
    load_results,
    reviewed_report_profiles,
    validate_results,
)
from ckbbench.run.model_profile import (
    REPO_ROOT,
    ModelProfileError,
    ReportModelProfile,
    load_report_profile,
)
_CANONICAL_RUN_TIMESTAMP = re.compile(r"-(\d{10})$")
REPORT_MANIFEST_SCHEMA = "ckbbench-report-manifest-v1"


class ReportManifestError(ValueError):
    """A report manifest is invalid."""


def _sources_for_results_dir(
    results: list[dict[str, Any]], profiles: tuple[ReportModelProfile, ...]
) -> list[dict[str, Any]]:
    """Bind represented profile digests to their tracked stability metadata."""
    profiles_by_key = {(profile.profile_id, profile.sha256): profile for profile in profiles}
    counts: dict[tuple[str, str], int] = {}
    for row in results:
        key = (str(row.get("model_profile_id")), str(row.get("model_profile_sha256")))
        counts[key] = counts.get(key, 0) + 1

    sources = []
    for key in sorted(counts):
        profile = profiles_by_key[key]
        sources.append({
            "cohort": None,
            "model": profile.requested_model,
            "profile_id": profile.profile_id,
            "profile_sha256": profile.sha256,
            "model_stability": profile.model_stability,
            "thinking_level": profile.thinking_level,
            "model_variant_id": profile.model_variant_id,
            "schema_adapter": None,
            "rows": counts[key],
        })
    return sources


def _repo_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ReportManifestError(f"{field} must be a non-empty repository-relative path")
    candidate = (REPO_ROOT / value).resolve()
    try:
        candidate.relative_to(REPO_ROOT.resolve())
    except ValueError:
        raise ReportManifestError(f"{field} must remain inside the repository") from None
    return candidate


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
        if adapter is not None:
            raise ReportManifestError("schema_adapter must be null for current result rows")
        try:
            profile = load_report_profile(profile_path)
        except ModelProfileError as exc:
            raise ReportManifestError(str(exc)) from None
        rows = load_results(results_dir)
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
            "thinking_level": profile.thinking_level,
            "model_variant_id": profile.model_variant_id,
            "schema_adapter": adapter,
            "rows": len(rows),
        })
    return results, tuple(profiles[key] for key in sorted(profiles)), sources


def results_through_utc(results: list[dict[str, object]]) -> str:
    """Return a deterministic UTC data timestamp from canonical production run IDs.

    Production rows end their run ID with the Unix time captured when the cell starts. Using the
    newest such value gives the report a meaningful data vintage without making identical rebuilds
    differ by wall clock. Synthetic rows without that suffix remain explicit.
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
    suite_contracts: tuple[ResultSuiteContract, ...] | None = None,
) -> Path:
    """Load, validate, aggregate, and render the ladder chart site."""
    results = load_results(results_dir)
    profiles = reviewed_report_profiles() if results else ()
    validate_results(results, profiles=profiles or None, suite_contracts=suite_contracts)
    dataset = build_dataset(
        results,
        synthetic=synthetic,
        generated_at=(
            results_through_utc(results) if generated_at is None else generated_at
        ),
        report_sources=_sources_for_results_dir(results, profiles),
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
    suite_contracts: tuple[ResultSuiteContract, ...] | None = None,
) -> Path:
    """Public alias for the reporting build entry point."""
    return build_site_from_results_dir(
        results_dir,
        site_dir,
        synthetic=synthetic,
        generated_at=generated_at,
        suite_contracts=suite_contracts,
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
