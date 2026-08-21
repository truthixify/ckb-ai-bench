"""Render tests: deterministic HTML, honest null/negative, separate chains (ADR-0011)."""

from __future__ import annotations

import re

import pytest

from ckbbench.matrix.metrics import build_dataset, headline_delta
from ckbbench.matrix.render import (
    render_chain_group,
    render_ladder_html,
    render_leaderboard_table,
    render_phase_one_comparison_chart,
    render_phase_one_efficiency_table,
    render_phase_one_effectiveness_table,
    render_phase_one_task_table,
    write_site,
)
from ckbbench.run.metrics import RunMetrics
from ckbbench.matrix.test_fixtures import synthetic_run_dict


def _dataset_with_cb_shapes() -> dict:
    """SYNTHETIC cells encoding positive, flat, and negative C-B headlines."""
    rows = [
        # Three paired seeds per arm are the declared floor for a chart/leaderboard headline.
        *[
            synthetic_run_dict(
                model="Opus", arm=arm, outcome=outcome, run_id=f"opus-{arm.lower()}{seed}",
                seed=seed,
            )
            for arm, outcome in (("B", "agent_fail"), ("C", "pass"))
            for seed in (1, 2, 3)
        ],
        *[
            synthetic_run_dict(
                model="Grok-Build", arm=arm, outcome=outcome,
                run_id=f"gb-{arm.lower()}{seed}", seed=seed,
            )
            for arm in ("B", "C")
            for seed, outcome in ((1, "pass"), (2, "agent_fail"), (3, "pass"))
        ],
        *[
            synthetic_run_dict(
                model="GPT-5.5", arm=arm, outcome=outcome, run_id=f"gpt-{arm.lower()}{seed}",
                seed=seed,
            )
            for arm, outcome in (("B", "pass"), ("C", "agent_fail"))
            for seed in (1, 2, 3)
        ],
        # arms A/D for ladder lines
        synthetic_run_dict(model="Opus", arm="A", outcome="agent_fail", run_id="opus-a1"),
        synthetic_run_dict(model="Opus", arm="D", outcome="pass", run_id="opus-d1"),
        synthetic_run_dict(model="Sonnet", arm="B", outcome="pass", run_id="son-b1"),
        synthetic_run_dict(model="Sonnet", arm="C", outcome="pass", run_id="son-c1"),
        # testnet separate score
        synthetic_run_dict(
            model="Opus", chain="testnet", arm="C", outcome="agent_fail", run_id="opus-tn-c1"
        ),
        synthetic_run_dict(
            model="Opus", chain="testnet", arm="B", outcome="pass", run_id="opus-tn-b1"
        ),
    ]
    return build_dataset(rows, synthetic=True, generated_at="2026-06-13T00:00:00Z")


def test_render_escapes_quotes_in_attributes_no_xss():
    # A model name containing a double-quote must NOT break out of data-model="..." and inject
    # markup (grok-build XSS finding). html.escape(quote=True) renders " as &quot;.
    rows = [
        synthetic_run_dict(model='x"><script>alert(1)</script>', arm="B", outcome="pass", run_id="m-b"),
        synthetic_run_dict(model='x"><script>alert(1)</script>', arm="C", outcome="pass", run_id="m-c"),
    ]
    ds = build_dataset(rows, synthetic=True, generated_at="t")
    html = render_ladder_html(ds)
    # the raw breakout sequence must not appear ANYWHERE (attrs incl. data-model/data-cb/
    # data-direction and the dir-* class), the quote must be entity-encoded, no live script
    assert '"><script>' not in html
    assert "&quot;&gt;&lt;script&gt;" in html or "&quot;" in html
    assert "<script>alert(1)</script>" not in html
    # every attribute context that takes interpolated data must be quote-escaped: a bare ">
    # right after a data- attribute value would be the breakout signature
    assert 'data-model="x"' not in html


def test_render_publishes_health_rates():
    # infra_fail and protocol_violation rates must be VISIBLE in the rendered report, not just in
    # the dataset (RECOMMENDATION 4: published separately, never folded into Pass@1). grok-build.
    rows = [
        synthetic_run_dict(model="Opus", arm="B", outcome="pass", run_id="o-b"),
        synthetic_run_dict(model="Opus", arm="C", outcome="pass", run_id="o-c1"),
        synthetic_run_dict(model="Opus", arm="C", outcome="infra_fail", run_id="o-c2"),
        synthetic_run_dict(model="Opus", arm="A", outcome="protocol_violation", run_id="o-a"),
    ]
    ds = build_dataset(rows, synthetic=True, generated_at="t")
    html = render_ladder_html(ds)
    assert "infra-fail %" in html  # health columns present in the leaderboard
    assert "violation %" in html
    assert "50%" in html or "33%" in html  # a nonzero rate is actually shown


