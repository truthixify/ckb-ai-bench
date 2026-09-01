from __future__ import annotations

import io
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ckbbench.run.attempt_store import AttemptStore
from ckbbench.run.campaign import (
    CampaignBatch,
    CampaignManifest,
    execution_plan_sha256,
    publish_document,
)
from ckbbench.run.campaign_operator import (
    CampaignOperator,
    main,
    resolve_accepted_report,
)
from ckbbench.run.campaign_report import (
    CampaignReportDataset,
    CampaignReportError,
    ReportBuilderSource,
    build_campaign_report_dataset,
    load_campaign_report_dataset,
    publish_campaign_report,
    render_campaign_report,
    resolve_report_builder_source,
)
from ckbbench.run.model_profile import model_variant_id
from ckbbench.run.task_attempt import canonical_json_bytes
from ckbbench.run.test_campaign_operator import Probe, Runtime, _operator
from ckbbench.run.test_suite_release import CHAIN, _surface


SOURCE = ReportBuilderSource("a" * 40, "b" * 64)


def _complete(tmp_path: Path, outcomes=None):
    manifest, store, _runtime, operator = _operator(
        tmp_path,
        Runtime(outcomes or {}),
    )
    operator.run_batch("batch-a")
    return manifest, store, resolve_accepted_report(manifest, store)


def _dataset(tmp_path: Path, outcomes=None):
    manifest, store, resolution = _complete(tmp_path, outcomes)
    return (
        manifest,
        store,
        resolution,
        build_campaign_report_dataset(manifest, resolution, store, SOURCE),
    )


def test_report_is_deterministic_and_preserves_retry_acquisition_usage(tmp_path: Path):
    manifest, store, resolution, first = _dataset(
        tmp_path,
        {("slot-2", 0): "infra_fail"},
    )
    second = build_campaign_report_dataset(manifest, resolution, store, SOURCE)

    assert first.canonical_bytes == second.canonical_bytes
    document = first.to_dict()
    assert len(document["attempts"]) == 5
    assert len(document["slot_acquisitions"]) == 4
    lineage = next(row for row in document["slot_acquisitions"] if row["slot_id"] == "slot-2")
    assert lineage["retry_count"] == 1
    assert len(lineage["attempt_ids"]) == 2
    assert lineage["infrastructure_failure_attempts"] == 1
    assert lineage["terminal_outcome"] == "pass"
    assert lineage["token_status"] == "incomplete"
    assert lineage["cost_status"] == "lower_bound"
    assert lineage["observed_cost_usd"] == "0.01"
    assert lineage["controller_request_count_status"] == "exact"
    assert lineage["controller_request_count"] is not None
    summary = document["variant_summaries"][0]
    assert summary["arms"]["C"]["infra_failures"] == 1
    assert summary["arms"]["C"]["correctness_observations"] == 2
    assert summary["matched"]["correctness_pairs"] == 2
    assert summary["matched"]["c_minus_b_score_percent"] == 0.0
    assert summary["acquisition_cost_status"] == "lower_bound"
    assert summary["attempt_ids"] == [
        attempt["attempt_id"] for attempt in document["attempts"]
    ]
    assert summary["slot_ids"] == [
        acquisition["slot_id"] for acquisition in document["slot_acquisitions"]
    ]

    first_hashes = publish_campaign_report(tmp_path / "first-site", first)
    second_hashes = publish_campaign_report(tmp_path / "second-site", second)
    assert first_hashes == second_hashes
    assert (tmp_path / "first-site" / "dataset.json").read_bytes() == (
        tmp_path / "second-site" / "dataset.json"
    ).read_bytes()
    assert (tmp_path / "first-site" / "index.html").read_bytes() == (
        tmp_path / "second-site" / "index.html"
    ).read_bytes()
    assert load_campaign_report_dataset(tmp_path / "first-site" / "dataset.json") == first


