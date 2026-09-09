from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ckbbench.run.attempt_store import AttemptStore
from ckbbench.run.campaign import CampaignBatch, execution_plan_sha256, publish_document
from ckbbench.run.campaign_operator import CampaignOperator, main, resolve_accepted_report
from ckbbench.run.campaign_report import ReportBuilderSource, build_campaign_report_dataset
from ckbbench.run.model_profile import model_variant_id
from ckbbench.run.publication import (
    CampaignPublicationDataset,
    PublicationError,
    build_campaign_publication_dataset,
    load_campaign_publication_dataset,
    publish_campaign_publication,
    render_campaign_publication,
)
from ckbbench.run.report_site import _arm_usage
from ckbbench.run.test_campaign import _manifest
from ckbbench.run.test_campaign_operator import Runtime
from ckbbench.run.test_suite_release import CHAIN, _surface
from ckbbench.run.task_attempt import canonical_json_bytes


SOURCE = ReportBuilderSource("a" * 40, "b" * 64)


class Binding:
    chain_profiles = (CHAIN,)
    treatment_profiles = (_surface("B"), _surface("C"))

    def validate_manifest(self, selected):
        identities = {
            (profile.profile_id, profile.sha256) for profile in self.treatment_profiles
        }
        assert {
            (slot.treatment_profile_id, slot.treatment_profile_sha256)
            for slot in selected.slots
        } == identities


def _dataset(
    root: Path,
    *,
    marker: str,
    model: str,
    attempt_offset: int,
    changed_budget: bool = False,
    profile_marker: str | None = None,
):
    base = _manifest()
    control, treatment = Binding.treatment_profiles
    surfaces = {"B": control, "C": treatment}
    selected_profile_marker = profile_marker or marker
    profile_id = f"model-profile-{selected_profile_marker}-v1"
    profile_sha256 = selected_profile_marker * 64
    variant = model_variant_id(
        requested_model=model,
        thinking_level="high",
        profile_id=profile_id,
        profile_sha256=profile_sha256,
    )
    slots = []
    for slot in base.slots:
        budget = slot.budget
        if changed_budget:
            budget = replace(
                budget,
                profile_id=f"{budget.profile_id}-changed",
                profile_sha256="9" * 64,
                step_limit=budget.step_limit + 1,
            )
        slots.append(replace(
            slot,
            slot_id=f"{slot.slot_id}-{marker}",
            trial_id=f"{slot.trial_id}-{marker}",
            chain_track=CHAIN.chain_track,
            chain_profile_id=CHAIN.profile_id,
            chain_profile_sha256=CHAIN.sha256,
            treatment_profile_id=surfaces[slot.arm].profile_id,
            treatment_profile_sha256=surfaces[slot.arm].sha256,
            requested_model=model,
            model_variant_id=variant,
            model_profile_id=profile_id,
            model_profile_sha256=profile_sha256,
            budget=budget,
        ))
    selected_slots = tuple(slots)
    batches = (CampaignBatch("batch-a", tuple(slot.slot_id for slot in selected_slots)),)
    manifest = replace(
        base,
        campaign_id="campaign-" + marker * 32,
        execution_plan_id=f"execution-plan-{marker}",
        execution_plan_sha256=execution_plan_sha256(batches, selected_slots),
        batches=batches,
        slots=selected_slots,
    )
    store = AttemptStore(root / "attempts")
    runtime = Runtime()
    runtime.counter = attempt_offset
    CampaignOperator(manifest, store, runtime, root / "coordination").run_batch("batch-a")
    resolution = resolve_accepted_report(manifest, store)
    dataset = build_campaign_report_dataset(
        manifest,
        resolution,
        store,
        SOURCE,
        Binding(),  # type: ignore[arg-type]
    )
    return manifest, store, resolution, dataset


