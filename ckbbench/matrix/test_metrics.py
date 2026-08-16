"""Metrics tests: Pass@1 semantics, C-B delta, chain separation (ADR-0011/0012)."""

from __future__ import annotations

import math

import json

import pytest

from ckbbench.matrix.metrics import (
    aggregate_cell,
    aggregate_results,
    build_dataset,
    correctness_value,
    family_for_model,
    headline_delta,
    leaderboard_rows,
    line_series_for_chain,
    pass_at1_ci,
    refuse_chain_merge,
)
from ckbbench.matrix.test_fixtures import synthetic_run_dict


def _cell(mean: float, low: float, high: float) -> dict[str, float]:
    return {"mean": mean, "ci_low": low, "ci_high": high}


def test_pass_at1_excludes_infra_fail_from_denominator():
    """1 pass + 1 infra_fail => 100%, not 50% (RECOMMENDATION §4)."""
    runs = [
        synthetic_run_dict(outcome="pass", run_id="r1"),
        synthetic_run_dict(outcome="infra_fail", run_id="r2"),
    ]
    cell = aggregate_cell(
        suite_semver="1.0.0-synthetic",
        model="Opus",
        chain="devnet",
        arm="C",
        runs=runs,
    )
    assert cell.mean == 1.0
    assert cell.scored_runs == 1
    assert cell.runs == 2
    assert cell.infra_fail_rate == 0.5


def test_pass_at1_counts_agent_fail_and_violation_as_zero():
    runs = [
        synthetic_run_dict(outcome="agent_fail", run_id="r1"),
        synthetic_run_dict(outcome="protocol_violation", run_id="r2"),
    ]
    cell = aggregate_cell(
        suite_semver="1.0.0-synthetic",
        model="Opus",
        chain="devnet",
        arm="B",
        runs=runs,
    )
    assert cell.mean == 0.0
    assert cell.scored_runs == 2
    assert cell.protocol_violation_rate == 0.5


def test_correctness_value_mapping():
    assert correctness_value("pass") == 1
    assert correctness_value("agent_fail") == 0
    assert correctness_value("protocol_violation") == 0
    assert correctness_value("infra_fail") is None
    with pytest.raises(ValueError):
        correctness_value("nope")


def test_pass_at1_ci_widens_honestly_with_one_scored_run():
    mean, low, high = pass_at1_ci(successes=1, scored_runs=1)
    assert mean == 1.0
    assert low == 0.0 and high == 1.0


def test_pass_at1_ci_rejects_impossible_inputs():
    # Direct callers must not be able to pass successes > scored_runs (codex): invalid Pass@1.
    with pytest.raises(ValueError, match="invalid Pass@1 inputs"):
        pass_at1_ci(successes=3, scored_runs=2)
    with pytest.raises(ValueError, match="invalid Pass@1 inputs"):
        pass_at1_ci(successes=-1, scored_runs=2)


def test_pass_at1_ci_exact_wilson_values():
    # Pin the Wilson CI for a known input so a math regression is caught (Rule 9), not just shape.
    mean, low, high = pass_at1_ci(successes=2, scored_runs=3)
    assert mean == 0.667
    assert 0.0 <= low < mean < high <= 1.0
    assert low == 0.208 and high == 0.939  # 95% Wilson for 2/3


def test_headline_delta_significant_when_ci_excludes_zero():
    # A delta larger than its propagated half-width is significant (CI excludes 0).
    hd = headline_delta(_cell(0.2, 0.18, 0.22), _cell(0.9, 0.88, 0.92))
    assert hd.delta > 0
    assert hd.ci_low > 0
    assert hd.significant is True


def test_pass_at1_ci_zero_scored_runs_is_undefined_not_zero():
    """An excluded denominator has no Pass@1.

    Returning `(0.0, 0.0, 1.0)` here is what let Task 20's two `infra_fail` cells publish a
    `C - B +0.00 flat` headline from zero scored runs.
    """
    assert pass_at1_ci(successes=0, scored_runs=0) == (None, None, None)


