"""Build-site entry tests and CLI guard."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ckbbench.matrix import build_site as build_site_mod
from ckbbench.matrix.build_site import (
    build_site,
    build_site_from_results_dir,
    results_through_utc,
)
from ckbbench.matrix.store import ResultsValidationError
from ckbbench.matrix.test_fixtures import synthetic_run_dict, write_synthetic_results
from ckbbench.run.result import RunResult, write_result


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
        synthetic_run_dict(model="Opus", arm="B", outcome="pass", run_id="o-b"),
        synthetic_run_dict(model="Opus", arm="C", outcome="pass", run_id="o-c"),
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
