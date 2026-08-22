"""Render tests: deterministic HTML, honest null/negative, separate chains (ADR-0011, ADR-0012)."""

from __future__ import annotations

import re

from ckbbench.matrix.metrics import build_dataset
from ckbbench.matrix.render import render_ladder_html, write_site
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
        # arms A/D for the condition ladder
        synthetic_run_dict(model="Opus", arm="A", outcome="agent_fail", run_id="opus-a1"),
        synthetic_run_dict(model="Opus", arm="D", outcome="pass", run_id="opus-d1"),
        synthetic_run_dict(model="Sonnet", arm="B", outcome="pass", run_id="son-b1"),
        synthetic_run_dict(model="Sonnet", arm="C", outcome="pass", run_id="son-c1"),
        # testnet stays a separate score
        synthetic_run_dict(
            model="Opus", chain="testnet", arm="C", outcome="agent_fail", run_id="opus-tn-c1"
        ),
        synthetic_run_dict(
            model="Opus", chain="testnet", arm="B", outcome="pass", run_id="opus-tn-b1"
        ),
    ]
    return build_dataset(rows, synthetic=True, generated_at="2026-06-13T00:00:00Z")


def _phase_one_render_dataset() -> dict:
    """One completion-conditioned model: B loses two rows to infrastructure, C keeps two."""
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
         "reason": "synthetic", "proof": "SECRET-PROOF-BODY", "scored": True},
    ]
    c["tasks"] = [
        {"task_id": "task-a", "passed": False, "score": 30, "score_awarded": 0,
         "reason": "synthetic", "proof": "SECRET-PROOF-BODY", "scored": True},
    ]
    c2["tasks"] = list(c["tasks"])
    return build_dataset([b, *b_infra, c, c2])


def _r(arm, outcome, run_id, model="gpt-5.6-sol", seed=1):
    return {"suite_semver": "2.0.0", "suite_freeze_hash": "f" * 64,
            "mcp_server_version": "1.6.13", "chain": "devnet", "arm": arm, "model": model,
            "seed": seed, "run_id": run_id, "outcome": outcome, "total_score": 0, "max_score": 100,
            "tasks": []}


def _html(rows):
    return render_ladder_html(build_dataset(rows))


def _evidence_status(html: str) -> str:
    """Just the verdict cards, so methodology prose cannot satisfy a verdict assertion."""
    return html.split("Evidence status")[1].split("</section>")[0]


# --- integrity -------------------------------------------------------------------------------


def test_render_escapes_quotes_in_attributes_no_xss():
    rows = [
        synthetic_run_dict(
            model='x"><script>alert(1)</script>', arm="B", outcome="pass", run_id="m-b",
        ),
        synthetic_run_dict(
            model='x"><script>alert(1)</script>', arm="C", outcome="pass", run_id="m-c",
        ),
    ]
    html = render_ladder_html(build_dataset(rows, synthetic=True, generated_at="t"))
    assert '"><script>' not in html
    assert "&quot;" in html
    assert "<script>alert(1)</script>" not in html
    assert 'data-model="x"' not in html


def test_the_report_never_publishes_a_submitted_proof_body():
    """Outcomes and sanitized reasons are publishable; the artefacts an agent submitted are not."""
    html = render_ladder_html(_phase_one_render_dataset())
    assert "SECRET-PROOF-BODY" not in html


def test_render_deterministic_same_bytes():
    dataset = _dataset_with_cb_shapes()
    first = render_ladder_html(dataset)
    assert first == render_ladder_html(dataset)
    assert len(first) > 500


def test_repro_render_twice_identical_bytes():
    """ADR-0012 repro check: same results -> byte-identical HTML."""
    dataset = _dataset_with_cb_shapes()
    assert render_ladder_html(dataset).encode("utf-8") \
        == render_ladder_html(dataset).encode("utf-8")


def test_rendering_is_deterministic_for_an_unscored_dataset():
    rows = [_r("B", "infra_fail", "b1"), _r("C", "infra_fail", "c1")]
    assert _html(rows) == _html(rows)


def test_render_no_external_cdn_refs():
    html = render_ladder_html(_dataset_with_cb_shapes())
    assert "cdn" not in html.lower()
    assert "http://" not in html
    assert "https://" not in html


def test_render_synthetic_banner():
    assert "SYNTHETIC" in render_ladder_html(_dataset_with_cb_shapes())