def test_headline_delta_positive_sign_and_quadrature():
    r = headline_delta(_cell(0.50, 0.40, 0.60), _cell(0.75, 0.65, 0.85))
    assert abs(r.delta - 0.25) < 1e-9
    assert r.direction == "positive"
    assert r.half_width > 0.10
    assert abs(r.half_width - math.sqrt(0.02)) < 1e-9


def test_headline_delta_flat_honest():
    r = headline_delta(_cell(0.60, 0.50, 0.70), _cell(0.60, 0.50, 0.70))
    assert abs(r.delta) < 1e-9
    assert r.direction == "flat"
    assert r.significant is False


def test_headline_delta_negative_honest():
    r = headline_delta(_cell(0.53, 0.43, 0.63), _cell(0.49, 0.39, 0.59))
    assert r.delta < 0
    assert r.direction == "negative"


def test_headline_delta_not_significant_when_ci_straddles_zero():
    r = headline_delta(_cell(0.50, 0.20, 0.80), _cell(0.55, 0.25, 0.85))
    assert r.ci_low < 0 and r.ci_high > 0
    assert r.significant is False


def test_headline_delta_missing_arm_raises():
    with pytest.raises(ValueError):
        headline_delta({}, _cell(0.5, 0.4, 0.6))


def test_refuse_chain_merge_banned_sentinels():
    for sentinel in ("all", "both", "merged", "pooled", "combined", "MERGED"):
        with pytest.raises(ValueError, match="separate"):
            refuse_chain_merge(sentinel)
    assert refuse_chain_merge("devnet") == "devnet"


def test_line_series_unknown_chain_raises():
    ds = build_dataset([], synthetic=True)
    with pytest.raises(ValueError, match="unknown chain"):
        line_series_for_chain(ds, "mainnet")


def test_chains_not_pooled_distinct_series():
    rows = [
        synthetic_run_dict(chain="devnet", arm="C", outcome="pass", run_id="d1"),
        synthetic_run_dict(chain="testnet", arm="C", outcome="agent_fail", run_id="t1"),
    ]
    ds = build_dataset(rows, synthetic=True)
    dev = line_series_for_chain(ds, "devnet")
    test_ = line_series_for_chain(ds, "testnet")
    assert dev[0]["points"]["C"]["mean"] == 1.0
    assert test_[0]["points"]["C"]["mean"] == 0.0


def test_refuse_chain_merge_via_line_series():
    ds = build_dataset([], synthetic=True)
    with pytest.raises(ValueError, match="separate"):
        line_series_for_chain(ds, "merged")


def test_build_dataset_synthetic_marker():
    ds = build_dataset([], synthetic=True)
    assert ds["_SYNTHETIC"] is True
    assert "SYNTHETIC" in ds["_WARNING"]


def test_family_for_model_known_and_other():
    assert family_for_model("Opus") == "Anthropic"
    assert family_for_model("unknown-model") == "Other"


def test_aggregate_results_and_leaderboard():
    rows = [
        synthetic_run_dict(arm="B", outcome="pass", run_id="b1"),
        synthetic_run_dict(arm="C", outcome="pass", run_id="c1"),
    ]
    cells = aggregate_results(rows)
    assert len(cells) == 2
    ds = build_dataset(rows, synthetic=True)
    board = leaderboard_rows(ds, "devnet")
    assert board[0]["headline"]["direction"] == "flat"

# --- an empty correctness denominator is undefined, never zero ------------------------------------
#
# Task 20 ran two cells that both ended `infra_fail`. The report published
# `C - B +0.00 [-1.41,+1.41] flat` from zero scored runs. These regressions pin the contract that
# makes that impossible.

def _row(arm, outcome, run_id, model="gpt-5.6-sol"):
    return {"suite_semver": "2.0.0", "suite_freeze_hash": "f" * 64,
            "mcp_server_version": "1.6.13", "chain": "devnet", "arm": arm, "model": model,
            "seed": 1, "run_id": run_id, "outcome": outcome, "total_score": 0, "max_score": 100,
            "tasks": []}


def test_an_all_infra_fail_cell_has_undefined_correctness_and_a_full_health_rate():
    cell = aggregate_cell(suite_semver="2.0.0", model="gpt-5.6-sol", chain="devnet", arm="B",
                          runs=[_row("B", "infra_fail", "b1"), _row("B", "infra_fail", "b2")])
    assert cell.scored_runs == 0 and cell.runs == 2
    assert (cell.mean, cell.ci_low, cell.ci_high) == (None, None, None)
    assert cell.has_correctness is False
    # Health evidence stays publishable: the cell must not vanish from the report.
    assert cell.infra_fail_rate == 1.0


