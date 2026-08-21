"""Store/validator tests: invariant violations must fail loud (ADR-0012)."""

from __future__ import annotations

import json
from dataclasses import replace
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
from ckbbench.matrix.store import _reviewed_profile as _real_reviewed_profile
from ckbbench.matrix.store import _validate_provider_failure_category
from ckbbench.matrix.test_fixtures import (
    SYNTHETIC_MODEL,
    SYNTHETIC_RESPONSE_MODEL,
    synthetic_run_dict,
    write_synthetic_results,
)
from ckbbench.run.model_profile import report_profile
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
    assert cell_key(row) == (
        "1.0.0-synthetic", "devnet", "B", SYNTHETIC_MODEL, 7, "rid"
    )


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
    ("chain", "testnet"), ("suite_semver", "9.9.9-synthetic"),
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


# --- model profile and token provenance (ADR-0014) ------------------------------------------------

_COMPLETE = {
    "total_wall_seconds": 1.0, "model_calls": 2, "provider_attempts": 2, "provider_responses": 2,
    "provider_retry_count": 0, "provider_retry_delay_seconds": 0,
    "history_compaction_count": 0, "history_dropped_groups": 0,
    "history_dropped_items": 0, "history_max_prepared_bytes": 1024,
    "prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100,
    "token_usage_status": "complete", "provider_failure_category": None,
    "provider_failure_counts": {},
}
_NOT_STARTED = {
    "total_wall_seconds": 0.0, "model_calls": 0, "provider_attempts": 0, "provider_responses": 0,
    "provider_retry_count": 0, "provider_retry_delay_seconds": 0,
    "history_compaction_count": 0, "history_dropped_groups": 0,
    "history_dropped_items": 0, "history_max_prepared_bytes": 0,
    "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
    "token_usage_status": "not_started", "provider_failure_category": None,
    "provider_failure_counts": {},
}
# One attempt went unanswered, so the result requires a category explaining it.
_INCOMPLETE = {
    "total_wall_seconds": 1.0, "model_calls": 2, "provider_attempts": 2, "provider_responses": 1,
    "provider_retry_count": 0, "provider_retry_delay_seconds": 0,
    "history_compaction_count": 0, "history_dropped_groups": 0,
    "history_dropped_items": 0, "history_max_prepared_bytes": 1024,
    "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
    "token_usage_status": "incomplete", "provider_failure_category": "connection",
    "provider_failure_counts": {"connection": 1},
}


def _row(arm="B", *, metrics=None, outcome="pass", **overrides):
    overrides.setdefault("run_id", f"{arm}-{outcome}-x")
    row = synthetic_run_dict(arm=arm, outcome=outcome, **overrides)
    if metrics is not None:
        row["metrics"] = dict(metrics)
    if outcome == "infra_fail" and metrics is None:
        row["metrics"] = dict(_NOT_STARTED)
        row["model_response_id"] = None
    return row


def test_a_complete_phase_one_row_validates():
    validate_results([_row("B", metrics=_COMPLETE), _row("C", metrics=_COMPLETE)])


def test_an_explicit_report_profile_set_accepts_distinct_model_cohorts(reviewed_profile):
    current = report_profile(reviewed_profile())
    historical = replace(
        current,
        profile_id="phase1-gpt-v2",
        sha256="2" * 64,
        requested_model="gpt-5.6-sol",
        probed_response_model="gpt-5.6-sol",
        max_agent_query_attempts=1,
        provider_retry_backoff_seconds=(),
        replay_max_bytes=0,
    )
    current_row = _row("B", metrics=_COMPLETE, run_id="current")
    historical_row = _row(
        "C",
        metrics={**_COMPLETE, "history_max_prepared_bytes": 0},
        run_id="historical",
        model="gpt-5.6-sol",
        model_profile_id=historical.profile_id,
        model_profile_sha256=historical.sha256,
        model_response_id="gpt-5.6-sol",
    )
    validate_results([current_row, historical_row], profiles=(current, historical))

    with pytest.raises(ResultsValidationError, match="not in the report manifest"):
        validate_results([historical_row], profiles=(current,))