def test_terminal_infrastructure_failure_is_health_not_correctness(tmp_path: Path):
    class SourceDriftRuntime(Runtime):
        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            if slot.slot_id == "slot-1" and predecessor is None:
                assert isinstance(prepared.preflight_probe, Probe)
                prepared.preflight_probe.source_value = replace(
                    prepared.preflight_probe.source_value,
                    tracked_change_count=1,
                )
            return prepared

    manifest = _operator(tmp_path)[0]
    store = AttemptStore(tmp_path / "attempts")
    operator = CampaignOperator(
        manifest,
        store,
        SourceDriftRuntime(),
        tmp_path / "coordination",
    )
    operator.run_batch("batch-a")
    resolution = resolve_accepted_report(manifest, store)
    document = build_campaign_report_dataset(
        manifest,
        resolution,
        store,
        SOURCE,
    ).to_dict()

    terminal = document["slot_acquisitions"][0]
    assert terminal["terminal_outcome"] == "infra_fail"
    assert not terminal["terminal_correctness_eligible"]
    task = next(
        row for row in document["task_comparisons"] if row["task_id"] == "task-read-tip"
    )
    assert task["arms"]["B"]["correctness_observations"] == 0
    assert task["arms"]["B"]["score_possible"] == 0
    assert task["matched"]["comparison_status"] == "withheld"
    assert task["matched"]["c_minus_b_score_percent"] is None
    summary = document["variant_summaries"][0]
    assert summary["matched"]["correctness_pairs"] == 1
    assert summary["matched"]["pairs"] == 2
    assert summary["matched"]["comparison_status"] == "withheld"
    assert summary["matched"]["score_percent_b"] is None
    assert summary["matched"]["score_percent_c"] is None


def test_all_preflight_failures_preserve_not_started_usage_status(tmp_path: Path):
    class SourceDriftRuntime(Runtime):
        def prepare(self, manifest, slot, predecessor):
            prepared = super().prepare(manifest, slot, predecessor)
            assert predecessor is None
            assert isinstance(prepared.preflight_probe, Probe)
            prepared.preflight_probe.source_value = replace(
                prepared.preflight_probe.source_value,
                tracked_change_count=1,
            )
            return prepared

    manifest = _operator(tmp_path)[0]
    store = AttemptStore(tmp_path / "attempts")
    operator = CampaignOperator(
        manifest,
        store,
        SourceDriftRuntime(),
        tmp_path / "coordination",
    )
    operator.run_batch("batch-a")
    resolution = resolve_accepted_report(manifest, store)
    summary = build_campaign_report_dataset(
        manifest,
        resolution,
        store,
        SOURCE,
    ).to_dict()["variant_summaries"][0]

    assert summary["acquisition_token_status"] == "not_started"
    assert summary["acquisition_total_tokens"] is None
    assert summary["acquisition_cost_status"] == "unavailable"
    assert summary["acquisition_observed_cost_usd"] is None


def _two_variant_manifest() -> CampaignManifest:
    base = _operator(Path("unused"))[0]
    profile_id = "model-profile-synthetic-medium-v1"
    profile_sha = "2" * 64
    variant = model_variant_id(
        requested_model="provider/synthetic-model",
        thinking_level="medium",
        profile_id=profile_id,
        profile_sha256=profile_sha,
    )
    slots = tuple(
        replace(
            slot,
            chain_track="testnet",
            chain_profile_id="testnet-profile-v1",
            chain_profile_sha256="9" * 64,
            thinking_level="medium",
            model_variant_id=variant,
            model_profile_id=profile_id,
            model_profile_sha256=profile_sha,
        )
        if slot.trial_id == "trial-2"
        else slot
        for slot in base.slots
    )
    batches = (CampaignBatch("batch-a", tuple(slot.slot_id for slot in slots)),)
    return replace(
        base,
        execution_plan_sha256=execution_plan_sha256(batches, slots),
        batches=batches,
        slots=slots,
    )


def test_models_thinking_levels_and_chains_are_never_pooled(tmp_path: Path):
    manifest = _two_variant_manifest()
    store = AttemptStore(tmp_path / "attempts")
    operator = CampaignOperator(
        manifest,
        store,
        Runtime(),
        tmp_path / "coordination",
    )
    operator.run_batch("batch-a")
    resolution = resolve_accepted_report(manifest, store)
    document = build_campaign_report_dataset(
        manifest,
        resolution,
        store,
        SOURCE,
    ).to_dict()

    assert len(document["variant_summaries"]) == 2
    assert {(row["thinking_level"], row["chain_track"]) for row in document["variant_summaries"]} == {
        ("high", "local-hermetic"),
        ("medium", "testnet"),
    }
    by_thinking = {row["thinking_level"]: row for row in document["variant_summaries"]}
    assert by_thinking["high"]["matched"]["correctness_pairs"] == 1
    assert by_thinking["medium"]["matched"]["correctness_pairs"] == 0
    assert by_thinking["medium"]["matched"]["comparison_status"] == "withheld"
    assert len(document["task_comparisons"]) == 2


