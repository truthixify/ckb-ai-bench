from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ckbbench.run.campaign import load_campaign
from ckbbench.run.campaign_report import ReportBuilderSource
from ckbbench.run.campaign_paths import (
    discover_model_profile,
    discover_model_qualification,
    discover_release_binding,
)
from ckbbench.run.campaign_start import _fresh_campaign, start_campaign
from ckbbench.run.model_profile import load_run_profile
from ckbbench.run.test_suite_release import _qualification


def test_fresh_campaign_creates_qualification_draft_and_manifest_under_one_id(
    tmp_path: Path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    project = Path.cwd()
    profile = load_run_profile("gpt-5.6-luna")
    record = _qualification(profile)
    counter = 0

    def token_hex(size: int) -> str:
        nonlocal counter
        counter += 1
        return f"{counter:0{size * 2}x}"

    monkeypatch.setattr(
        "ckbbench.run.campaign_start.resolve_report_builder_source",
        lambda _root: ReportBuilderSource("1" * 40, "2" * 64),
    )
    manifest, manifest_path, selected, qualification_path, binding = _fresh_campaign(
        profile_selection="gpt-5.6-luna",
        trials_per_task=1,
        repository_root=repository,
        campaign_root="benchmark-output/campaigns",
        suite_path=project / "suites/ckb-core-v2",
        chain_paths=(
            project / "configs/chains/local-hermetic-v1.json",
            project / "configs/chains/ckb-testnet-pudge-v1.json",
        ),
        treatment_paths=tuple(
            sorted((project / "configs/ckb-ai-surfaces-v1").glob("*.json"))
        ),
        qualification_path=None,
        qualification_runner=lambda _profile, **_kwargs: record,
        clock=lambda: "2026-09-01T12:00:00Z",
        token_hex=token_hex,
    )

    assert selected == profile
    assert manifest_path.parent.name == manifest.campaign_id
    assert manifest_path.name == "campaign.json"
    assert qualification_path == manifest_path.parent / "model-qualification.json"
    assert (manifest_path.parent / "campaign-draft.json").is_file()
    assert load_campaign(manifest_path) == manifest
    binding.validate_manifest(manifest)
    assert len(manifest.slots) == 16
    assert manifest.model_qualifications[0].qualification_sha256 == record.sha256
    discover_release_binding(manifest, repository_root=project).validate_manifest(manifest)
    assert discover_model_profile(manifest, repository_root=project) == profile
    discovered_path, discovered_record = discover_model_qualification(
        manifest,
        manifest_path=manifest_path,
        repository_root=project,
    )
    assert discovered_path == qualification_path
    assert discovered_record == record


def test_start_composes_fresh_campaign_provisioning_and_every_frozen_batch(
    tmp_path: Path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    project = Path.cwd()
    profile = load_run_profile("gpt-5.6-luna")
    record = _qualification(profile)
    provisioned = []
    batches = []
    token_index = 0
    inspection_count = 0

    def token_hex(size: int) -> str:
        nonlocal token_index
        token_index += 1
        return f"{token_index:0{size * 2}x}"

    class Runtime:
        pass

    class Operator:
        def __init__(self, manifest, store, runtime, *_args, **_kwargs):
            assert runtime.__class__ is Runtime
            self.manifest = manifest

        def run_batch(self, batch_id):
            batches.append(batch_id)
            return ()

    def inspect(manifest, _store):
        nonlocal inspection_count
        inspection_count += 1
        current = (
            SimpleNamespace(status="pending", slot=manifest.ordered_slots[0])
            if inspection_count <= 2
            else None
        )
        return SimpleNamespace(current=current, complete=current is None)

    monkeypatch.setattr(
        "ckbbench.run.campaign_start.resolve_report_builder_source",
        lambda _root: ReportBuilderSource("1" * 40, "2" * 64),
    )
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    monkeypatch.setattr("ckbbench.run.campaign_operator.CampaignOperator", Operator)
    monkeypatch.setattr("ckbbench.run.campaign_operator.inspect_campaign", inspect)

    result = start_campaign(
        profile_selection="gpt-5.6-luna",
        trials_per_task=1,
        campaign_id=None,
        repository_root=repository,
        campaign_root="campaigns",
        private_data_root=tmp_path / "private",
        suite=project / "suites/ckb-core-v2",
        chain_profiles=tuple(str(path) for path in (
            project / "configs/chains/local-hermetic-v1.json",
            project / "configs/chains/ckb-testnet-pudge-v1.json",
        )),
        treatment_profiles=tuple(
            str(path)
            for path in sorted((project / "configs/ckb-ai-surfaces-v1").glob("*.json"))
        ),
        authorized_by_user=True,
        qualification_runner=lambda _profile, **_kwargs: record,
        provisioner=lambda manifest, binding, **kwargs: provisioned.append(
            (manifest, binding, kwargs)
        ),
        runtime_factory=lambda *_args, **_kwargs: Runtime(),
        clock=lambda: "2026-09-01T12:00:00Z",
        token_hex=token_hex,
    )

    assert result.complete is True
    assert batches == [batch.batch_id for batch in result.manifest.batches]
    assert len(provisioned) == 1
    assert provisioned[0][2]["authorized_by_user"] is True
    assert provisioned[0][2]["private_root"] == (
        tmp_path / "private" / result.manifest.campaign_id
    ).resolve()


def test_start_resumes_an_interrupted_campaign_before_continuing_its_batch(
    tmp_path: Path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    project = Path.cwd()
    profile = load_run_profile("gpt-5.6-luna")
    record = _qualification(profile)
    token_index = 0

    def token_hex(size: int) -> str:
        nonlocal token_index
        token_index += 1
        return f"{token_index:0{size * 2}x}"

    monkeypatch.setattr(
        "ckbbench.run.campaign_start.resolve_report_builder_source",
        lambda _root: ReportBuilderSource("1" * 40, "2" * 64),
    )
    manifest, manifest_path, _profile, _qualification_path, binding = _fresh_campaign(
        profile_selection="gpt-5.6-luna",
        trials_per_task=1,
        repository_root=repository,
        campaign_root="campaigns",
        suite_path=project / "suites/ckb-core-v2",
        chain_paths=(
            project / "configs/chains/local-hermetic-v1.json",
            project / "configs/chains/ckb-testnet-pudge-v1.json",
        ),
        treatment_paths=tuple(
            sorted((project / "configs/ckb-ai-surfaces-v1").glob("*.json"))
        ),
        qualification_path=None,
        qualification_runner=lambda _profile, **_kwargs: record,
        clock=lambda: "2026-09-01T12:00:00Z",
        token_hex=token_hex,
    )
    interrupted = SimpleNamespace(
        intent=SimpleNamespace(attempt_id="attempt-interrupted")
    )
    active = SimpleNamespace(
        status="active",
        slot=manifest.ordered_slots[0],
        original=interrupted,
        retry=None,
    )
    complete = SimpleNamespace(current=None, complete=True)
    inspections = iter((
        SimpleNamespace(current=active, complete=False),
        SimpleNamespace(current=active, complete=False),
        complete,
        complete,
    ))
    recovered = SimpleNamespace(
        intent=interrupted.intent,
        result=SimpleNamespace(outcome="pass"),
        receipts=(SimpleNamespace(status="complete"),),
    )
    calls = []

    class Runtime:
        pass

    class Operator:
        def __init__(self, *_args, **_kwargs):
            pass

        def recover(self, attempt_id):
            calls.append(("recover", attempt_id))
            return recovered

        def run_batch(self, batch_id):
            calls.append(("run_batch", batch_id))
            return ()

    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    monkeypatch.setattr(
        "ckbbench.run.campaign_start.discover_release_binding",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(
        "ckbbench.run.campaign_start.discover_model_profile",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr("ckbbench.run.campaign_operator.CampaignOperator", Operator)
    monkeypatch.setattr(
        "ckbbench.run.campaign_operator.inspect_campaign",
        lambda *_args, **_kwargs: next(inspections),
    )

    result = start_campaign(
        profile_selection=None,
        trials_per_task=2,
        campaign_id=manifest.campaign_id,
        repository_root=repository,
        campaign_root="campaigns",
        private_data_root=tmp_path / "private",
        authorized_by_user=True,
        provisioner=lambda *_args, **_kwargs: None,
        runtime_factory=lambda *_args, **_kwargs: Runtime(),
        clock=lambda: "2026-09-01T12:01:00Z",
    )

    assert result.manifest_path == manifest_path
    assert result.envelopes == (recovered,)
    assert result.complete is True
    assert calls == [
        ("recover", "attempt-interrupted"),
        ("run_batch", manifest.batches[0].batch_id),
    ]
