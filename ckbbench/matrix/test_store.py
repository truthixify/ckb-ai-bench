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


def test_validate_missing_agent_limits_raises():
    row = synthetic_run_dict()
    del row["agent_limits"]
    with pytest.raises(ResultsValidationError, match="missing required field 'agent_limits'"):
        validate_results([row])


def test_validate_bad_agent_limits_raises():
    row = synthetic_run_dict()
    row["agent_limits"]["step_limit"] = "80"
    with pytest.raises(ResultsValidationError, match="agent_limits.step_limit"):
        validate_results([row])


def test_validate_agent_limits_must_be_object():
    row = synthetic_run_dict()
    row["agent_limits"] = []
    with pytest.raises(ResultsValidationError, match="agent_limits must be an object"):
        validate_results([row])


def test_validate_agent_limits_exact_keys():
    row = synthetic_run_dict()
    del row["agent_limits"]["cost_limit"]
    with pytest.raises(ResultsValidationError, match="agent_limits keys"):
        validate_results([row])


def test_validate_agent_limits_reject_non_finite_cost():
    row = synthetic_run_dict()
    row["agent_limits"]["cost_limit"] = float("nan")
    with pytest.raises(ResultsValidationError, match="finite non-negative"):
        validate_results([row])


def test_validate_agent_limits_allow_null_for_early_infra_rows():
    row = synthetic_run_dict(outcome="infra_fail")
    row["agent_limits"] = {
        "step_limit": None,
        "cost_limit": None,
        "wall_time_limit_seconds": None,
    }
    validate_results([row])


def test_validate_agent_limits_reject_null_for_agent_run_rows():
    row = synthetic_run_dict(outcome="agent_fail")
    row["agent_limits"]["step_limit"] = None
    with pytest.raises(ResultsValidationError, match="agent_limits.step_limit"):
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


# --- managed DevNet provenance (plan §9.1) -----------------------------------------------------

_GOOD_DEVNET_STATE = {
    "lifecycle_policy": "per-cell-fresh-v1",
    "chain": "ckb_dev",
    "genesis_hash": "0x" + "ab" * 32,
    "config_sha256": "d" * 64,
    "prepared_tip_number": 9,
    "prepared_tip_hash": "0x" + "cd" * 32,
}


def _devnet_row(**overrides):
    state = {**_GOOD_DEVNET_STATE, **overrides.pop("devnet_state", {})}
    row = synthetic_run_dict(**overrides)
    row["devnet_state"] = state
    return row


def test_validate_accepts_managed_devnet_rows_with_different_prepared_tips():
    """The miner runs continuously, so equal tips would be a fabricated claim: only the immutable
    identity has to match."""
    rows = [
        _devnet_row(arm="B", seed=1, run_id="b1", devnet_state={"prepared_tip_number": 9}),
        _devnet_row(arm="C", seed=1, run_id="c1",
                    devnet_state={"prepared_tip_number": 41,
                                  "prepared_tip_hash": "0x" + "ef" * 32}),
    ]
    validate_results(rows)


def test_validate_accepts_rows_without_devnet_state():
    """TestNet cells, local runs and schema-1.0.0 artifacts carry no provenance."""
    validate_results([synthetic_run_dict(arm="B", seed=1, run_id="old")])


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("lifecycle_policy", "some-other-policy", "lifecycle_policy"),
        ("chain", "ckb_testnet", "expected 'ckb_dev'"),
        ("genesis_hash", "0xshort", "genesis_hash"),
        ("prepared_tip_hash", "not-a-hash", "prepared_tip_hash"),
        ("config_sha256", "nothex", "config_sha256"),
        ("prepared_tip_number", -1, "non-negative"),
        ("prepared_tip_number", "9", "non-negative"),
    ],
)
def test_validate_rejects_malformed_devnet_state(field, value, match):
    with pytest.raises(ResultsValidationError, match=match):
        validate_results([_devnet_row(devnet_state={field: value})])


def test_validate_rejects_missing_devnet_state_field():
    row = _devnet_row()
    del row["devnet_state"]["genesis_hash"]
    with pytest.raises(ResultsValidationError, match="missing 'genesis_hash'"):
        validate_results([row])


@pytest.mark.parametrize("drifting", ["genesis_hash", "config_sha256"])
def test_validate_rejects_devnet_identity_drift(drifting):
    """Two rows that claim the managed lifecycle but ran against different chain definitions are
    not comparable, whatever their scores say."""
    rows = [
        _devnet_row(arm="B", seed=1, run_id="b1"),
        _devnet_row(arm="C", seed=1, run_id="c1",
                    devnet_state={drifting: ("0x" + "12" * 32) if drifting == "genesis_hash"
                                  else "a" * 64}),
    ]
    with pytest.raises(ResultsValidationError, match="identity drift"):
        validate_results(rows)


def test_validate_scopes_devnet_identity_per_suite():
    """Two suites legitimately have different chain definitions; validating a combined set must
    not read that as drift (the freeze check is suite-scoped for the same reason)."""
    a = _devnet_row(arm="B", seed=1, run_id="a1", suite_semver="1.0.0")
    b = _devnet_row(arm="B", seed=1, run_id="b1", suite_semver="2.0.0",
                    devnet_state={"genesis_hash": "0x" + "12" * 32, "config_sha256": "b" * 64})
    validate_results([a, b])


def test_validate_rejects_devnet_provenance_on_a_non_devnet_row():
    """A row graded on TestNet cannot carry a ckb_dev attestation."""
    row = _devnet_row(arm="B", seed=1, run_id="t1")
    row["chain"] = "testnet"
    with pytest.raises(ResultsValidationError, match="chain is 'testnet'"):
        validate_results([row])