def test_write_site_creates_index(tmp_path):
    dataset = _dataset_with_cb_shapes()
    path = write_site(tmp_path / "site", dataset)
    assert path.name == "index.html"
    assert path.read_text(encoding="utf-8") == render_ladder_html(dataset)


# --- structure -------------------------------------------------------------------------------


def test_every_view_is_present_in_the_markup_so_the_report_survives_without_scripting():
    html = render_ladder_html(_dataset_with_cb_shapes())
    for view in ("overview", "models", "tasks", "runs", "methodology", "provenance"):
        assert f'data-view="{view}"' in html
    # The script only switches which view is shown; it never fetches or builds a value.
    assert "fetch(" not in html
    assert "XMLHttpRequest" not in html


def test_the_overview_is_one_unnumbered_spine():
    html = render_ladder_html(_phase_one_render_dataset())
    assert 'data-r="spine"' in html
    # Sections are ordered by the spine itself, not by printed station numbers.
    assert not re.search(r"letter-spacing:\.1em\">\d\d<", html)


def test_render_separate_chain_groups():
    html = render_ladder_html(_dataset_with_cb_shapes())
    assert 'data-chain="devnet"' in html
    assert 'data-chain="testnet"' in html
    assert 'data-chain-set="devnet"' in html


def test_chain_selector_is_accessible_and_responsive():
    html = render_ladder_html(_dataset_with_cb_shapes())
    assert 'role="group" aria-label="Chain"' in html
    assert 'aria-pressed="true"' in html
    assert 'aria-pressed="false"' in html
    assert ":focus-visible" in html
    assert "@media(max-width:920px)" in html
    assert "@media(prefers-reduced-motion:reduce)" in html


def test_report_is_light_native_and_arms_are_legible_without_colour():
    html = render_ladder_html(_phase_one_render_dataset())
    assert '<meta name="color-scheme" content="light">' in html
    assert "background:#f6f4ef" in html
    assert "#17505a" in html
    # B is hatched and C is solid, so the pair survives greyscale and print.
    assert '[data-arm="B"] [data-bar]{background:repeating-linear-gradient' in html
    assert '[data-arm="C"] [data-bar]{background:#17505a' in html
    assert "@media print" in html


# --- the phase-one claim ---------------------------------------------------------------------


def test_report_leads_with_the_phase_one_question_and_a_results_vintage():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "Does CKB AI improve CKB development?" in html
    assert "Evidence status" in html
    assert "Inconclusive" in html
    assert "Results through" in html
    assert "Generated_at:" not in html


def test_full_report_labels_descriptive_deltas_without_claiming_literal_causality():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "descriptive difference" in html or "descriptive" in html
    assert "completion-conditioned" in html
    assert "is literally the MCP's marginal value" not in html
    assert "not a claim of statistical power" in html


def test_an_ineligible_comparison_is_marked_provisional_and_never_promoted():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "Provisional C − B" in html
    assert "not headline-eligible" in html


def test_render_phase_one_effectiveness_shows_weighted_raw_values_and_delta():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "100.0 / 100" in html
    assert "70.0 / 100" in html
    assert "-30.0 points" in html


def test_render_phase_one_efficiency_suppresses_ineligible_token_and_wall_deltas():
    """The dataset withholds these deltas; the report must not recompute them from arm means."""
    html = render_ladder_html(_phase_one_render_dataset())
    assert "withheld — usage cohort ineligible" in html
    assert "+50 tokens" not in html
    assert "+2.5 seconds" not in html


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
    html = render_ladder_html(build_dataset(rows))
    assert "+50 tokens" in html
    assert "withheld — usage cohort ineligible" not in html
    assert "headline-eligible" in html


def test_render_publishes_health_rates():
    """Infrastructure and protocol failures stay visible; they are never folded into Pass@1."""
    rows = [
        synthetic_run_dict(model="Opus", arm="B", outcome="pass", run_id="o-b"),
        synthetic_run_dict(model="Opus", arm="C", outcome="pass", run_id="o-c1"),
        synthetic_run_dict(model="Opus", arm="C", outcome="infra_fail", run_id="o-c2"),
        synthetic_run_dict(model="Opus", arm="A", outcome="protocol_violation", run_id="o-a"),
    ]
    html = render_ladder_html(build_dataset(rows, synthetic=True, generated_at="t"))
    assert "Reliability" in html
    assert "Infra fail" in html
    assert "Protocol" in html
    assert "(50%)" in html


