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
    phase_one_comparisons,
    refuse_chain_merge,
)
from ckbbench.run.metrics import RunMetrics
from ckbbench.run.model_profile import model_variant_id
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

    Returning `(0.0, 0.0, 1.0)` would publish a `C - B +0.00 flat` headline from zero scored runs.
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


def test_build_dataset_copies_report_provenance_instead_of_aliasing_it():
    sources = [{"model": "Opus", "rows": 2}]
    dataset = build_dataset([], report_sources=sources)
    sources[0]["rows"] = 999
    assert dataset["report_sources"] == [{"model": "Opus", "rows": 2}]


@pytest.mark.parametrize("field,value", [
    ("model", "another-model"),
    ("thinking_level", "automatic"),
    ("model_variant_id", "mv1-" + "f" * 64),
])
def test_build_dataset_refuses_forged_variant_source_metadata(field, value):
    row = synthetic_run_dict()
    profile_id = row["model_profile_id"]
    digest = row["model_profile_sha256"]
    source = {
        "model": row["model"],
        "profile_id": profile_id,
        "profile_sha256": digest,
        "thinking_level": "medium",
        "model_variant_id": model_variant_id(
            requested_model=row["model"],
            thinking_level="medium",
            profile_id=profile_id,
            profile_sha256=digest,
        ),
    }
    source[field] = value
    with pytest.raises(ValueError, match="report source"):
        build_dataset([row], report_sources=[source])


def test_same_model_and_thinking_profiles_have_distinct_human_labels():
    model = synthetic_run_dict()["model"]
    rows = []
    sources = []
    for version, digest in (("v1", "1" * 64), ("v2", "2" * 64)):
        profile_id = f"model-profile-synthetic-{version}"
        variant = model_variant_id(
            requested_model=model,
            thinking_level="high",
            profile_id=profile_id,
            profile_sha256=digest,
        )
        rows.append(synthetic_run_dict(
            run_id=version,
            model_profile_id=profile_id,
            model_profile_sha256=digest,
        ))
        sources.append({
            "model": model,
            "profile_id": profile_id,
            "profile_sha256": digest,
            "thinking_level": "high",
            "model_variant_id": variant,
        })

    labels = {
        row["model_variant_label"]
        for row in build_dataset(rows, report_sources=sources)["phase_one_arms"]
    }
    assert len(labels) == 2
    assert all("thinking high · variant " in label for label in labels)


def test_phase_one_summary_keeps_profile_and_history_compaction_telemetry():
    metrics = RunMetrics(
        total_wall_seconds=1.0,
        prompt_tokens=70,
        completion_tokens=30,
        total_tokens=100,
        model_calls=2,
        provider_attempts=2,
        provider_responses=2,
        token_usage_status="complete",
        history_compaction_count=2,
        history_dropped_groups=3,
        history_dropped_items=6,
        history_max_prepared_bytes=131000,
    )
    dataset = build_dataset([
        synthetic_run_dict(arm="B", metrics=metrics),
        synthetic_run_dict(arm="C", run_id="c", metrics=metrics),
    ])
    for summary in dataset["phase_one_arms"]:
        assert summary["model_profile_id"] == "model-profile-synthetic-v1"
        assert summary["model_profile_sha256"] == "1" * 64
        assert summary["history_compaction_count"] == 2
        assert summary["history_dropped_groups"] == 3
        assert summary["history_dropped_items"] == 6
        assert summary["history_max_prepared_bytes"] == 131000


def test_one_model_arm_keeps_profile_versions_as_separate_series():
    rows = [
        synthetic_run_dict(arm="B", run_id="one"),
        synthetic_run_dict(
            arm="B", run_id="two", seed=2,
            model_profile_id="other", model_profile_sha256="2" * 64,
        ),
    ]
    dataset = build_dataset(rows)
    assert len(dataset["phase_one_arms"]) == 2
    assert {
        (row["model_profile_id"], row["model_profile_sha256"])
        for row in dataset["phase_one_arms"]
    } == {
        ("model-profile-synthetic-v1", "1" * 64),
        ("other", "2" * 64),
    }


def test_different_profile_variants_never_form_a_false_bc_pair():
    rows = [
        synthetic_run_dict(arm="B", run_id="medium-b"),
        synthetic_run_dict(
            arm="C", run_id="high-c", model_profile_id="model-profile-synthetic-v2",
            model_profile_sha256="2" * 64,
        ),
    ]
    comparisons = build_dataset(rows)["phase_one_comparisons"]
    assert len(comparisons) == 2
    assert {(row["B"] is not None, row["C"] is not None) for row in comparisons} == {
        (True, False), (False, True),
    }
    assert all(row["weighted_score_delta"] is None for row in comparisons)
    assert len(line_series_for_chain(build_dataset(rows), "devnet")) == 2
    assert len(leaderboard_rows(build_dataset(rows), "devnet")) == 2


