"""Metrics tests: Pass@1 semantics, C-B delta, chain separation (ADR-0011/0012)."""

from __future__ import annotations

import math

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


def test_pass_at1_ci_zero_scored_runs():
    mean, low, high = pass_at1_ci(successes=0, scored_runs=0)
    assert mean == 0.0
    assert low == 0.0 and high == 1.0


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