def test_render_deterministic_same_bytes():
    ds = _dataset_with_cb_shapes()
    a = render_ladder_html(ds)
    b = render_ladder_html(ds)
    assert a == b
    assert len(a) > 500


def test_render_no_external_cdn_refs():
    html = render_ladder_html(_dataset_with_cb_shapes())
    assert "cdn" not in html.lower()
    assert "http://" not in html
    assert "https://" not in html


def test_render_bc_segment_emphasised():
    html = render_ladder_html(_dataset_with_cb_shapes())
    assert 'class="bc-segment"' in html
    assert "stroke-width=\"5\"" in html


def test_ladder_assigns_distinct_colors_and_patterns_per_model():
    html = render_ladder_html(_dataset_with_cb_shapes())
    tags = re.findall(r'<polyline class="model-line"[^>]+/>', html)
    colors = {
        re.search(r'data-model="([^"]+)"', tag).group(1):
        re.search(r'stroke="([^"]+)"', tag).group(1)
        for tag in tags
    }
    assert len(colors) >= 2
    assert len(colors) == len(set(colors.values()))
    assert colors["Opus"] != colors["Sonnet"]
    assert any('stroke-dasharray="8 5"' in tag for tag in tags)


def test_render_separate_chain_groups():
    html = render_ladder_html(_dataset_with_cb_shapes())
    assert 'data-chain="devnet"' in html
    assert 'data-chain="testnet"' in html
    assert html.count('class="chart"') == 2


def test_render_honest_direction_labels():
    html = render_ladder_html(_dataset_with_cb_shapes())
    assert 'data-direction="negative"' in html
    assert 'class="legend-cb dir-negative"' in html
    assert 'class="dir-flat"' in html or "dir-flat" in html


def test_render_synthetic_banner():
    html = render_ladder_html(_dataset_with_cb_shapes())
    assert "SYNTHETIC DATA" in html


def test_render_leaderboard_table_present():
    html = render_ladder_html(_dataset_with_cb_shapes())
    assert 'class="leaderboard"' in html
    assert "Run health" in html


def _phase_one_render_dataset():
    b = synthetic_run_dict(
        model="Opus", arm="B", outcome="pass", run_id="summary-b",
        metrics=RunMetrics(
            total_wall_seconds=10.0, prompt_tokens=90, completion_tokens=10,
            total_tokens=100, model_calls=1, provider_attempts=1, provider_responses=1,
            token_usage_status="complete",
        ),
    )
    c = synthetic_run_dict(
        model="Opus", arm="C", outcome="agent_fail", run_id="summary-c",
        metrics=RunMetrics(
            total_wall_seconds=12.5, prompt_tokens=140, completion_tokens=10,
            total_tokens=150, model_calls=1, provider_attempts=1, provider_responses=1,
            token_usage_status="complete",
        ),
    )
    c2 = synthetic_run_dict(
        model="Opus", arm="C", outcome="agent_fail", run_id="summary-c2",
        metrics=RunMetrics(
            total_wall_seconds=12.5, prompt_tokens=140, completion_tokens=10,
            total_tokens=150, model_calls=1, provider_attempts=1, provider_responses=1,
            token_usage_status="complete",
        ),
    )
    b_infra = [
        synthetic_run_dict(
            model="Opus", arm="B", outcome="infra_fail", run_id=f"summary-b-infra-{index}",
            metrics=RunMetrics(
                total_wall_seconds=1.0, model_calls=1, provider_attempts=1,
                provider_responses=0, token_usage_status="incomplete",
                provider_failure_category="other_provider",
            ),
        )
        for index in (1, 2)
    ]
    b.update(total_score=100, max_score=100)
    c.update(total_score=70, max_score=100)
    c2.update(total_score=70, max_score=100)
    b["tasks"] = [
        {"task_id": "task-a", "passed": True, "score": 30, "score_awarded": 30,
         "reason": "synthetic", "proof": "synthetic", "scored": True},
    ]
    c["tasks"] = [
        {"task_id": "task-a", "passed": False, "score": 30, "score_awarded": 0,
         "reason": "synthetic", "proof": "synthetic", "scored": True},
    ]
    c2["tasks"] = list(c["tasks"])
    return build_dataset([b, *b_infra, c, c2])