def test_publication_is_order_independent_attribution_preserving_and_self_contained(
    tmp_path: Path,
):
    first = _dataset(
        tmp_path / "first",
        marker="a",
        model="provider/model-a",
        attempt_offset=0,
    )[3]
    second = _dataset(
        tmp_path / "second",
        marker="b",
        model="provider/model-b",
        attempt_offset=100,
    )[3]

    forward = build_campaign_publication_dataset((first, second), SOURCE)
    reverse = build_campaign_publication_dataset((second, first), SOURCE)
    assert forward == reverse
    assert [
        row["campaign_id"] for row in forward.to_dict()["campaigns"]
    ] == ["campaign-" + "a" * 32, "campaign-" + "b" * 32]

    site = render_campaign_publication(forward)
    assert b"2 campaigns" in site
    assert b"provider/model-a" in site and b"provider/model-b" in site
    assert ("campaign-" + "a" * 32).encode("ascii") in site
    assert ("campaign-" + "b" * 32).encode("ascii") in site
    assert b"http://" not in site and b"https://" not in site
    assert b'"proof"' not in forward.canonical_bytes

    first_hashes = publish_campaign_publication(tmp_path / "site-a", forward)
    second_hashes = publish_campaign_publication(tmp_path / "site-b", reverse)
    assert first_hashes == second_hashes
    assert (tmp_path / "site-a" / "dataset.json").read_bytes() == (
        tmp_path / "site-b" / "dataset.json"
    ).read_bytes()
    assert (tmp_path / "site-a" / "index.html").read_bytes() == (
        tmp_path / "site-b" / "index.html"
    ).read_bytes()
    assert load_campaign_publication_dataset(tmp_path / "site-a" / "dataset.json") == forward


def test_publication_uses_the_established_routed_report_contract(tmp_path: Path):
    first = _dataset(
        tmp_path / "first",
        marker="a",
        model="provider/model-a",
        attempt_offset=0,
    )[3]
    second = _dataset(
        tmp_path / "second",
        marker="b",
        model="provider/model-b",
        attempt_offset=100,
    )[3]
    site = render_campaign_publication(
        build_campaign_publication_dataset((first, second), SOURCE)
    )

    assert b"<title>CKB AI Bench</title>" in site
    assert site.count(b'<div data-report-view="') == 9
    for route in (b"overview", b"models", b"tasks", b"runs", b"methodology", b"provenance"):
        assert b'data-report-view="' + route + b'"' in site
        assert b'data-nav="' + route + b'"' in site
    for route in (b"model", b"task", b"run"):
        assert b'data-report-view="' + route + b'"' in site
    assert b'<main data-detail="' in site
    assert b'href="#/models/' in site
    assert b'href="#/tasks/' in site
    assert b'href="#/runs/' in site
    assert b'data-theme-toggle' in site
    assert b'data-track-set=' in site
    assert b'data-r="spine"' in site
    assert b'data-hero-plot' in site
    assert b'data-hero-tooltip' in site
    assert b'data-hero-sort="score"' in site
    assert b'data-hero-sort="delta"' in site
    assert b'data-hero-sort="tokens"' in site
    assert site.count(b'<div data-arm="B" data-hero-point=') == 2
    assert site.count(b'<div data-arm="C" data-hero-point=') == 2
    assert b'data-comparison-scope' in site
    for metric in (b"score", b"tokens", b"time"):
        assert b'data-metric-set="' + metric + b'"' in site
        assert b'data-metric="' + metric + b'"' in site
    assert b"Exact values as a table" in site
    assert b"Comparison basis" in site
    for station in range(8):
        assert f">{station:02d}</div>".encode("ascii") in site
    for heading in (
        b"Evidence status",
        b"B versus C",
        b"Model comparison",
        b"Where B and C differ, task by task",
        b"Efficiency",
        b"Reliability",
        b"Condition ladder",
        b"Pinned evidence sources",
    ):
        assert heading in site
    assert b"Run explorer" in site
    assert b"Evidence registry" in site
    assert b"Retry policy" in site and b"Stopping rule" in site
    assert b"Chain profile" in site and b"Treatment profile" in site
    assert b"@media(prefers-reduced-motion:reduce)" in site
    assert b":focus-visible" in site

    lower = site.lower()
    legacy_question = b"does ckb ai " + b"improve ckb development?"
    rejected_copy = b"the same model runs " + b"the same frozen suite twice"
    provider_brand = b"ck" + b"builders"
    assert legacy_question not in lower
    assert rejected_copy not in lower
    assert provider_brand not in lower


