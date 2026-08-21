"""Build-site entry tests and CLI guard."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ckbbench.matrix import build_site as build_site_mod
from ckbbench.matrix.build_site import (
    LEGACY_RESULT_ADAPTER,
    REPORT_MANIFEST_SCHEMA,
    RETRY_RESULT_ADAPTER,
    ReportManifestError,
    adapt_legacy_result,
    build_site,
    build_site_from_manifest,
    build_site_from_results_dir,
    load_report_manifest,
    results_through_utc,
)
from ckbbench.matrix.store import ResultsValidationError
from ckbbench.matrix.test_fixtures import (
    SYNTHETIC_MODEL,
    synthetic_run_dict,
    write_synthetic_results,
)
from ckbbench.run.result import RunResult, write_result
from ckbbench.run.model_profile import report_profile


def _legacy_row(**overrides):
    row = synthetic_run_dict(
        model="gpt-5.6-sol",
        model_profile_id="phase1-gpt-v2",
        model_profile_sha256="2" * 64,
        model_response_id="gpt-5.6-sol",
        **overrides,
    )
    row["schema_version"] = "1.4.0"
    row["metrics"] = {
        key: row["metrics"][key]
        for key in (
            "total_wall_seconds", "model_calls", "provider_attempts", "provider_responses",
            "prompt_tokens", "completion_tokens", "total_tokens", "token_usage_status",
            "provider_failure_category",
        )
    }
    return row


def _retry_row(**overrides):
    row = synthetic_run_dict(
        model="gpt-5.6-sol",
        model_profile_id="phase1-gpt-v6",
        model_profile_sha256=(
            "266c77ef67d6954a0daf4d9dfdff87d8d788995930f54769c279dffc58e2a275"
        ),
        model_response_id="gpt-5.6-sol",
        **overrides,
    )
    row["schema_version"] = "1.6.0"
    for field in (
        "history_compaction_count", "history_dropped_groups", "history_dropped_items",
        "history_max_prepared_bytes",
    ):
        del row["metrics"][field]
    return row


def test_build_site_from_results_dir(tmp_path: Path):
    rows = [
        synthetic_run_dict(arm="B"),
        synthetic_run_dict(arm="C", run_id="c-run"),
    ]
    dest = write_synthetic_results(tmp_path, rows)
    path = build_site_from_results_dir(dest, tmp_path / "site", synthetic=True)
    html = path.read_text(encoding="utf-8")
    assert "SYNTHETIC" in html


def test_build_site_alias(tmp_path: Path):
    dest = tmp_path / "results"
    dest.mkdir()
    write_result(RunResult.from_dict(synthetic_run_dict()), dest)
    path = build_site(dest, tmp_path / "site2")
    assert path.name == "index.html"


def test_results_through_utc_uses_newest_canonical_run_timestamp():
    rows = [
        {"run_id": "2.0.0-devnet-B-model-s1-1735689600"},
        {"run_id": "2.0.0-devnet-C-model-s1-1735776000"},
        {"run_id": "legacy-without-a-timestamp"},
    ]
    assert results_through_utc(rows) == "2025-01-02T00:00:00Z"


def test_results_through_utc_is_explicit_when_rows_have_no_canonical_timestamp():
    assert results_through_utc([{"run_id": "synthetic-b"}]) == "timestamp unavailable"


def test_default_build_displays_deterministic_results_vintage(tmp_path: Path):
    row = synthetic_run_dict(run_id="1.0.0-synthetic-devnet-B-Opus-s1-1735689600")
    dest = write_synthetic_results(tmp_path, [row])
    path = build_site_from_results_dir(dest, tmp_path / "site", synthetic=True)
    html = path.read_text(encoding="utf-8")
    assert "Results through" in html
    assert "2025-01-01T00:00:00Z" in html
    assert "Generated_at: deterministic" not in html


def test_build_site_rejects_poisoned_results(tmp_path: Path):
    # End-to-end: an invalid result JSON in the results dir must FAIL the build (the validator is
    # the storage mitigation, ADR-0012), not silently render bad numbers. grok-build poison test.
    dest = tmp_path / "results"
    dest.mkdir()
    write_result(RunResult.from_dict(synthetic_run_dict(arm="B")), dest)
    # a second file with an invalid outcome
    (dest / "poison.json").write_text(
        json.dumps({**synthetic_run_dict(arm="C", run_id="poison"), "outcome": "totally-bogus"})
    )
    with pytest.raises(ResultsValidationError):
        build_site_from_results_dir(dest, tmp_path / "site")


def test_build_site_is_reproducible_byte_identical(tmp_path: Path):
    # The repro gate (ADR-0012): the SAME committed results produce byte-identical site output.
    rows = [
        synthetic_run_dict(model=SYNTHETIC_MODEL, arm="B", outcome="pass", run_id="o-b"),
        synthetic_run_dict(model=SYNTHETIC_MODEL, arm="C", outcome="pass", run_id="o-c"),
    ]
    dest = write_synthetic_results(tmp_path, rows)
    p1 = build_site_from_results_dir(dest, tmp_path / "site1", synthetic=True, generated_at="fixed")
    p2 = build_site_from_results_dir(dest, tmp_path / "site2", synthetic=True, generated_at="fixed")
    assert p1.read_bytes() == p2.read_bytes()


def test_main_cli_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        build_site_mod.main()
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err


def test_main_cli_builds(tmp_path: Path, monkeypatch):
    dest = tmp_path / "results"
    dest.mkdir()
    write_result(RunResult.from_dict(synthetic_run_dict()), dest)
    site = tmp_path / "site"
    monkeypatch.setattr(sys, "argv", ["build_site", str(dest), str(site)])
    build_site_mod.main()
    assert (site / "index.html").is_file()


def test_legacy_adapter_is_exact_and_never_mutates_source():
    source = _legacy_row()
    before = json.dumps(source, sort_keys=True)
    adapted = adapt_legacy_result(source, LEGACY_RESULT_ADAPTER)
    assert json.dumps(source, sort_keys=True) == before
    assert adapted["schema_version"] == "1.7.0"
    assert adapted["metrics"] == {
        **source["metrics"],
        "provider_retry_count": 0,
        "provider_retry_delay_seconds": 0,
        "provider_failure_counts": {},
        "history_compaction_count": 0,
        "history_dropped_groups": 0,
        "history_dropped_items": 0,
        "history_max_prepared_bytes": 0,
    }


def test_retry_schema_adapter_only_adds_zero_history_evidence_without_mutation():
    source = _retry_row()
    before = json.dumps(source, sort_keys=True)
    adapted = adapt_legacy_result(source, RETRY_RESULT_ADAPTER)
    assert json.dumps(source, sort_keys=True) == before
    assert adapted["schema_version"] == "1.7.0"
    assert adapted["metrics"] == {
        **source["metrics"],
        "history_compaction_count": 0,
        "history_dropped_groups": 0,
        "history_dropped_items": 0,
        "history_max_prepared_bytes": 0,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(schema_version="1.5.0"),
        lambda row: row["metrics"].update(extra=0),
        lambda row: row["metrics"].update(provider_attempts=0, provider_responses=1),
        lambda row: row["metrics"].update(provider_failure_category="timeout"),
    ],
)
def test_legacy_adapter_refuses_schema_or_metric_drift(mutation):
    row = _legacy_row()
    mutation(row)
    with pytest.raises(ReportManifestError):
        adapt_legacy_result(row, LEGACY_RESULT_ADAPTER)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(schema_version="1.5.0"),
        lambda row: row["metrics"].update(extra=0),
        lambda row: row["metrics"].pop("provider_retry_count"),
    ],
)
def test_retry_schema_adapter_refuses_schema_or_metric_drift(mutation):
    row = _retry_row()
    mutation(row)
    with pytest.raises(ReportManifestError):
        adapt_legacy_result(row, RETRY_RESULT_ADAPTER)


def test_report_manifest_combines_explicit_model_cohorts(
    tmp_path: Path, monkeypatch, reviewed_profile
):
    reviewed = reviewed_profile()
    current_profile = report_profile(reviewed)
    legacy_profile = replace(
        current_profile,
        profile_id="phase1-gpt-v2",
        sha256="2" * 64,
        requested_model="gpt-5.6-sol",
        probed_response_model="gpt-5.6-sol",
        max_agent_query_attempts=1,
        provider_retry_backoff_seconds=(),
        replay_max_bytes=0,
    )
    monkeypatch.setattr(build_site_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        build_site_mod,
        "load_report_profile",
        lambda path: legacy_profile if Path(path).name == "legacy.json" else current_profile,
    )

    current_dir = tmp_path / "current"
    legacy_dir = tmp_path / "legacy"
    current_dir.mkdir()
    legacy_dir.mkdir()
    current = synthetic_run_dict(run_id="current", model=reviewed.requested_model)
    (current_dir / "current.json").write_text(json.dumps(current), encoding="utf-8")
    legacy = _legacy_row(run_id="legacy")
    (legacy_dir / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
    (tmp_path / "current.json").write_text("{}", encoding="utf-8")
    (tmp_path / "legacy.json").write_text("{}", encoding="utf-8")
    manifest = tmp_path / "report.json"
    manifest.write_text(
        json.dumps({
            "schema_version": REPORT_MANIFEST_SCHEMA,
            "cohorts": [
                {
                    "results_dir": "legacy",
                    "model_profile": "legacy.json",
                    "schema_adapter": LEGACY_RESULT_ADAPTER,
                },
                {
                    "results_dir": "current",
                    "model_profile": "current.json",
                    "schema_adapter": None,
                },
            ],
        }),
        encoding="utf-8",
    )

    rows, profiles, sources = load_report_manifest(manifest)
    assert [row["model"] for row in rows] == ["gpt-5.6-sol", reviewed.requested_model]
    assert {profile.profile_id for profile in profiles} == {"phase1-gpt-v2", "phase1-gpt-v10"}
    assert [source["rows"] for source in sources] == [1, 1]
    assert [source["model_stability"] for source in sources] == [
        legacy_profile.model_stability,
        current_profile.model_stability,
    ]
    path = build_site_from_manifest(manifest, tmp_path / "site", synthetic=True)
    html = path.read_text(encoding="utf-8")
    assert "gpt-5.6-sol" in html
    assert reviewed.requested_model in html


def test_report_manifest_refuses_escape_and_duplicate_result_directories(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(build_site_mod, "REPO_ROOT", tmp_path)
    manifest = tmp_path / "report.json"
    manifest.write_text(
        json.dumps({
            "schema_version": REPORT_MANIFEST_SCHEMA,
            "cohorts": [{
                "results_dir": "../outside",
                "model_profile": "profile.json",
                "schema_adapter": None,
            }],
        }),
        encoding="utf-8",
    )
    with pytest.raises(ReportManifestError, match="inside the repository"):
        load_report_manifest(manifest)

    (tmp_path / "results").mkdir()
    (tmp_path / "profile.json").write_text("{}", encoding="utf-8")
    manifest.write_text(
        json.dumps({
            "schema_version": REPORT_MANIFEST_SCHEMA,
            "cohorts": [
                {"results_dir": "results", "model_profile": "profile.json", "schema_adapter": None},
                {"results_dir": "results", "model_profile": "profile.json", "schema_adapter": None},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        build_site_mod,
        "load_report_profile",
        lambda path: build_site_mod.ReportModelProfile(
            "profile", "3" * 64, "model", "model", "moving_alias", 1, (), 0
        ),
    )
    monkeypatch.setattr(build_site_mod, "load_results", lambda path: [{}])
    with pytest.raises(ReportManifestError, match="only once"):
        load_report_manifest(manifest)


def test_main_cli_builds_from_manifest(tmp_path: Path, monkeypatch):
    site = tmp_path / "site"
    manifest = tmp_path / "report.json"
    manifest.write_text("{}", encoding="utf-8")
    expected = site / "index.html"
    monkeypatch.setattr(
        build_site_mod, "build_site_from_manifest", lambda manifest_path, site_dir: expected
    )
    monkeypatch.setattr(
        sys, "argv", ["build_site", "--manifest", str(manifest), str(site)]
    )
    build_site_mod.main()
