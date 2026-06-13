"""Store/validator tests: invariant violations must fail loud (ADR-0012)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ckbbench.matrix.store import (
    ResultsValidationError,
    cell_key,
    load_results,
    outcome_is_valid,
    persist_result,
    suite_results_dir,
    validate_results,
)
from ckbbench.matrix.test_fixtures import synthetic_run_dict, write_synthetic_results
from ckbbench.run.result import RunResult


def test_load_results_reads_sorted_json(tmp_path: Path):
    rows = [
        synthetic_run_dict(run_id="z-run", arm="B"),
        synthetic_run_dict(run_id="a-run", arm="C"),
    ]
    dest = write_synthetic_results(tmp_path, rows)
    loaded = load_results(dest)
    assert len(loaded) == 2
    assert loaded[0]["run_id"] == "a-run"


def test_load_results_missing_dir_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_results(tmp_path / "nope")


def test_load_results_non_object_json_raises(tmp_path: Path):
    dest = tmp_path / "results"
    dest.mkdir()
    (dest / "bad.json").write_text("[1,2,3]")
    with pytest.raises(ResultsValidationError, match="expected JSON object"):
        load_results(dest)


def test_validate_clean_synthetic_set_passes():
    rows = [
        synthetic_run_dict(arm="B", seed=1, outcome="pass"),
        synthetic_run_dict(arm="C", seed=1, outcome="agent_fail"),
    ]
    validate_results(rows)


def test_validate_duplicate_cell_key_raises():
    row = synthetic_run_dict()
    with pytest.raises(ResultsValidationError, match="duplicate cell key"):
        validate_results([row, dict(row)])


def test_validate_invalid_outcome_raises():
    row = synthetic_run_dict(outcome="pass")
    row["outcome"] = "mystery"
    with pytest.raises(ResultsValidationError, match="invalid outcome"):
        validate_results([row])


def test_validate_missing_field_raises():
    row = synthetic_run_dict()
    del row["suite_freeze_hash"]
    with pytest.raises(ResultsValidationError, match="missing required field"):
        validate_results([row])


def test_validate_unknown_chain_raises():
    row = synthetic_run_dict(chain="mainnet")
    with pytest.raises(ResultsValidationError, match="unknown chain"):
        validate_results([row])


def test_validate_frozen_suite_drift_raises():
    a = synthetic_run_dict(suite_freeze_hash="hash-a")
    b = synthetic_run_dict(
        arm="C",
        run_id="other",
        suite_freeze_hash="hash-b",
    )
    with pytest.raises(ResultsValidationError, match="frozen-suite drift"):
        validate_results([a, b])


def test_validate_non_dict_row_raises():
    with pytest.raises(ResultsValidationError, match="expected a JSON object"):
        validate_results(["not-a-dict"])  # type: ignore[list-item]


def test_validate_blank_string_field_raises():
    # A null/blank suite_freeze_hash must fail loud, not pass via "field present" (codex/grok-build).
    row = synthetic_run_dict()
    row["suite_freeze_hash"] = "   "
    with pytest.raises(ResultsValidationError, match="must be a non-empty string"):
        validate_results([row])


def test_validate_non_int_seed_raises():
    row = synthetic_run_dict()
    row["seed"] = "1"  # a string, not an int
    with pytest.raises(ResultsValidationError, match="seed must be an int"):
        validate_results([row])


def test_validate_bool_seed_raises():
    # bool is a subclass of int in Python; a True/False seed must still be rejected.
    row = synthetic_run_dict()
    row["seed"] = True
    with pytest.raises(ResultsValidationError, match="seed must be an int"):
        validate_results([row])


def test_validate_empty_list_is_noop():
    validate_results([])


def test_cell_key_tuple():
    row = synthetic_run_dict(seed=7, run_id="rid")
    assert cell_key(row) == ("1.0.0-synthetic", "devnet", "B", "Opus", 7, "rid")


def test_outcome_is_valid():
    assert outcome_is_valid("pass")
    assert not outcome_is_valid("bogus")


def test_persist_result_writes_under_suite_dir(tmp_path: Path):
    row = synthetic_run_dict()
    result = RunResult.from_dict(row)
    path = persist_result(result, tmp_path)
    assert path.parent == suite_results_dir(tmp_path, "1.0.0-synthetic")
    assert path.name.endswith(".json")
    loaded = json.loads(path.read_text())
    assert loaded["run_id"] == result.run_id