def test_a_pre_agent_infra_row_with_not_started_usage_validates():
    validate_results([_row("B", outcome="infra_fail")])


@pytest.mark.parametrize("field", ["model_profile_id", "model_profile_sha256"])
def test_a_missing_or_blank_profile_field_fails(field):
    for bad in (None, "", "   "):
        row = _row("B", metrics=_COMPLETE)
        row[field] = bad
        with pytest.raises(ResultsValidationError, match=field):
            validate_results([row])


@pytest.mark.parametrize("digest", ["abc", "A" * 64, "g" * 64, 1, "0" * 63])
def test_a_malformed_profile_digest_fails(digest):
    row = _row("B", metrics=_COMPLETE, model_profile_sha256=digest)
    with pytest.raises(ResultsValidationError, match="model_profile_sha256"):
        validate_results([row])


def test_metrics_must_carry_exactly_the_current_fields():
    row = _row("B", metrics={k: v for k, v in _COMPLETE.items() if k != "model_calls"})
    with pytest.raises(ResultsValidationError, match="metrics keys"):
        validate_results([row])
    row = _row("B", metrics={**_COMPLETE, "cost": 1})
    with pytest.raises(ResultsValidationError, match="metrics keys"):
        validate_results([row])


@pytest.mark.parametrize("field,value", [
    ("model_calls", -1), ("provider_attempts", True), ("provider_responses", 1.5),
    ("model_calls", "2"), ("provider_attempts", None),
])
def test_a_malformed_count_fails(field, value):
    with pytest.raises(ResultsValidationError, match=f"metrics.{field}"):
        validate_results([_row("B", metrics={**_COMPLETE, field: value})])


@pytest.mark.parametrize("field,value", [
    ("prompt_tokens", -1), ("completion_tokens", True), ("total_tokens", 1.5),
    ("prompt_tokens", "70"),
])
def test_a_malformed_token_value_fails(field, value):
    with pytest.raises(ResultsValidationError, match=f"metrics.{field}"):
        validate_results([_row("B", metrics={**_COMPLETE, field: value})])


def test_a_partial_token_triple_fails():
    with pytest.raises(ResultsValidationError, match="all present or all null"):
        validate_results([_row("B", metrics={**_COMPLETE, "completion_tokens": None})])


def test_a_broken_token_identity_fails():
    with pytest.raises(ResultsValidationError, match="total = prompt \\+ completion"):
        validate_results([_row("B", metrics={**_COMPLETE, "total_tokens": 999})])


def test_an_unknown_usage_status_fails():
    with pytest.raises(ResultsValidationError, match="token_usage_status"):
        validate_results([_row("B", metrics={**_COMPLETE, "token_usage_status": "partial"})])