def test_task_content_and_budget_versions_are_never_pooled(tmp_path: Path):
    base = _operator(tmp_path)[0]
    slots = tuple(
        replace(slot, task_id="task-read-tip") if slot.trial_id == "trial-2" else slot
        for slot in base.slots
    )
    batches = (CampaignBatch("batch-a", tuple(slot.slot_id for slot in slots)),)
    manifest = replace(
        base,
        execution_plan_sha256=execution_plan_sha256(batches, slots),
        batches=batches,
        slots=slots,
    )
    store = AttemptStore(tmp_path / "attempts")
    operator = CampaignOperator(manifest, store, Runtime(), tmp_path / "coordination")
    operator.run_batch("batch-a")
    resolution = resolve_accepted_report(manifest, store)
    tasks = build_campaign_report_dataset(
        manifest,
        resolution,
        store,
        SOURCE,
    ).to_dict()["task_comparisons"]

    assert len(tasks) == 2
    assert {row["task_id"] for row in tasks} == {"task-read-tip"}
    assert len({row["task_content_sha256"] for row in tasks}) == 2
    assert len({row["budget"]["profile_sha256"] for row in tasks}) == 2


def test_validated_release_profiles_are_bound_and_visible(tmp_path: Path):
    base = _operator(tmp_path)[0]
    control = _surface("B")
    treatment = _surface("C")
    surfaces = {"B": control, "C": treatment}
    slots = tuple(
        replace(
            slot,
            chain_track=CHAIN.chain_track,
            chain_profile_id=CHAIN.profile_id,
            chain_profile_sha256=CHAIN.sha256,
            treatment_profile_id=surfaces[slot.arm].profile_id,
            treatment_profile_sha256=surfaces[slot.arm].sha256,
        )
        for slot in base.slots
    )
    batches = (CampaignBatch("batch-a", tuple(slot.slot_id for slot in slots)),)
    manifest = replace(
        base,
        execution_plan_sha256=execution_plan_sha256(batches, slots),
        batches=batches,
        slots=slots,
    )
    store = AttemptStore(tmp_path / "attempts")
    operator = CampaignOperator(manifest, store, Runtime(), tmp_path / "coordination")
    operator.run_batch("batch-a")
    resolution = resolve_accepted_report(manifest, store)

    class Binding:
        chain_profiles = (CHAIN,)
        treatment_profiles = (control, treatment)

        def validate_manifest(self, selected):
            assert selected == manifest

    dataset = build_campaign_report_dataset(
        manifest,
        resolution,
        store,
        SOURCE,
        Binding(),  # type: ignore[arg-type]
    )
    document = dataset.to_dict()
    assert document["profiles"]["release_validated"]
    assert document["profiles"]["chain_profiles"][0]["sha256"] == CHAIN.sha256
    assert {row["sha256"] for row in document["profiles"]["treatment_profiles"]} == {
        control.sha256,
        treatment.sha256,
    }
    site = render_campaign_report(dataset)
    assert CHAIN.profile_id.encode("ascii") in site
    assert control.profile_id.encode("ascii") in site
    assert treatment.profile_id.encode("ascii") in site


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update(extra=True),
        lambda row: row["attempts"].reverse(),
        lambda row: row["attempts"][0].update(score_awarded=1),
        lambda row: row["slot_acquisitions"][0].update(provider_attempts=999),
        lambda row: row["variant_summaries"][0]["matched"].update(correctness_pairs=999),
        lambda row: row.update(methodology={}),
        lambda row: row["report_builder"].update(source_tree_sha256="z" * 64),
        lambda row: row["resolution"].update(attempt_count=999),
    ],
)
def test_dataset_refuses_schema_and_derived_value_tampering(tmp_path: Path, mutate):
    document = _dataset(tmp_path)[3].to_dict()
    mutate(document)
    with pytest.raises(CampaignReportError):
        CampaignReportDataset.from_dict(document)