def test_render_phase_one_task_table_shows_counts_and_rates():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "Where B and C differ, task by task" in html
    assert "1/1" in html
    assert "0/2" in html
    assert "100%" in html


def test_multi_model_report_has_comparison_and_pinned_source_provenance():
    dataset = _dataset_with_cb_shapes()
    for run in dataset["runs"]:
        if run["model"] == "Opus":
            run["model_profile_sha256"] = "a" * 64
        elif run["model"] == "GPT-5.5":
            run["model_profile_sha256"] = "b" * 64
    dataset["report_sources"] = [
        {"cohort": 1, "model": "Opus", "profile_id": "profile-opus",
         "profile_sha256": "a" * 64, "model_stability": "dated_snapshot",
         "schema_adapter": None, "rows": 8},
        {"cohort": 2, "model": "GPT-5.5", "profile_id": "profile-gpt",
         "profile_sha256": "b" * 64, "model_stability": "moving_alias",
         "schema_adapter": None, "rows": 6},
    ]
    html = render_ladder_html(dataset)
    assert "Model comparison" in html
    assert "Pinned evidence sources" in html
    assert "native current schema" in html
    assert "Evidence registry" in html
    assert "Opus" in html and "GPT-5.5" in html
    assert "dated snapshot" in html and "moving alias" in html
    assert html.count("Compare C minus B within a model") == 2
    assert html.count("All profiles use high reasoning") == 1
    assert "CKBuilders sets temperature 0 and omits truncation" in html
    assert "OpenRouter omits temperature and disables truncation" in html
    assert "treatment comparison remains controlled within that model" in html
    assert "off / docs-only-v1" in html


def test_condition_ladder_shows_one_model_at_a_time():
    html = render_ladder_html(_dataset_with_cb_shapes())
    # one ladder block starts active per chain; the rest are markup-present but hidden
    for chain in ("devnet", "testnet"):
        section = html.split(f'data-chain="{chain}"')[1].split("</main>")[0]
        blocks = re.findall(r'<div data-ladder="([^"]+)"( class="ladder-on")?', section)
        if blocks:
            assert sum(1 for _, on in blocks if on) == 1
    assert len(re.findall(r'<div data-ladder="', html)) >= 2
    assert "data-ladder-select" in html
    assert ".js [data-ladder]{display:none}" in html


def test_primary_chart_offers_every_metric_without_pooling_models():
    html = render_ladder_html(_dataset_with_cb_shapes())
    for metric in ("weighted", "suite", "tokens", "wall"):
        assert f'data-metric-set="{metric}"' in html
        assert f'data-metric="{metric}"' in html
    # one figure per model per metric: models are never averaged into a single series
    figures = re.findall(r'<figure data-metric="weighted"', html)
    assert len(figures) >= 2


def test_primary_chart_retains_exact_values_and_accessible_details():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "Exact values as a table" in html
    assert "<details" in html and "<summary" in html
    assert "Usage cohort" in html
    assert "n=1" in html


def test_the_run_explorer_lists_every_retained_row_including_excluded_ones():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "Run explorer" in html
    assert "summary-b-infra-1" in html
    assert "Infra fail" in html
    assert "not scored" in html
    assert "5 of 5 retained rows" in html


# --- absence is never drawn as zero ------------------------------------------------------------


def test_two_infra_fail_arms_publish_no_correctness_claim():
    html = _html([_r("B", "infra_fail", "b1"), _r("C", "infra_fail", "c1")])
    for fabricated in ("+0.00", "±0.0 points", "0.0 / 100"):
        assert fabricated not in html, f"the report fabricated {fabricated!r} from 0 scored runs"
    assert "Evidence incomplete" in html
    assert "no data" in html
    assert "gpt-5.6-sol" in html
    assert "(100%)" in html


def test_a_scored_arm_still_renders_next_to_an_unscored_one():
    html = _html([_r("B", "pass", "b1"), _r("B", "agent_fail", "b2"),
                  _r("C", "infra_fail", "c1")])
    assert "no data" in html
    assert "Evidence incomplete" in html
    assert "no difference available" in html


def test_two_singleton_scored_arms_render_points_without_a_headline():
    html = _html([_r("B", "agent_fail", "b1"), _r("C", "pass", "c1")])
    assert "not headline-eligible" in html
    assert "At least 3 scored runs per arm" in html


