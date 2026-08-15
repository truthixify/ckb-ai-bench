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


# --- B/C comparison-budget guard (RD2) -----------------------------------------------------------

_BUDGET_80 = {"step_limit": 80, "cost_limit": 0.0, "wall_time_limit_seconds": 900}
_ALL_NULL = {"step_limit": None, "cost_limit": None, "wall_time_limit_seconds": None}


def _budget_row(arm: str, seed: int, limits: dict, **overrides):
    row = synthetic_run_dict(arm=arm, seed=seed, run_id=f"{arm}-s{seed}", **overrides)
    row["agent_limits"] = dict(limits)
    return row


def test_matched_bc_budgets_pass():
    validate_results([
        _budget_row("B", 1, _BUDGET_80),
        _budget_row("B", 2, _BUDGET_80),
        _budget_row("C", 1, _BUDGET_80),
        _budget_row("C", 2, _BUDGET_80),
    ])


@pytest.mark.parametrize(
    "field,value",
    [("step_limit", 40), ("cost_limit", 1.5), ("wall_time_limit_seconds", 600)],
)
def test_any_bc_limit_mismatch_fails(field, value):
    """A C - B difference measured under different ceilings is causally ambiguous."""
    c = dict(_BUDGET_80)
    c[field] = value
    with pytest.raises(ResultsValidationError, match="mixed B/C agent budgets"):
        validate_results([_budget_row("B", 1, _BUDGET_80), _budget_row("C", 1, c)])


def test_within_arm_budget_drift_across_trials_fails():
    """B seed 1 at 80 and B seed 2 at 60 is already mixed methodology, before any C row loads."""
    drifted = {**_BUDGET_80, "step_limit": 60}
    with pytest.raises(ResultsValidationError, match="mixed B/C agent budgets"):
        validate_results([_budget_row("B", 1, _BUDGET_80), _budget_row("B", 2, drifted)])


def test_budget_verdict_and_message_are_row_order_independent():
    import itertools

    rows = [
        _budget_row("B", 1, _BUDGET_80),
        _budget_row("C", 1, {**_BUDGET_80, "step_limit": 40}),
        _budget_row("C", 2, {**_BUDGET_80, "step_limit": 40}),
    ]
    messages = set()
    for order in itertools.permutations(rows):
        with pytest.raises(ResultsValidationError) as exc:
            validate_results(list(order))
        messages.add(str(exc.value))
    assert len(messages) == 1, messages


@pytest.mark.parametrize("field,value", [
    ("model", "other-model"), ("chain", "testnet"), ("suite_semver", "9.9.9-synthetic"),
])
def test_different_methodology_identities_are_not_compared(field, value):
    """Only rows that would actually be pooled into one C - B claim are compared."""
    other = _budget_row("C", 1, {**_BUDGET_80, "step_limit": 40}, **{field: value})
    validate_results([_budget_row("B", 1, _BUDGET_80), other])


def test_different_freeze_or_mcp_identity_is_not_compared():
    other = _budget_row(
        "C", 1, {**_BUDGET_80, "step_limit": 40},
        suite_semver="2.0.0-synthetic", suite_freeze_hash="other-freeze", mcp_server_version="9.9.9",
    )
    validate_results([_budget_row("B", 1, _BUDGET_80), other])


@pytest.mark.parametrize("arm", ["B", "C"])
def test_one_sided_result_sets_remain_valid(arm):
    """B-only smoke data and C-only diagnostics must stay loadable."""
    validate_results([_budget_row(arm, 1, _BUDGET_80), _budget_row(arm, 2, _BUDGET_80)])


def test_a_and_d_budget_differences_do_not_trigger_the_bc_guard():
    """A and D use the same production defaults but are not the RD2 headline pair."""
    validate_results([
        _budget_row("A", 1, {**_BUDGET_80, "step_limit": 40}),
        _budget_row("B", 1, _BUDGET_80),
        _budget_row("C", 1, _BUDGET_80),
        _budget_row("D", 1, {**_BUDGET_80, "wall_time_limit_seconds": 60}),
    ])