def test_family_for_model_known_and_other():
    assert family_for_model("Opus") == "Anthropic"
    assert family_for_model("gpt-5.6-sol") == "OpenAI"
    assert family_for_model("gpt-5.6-terra") == "OpenAI"
    assert family_for_model("gpt-5.6-luna") == "OpenAI"
    assert family_for_model("deepseek/deepseek-v4-flash-0731") == "DeepSeek"
    assert family_for_model("deepseek/deepseek-v4-pro-0813") == "DeepSeek"
    assert family_for_model("google/gemini-3.7-flash") == "Google"
    assert family_for_model("stealth/ox-alpha") == "Ox"
    assert family_for_model("unknown-model") == "Other"


def test_aggregate_results_and_leaderboard():
    rows = [
        synthetic_run_dict(arm=arm, outcome="pass", run_id=f"{arm.lower()}{seed}", seed=seed)
        for arm in ("B", "C") for seed in (1, 2, 3)
    ]
    cells = aggregate_results(rows)
    assert len(cells) == 2
    ds = build_dataset(rows, synthetic=True)
    board = leaderboard_rows(ds, "devnet")
    assert board[0]["headline"]["direction"] == "flat"

# --- an empty correctness denominator is undefined, never zero ------------------------------------
#
# Two `infra_fail` cells once produced `C - B +0.00 [-1.41,+1.41] flat` from zero scored runs.
# These regressions pin the contract that makes that impossible.