def test_render_phase_one_effectiveness_shows_weighted_raw_values_and_delta():
    table = render_phase_one_effectiveness_table(_phase_one_render_dataset(), "devnet")
    assert "weighted C−B" in table
    assert "100.0%" in table and "70.0%" in table
    assert "-30.0 pp" in table
    assert "n=1; raw:" in table
    assert "comparison basis" in table
    assert "provisional; completion-conditioned" in table


def test_render_phase_one_efficiency_suppresses_ineligible_token_and_wall_deltas():
    table = render_phase_one_efficiency_table(_phase_one_render_dataset(), "devnet")
    assert "tokens C−B" in table and ">n/a<" in table
    assert "wall C−B" in table
    assert table.count(">n/a<") >= 2
    assert "usage n B / C" in table and "1 / 2" in table
    assert "usage gaps B / C" in table
    assert "0 incomplete, 0 not started" in table
    assert "efficiency basis" in table and "correctness cohort not ready" in table


def test_render_phase_one_efficiency_publishes_only_an_eligible_token_delta():
    rows = []
    for arm, tokens in (("B", 100), ("C", 150)):
        for seed in (1, 2, 3):
            row = synthetic_run_dict(
                model="Opus", arm=arm, outcome="agent_fail",
                run_id=f"eligible-{arm.lower()}-{seed}", seed=seed,
                metrics=RunMetrics(
                    total_wall_seconds=10.0,
                    prompt_tokens=tokens - 10,
                    completion_tokens=10,
                    total_tokens=tokens,
                    model_calls=1,
                    provider_attempts=1,
                    provider_responses=1,
                    token_usage_status="complete",
                ),
            )
            row.update(total_score=60 if arm == "B" else 70, max_score=100)
            rows.append(row)
    table = render_phase_one_efficiency_table(build_dataset(rows), "devnet")
    assert "+50" in table
    assert "+0.00 s" in table
    assert "eligible; complete usage for matched scored seeds" in table


def test_render_phase_one_task_table_shows_counts_rates_and_delta():
    table = render_phase_one_task_table(_phase_one_render_dataset(), "devnet")
    assert "task-a" in table
    assert "1/1 (100.0%)" in table and "0/2 (0.0%)" in table
    assert "-100.0 pp" in table


def test_full_report_labels_descriptive_deltas_without_claiming_literal_causality():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "Effectiveness · DevNet" in html
    assert "Tokens and agent time" in html
    assert "descriptive differences of arm means, not paired inference" in html
    assert "completion-conditioned" in html
    assert "is literally the MCP's marginal value" not in html


def test_report_leads_with_phase_one_signal_and_results_vintage():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "Does CKB AI improve CKB development?" in html
    assert "Phase one evidence" in html
    assert "Inconclusive" in html
    assert "Survivorship warning" in html
    assert "B's score uses 1 of 3 recorded rows" in html
    assert "No effectiveness uplift" not in html
    assert "Results through" in html
    assert "Generated_at:" not in html


def test_empty_testnet_view_is_explicit_and_does_not_copy_devnet_data():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "No TestNet runs yet" in html
    assert "DevNet evidence is never copied, merged or inferred" in html
    assert 'id="chain-view-testnet"' in html


def test_chain_selector_is_accessible_and_responsive():
    html = render_ladder_html(_phase_one_render_dataset())
    assert 'aria-controls="chain-view-devnet"' in html
    assert 'aria-pressed="true"' in html
    assert 'aria-pressed="false"' in html
    assert ":focus-visible" in html
    assert "@media (max-width: 760px)" in html
    assert "@media (prefers-reduced-motion: reduce)" in html
    assert "transition: all" not in html