@pytest.mark.parametrize("mutation,match", [
    ({"model_calls": 1, "provider_attempts": 1}, "'not_started' usage"),
    ({"model_calls": 1}, "at least one provider attempt"),
    ({"provider_attempts": 1}, "exceed the reviewed ceiling"),
    ({"provider_responses": 1}, "response\\(s\\) for 0 attempt"),
    ({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
     "no provider response can carry tokens"),
])
def test_not_started_cannot_carry_activity_or_tokens(mutation, match):
    row = _row("B", outcome="infra_fail", metrics={**_NOT_STARTED, **mutation})
    row["model_response_id"] = None
    with pytest.raises(ResultsValidationError, match=match):
        validate_results([row])


@pytest.mark.parametrize("outcome", ["pass", "agent_fail", "protocol_violation"])
def test_a_correctness_row_cannot_carry_not_started_usage(outcome):
    """An agent that returned before its first model call is infrastructure evidence, not a score."""
    row = _row("B", outcome=outcome, metrics=_NOT_STARTED)
    row["model_response_id"] = None
    with pytest.raises(ResultsValidationError, match="cannot carry 'not_started'"):
        validate_results([row])


@pytest.mark.parametrize("metrics,match", [
    ({**_COMPLETE, "provider_responses": 99}, "response\\(s\\) for"),
    ({**_COMPLETE, "model_calls": 1, "provider_attempts": 2, "provider_responses": 1},
     "model_calls == provider_attempts"),
    ({**_INCOMPLETE, "model_calls": 1, "provider_attempts": 5, "provider_responses": 1},
     "exceed the reviewed ceiling"),
    ({**_INCOMPLETE, "provider_responses": 0}, "no provider response can carry tokens"),
])
def test_impossible_counter_relationships_fail(metrics, match):
    with pytest.raises(ResultsValidationError, match=match):
        validate_results([_row("B", outcome="infra_fail", metrics=metrics)])


@pytest.mark.parametrize("wall", ["not-a-number", None, True, float("nan"), float("inf"), -1.0])
def test_a_malformed_wall_time_fails(wall):
    with pytest.raises(ResultsValidationError, match="total_wall_seconds"):
        validate_results([_row("B", metrics={**_COMPLETE, "total_wall_seconds": wall})])


@pytest.mark.parametrize("status", [None, 7, ["complete"], {"complete": 1}])
def test_an_unhashable_or_non_string_status_is_a_validation_error(status):
    """`in` on an unhashable value raises TypeError; that must not escape the validator."""
    with pytest.raises(ResultsValidationError, match="token_usage_status"):
        validate_results([_row("B", metrics={**_COMPLETE, "token_usage_status": status})])


def test_not_started_cannot_carry_a_returned_model():
    row = _row("B", outcome="infra_fail", metrics=_NOT_STARTED)
    row["model_response_id"] = SYNTHETIC_RESPONSE_MODEL
    with pytest.raises(ResultsValidationError, match="no provider response can carry"):
        validate_results([row])


@pytest.mark.parametrize("mutation,match", [
    ({"provider_attempts": 0, "model_calls": 0, "provider_responses": 0},
     "no provider response can carry"),
    ({"provider_responses": 1}, "model_calls == provider_attempts"),
    ({"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}, "null tokens"),
])
def test_complete_requires_matching_counts_and_tokens(mutation, match):
    with pytest.raises(ResultsValidationError, match=match):
        validate_results([_row("B", metrics={**_COMPLETE, **mutation})])


def test_complete_requires_a_returned_model_identity():
    row = _row("B", metrics=_COMPLETE)
    row["model_response_id"] = None
    with pytest.raises(ResultsValidationError, match="one returned model identity"):
        validate_results([row])


@pytest.mark.parametrize("outcome", ["pass", "agent_fail", "protocol_violation"])
def test_incomplete_usage_cannot_be_a_correctness_scored_row(outcome):
    """An unanswered model turn still makes the cell infrastructure evidence."""
    with pytest.raises(ResultsValidationError, match="eventually receive a response"):
        validate_results([_row("B", outcome=outcome, metrics=_INCOMPLETE)])


@pytest.mark.parametrize("outcome", ["pass", "agent_fail", "protocol_violation"])
def test_a_recovered_attempt_can_be_scored_but_usage_stays_incomplete(outcome):
    recovered = {
        **_INCOMPLETE,
        "model_calls": 2,
        "provider_attempts": 3,
        "provider_responses": 2,
        "provider_retry_count": 1,
        "provider_retry_delay_seconds": 4,
    }
    validate_results([_row("B", outcome=outcome, metrics=recovered)])


def test_a_scored_missing_usage_response_is_allowed_but_not_efficiency_eligible():
    missing_usage = {
        **_INCOMPLETE,
        "model_calls": 2,
        "provider_attempts": 2,
        "provider_responses": 2,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "provider_failure_category": None,
        "provider_failure_counts": {},
    }
    validate_results([_row("B", outcome="agent_fail", metrics=missing_usage)])


def test_missing_usage_may_retain_a_lower_bound_from_other_responses():
    lower_bound = {**_COMPLETE, "token_usage_status": "incomplete"}
    validate_results([_row("B", outcome="agent_fail", metrics=lower_bound)])


def test_incomplete_usage_is_a_valid_health_row_on_infra_fail():
    validate_results([_row("B", outcome="infra_fail", metrics=_INCOMPLETE)])


def test_incomplete_needs_at_least_one_attempt():
    row = _row("B", outcome="infra_fail",
               metrics={**_INCOMPLETE, "model_calls": 0, "provider_attempts": 0,
                        "provider_responses": 0, "prompt_tokens": None,
                        "completion_tokens": None, "total_tokens": None})
    row["model_response_id"] = None
    with pytest.raises(ResultsValidationError, match="at least one provider attempt"):
        validate_results([row])


@pytest.mark.parametrize("field,value,label", [
    ("model_response_id", "gpt-other", "returned model"),
    ("model_profile_sha256", "2" * 64, "profile digest"),
    ("model", "gpt-other", "requested model"),
])
def test_a_row_that_leaves_the_reviewed_model_path_fails_per_row(field, value, label):
    """Each of the three drifts is refused on its own row, before any pairing is considered."""
    with pytest.raises(ResultsValidationError):
        validate_results([_row("C", metrics=_COMPLETE, run_id="c1", **{field: value})]), label


@pytest.mark.parametrize("field,value,label", [
    ("model_response_id", "gpt-other", "returned model"),
    ("model_profile_sha256", "2" * 64, "profile digest"),
])
def test_the_bc_methodology_guard_catches_each_drift_independently(field, value, label):
    """Defence in depth behind the per-row pin, so it keeps its own order-independent regression."""
    import itertools

    from ckbbench.matrix.store import _validate_model_methodology

    rows = [
        _row("B", metrics=_COMPLETE, run_id="b1"),
        _row("C", metrics=_COMPLETE, run_id="c1", **{field: value}),
    ]
    messages = set()
    for order in itertools.permutations(rows):
        with pytest.raises(ResultsValidationError) as exc:
            _validate_model_methodology(list(order))
        messages.add(str(exc.value))
    assert len(messages) == 1, (label, messages)
    assert "mixed B/C model methodology" in messages.pop()


def test_the_methodology_guard_keeps_distinct_requested_models_in_separate_cohorts():
    from ckbbench.matrix.store import _validate_model_methodology

    _validate_model_methodology([
        _row("B", metrics=_COMPLETE, run_id="current-b"),
        _row("C", metrics=_COMPLETE, run_id="historical-c", model="gpt-5.6-sol"),
    ])


def test_the_bc_methodology_guard_ignores_a_and_d():
    from ckbbench.matrix.store import _validate_model_methodology

    _validate_model_methodology([
        _row("A", metrics=_COMPLETE, run_id="a1", model_response_id="gpt-other"),
        _row("B", metrics=_COMPLETE, run_id="b1"),
        _row("C", metrics=_COMPLETE, run_id="c1"),
        _row("D", metrics=_COMPLETE, run_id="d1", model_profile_sha256="9" * 64),
    ])


def test_a_missing_tracked_profile_refuses_the_whole_report(monkeypatch, tmp_path):
    """Falling back to an unpinned mode would accept an arbitrary model path as the approved one.

    The absence is injected at the loader the store actually calls. Patching `PROFILE_PATH` does
    nothing: `load_model_profile`'s default argument is bound at definition time, so this once
    passed only because the tracked profile happened not to exist yet.
    """
    from ckbbench.matrix import store as store_mod
    from ckbbench.run.model_profile import load_model_profile

    absent = tmp_path / "absent.json"
    monkeypatch.setattr(store_mod, "_reviewed_profile", _real_reviewed_profile)
    monkeypatch.setattr(store_mod, "load_model_profile", lambda *a, **k: load_model_profile(absent))
    with pytest.raises(ResultsValidationError, match="needs the tracked model profile"):
        validate_results([_row("B", metrics=_COMPLETE)])


def test_the_real_tracked_profile_is_never_what_these_tests_validate_against():
    """Every store test injects a synthetic profile, so the committed one cannot mask a regression."""
    from ckbbench.matrix import store as store_mod

    from ckbbench.matrix.test_fixtures import SYNTHETIC_PROFILE_SHA256

    assert store_mod._reviewed_profile().sha256 == SYNTHETIC_PROFILE_SHA256


def test_a_repeated_bc_set_under_one_profile_still_validates_and_renders(tmp_path: Path):
    from ckbbench.matrix.build_site import build_site_from_results_dir

    rows = []
    for arm in ("B", "C"):
        for seed in (1, 2):
            rows.append(_row(arm, metrics=_COMPLETE, run_id=f"{arm}{seed}", seed=seed))
    results_dir = write_synthetic_results(tmp_path, rows)
    assert build_site_from_results_dir(results_dir, tmp_path / "site").is_file()


def test_the_earlier_guards_remain_independently_active():
    """Budget and MCP-surface guards remain orthogonal to profile validation."""
    b = _row("B", metrics=_COMPLETE, run_id="b1")
    c = _row("C", metrics=_COMPLETE, run_id="c1")
    c["agent_limits"] = {"step_limit": 40, "cost_limit": 0.0, "wall_time_limit_seconds": 900}
    with pytest.raises(ResultsValidationError, match="mixed B/C agent budgets"):
        validate_results([b, c])

    wrong_surface = _row("B", metrics=_COMPLETE, run_id="b2", mcp_surface_profile="docs-only-v1")
    with pytest.raises(ResultsValidationError, match="must run under mcp_surface_profile"):
        validate_results([wrong_surface])


def test_no_secret_or_provider_body_was_added_to_a_row():
    row = _row("C", metrics=_COMPLETE)
    serialized = json.dumps(row)
    for leak in ("sk-live", "api_key", "Authorization", "Bearer ", "http://", "https://",
                 "tool_calls", "\"messages\"", "choices"):
        assert leak not in serialized


# --- an unanswered attempt must name its cause -----------------------------------------------------

_FAILED = {
    "total_wall_seconds": 1.0, "model_calls": 3, "provider_attempts": 3, "provider_responses": 2,
    "provider_retry_count": 0, "provider_retry_delay_seconds": 0,
    "history_compaction_count": 0, "history_dropped_groups": 0,
    "history_dropped_items": 0, "history_max_prepared_bytes": 1024,
    "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
    "token_usage_status": "incomplete", "provider_failure_category": "connection",
    "provider_failure_counts": {"connection": 1},
}


def test_an_unanswered_attempt_needs_a_category():
    row = _row("B", metrics={**_FAILED, "provider_failure_category": None}, outcome="infra_fail")
    with pytest.raises(ResultsValidationError, match="require a metrics.provider_failure_category"):
        validate_results([row])


def test_an_unanswered_attempt_with_a_category_validates():
    validate_results([_row("B", metrics=_FAILED, outcome="infra_fail")])


@pytest.mark.parametrize("category", [
    "OSError", "ConnectError", "connection failed", "", " ", "CONNECTION", "unknown",
    True, 1, 1.5, ["connection"], {"category": "connection"},
])
def test_a_value_outside_the_allowlist_is_refused_without_echoing_it(category):
    row = _row("B", metrics={**_FAILED, "provider_failure_category": category},
               outcome="infra_fail")
    with pytest.raises(ResultsValidationError) as exc:
        validate_results([row])
    assert "must be null or one of" in str(exc.value)
    # A rejected value is file-controlled; it must not be echoed back.
    assert str(category) not in str(exc.value) or category in ("", " ")


def test_a_complete_row_cannot_carry_a_category():
    metrics = {**_COMPLETE, "provider_failure_category": "connection"}
    with pytest.raises(ResultsValidationError, match="must be null when every provider"):
        validate_results([_row("B", metrics=metrics)])


def test_a_not_started_row_cannot_carry_a_category():
    metrics = {**_NOT_STARTED, "provider_failure_category": "connection"}
    row = _row("B", metrics=metrics, outcome="infra_fail", model_response_id=None)
    with pytest.raises(ResultsValidationError, match="must be null when every provider"):
        validate_results([row])


def test_incomplete_from_missing_usage_alone_carries_no_category():
    """Answered but unusable usage is not an unanswered attempt."""
    metrics = {**_FAILED, "provider_attempts": 2, "provider_responses": 2, "model_calls": 2,
               "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
               "provider_failure_category": None, "provider_failure_counts": {}}
    validate_results([_row("B", metrics=metrics, outcome="infra_fail")])


def test_multiple_needs_at_least_two_unanswered_attempts():
    one = {**_FAILED, "provider_failure_category": "multiple"}
    with pytest.raises(ResultsValidationError, match="at least two unanswered"):
        validate_results([_row("B", metrics=one, outcome="infra_fail")])

    two = {
        **one,
        "model_calls": 4,
        "provider_attempts": 4,
        "provider_responses": 2,
        "provider_failure_counts": {"connection": 1, "timeout": 1},
    }
    validate_results([_row("B", metrics=two, outcome="infra_fail")])


def test_the_category_helper_accepts_a_recovered_scored_attempt():
    recovered = {
        **_FAILED, "model_calls": 2, "provider_attempts": 3, "provider_responses": 2,
        "provider_retry_count": 1, "provider_retry_delay_seconds": 4,
    }
    _validate_provider_failure_category("row", recovered, outcome="protocol_violation")


def test_three_retries_use_the_full_reviewed_delay_schedule():
    exhausted = {
        **_INCOMPLETE,
        "model_calls": 1,
        "provider_attempts": 4,
        "provider_responses": 0,
        "provider_retry_count": 3,
        "provider_retry_delay_seconds": 28,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "provider_failure_counts": {"connection": 4},
    }
    validate_results([
        _row("B", metrics=exhausted, outcome="infra_fail", model_response_id=None)
    ])


def test_retries_on_two_turns_restart_the_backoff_schedule():
    recovered = {
        **_INCOMPLETE,
        "model_calls": 2,
        "provider_attempts": 4,
        "provider_responses": 2,
        "provider_retry_count": 2,
        "provider_retry_delay_seconds": 8,
        "provider_failure_counts": {"connection": 2},
    }
    validate_results([_row("B", metrics=recovered, outcome="agent_fail")])


def test_an_internal_error_after_a_completed_retry_keeps_the_cohort_reportable():
    internal_after_retry = {
        **_INCOMPLETE,
        "model_calls": 1,
        "provider_attempts": 2,
        "provider_responses": 0,
        "provider_retry_count": 2,
        "provider_retry_delay_seconds": 12,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "provider_failure_counts": {"connection": 2},
    }
    validate_results([
        _row("B", metrics=_COMPLETE),
        _row(
            "C", metrics=internal_after_retry, outcome="infra_fail", model_response_id=None
        ),
    ])


def test_one_terminal_pre_send_failure_keeps_an_infra_row_reportable():
    metrics = {
        **_INCOMPLETE,
        "model_calls": 9,
        "provider_attempts": 8,
        "provider_responses": 8,
        "provider_failure_category": None,
        "provider_failure_counts": {},
    }
    row = _row("B", metrics=metrics, outcome="infra_fail")
    row["agent_exit_status"] = "error"

    validate_results([row])


@pytest.mark.parametrize("agent_exit_status,calls", [(None, 9), ("error", 10)])
def test_missing_provider_attempts_need_exactly_one_terminal_infra_failure(
    agent_exit_status, calls
):
    metrics = {
        **_INCOMPLETE,
        "model_calls": calls,
        "provider_attempts": 8,
        "provider_responses": 8,
        "provider_failure_category": None,
        "provider_failure_counts": {},
    }
    row = _row("B", metrics=metrics, outcome="infra_fail")
    row["agent_exit_status"] = agent_exit_status

    with pytest.raises(ResultsValidationError, match="terminal failed model call"):
        validate_results([row])


def test_a_retry_must_be_backed_by_a_retryable_provider_failure():
    invalid = {
        **_INCOMPLETE,
        "model_calls": 1,
        "provider_attempts": 2,
        "provider_responses": 1,
        "provider_retry_count": 1,
        "provider_retry_delay_seconds": 4,
        "provider_failure_category": "authentication",
        "provider_failure_counts": {"authentication": 1},
    }
    with pytest.raises(ResultsValidationError, match="exceeds retryable provider failures"):
        validate_results([_row("B", metrics=invalid, outcome="agent_fail")])


@pytest.mark.parametrize("mutation,match", [
    ({"provider_retry_count": 0}, "provider_retry_count"),
    ({"provider_retry_delay_seconds": 20}, "provider_retry_delay_seconds"),
    ({"provider_failure_counts": {}}, "failure counts total"),
    ({"provider_failure_counts": {"unknown": 1}}, "keys must be in"),
    ({"provider_failure_counts": {"connection": 0}}, "positive integers"),
])
def test_malformed_retry_telemetry_is_rejected(mutation, match):
    recovered = {
        **_INCOMPLETE,
        "model_calls": 1,
        "provider_attempts": 2,
        "provider_responses": 1,
        "provider_retry_count": 1,
        "provider_retry_delay_seconds": 4,
        **mutation,
    }
    with pytest.raises(ResultsValidationError, match=match):
        validate_results([_row("B", metrics=recovered, outcome="agent_fail")])


@pytest.mark.parametrize("mutation,match", [
    ({"history_compaction_count": 3}, "exceeds model_calls"),
    ({"history_dropped_groups": 1}, "without a recorded compaction"),
    ({"history_dropped_items": 1}, "without a recorded compaction"),
    ({"history_compaction_count": 1, "history_dropped_groups": 0},
     "internally inconsistent"),
    ({"history_compaction_count": 1, "history_dropped_groups": 2,
      "history_dropped_items": 1}, "internally inconsistent"),
    ({"history_max_prepared_bytes": 131073}, "exceeds the reviewed replay ceiling"),
    ({"history_compaction_count": 1, "history_dropped_groups": 1,
      "history_dropped_items": 1, "history_max_prepared_bytes": 0},
     "non-zero prepared-byte"),
])
def test_malformed_replay_telemetry_is_rejected(mutation, match):
    with pytest.raises(ResultsValidationError, match=match):
        validate_results([_row("B", metrics={**_COMPLETE, **mutation})])


@pytest.mark.parametrize("field", [
    "provider_failure_category", "provider_failure_counts", "provider_retry_count",
    "provider_retry_delay_seconds", "history_compaction_count", "history_dropped_groups",
    "history_dropped_items", "history_max_prepared_bytes",
])
def test_a_row_missing_a_new_metrics_key_is_refused(field):
    stale = {k: v for k, v in _COMPLETE.items() if k != field}
    with pytest.raises(ResultsValidationError, match="metrics keys must be"):
        validate_results([_row("B", metrics=stale)])


def test_a_previous_schema_version_is_still_refused():
    row = _row("B", metrics=_COMPLETE)
    row["schema_version"] = "1.3.0"
    with pytest.raises(ResultsValidationError):
        validate_results([row])