def test_all_null_early_infra_row_is_ignored_by_the_budget_comparison():
    validate_results([
        _budget_row("B", 1, _ALL_NULL, outcome="infra_fail"),
        _budget_row("C", 1, _ALL_NULL, outcome="infra_fail"),
    ])


def test_all_null_infra_on_one_side_leaves_the_other_side_valid():
    """One side failing before agent construction must not invalidate the concrete side."""
    validate_results([
        _budget_row("B", 1, _ALL_NULL, outcome="infra_fail"),
        _budget_row("C", 1, _BUDGET_80),
        _budget_row("C", 2, _BUDGET_80),
    ])


@pytest.mark.parametrize("present", ["step_limit", "cost_limit", "wall_time_limit_seconds"])
def test_partially_null_infra_limits_are_rejected(present):
    """Half-recorded provenance is not evidence of anything."""
    limits = dict(_ALL_NULL)
    limits[present] = _BUDGET_80[present]
    with pytest.raises(ResultsValidationError, match="agent_limits"):
        validate_results([_budget_row("B", 1, limits, outcome="infra_fail")])


@pytest.mark.parametrize("outcome", ["pass", "agent_fail", "protocol_violation"])
def test_non_infra_outcomes_still_require_concrete_limits(outcome):
    with pytest.raises(ResultsValidationError, match="must be present for outcome"):
        validate_results([_budget_row("B", 1, _ALL_NULL, outcome=outcome)])


def test_mixed_budget_directory_cannot_reach_static_site_generation(tmp_path: Path):
    """Pipeline-level: the store validator is the single fail-loud boundary before rendering."""
    from ckbbench.matrix.build_site import build_site_from_results_dir

    results_dir = write_synthetic_results(tmp_path, [
        _budget_row("B", 1, _BUDGET_80),
        _budget_row("C", 1, {**_BUDGET_80, "step_limit": 40}),
    ])
    site_dir = tmp_path / "site"
    with pytest.raises(ResultsValidationError, match="mixed B/C agent budgets"):
        build_site_from_results_dir(results_dir, site_dir)
    assert not site_dir.exists()


def test_explicit_empty_limits_are_not_replaced_by_the_default_fixture_budget():
    """An explicitly empty mapping is a malformed fixture, not an omitted argument.

    Selecting the default by truthiness silently handed back 80/0.0/900, so a future
    malformed-provenance test would have passed against valid defaults instead of exercising the
    validator.
    """
    default_row = synthetic_run_dict()
    assert default_row["agent_limits"] == {
        "step_limit": 80, "cost_limit": 0.0, "wall_time_limit_seconds": 900,
    }

    empty_row = synthetic_run_dict(agent_limits={}, outcome="pass")
    assert empty_row["agent_limits"] != default_row["agent_limits"]
    assert 80 not in empty_row["agent_limits"].values()
    with pytest.raises(ResultsValidationError, match="must be present for outcome 'pass'"):
        validate_results([empty_row])


def test_synthetic_rows_do_not_share_one_mutable_limits_object():
    """Each row owns its limits; mutating one fixture must not rewrite the next one's provenance."""
    first, second = synthetic_run_dict(seed=1), synthetic_run_dict(seed=2)
    first["agent_limits"]["step_limit"] = 999
    assert second["agent_limits"]["step_limit"] == 80


# --- MCP surface provenance and schema currency (ADR-0013) ---------------------------------------

_ALL_ARMS = ("A", "B", "C", "D")


def test_every_arm_validates_under_its_fixed_profile():
    validate_results([
        synthetic_run_dict(arm=arm, run_id=f"ok-{arm}") for arm in _ALL_ARMS
    ])


@pytest.mark.parametrize("arm,wrong", [
    ("A", "docs-only-v1"), ("B", "docs-only-v1"), ("C", "off"), ("D", "off"),
])
def test_a_row_cannot_claim_the_other_arms_profile(arm, wrong):
    """B claiming the documentation surface, or C claiming none, would misdescribe the treatment."""
    row = synthetic_run_dict(arm=arm, run_id=f"bad-{arm}", mcp_surface_profile=wrong)
    with pytest.raises(ResultsValidationError, match="must run under mcp_surface_profile"):
        validate_results([row])


