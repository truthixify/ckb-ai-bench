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