def test_report_omits_submitted_proof_and_has_no_external_dependencies(tmp_path: Path):
    dataset = _dataset(tmp_path)[3]
    data = dataset.canonical_bytes
    site = render_campaign_report(dataset)

    assert b'"proof"' not in data
    assert b">proof<" not in site
    assert b"http://" not in site and b"https://" not in site
    assert b"<script src=" not in site and b"<link rel=" not in site
    assert b"thinking_level" not in site
    assert b"Model / thinking" in site


def test_report_publication_refuses_existing_and_symlink_destinations(tmp_path: Path):
    dataset = _dataset(tmp_path / "campaign")[3]
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(CampaignReportError, match="already exist"):
        publish_campaign_report(existing, dataset)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CampaignReportError, match="already exist"):
        publish_campaign_report(link, dataset)

    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CampaignReportError, match="parent"):
        publish_campaign_report(parent_link / "site", dataset)
    with pytest.raises(CampaignReportError, match="parent"):
        publish_campaign_report(parent_link / "missing" / "site", dataset)


def test_manual_cli_consumes_the_published_resolution_and_never_runs_runtime(tmp_path: Path):
    manifest, store, resolution = _complete(tmp_path / "campaign")
    manifest_path = tmp_path / "manifest.json"
    resolution_path = tmp_path / "resolution.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    publish_document(resolution_path, resolution.to_dict(), "resolution")
    output = tmp_path / "site"
    stdout = io.StringIO()

    assert main(
        [
            "build-report",
            "--manifest", str(manifest_path),
            "--attempt-root", str(store.root),
            "--resolution", str(resolution_path),
            "--output", str(output),
        ],
        runtime=None,
        report_builder_source=SOURCE,
        stdout=stdout,
        stderr=io.StringIO(),
    ) == 0
    assert "accepted campaign report" in stdout.getvalue()
    assert load_campaign_report_dataset(output / "dataset.json").to_dict()["resolution"][
        "sha256"
    ] == resolution.sha256

    stderr = io.StringIO()
    assert main(
        [
            "build-report",
            "--manifest", str(manifest_path),
            "--attempt-root", str(store.root),
            "--resolution", str(resolution_path),
            "--output", str(store.root / "site"),
        ],
        runtime=None,
        report_builder_source=SOURCE,
        stdout=io.StringIO(),
        stderr=stderr,
    ) == 1
    assert "outside" in stderr.getvalue()


def test_report_refuses_a_resolution_that_does_not_match_retained_evidence(tmp_path: Path):
    manifest, store, resolution = _complete(tmp_path)
    altered = replace(
        resolution,
        slots=(resolution.slots[1], resolution.slots[0], *resolution.slots[2:]),
    )
    with pytest.raises(ValueError):
        build_campaign_report_dataset(manifest, altered, store, SOURCE)


def test_git_builder_source_binds_the_clean_tracked_tree_only(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q", str(repo)), check=True)
    subprocess.run(("git", "-C", str(repo), "config", "user.email", "test@example.com"), check=True)
    subprocess.run(("git", "-C", str(repo), "config", "user.name", "Test"), check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("first\n", encoding="ascii")
    subprocess.run(("git", "-C", str(repo), "add", "tracked.txt"), check=True)
    subprocess.run(("git", "-C", str(repo), "commit", "-qm", "Initial"), check=True)

    first = resolve_report_builder_source(repo)
    (repo / "untracked.txt").write_text("ignored\n", encoding="ascii")
    assert resolve_report_builder_source(repo) == first
    tracked.write_text("changed\n", encoding="ascii")
    with pytest.raises(CampaignReportError, match="tracked tree"):
        resolve_report_builder_source(repo)


def test_dataset_loader_refuses_noncanonical_duplicate_and_nonfile_inputs(tmp_path: Path):
    dataset = _dataset(tmp_path / "campaign")[3]
    pretty = tmp_path / "pretty.json"
    pretty.write_text(json.dumps(dataset.to_dict(), indent=2), encoding="ascii")
    with pytest.raises(CampaignReportError, match="canonical"):
        load_campaign_report_dataset(pretty)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"x","schema_version":"y"}\n', encoding="ascii")
    with pytest.raises(CampaignReportError, match="duplicate"):
        load_campaign_report_dataset(duplicate)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(CampaignReportError, match="regular"):
        load_campaign_report_dataset(directory)