@pytest.mark.parametrize("profile", ["", "   ", "docs-only", "DOCS-ONLY-V1", "full", "off "])
def test_blank_or_unknown_profiles_fail(profile):
    row = synthetic_run_dict(arm="C", mcp_surface_profile=profile)
    with pytest.raises(ResultsValidationError, match="mcp_surface_profile"):
        validate_results([row])


@pytest.mark.parametrize("profile", [None, 7, True, [], {}])
def test_a_non_string_profile_fails(profile):
    """Set on the serialized row: the fixture reads `None` as "omitted", as agent_limits does."""
    row = synthetic_run_dict(arm="C")
    row["mcp_surface_profile"] = profile
    with pytest.raises(ResultsValidationError, match="mcp_surface_profile"):
        validate_results([row])


def test_a_missing_profile_is_never_inferred_from_the_arm():
    row = synthetic_run_dict(arm="C")
    del row["mcp_surface_profile"]
    with pytest.raises(ResultsValidationError, match="missing required field 'mcp_surface_profile'"):
        validate_results([row])


def test_an_unknown_arm_fails_rather_than_defaulting():
    row = synthetic_run_dict(arm="Z", mcp_surface_profile="off")
    with pytest.raises(ResultsValidationError, match="unknown arm"):
        validate_results([row])


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "2.0.0", "", "   "])
def test_a_legacy_or_unknown_schema_row_cannot_build_a_current_report(version):
    """Legacy rows predate the profile, so their treatment is unknown and must not be inferred."""
    row = synthetic_run_dict(arm="B")
    row["schema_version"] = version
    with pytest.raises(ResultsValidationError, match="schema_version"):
        validate_results([row])


def test_a_missing_schema_version_fails():
    row = synthetic_run_dict(arm="B")
    del row["schema_version"]
    with pytest.raises(ResultsValidationError, match="missing required field 'schema_version'"):
        validate_results([row])


def test_profile_conflicts_are_order_independent():
    import itertools

    rows = [
        synthetic_run_dict(arm="C", seed=1, run_id="c1"),
        synthetic_run_dict(arm="C", seed=2, run_id="c2", mcp_surface_profile="off"),
        synthetic_run_dict(arm="B", seed=1, run_id="b1"),
    ]
    messages = set()
    for order in itertools.permutations(rows):
        with pytest.raises(ResultsValidationError) as exc:
            validate_results(list(order))
        messages.add(str(exc.value).split(":", 1)[1].strip())
    assert len(messages) == 1, messages


def test_valid_bc_rows_still_validate_aggregate_and_render(tmp_path: Path):
    from ckbbench.matrix.build_site import build_site_from_results_dir

    results_dir = write_synthetic_results(tmp_path, [
        synthetic_run_dict(arm="B", seed=1, run_id="b1", outcome="pass"),
        synthetic_run_dict(arm="B", seed=2, run_id="b2", outcome="agent_fail"),
        synthetic_run_dict(arm="C", seed=1, run_id="c1", outcome="pass"),
        synthetic_run_dict(arm="C", seed=2, run_id="c2", outcome="pass"),
    ])
    index = build_site_from_results_dir(results_dir, tmp_path / "site")
    assert index.is_file()


def test_the_budget_guard_still_fails_independently_of_the_profile_guard():
    """Two orthogonal invariants: a correct profile must not excuse a mixed budget."""
    b = synthetic_run_dict(arm="B", run_id="b1")
    c = synthetic_run_dict(arm="C", run_id="c1")
    c["agent_limits"] = {"step_limit": 40, "cost_limit": 0.0, "wall_time_limit_seconds": 900}
    assert b["mcp_surface_profile"] == "off"
    assert c["mcp_surface_profile"] == "docs-only-v1"
    with pytest.raises(ResultsValidationError, match="mixed B/C agent budgets"):
        validate_results([b, c])


def test_no_endpoint_credential_prompt_or_transcript_was_added_to_the_row():
    row = synthetic_run_dict(arm="C")
    serialized = json.dumps(row)
    for leak in ("http://", "https://", "api_key", "Authorization", "system_template",
                 "mcp_call", "resources/read"):
        assert leak not in serialized
    assert row["mcp_surface_profile"] == "docs-only-v1"