def test_task_20_shape_produces_no_headline():
    dataset = build_dataset([_row("B", "infra_fail", "b1"), _row("C", "infra_fail", "c1")])
    lines = line_series_for_chain(dataset, "devnet")
    assert len(lines) == 1
    assert lines[0]["headline"] is None
    assert lines[0]["infra_fail_rate"] == 1.0


@pytest.mark.parametrize("b_outcome,c_outcome,expect_headline", [
    ("infra_fail", "infra_fail", False),
    ("pass", "infra_fail", False),
    ("infra_fail", "pass", False),
    ("pass", "pass", True),
    ("agent_fail", "pass", True),
])
def test_a_headline_needs_scored_evidence_on_both_arms(b_outcome, c_outcome, expect_headline):
    dataset = build_dataset([_row("B", b_outcome, "b1"), _row("C", c_outcome, "c1")])
    lines = line_series_for_chain(dataset, "devnet")
    assert (lines[0]["headline"] is not None) is expect_headline


def test_a_scored_arm_survives_alongside_an_unscored_one():
    """One missing denominator must suppress only the invalid geometry, not the scored arm."""
    dataset = build_dataset([
        _row("B", "pass", "b1"), _row("B", "agent_fail", "b2"),
        _row("C", "infra_fail", "c1"),
    ])
    points = line_series_for_chain(dataset, "devnet")[0]["points"]
    assert points["B"]["scored_runs"] == 2 and points["B"]["mean"] == 0.5
    assert points["C"]["scored_runs"] == 0 and points["C"]["mean"] is None


def test_mixed_scored_and_infra_rows_use_only_the_scored_denominator():
    dataset = build_dataset([
        _row("B", "pass", "b1"), _row("B", "infra_fail", "b2"),
        _row("C", "pass", "c1"), _row("C", "infra_fail", "c2"),
    ])
    line = line_series_for_chain(dataset, "devnet")[0]
    assert line["points"]["B"]["scored_runs"] == 1 and line["points"]["B"]["mean"] == 1.0
    assert line["headline"] is not None and line["headline"]["delta"] == 0.0
    # Health rates are still published beside the score.
    assert line["infra_fail_rate"] == 0.5


@pytest.mark.parametrize("field", ["mean", "ci_low", "ci_high"])
def test_headline_delta_refuses_undefined_statistics(field):
    good = {"mean": 0.5, "ci_low": 0.1, "ci_high": 0.9}
    bad = {**good, field: None}
    with pytest.raises(ValueError, match="numeric"):
        headline_delta(bad, good)
    with pytest.raises(ValueError, match="numeric"):
        headline_delta(good, bad)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_headline_delta_refuses_non_finite_statistics(value):
    good = {"mean": 0.5, "ci_low": 0.1, "ci_high": 0.9}
    with pytest.raises(ValueError, match="finite"):
        headline_delta({**good, "mean": value}, good)


def test_the_dataset_serializes_undefined_as_json_null():
    """`NaN`/`Infinity` are not JSON; a consumer must see `null`."""
    dataset = build_dataset([_row("B", "infra_fail", "b1"), _row("C", "infra_fail", "c1")])
    text = json.dumps(dataset, allow_nan=False)
    assert '"mean": null' in text
    assert "NaN" not in text and "Infinity" not in text


def test_leaderboard_puts_rows_without_a_headline_after_scored_ones():
    dataset = build_dataset([
        _row("B", "pass", "b1", model="scored-model"), _row("C", "pass", "c1", model="scored-model"),
        _row("B", "infra_fail", "b2", model="aaa-unscored"),
        _row("C", "infra_fail", "c2", model="aaa-unscored"),
    ])
    rows = leaderboard_rows(dataset, "devnet")
    assert [r["model"] for r in rows] == ["scored-model", "aaa-unscored"]
    assert rows[0]["headline"] is not None and rows[1]["headline"] is None