def _row(arm, outcome, run_id, model="gpt-5.6-sol", seed=1):
    return {"suite_semver": "2.0.0", "suite_freeze_hash": "f" * 64,
            "mcp_server_version": "1.6.13", "chain": "devnet", "arm": arm, "model": model,
            "seed": seed, "run_id": run_id, "outcome": outcome, "total_score": 0, "max_score": 100,
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


@pytest.mark.parametrize("b_outcome,c_outcome", [
    ("infra_fail", "infra_fail"),
    ("pass", "infra_fail"),
    ("infra_fail", "pass"),
    ("pass", "pass"),
    ("agent_fail", "pass"),
])
def test_one_run_per_arm_is_raw_evidence_but_never_a_headline(b_outcome, c_outcome):
    dataset = build_dataset([_row("B", b_outcome, "b1"), _row("C", c_outcome, "c1")])
    lines = line_series_for_chain(dataset, "devnet")
    assert lines[0]["headline"] is None


def test_headline_requires_three_balanced_fully_scored_paired_seeds():
    rows = [
        _row(arm, outcome, f"{arm.lower()}{seed}", seed=seed)
        for arm, outcome in (("B", "agent_fail"), ("C", "pass"))
        for seed in (1, 2, 3)
    ]
    dataset = build_dataset(rows)
    readiness = dataset["phase_one_comparisons"][0]["comparison_readiness"]
    assert readiness["headline_eligible"] is True
    assert readiness["reasons"] == []
    assert line_series_for_chain(dataset, "devnet")[0]["headline"]["delta"] == 1.0


@pytest.mark.parametrize(
    ("exit_status", "count_field"),
    [
        ("LimitsExceeded", "step_limit_exhausted_runs"),
        ("TimeExceeded", "wall_time_limit_exhausted_runs"),
    ],
)
def test_a_budget_exhausted_row_keeps_its_score_and_the_comparison(
    exit_status, count_field
):
    rows = [
        synthetic_run_dict(
            arm=arm,
            outcome="agent_fail" if arm == "B" else "pass",
            run_id=f"{arm.lower()}{seed}",
            seed=seed,
            agent_exit_status=exit_status if arm == "B" and seed == 2 else "Submitted",
        )
        for arm in ("B", "C")
        for seed in (1, 2, 3)
    ]
    dataset = build_dataset(rows)
    comparison = dataset["phase_one_comparisons"][0]
    readiness = comparison["comparison_readiness"]

    assert comparison["weighted_score_delta"] is not None
    assert readiness["headline_eligible"] is True
    assert readiness["reasons"] == []
    assert readiness["budget_exhausted_runs"] == {"B": 1, "C": 0}
    assert readiness[count_field] == {"B": 1, "C": 0}
    assert comparison["efficiency_readiness"]["comparison_eligible"] is True
    assert line_series_for_chain(dataset, "devnet")[0]["headline"] is not None


def test_completed_agent_rows_remain_headline_eligible():
    rows = [
        synthetic_run_dict(
            arm=arm,
            outcome="agent_fail" if arm == "B" else "pass",
            run_id=f"{arm.lower()}{seed}",
            seed=seed,
            agent_exit_status="Submitted",
        )
        for arm in ("B", "C")
        for seed in (1, 2, 3)
    ]
    readiness = build_dataset(rows)["phase_one_comparisons"][0]["comparison_readiness"]
    assert readiness["headline_eligible"] is True
    assert readiness["budget_exhausted_runs"] == {"B": 0, "C": 0}


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
    assert line["headline"] is None
    assert "completion_conditioned" in line["comparison_readiness"]["reasons"]
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
    dataset = build_dataset(
        [
            _row(arm, "pass", f"{arm.lower()}{seed}", model="scored-model", seed=seed)
            for arm in ("B", "C") for seed in (1, 2, 3)
        ]
        + [
            _row("B", "infra_fail", "b2", model="aaa-unscored"),
            _row("C", "infra_fail", "c2", model="aaa-unscored"),
        ]
    )
    rows = leaderboard_rows(dataset, "devnet")
    assert [r["model"] for r in rows] == ["scored-model", "aaa-unscored"]
    assert rows[0]["headline"] is not None and rows[1]["headline"] is None


def _summary_row(
    arm, outcome, run_id, *, score, tokens, wall, usage="complete", model="gpt-5.6-sol",
    seed=1,
):
    row = synthetic_run_dict(
        arm=arm,
        outcome=outcome,
        run_id=run_id,
        model=model,
        seed=seed,
        metrics=RunMetrics(
            total_wall_seconds=wall,
            prompt_tokens=tokens - 10 if tokens is not None else None,
            completion_tokens=10 if tokens is not None else None,
            total_tokens=tokens,
            model_calls=1,
            provider_attempts=1,
            provider_responses=1 if usage == "complete" else 0,
            token_usage_status=usage,
            provider_failure_category=None if usage == "complete" else "other_provider",
        ),
    )
    row["total_score"] = score
    row["max_score"] = 100
    return row


def _task(task_id, passed):
    return {
        "task_id": task_id,
        "passed": passed,
        "score": 10,
        "score_awarded": 10 if passed else 0,
        "reason": "synthetic",
        "proof": "synthetic",
        "scored": True,
    }


def test_phase_one_summary_reports_weighted_score_tokens_time_and_health():
    rows = [
        _summary_row("B", "pass", "b-ok", score=100, tokens=100, wall=10.0),
        _summary_row(
            "B", "infra_fail", "b-infra", score=0, tokens=15, wall=1.0,
            usage="incomplete",
        ),
        _summary_row("C", "agent_fail", "c-ok", score=70, tokens=150, wall=12.5),
    ]
    rows[0]["tasks"] = [_task("task-a", True), _task("task-b", True)]
    rows[2]["tasks"] = [_task("task-a", True), _task("task-b", False)]
    comparison = build_dataset(rows)["phase_one_comparisons"][0]
    assert comparison["B"]["runs"] == 2
    assert comparison["B"]["scored_runs"] == 1
    assert comparison["B"]["infra_fail_rate"] == 0.5
    assert comparison["B"]["incomplete_usage_runs"] == 1
    assert comparison["B"]["weighted_score_values"] == [1.0]
    assert comparison["C"]["weighted_score_values"] == [0.7]
    assert comparison["weighted_score_delta"] == -0.3
    assert comparison["B"]["total_tokens_values"] == [100]
    assert comparison["C"]["total_tokens_values"] == [150]
    assert comparison["B"]["observed_total_tokens_values"] == [100]
    assert comparison["C"]["observed_total_tokens_values"] == [150]
    assert comparison["observed_total_tokens_delta"] == 50.0
    assert comparison["B"]["provider_attempts"] == 1
    assert comparison["B"]["provider_responses"] == 1
    assert comparison["total_tokens_delta"] is None
    assert comparison["efficiency_readiness"]["comparison_eligible"] is False
    assert "correctness_cohort_not_ready" in comparison["efficiency_readiness"]["reasons"]
    assert comparison["agent_wall_seconds_delta"] is None
    assert comparison["B"]["suite_passes"] == 1
    assert comparison["C"]["suite_passes"] == 0
    assert comparison["B"]["protocol_violation_rate"] == 0.0
    assert comparison["comparison_readiness"]["headline_eligible"] is False
    assert comparison["comparison_readiness"]["recorded_rows"] == {"B": 2, "C": 1}
    assert "attempted_runs" not in comparison["comparison_readiness"]
    assert comparison["comparison_readiness"]["reasons"] == [
        "fewer_than_three_scored_runs_per_arm",
        "completion_conditioned",
    ]
    assert comparison["task_comparisons"] == [
        {
            "task_id": "task-a",
            "B": {"task_id": "task-a", "passes": 1, "runs": 1,
                  "pass_rate": 1.0, "pass_values": [1]},
            "C": {"task_id": "task-a", "passes": 1, "runs": 1,
                  "pass_rate": 1.0, "pass_values": [1]},
            "pass_rate_delta": 0.0,
        },
        {
            "task_id": "task-b",
            "B": {"task_id": "task-b", "passes": 1, "runs": 1,
                  "pass_rate": 1.0, "pass_values": [1]},
            "C": {"task_id": "task-b", "passes": 0, "runs": 1,
                  "pass_rate": 0.0, "pass_values": [0]},
            "pass_rate_delta": -1.0,
        },
    ]


def test_phase_one_summary_raw_values_are_order_independent_and_sorted():
    rows = [
        _summary_row("B", "pass", "b", score=100, tokens=100, wall=10.0),
        _summary_row("C", "agent_fail", "c2", score=70, tokens=300, wall=13.0),
        _summary_row("C", "agent_fail", "c1", score=70, tokens=200, wall=12.0),
    ]
    forward = build_dataset(rows)["phase_one_comparisons"]
    reverse = build_dataset(list(reversed(rows)))["phase_one_comparisons"]
    assert forward == reverse
    assert forward[0]["C"]["total_tokens_values"] == [200, 300]
    assert forward[0]["C"]["agent_wall_seconds_values"] == [12.0, 13.0]
    assert forward[0]["C"]["observed_total_tokens_values"] == [200, 300]
    assert forward[0]["C"]["observed_agent_wall_seconds_values"] == [12.0, 13.0]


def test_task_summaries_preserve_the_suite_result_order():
    order = [
        "task-01-tip",
        "task-04-send-tx",
        "task-06-sudt-script",
        "task-08-type-id-data-cell",
        "task-05-hashlock",
    ]
    rows = [
        _summary_row(arm, "agent_fail", arm.lower(), score=70, tokens=100, wall=10.0)
        for arm in ("B", "C")
    ]
    for row in rows:
        row["tasks"] = [_task(task_id, True) for task_id in order]

    comparison = build_dataset(rows)["phase_one_comparisons"][0]
    assert [task["task_id"] for task in comparison["B"]["task_pass_rates"]] == order
    assert [task["task_id"] for task in comparison["task_comparisons"]] == order


def test_token_delta_requires_three_matched_complete_usage_rows_per_arm():
    rows = [
        _summary_row(
            arm, "agent_fail", f"{arm.lower()}{seed}", score=60 if arm == "B" else 70,
            tokens=100 if arm == "B" else 150, wall=10.0, seed=seed,
        )
        for arm in ("B", "C")
        for seed in (1, 2, 3)
    ]
    comparison = build_dataset(rows)["phase_one_comparisons"][0]
    assert comparison["comparison_readiness"]["headline_eligible"] is True
    assert comparison["efficiency_readiness"]["comparison_eligible"] is True
    assert comparison["efficiency_readiness"]["complete_usage_seed_values"] == {
        "B": [1, 2, 3], "C": [1, 2, 3]
    }
    assert comparison["total_tokens_delta"] == 50.0


def test_a_recovered_scored_row_blocks_token_and_wall_deltas():
    rows = [
        _summary_row(
            arm, "agent_fail", f"{arm.lower()}{seed}", score=60 if arm == "B" else 70,
            tokens=100 if arm == "B" else 150, wall=10.0, seed=seed,
        )
        for arm in ("B", "C")
        for seed in (1, 2, 3)
    ]
    recovered = rows[0]["metrics"]
    recovered.update({
        "provider_attempts": 2,
        "provider_responses": 1,
        "provider_retry_count": 1,
        "provider_retry_delay_seconds": 4,
        "token_usage_status": "incomplete",
        "provider_failure_category": "connection",
        "provider_failure_counts": {"connection": 1},
    })
    comparison = build_dataset(rows)["phase_one_comparisons"][0]
    assert comparison["comparison_readiness"]["headline_eligible"] is True
    assert comparison["weighted_score_delta"] == 0.1
    assert comparison["efficiency_readiness"]["comparison_eligible"] is False
    assert "incomplete_usage_in_scored_rows" in comparison["efficiency_readiness"]["reasons"]
    assert comparison["total_tokens_delta"] is None
    assert comparison["agent_wall_seconds_delta"] is None
    assert comparison["observed_total_tokens_delta"] == 50.0
    assert comparison["observed_agent_wall_seconds_delta"] == 0.0
    assert comparison["B"]["observed_token_runs"] == 3
    assert comparison["C"]["observed_token_runs"] == 3
    assert comparison["B"]["observed_total_tokens_sum"] == 300
    assert comparison["C"]["observed_total_tokens_sum"] == 450
    assert comparison["B"]["provider_attempts"] == 4
    assert comparison["B"]["provider_responses"] == 3
    assert comparison["B"]["unanswered_provider_attempts"] == 1
    assert comparison["B"]["wall_time_runs"] == 2
    assert comparison["C"]["wall_time_runs"] == 3


def test_real_phase_one_shape_keeps_delta_but_blocks_a_survivor_conditioned_headline():
    rows = [
        _summary_row("B", "pass", "b-ok", score=100, tokens=100, wall=10.0),
        _summary_row(
            "B", "infra_fail", "b-infra-1", score=0, tokens=10, wall=1.0,
            usage="incomplete",
        ),
        _summary_row(
            "B", "infra_fail", "b-infra-2", score=0, tokens=10, wall=1.0,
            usage="incomplete",
        ),
        _summary_row("C", "agent_fail", "c-1", score=70, tokens=150, wall=12.0),
        _summary_row("C", "agent_fail", "c-2", score=70, tokens=160, wall=13.0),
    ]
    dataset = build_dataset(rows)
    comparison = dataset["phase_one_comparisons"][0]
    assert comparison["weighted_score_delta"] == -0.3
    assert comparison["comparison_readiness"]["headline_eligible"] is False
    assert comparison["comparison_readiness"]["recorded_rows"] == {"B": 3, "C": 2}
    assert comparison["comparison_readiness"]["reasons"] == [
        "fewer_than_three_scored_runs_per_arm",
        "unbalanced_scored_runs",
        "unmatched_scored_seed_multiset",
        "completion_conditioned",
    ]
    assert line_series_for_chain(dataset, "devnet")[0]["headline"] is None


def test_phase_one_comparison_needs_both_means_before_publishing_a_delta():
    summaries = build_dataset([
        _summary_row("B", "pass", "b", score=100, tokens=100, wall=10.0)
    ])["phase_one_arms"]
    comparison = phase_one_comparisons(summaries)[0]
    assert comparison["B"] is not None and comparison["C"] is None
    assert comparison["weighted_score_delta"] is None
    assert comparison["total_tokens_delta"] is None
    assert comparison["agent_wall_seconds_delta"] is None


@pytest.mark.parametrize(
    "score,maximum",
    [(101, 100), (-1, 100), (1, 0), (True, 100), (1, float("inf"))],
)
def test_phase_one_summary_refuses_invalid_weighted_scores(score, maximum):
    row = _summary_row("B", "pass", "b", score=100, tokens=100, wall=10.0)
    row["total_score"] = score
    row["max_score"] = maximum
    with pytest.raises(ValueError, match="phase-one reporting|must be"):
        build_dataset([row])


@pytest.mark.parametrize("seed", [True, 1.0, "1", None])
def test_phase_one_summary_refuses_a_non_integer_scored_seed(seed):
    row = _summary_row("B", "pass", "b", score=100, tokens=100, wall=10.0)
    row["seed"] = seed
    with pytest.raises(ValueError, match="seed must be an integer"):
        build_dataset([row])


def test_phase_one_task_summary_refuses_duplicate_or_non_boolean_verdicts():
    row = _summary_row("B", "pass", "b", score=100, tokens=100, wall=10.0)
    row["tasks"] = [_task("task-a", True), _task("task-a", False)]
    with pytest.raises(ValueError, match="duplicate task outcome"):
        build_dataset([row])

    row["tasks"] = [{**_task("task-a", True), "passed": 1}]
    with pytest.raises(ValueError, match="boolean passed"):
        build_dataset([row])

    row["tasks"] = [{**_task("task-a", True), "scored": 1}]
    with pytest.raises(ValueError, match="boolean scored"):
        build_dataset([row])

    row["tasks"] = [{**_task("task-a", True), "task_id": "  "}]
    with pytest.raises(ValueError, match="needs a task_id"):
        build_dataset([row])
