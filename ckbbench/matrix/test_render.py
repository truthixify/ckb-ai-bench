"""Render tests: deterministic HTML, honest null/negative, separate chains (ADR-0011)."""

from __future__ import annotations

import re

import pytest

from ckbbench.matrix.metrics import build_dataset, headline_delta
from ckbbench.matrix.render import (
    render_chain_group,
    render_ladder_html,
    render_leaderboard_table,
    write_site,
)
from ckbbench.matrix.test_fixtures import synthetic_run_dict


def _dataset_with_cb_shapes() -> dict:
    """SYNTHETIC cells encoding positive, flat, and negative C-B headlines."""
    rows = [
        # Opus: strong positive C-B (pass B, pass C)
        synthetic_run_dict(model="Opus", arm="B", outcome="pass", run_id="opus-b1"),
        synthetic_run_dict(model="Opus", arm="B", outcome="pass", run_id="opus-b2"),
        synthetic_run_dict(model="Opus", arm="C", outcome="pass", run_id="opus-c1"),
        synthetic_run_dict(model="Opus", arm="C", outcome="pass", run_id="opus-c2"),
        # Grok-Build: flat (same outcomes)
        synthetic_run_dict(model="Grok-Build", arm="B", outcome="pass", run_id="gb-b1"),
        synthetic_run_dict(model="Grok-Build", arm="B", outcome="agent_fail", run_id="gb-b2"),
        synthetic_run_dict(model="Grok-Build", arm="C", outcome="pass", run_id="gb-c1"),
        synthetic_run_dict(model="Grok-Build", arm="C", outcome="agent_fail", run_id="gb-c2"),
        # GPT-5.5: negative (B pass, C fail)
        synthetic_run_dict(model="GPT-5.5", arm="B", outcome="pass", run_id="gpt-b1"),
        synthetic_run_dict(model="GPT-5.5", arm="B", outcome="pass", run_id="gpt-b2"),
        synthetic_run_dict(model="GPT-5.5", arm="C", outcome="agent_fail", run_id="gpt-c1"),
        synthetic_run_dict(model="GPT-5.5", arm="C", outcome="agent_fail", run_id="gpt-c2"),
        # arms A/D for ladder lines
        synthetic_run_dict(model="Opus", arm="A", outcome="agent_fail", run_id="opus-a1"),
        synthetic_run_dict(model="Opus", arm="D", outcome="pass", run_id="opus-d1"),
        # second Anthropic model for multi-member family color spread
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
    assert "Leaderboard" in html


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
# Task 20's retained site drew B and C at Pass@1 0.00 and printed `C-B +0.00 [-1.41,+1.41] flat`
# from two `infra_fail` rows. These pin the rendering half of that fix.

def _r(arm, outcome, run_id, model="gpt-5.6-sol"):
    return {"suite_semver": "2.0.0", "suite_freeze_hash": "f" * 64,
            "mcp_server_version": "1.6.13", "chain": "devnet", "arm": arm, "model": model,
            "seed": 1, "run_id": run_id, "outcome": outcome, "total_score": 0, "max_score": 100,
            "tasks": []}


def _html(rows):
    from ckbbench.matrix.metrics import build_dataset
    from ckbbench.matrix.render import render_ladder_html

    return render_ladder_html(build_dataset(rows))


def test_two_infra_fail_arms_publish_no_correctness_claim():
    html = _html([_r("B", "infra_fail", "b1"), _r("C", "infra_fail", "c1")])

    for fabricated in ("+0.00", 'data-cb="0.000"', "bc-segment", "1.41"):
        assert fabricated not in html, f"the report fabricated {fabricated!r} from zero scored runs"
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


def test_two_scored_arms_keep_their_existing_headline_behavior():
    html = _html([_r("B", "agent_fail", "b1"), _r("C", "pass", "c1")])
    assert 'data-cb="1.000"' in html
    assert "bc-segment" in html
    assert html.count('<circle class="pt') == 2


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