def test_report_site_treats_a_reported_zero_cost_as_complete():
    summary = {
        "_campaign_id": "campaign-" + "a" * 32,
        "model_variant_id": "mv1-" + "b" * 64,
        "chain_track": "testnet",
    }
    acquisition = {
        **summary,
        "arm": "B",
        "total_tokens": 10,
        "token_status": "complete",
        "observed_cost_usd": "0",
        "cost_status": "complete",
        "model_calls": 1,
        "provider_attempts": 1,
        "provider_responses": 1,
        "provider_retry_count": 0,
        "timings": {"agent_seconds": 1.0},
    }

    usage = _arm_usage([acquisition], summary, "B")

    assert usage["cost"] == 0
    assert usage["cost_status"] == "complete"


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda row: row.update(extra=True), "reviewed fields"),
        (lambda row: row["campaigns"].reverse(), "sorted"),
        (
            lambda row: row["campaigns"][0].update(dataset_sha256="9" * 64),
            "digest does not match",
        ),
        (
            lambda row: row["campaigns"][0].update(campaign_id="campaign-" + "f" * 32),
            "ID does not match",
        ),
        (
            lambda row: row["campaigns"][0]["dataset"]["attempts"][0].update(
                score_awarded=1
            ),
            "source dataset is invalid",
        ),
    ],
)
def test_publication_refuses_schema_source_and_derivation_tampering(
    tmp_path: Path,
    mutation,
    match: str,
):
    datasets = (
        _dataset(tmp_path / "first", marker="a", model="provider/a", attempt_offset=0)[3],
        _dataset(tmp_path / "second", marker="b", model="provider/b", attempt_offset=100)[3],
    )
    document = build_campaign_publication_dataset(datasets, SOURCE).to_dict()
    mutation(document)
    with pytest.raises(PublicationError, match=match):
        CampaignPublicationDataset.from_dict(document)


def test_publication_refuses_unvalidated_incompatible_and_colliding_sources(tmp_path: Path):
    released = _dataset(
        tmp_path / "released",
        marker="a",
        model="provider/a",
        attempt_offset=0,
    )[3]
    unvalidated_manifest = _manifest()
    unvalidated_store = AttemptStore(tmp_path / "unvalidated" / "attempts")
    CampaignOperator(
        unvalidated_manifest,
        unvalidated_store,
        Runtime(),
        tmp_path / "unvalidated" / "coordination",
    ).run_batch("batch-a")
    unvalidated = build_campaign_report_dataset(
        unvalidated_manifest,
        resolve_accepted_report(unvalidated_manifest, unvalidated_store),
        unvalidated_store,
        SOURCE,
    )
    with pytest.raises(PublicationError, match="validated release"):
        build_campaign_publication_dataset((unvalidated,), SOURCE)

    incompatible = _dataset(
        tmp_path / "incompatible",
        marker="b",
        model="provider/b",
        attempt_offset=100,
        changed_budget=True,
    )[3]
    with pytest.raises(PublicationError, match="not comparable"):
        build_campaign_publication_dataset((released, incompatible), SOURCE)

    collisions = _dataset(
        tmp_path / "collisions",
        marker="b",
        model="provider/b",
        attempt_offset=0,
    )[3]
    with pytest.raises(PublicationError, match="attempt IDs"):
        build_campaign_publication_dataset((released, collisions), SOURCE)


def test_publication_refuses_duplicate_model_variants_and_builder_drift(tmp_path: Path):
    first = _dataset(
        tmp_path / "first",
        marker="a",
        model="provider/a",
        attempt_offset=0,
    )[3]
    duplicate_model = _dataset(
        tmp_path / "second",
        marker="b",
        model="provider/a",
        attempt_offset=100,
        profile_marker="a",
    )[3]
    with pytest.raises(PublicationError, match="model variants"):
        build_campaign_publication_dataset((first, duplicate_model), SOURCE)

    with pytest.raises(PublicationError, match="same report builder"):
        build_campaign_publication_dataset(
            (first,),
            ReportBuilderSource("c" * 40, "d" * 64),
        )