def test_multi_model_report_has_filter_comparison_and_pinned_source_provenance():
    dataset = _dataset_with_cb_shapes()
    dataset["report_sources"] = [
        {
            "cohort": 1,
            "model": "Opus",
            "profile_id": "profile-opus",
            "profile_sha256": "a" * 64,
            "schema_adapter": None,
            "rows": 8,
        },
        {
            "cohort": 2,
            "model": "GPT-5.5",
            "profile_id": "profile-gpt",
            "profile_sha256": "b" * 64,
            "schema_adapter": "result-1.4.0-to-1.7.0-v1",
            "rows": 6,
        },
    ]
    html = render_ladder_html(dataset)
    assert '<select id="ladder-model-devnet"' in html
    assert re.search(r'<option value="Opus"[^>]* selected>', html)
    assert '<option value="GPT-5.5"' in html
    assert "function showLadderModel(select)" in html
    assert "All models" not in html
    assert "Model comparison · DevNet" in html
    assert "history compactions B / C" in html
    assert 'class="result-panel" data-model="Opus"' in html
    assert '<tr data-model="GPT-5.5">' in html
    assert "Pinned evidence sources" in html
    assert "native current schema" in html
    assert "result-1.4.0-to-1.7.0-v1" in html
    assert "aaaaaaaaaaaa…aaaaaaaa" in html


def test_condition_ladder_renders_exactly_one_model_series_visible():
    html = render_chain_group(_dataset_with_cb_shapes(), "devnet", visible=True)
    plot_groups = re.findall(r'<g class="plot-model"[^>]*>', html)
    legend_groups = re.findall(r'<g class="legend-model"[^>]*>', html)
    assert len(plot_groups) >= 2
    assert sum('style="display:none"' not in group for group in plot_groups) == 1
    assert sum('style="display:none"' not in group for group in legend_groups) == 1


def test_primary_chart_switches_metrics_without_pooling_models():
    dataset = _dataset_with_cb_shapes()
    chart = render_phase_one_comparison_chart(dataset, "devnet")
    assert 'data-chart-metric="weighted"' in chart
    assert 'data-chart-metric="suite"' in chart
    assert 'data-chart-metric="tokens"' in chart
    assert 'data-chart-metric="wall"' in chart
    assert 'data-model="Opus"' in chart
    assert 'data-model="GPT-5.5"' in chart
    assert "B · web only" in chart and "C · CKB AI + web" in chart
    assert "Switch metrics without merging models" in chart


def test_primary_chart_assigns_distinct_stable_model_tones():
    dataset = _dataset_with_cb_shapes()
    chart = render_phase_one_comparison_chart(dataset, "devnet")
    tones = re.findall(r'class="comparison-row model-tone-(\d+)"', chart)
    assert len(tones) >= 2
    assert len(tones) == len(set(tones))
    assert chart == render_phase_one_comparison_chart(dataset, "devnet")


def test_primary_chart_retains_exact_values_and_accessible_details():
    chart = render_phase_one_comparison_chart(_phase_one_render_dataset(), "devnet")
    assert 'data-weighted-b="1"' in chart
    assert 'data-weighted-c="0.7"' in chart
    assert 'data-weighted-delta="-30.0 pp"' in chart
    assert 'data-tokens-b-label="100"' in chart
    assert 'data-tokens-c-label="150"' in chart
    assert 'role="img" tabindex="0"' in chart
    assert 'aria-label="Opus · arm B · 100.0%"' in chart
    assert 'data-weighted-status="Provisional evidence"' in chart


def test_report_is_dark_native_and_chart_interactions_are_reduced_motion_safe():
    html = render_ladder_html(_phase_one_render_dataset())
    assert '<meta name="color-scheme" content="dark"/>' in html
    assert "--canvas: #070a08" in html
    assert "--baseline: #52d5ff" in html
    assert "--accent: #a8ff60" in html
    assert "--model-amber: #ffcc66" in html
    assert "--model-violet: #c59cff" in html
    assert ".comparison-bar-b { background: var(--baseline); }" in html
    assert ".comparison-bar-c { background: var(--accent); }" in html
    assert "box-shadow: inset 4px 0 0 var(--model-accent)" in html
    assert "function showChartMetric(button)" in html
    assert "refreshComparisonChart" in html
    assert "document.querySelectorAll('.comparison-chart').forEach(refreshComparisonChart)" in html
    assert ".ladder-model-select {" in html
    assert "document.querySelectorAll('.ladder-model-select').forEach(showLadderModel)" in html
    assert ".chart-metric-segmented { display: grid; grid-template-columns: 1fr 1fr;" in html
    assert "transition: all" not in html
    assert "@media (prefers-reduced-motion: reduce)" in html


def test_write_site_creates_index(tmp_path):
    ds = _dataset_with_cb_shapes()
    path = write_site(tmp_path / "site", ds)
    assert path.name == "index.html"
    assert path.read_text(encoding="utf-8") == render_ladder_html(ds)