def test_three_balanced_paired_seed_runs_keep_the_headline_behavior():
    html = _html([
        _r(arm, outcome, f"{arm.lower()}{seed}", seed=seed)
        for arm, outcome in (("B", "agent_fail"), ("C", "pass"))
        for seed in (1, 2, 3)
    ])
    assert "headline-eligible" in html
    # These synthetic rows carry no per-task points, so the weighted read is honestly flat even
    # though Pass@1 separates the arms. The lead status follows weighted score, as it always has.
    assert "No observed difference" in html
    # "Inconclusive" still appears in the methodology prose; what matters is that no verdict
    # card carries it for this model.
    assert ">Inconclusive</span>" not in _evidence_status(html)


def test_budget_exhaustion_is_visible_and_keeps_the_scored_comparison():
    rows = [
        synthetic_run_dict(
            model="Opus",
            arm=arm,
            outcome="agent_fail" if arm == "B" else "pass",
            run_id=f"budget-{arm.lower()}-{seed}",
            seed=seed,
            agent_exit_status="LimitsExceeded" if arm == "B" and seed == 2 else "Submitted",
        )
        for arm in ("B", "C")
        for seed in (1, 2, 3)
    ]
    html = render_ladder_html(build_dataset(rows))
    evidence = _evidence_status(html)

    assert "Budget stops: B 1, C 0; verified scores remain included." in evidence
    assert "Budget stops" in html
    assert "Step limit" in html
    assert "keeps its verified score and remains in the comparison" in html
    assert ">Inconclusive</span>" not in evidence
    assert "headline-eligible descriptive difference" in evidence.lower()


def test_a_model_with_no_scored_arm_still_appears_in_the_report():
    html = _html([_r("B", "infra_fail", "b1"), _r("C", "infra_fail", "c1")])
    assert "gpt-5.6-sol" in html
    assert "no data" in html
    assert "+0.00" not in html


def test_a_model_with_only_arm_a_has_no_bc_headline():
    html = _html([_r("A", "agent_fail", "only-a")])
    assert "no runs recorded" in html
    assert "Observed positive difference" not in html


def test_absent_chains_are_not_rendered_or_offered_as_controls():
    html = render_ladder_html(_phase_one_render_dataset())
    assert "TestNet" not in html
    assert 'data-chain="testnet"' not in html
    assert 'data-chain-set="testnet"' not in html
    assert 'role="group" aria-label="Chain"' not in html
    assert 'data-chain="devnet"' in html


# --- the condition ladder ----------------------------------------------------------------------


def test_the_ladder_plots_weighted_score_for_every_arm_it_has():
    """All four arms are summarised now, so the ladder is not limited to the compared pair."""
    rows = []
    for arm, score in (("A", 20), ("B", 60), ("C", 90), ("D", 40)):
        row = synthetic_run_dict(
            model="Opus", arm=arm, outcome="agent_fail", run_id=f"ladder-{arm}", seed=1,
        )
        row.update(total_score=score, max_score=100)
        rows.append(row)
    html = render_ladder_html(build_dataset(rows))
    assert "Weighted score by condition" in html
    assert "Observed spread" in html
    # y = 100 - value, so every arm lands at its own height rather than on the axis
    points = re.findall(r'<polyline points="([^"]+)"', html)
    assert points == ["12.5,80.00 37.5,40.00 62.5,10.00 87.5,60.00"]


def test_the_ladder_line_breaks_across_an_unrun_arm_instead_of_interpolating():
    rows = []
    for arm, score in (("A", 20), ("B", 60), ("D", 40)):
        row = synthetic_run_dict(
            model="Opus", arm=arm, outcome="agent_fail", run_id=f"gap-{arm}", seed=1,
        )
        row.update(total_score=score, max_score=100)
        rows.append(row)
    html = render_ladder_html(build_dataset(rows))
    points = re.findall(r'<polyline points="([^"]+)"', html)
    # A→B is one segment; D is stranded, so no line reaches it and no point is drawn at zero.
    assert points == ["12.5,80.00 37.5,40.00"]
    assert "no runs recorded" in html
    assert "62.5,100" not in html


def test_the_ladder_whisker_needs_more_than_one_scored_run():
    def cohort(seeds_and_scores, arm):
        out = []
        for seed, score in seeds_and_scores:
            row = synthetic_run_dict(
                model="Opus", arm=arm, outcome="agent_fail",
                run_id=f"spread-{arm}-{seed}", seed=seed,
            )
            row.update(total_score=score, max_score=100)
            out.append(row)
        return out

    single = render_ladder_html(build_dataset(cohort([(1, 60)], "B")))
    assert "not defined at this n" in single

    spread = render_ladder_html(build_dataset(cohort([(1, 0), (2, 0), (3, 100)], "B")))
    # The whisker uses the observed extrema, even when the mean is not their midpoint.
    assert "0.0 – 100.0" in spread
    assert "95% CI" not in spread