def test_publication_cli_reopens_conventional_resolution_and_attempt_store(tmp_path: Path):
    repository = tmp_path / "repository"
    campaign_root = repository / "campaigns"
    campaign_id = "campaign-" + "a" * 32
    directory = campaign_root / campaign_id
    manifest, _store, resolution, expected = _dataset(
        directory,
        marker="a",
        model="provider/a",
        attempt_offset=0,
    )
    (directory / "campaign.json").write_bytes(canonical_json_bytes(manifest.to_dict()))
    publish_document(
        directory / "report-resolution.json",
        resolution.to_dict(),
        "accepted report resolution",
    )
    output = repository / "publication"
    stdout = io.StringIO()

    assert main(
        [
            "build-publication",
            "--campaign", campaign_id,
            "--campaign-root", "campaigns",
            "--repository-root", str(repository),
            "--output", str(output),
        ],
        release_binding=Binding(),  # type: ignore[arg-type]
        report_builder_source=SOURCE,
        stdout=stdout,
        stderr=io.StringIO(),
        coordination_root=tmp_path / "coordination",
    ) == 0
    assert "accepted campaign publication campaigns=1" in stdout.getvalue()
    publication = load_campaign_publication_dataset(output / "dataset.json")
    assert publication.to_dict()["campaigns"][0]["dataset_sha256"] == expected.sha256

    stderr = io.StringIO()
    assert main(
        [
            "build-publication",
            "--campaign", campaign_id,
            "--campaign-root", "campaigns",
            "--repository-root", str(repository),
            "--output", str(directory / "attempts" / "site"),
        ],
        release_binding=Binding(),  # type: ignore[arg-type]
        report_builder_source=SOURCE,
        stdout=io.StringIO(),
        stderr=stderr,
        coordination_root=tmp_path / "coordination",
    ) == 1
    assert "outside" in stderr.getvalue()


def test_publication_cli_discovers_completed_campaigns_under_the_root(tmp_path: Path):
    repository = tmp_path / "repository"
    campaign_root = repository / "campaigns"
    for marker, model, offset in (
        ("a", "provider/model-a", 0),
        ("b", "provider/model-b", 100),
    ):
        directory = campaign_root / ("campaign-" + marker * 32)
        manifest, _store, resolution, _dataset_document = _dataset(
            directory,
            marker=marker,
            model=model,
            attempt_offset=offset,
        )
        (directory / "campaign.json").write_bytes(canonical_json_bytes(manifest.to_dict()))
        publish_document(
            directory / "report-resolution.json",
            resolution.to_dict(),
            "accepted report resolution",
        )
    (campaign_root / ("campaign-" + "c" * 32)).mkdir()
    output = repository / "publication"
    stdout = io.StringIO()

    assert main(
        [
            "build-publication",
            "--campaign-root", "campaigns",
            "--repository-root", str(repository),
            "--output", str(output),
        ],
        release_binding=Binding(),  # type: ignore[arg-type]
        report_builder_source=SOURCE,
        stdout=stdout,
        stderr=io.StringIO(),
        coordination_root=tmp_path / "coordination",
    ) == 0
    assert "accepted campaign publication campaigns=2" in stdout.getvalue()
    publication = load_campaign_publication_dataset(output / "dataset.json")
    assert [
        row["campaign_id"] for row in publication.to_dict()["campaigns"]
    ] == ["campaign-" + "a" * 32, "campaign-" + "b" * 32]


def test_publication_loader_and_output_refuse_unsafe_paths(tmp_path: Path):
    dataset = build_campaign_publication_dataset((
        _dataset(tmp_path / "campaign", marker="a", model="provider/a", attempt_offset=0)[3],
    ), SOURCE)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(PublicationError, match="already exist"):
        publish_campaign_publication(existing, dataset)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PublicationError, match="already exist"):
        publish_campaign_publication(link, dataset)

    pretty = tmp_path / "pretty.json"
    pretty.write_text(json.dumps(dataset.to_dict(), indent=2), encoding="ascii")
    with pytest.raises(PublicationError, match="canonical"):
        load_campaign_publication_dataset(pretty)

    dataset_link = tmp_path / "dataset-link.json"
    dataset_link.symlink_to(pretty)
    with pytest.raises(PublicationError, match="regular non-symlink"):
        load_campaign_publication_dataset(dataset_link)