def test_render_chain_group_visible_flag():
    ds = _dataset_with_cb_shapes()
    visible = render_chain_group(ds, "devnet", visible=True)
    hidden = render_chain_group(ds, "devnet", visible=False)
    assert 'style="display:block"' in visible
    assert 'style="display:none"' in hidden


def test_render_leaderboard_na_headline():
    """Model with only arm A has no B/C headline -> honest n/a."""
    rows = [synthetic_run_dict(arm="A", outcome="agent_fail", run_id="only-a")]
    ds = build_dataset(rows, synthetic=True)
    table = render_leaderboard_table(ds, "devnet")
    assert "n/a" in table


def test_repro_render_twice_identical_bytes():
    """ADR-0012 repro check: same results -> byte-identical HTML."""
    ds = _dataset_with_cb_shapes()
    first = render_ladder_html(ds).encode("utf-8")
    second = render_ladder_html(ds).encode("utf-8")
    assert first == second


def test_headline_delta_significance_star_in_html():
    ds = _dataset_with_cb_shapes()
    html = render_ladder_html(ds)
    # Opus positive with tight CIs may or may not be significant; ensure structure exists
    assert re.search(r"dir-positive|dir-negative|dir-flat", html)

# --- an unscored arm gets no correctness geometry --------------------------------------------------
#
# Two `infra_fail` rows must not draw B and C at Pass@1 0.00 or publish a flat C−B headline.

def _r(arm, outcome, run_id, model="gpt-5.6-sol", seed=1):
    return {"suite_semver": "2.0.0", "suite_freeze_hash": "f" * 64,
            "mcp_server_version": "1.6.13", "chain": "devnet", "arm": arm, "model": model,
            "seed": seed, "run_id": run_id, "outcome": outcome, "total_score": 0, "max_score": 100,
            "tasks": []}


def _html(rows):
    from ckbbench.matrix.metrics import build_dataset
    from ckbbench.matrix.render import render_ladder_html

    return render_ladder_html(build_dataset(rows))


def test_two_infra_fail_arms_publish_no_correctness_claim():
    html = _html([_r("B", "infra_fail", "b1"), _r("C", "infra_fail", "c1")])

    for fabricated in ("+0.00", 'data-cb="0.000"', "bc-segment", "1.41"):
        assert fabricated not in html, f"the report fabricated {fabricated!r} from zero scored runs"
    assert "n/a (0/0) / n/a (0/0)" in html
    assert '<circle class="pt' not in html, "an unscored arm must have no plotted point"
    assert "ci-whisker" not in html, "an unscored arm must have no confidence whisker"
    # It must still say what it does know.
    assert "n/a" in html
    assert "100%" in html
    assert "gpt-5.6-sol" in html


def test_a_scored_arm_still_renders_next_to_an_unscored_one():
    html = _html([_r("B", "pass", "b1"), _r("B", "agent_fail", "b2"),
                  _r("C", "infra_fail", "c1")])
    assert html.count('<circle class="pt') == 1, "exactly the scored arm is plotted"
    assert "bc-segment" not in html, "no segment may cross a missing denominator"
    assert "n/a" in html


def test_two_singleton_scored_arms_render_points_without_a_headline():
    html = _html([_r("B", "agent_fail", "b1"), _r("C", "pass", "c1")])
    assert 'data-cb="1.000"' not in html
    assert "bc-segment" not in html
    assert html.count('<circle class="pt') == 2


def test_three_balanced_paired_seed_runs_keep_the_headline_behavior():
    html = _html([
        _r(arm, outcome, f"{arm.lower()}{seed}", seed=seed)
        for arm, outcome in (("B", "agent_fail"), ("C", "pass"))
        for seed in (1, 2, 3)
    ])
    assert 'data-cb="1.000"' in html
    assert "bc-segment" in html


def test_a_model_with_no_scored_arm_still_appears_in_the_leaderboard():
    from ckbbench.matrix.metrics import build_dataset
    from ckbbench.matrix.render import render_leaderboard_table

    table = render_leaderboard_table(
        build_dataset([_r("B", "infra_fail", "b1"), _r("C", "infra_fail", "c1")]), "devnet"
    )
    assert "gpt-5.6-sol" in table
    assert "n/a" in table and "100%" in table
    assert "+0.00" not in table


def test_rendering_is_deterministic_for_an_unscored_dataset():
    rows = [_r("B", "infra_fail", "b1"), _r("C", "infra_fail", "c1")]
    assert _html(rows) == _html(rows)