# --- drill-down views and copy affordances ------------------------------------------------------


def _detail_dataset() -> dict:
    """One model, both arms, three seeds, with per-task rows so detail pages populate."""
    weights = (("task-01-tip", 10), ("task-05-hashlock", 30), ("task-06-sudt-script", 10))
    rows = []
    for arm in ("B", "C"):
        for seed in (1, 2, 3):
            def passed(tid: str) -> bool:
                return tid != "task-05-hashlock" and not (
                    tid == "task-06-sudt-script" and arm == "B"
                )
            tasks = [
                {"task_id": tid, "passed": passed(tid), "scored": True, "score": w,
                 "score_awarded": w if passed(tid) else 0,
                 "reason": "verifier confirmed the submitted proof" if passed(tid)
                           else "hidden suite failed (exit 101)"}
                for tid, w in weights
            ]
            row = synthetic_run_dict(
                model="Opus", arm=arm, outcome="agent_fail", seed=seed,
                run_id=f"2.0.0-devnet-{arm}-Opus-s{seed}-17873201{seed}0",
                metrics=RunMetrics(
                    total_wall_seconds=500.0, prompt_tokens=900, completion_tokens=100,
                    total_tokens=1000, model_calls=40, provider_attempts=40,
                    provider_responses=40, token_usage_status="complete",
                ),
            )
            row.update(total_score=sum(t["score_awarded"] for t in tasks), max_score=100,
                       agent_exit_status="Submitted", tasks=tasks)
            rows.append(row)
    return build_dataset(rows, generated_at="2026-08-22T06:00:00Z")


def test_every_design_route_including_the_drill_downs_is_rendered():
    html = render_ladder_html(_detail_dataset())
    for view in ("overview", "models", "model", "tasks", "task", "runs", "run",
                 "methodology", "provenance"):
        assert f'data-view="{view}"' in html, f"missing view {view}"


def test_detail_ids_are_unique_so_the_router_cannot_reveal_two_pages():
    html = render_ladder_html(_detail_dataset())
    ids = re.findall(r'data-detail="([^"]+)"', html)
    assert ids, "no detail pages rendered"
    assert len(ids) == len(set(ids)), f"duplicate detail ids: {ids}"


def test_run_detail_is_keyed_by_run_id_not_a_timestamp():
    """Two cells can start in the same second; the run ID is unique by construction."""
    html = render_ladder_html(_detail_dataset())
    ids = [i for i in re.findall(r'data-detail="([^"]+)"', html) if i.startswith("2.0.0-")]
    assert len(ids) == 6
    assert all(not i.isdigit() for i in ids)


def test_list_views_link_into_their_detail_pages():
    html = render_ladder_html(_detail_dataset())
    assert 'href="#/models/Opus"' in html
    assert 'href="#/tasks/task-05-hashlock"' in html
    assert re.search(r'href="#/runs/2\.0\.0-devnet-[BC]-Opus-s\d-\d+"', html)


def test_long_identifiers_are_copyable_not_just_truncated():
    """A shortened digest is unusable if the full value cannot be copied."""
    dataset = _detail_dataset()
    dataset["report_sources"] = [{
        "cohort": "research/x", "model": "Opus", "profile_id": "p1",
        "profile_sha256": "a" * 64, "schema_adapter": None, "rows": 6,
    }]
    html = render_ladder_html(dataset)
    buttons = re.findall(r'<button[^>]*data-copy="([^"]*)"', html)
    assert buttons, "no copy affordance rendered"
    # The full value travels in the attribute, not just the visible truncation.
    assert any(len(v) >= 40 for v in buttons), "no full-length identifier is copyable"
    assert "data-copy-ack" in html, "no live region confirming the copy"
    assert "navigator.clipboard" in html


def test_run_detail_names_the_budget_ceiling_it_stopped_at():
    dataset = _detail_dataset()
    for run in dataset["runs"]:
        run["agent_exit_status"] = "LimitsExceeded"
    html = render_ladder_html(dataset)
    assert "agent stopped at the step or cost ceiling" in html
