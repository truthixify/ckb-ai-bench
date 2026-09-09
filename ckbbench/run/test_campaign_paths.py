from __future__ import annotations

from pathlib import Path

import pytest

from ckbbench.run.campaign import publish_document
from ckbbench.run.campaign_paths import (
    CampaignPathError,
    campaign_directory,
    discover_publication_campaign_ids,
    private_campaign_root,
    resolve_campaign_manifest_path,
)
from ckbbench.run.test_campaign import _manifest


def test_campaign_id_resolves_the_canonical_manifest_inside_the_output_root(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest = _manifest()
    directory = campaign_directory(manifest.campaign_id, repository_root=repository)
    publish_document(directory / "campaign.json", manifest.to_dict(), "campaign manifest")

    assert resolve_campaign_manifest_path(
        manifest=None,
        campaign_id=manifest.campaign_id,
        repository_root=repository,
    ) == directory / "campaign.json"


def test_campaign_selector_refuses_ambiguity_and_directory_identity_drift(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest = _manifest()
    other_id = "campaign-" + "f" * 32
    directory = campaign_directory(other_id, repository_root=repository)
    publish_document(directory / "campaign.json", manifest.to_dict(), "campaign manifest")

    with pytest.raises(CampaignPathError, match="exactly one"):
        resolve_campaign_manifest_path(
            manifest="campaign.json",
            campaign_id=other_id,
            repository_root=repository,
        )
    with pytest.raises(CampaignPathError, match="identity differ"):
        resolve_campaign_manifest_path(
            manifest=None,
            campaign_id=other_id,
            repository_root=repository,
        )


def test_campaign_id_refuses_symlinked_directories(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest = _manifest()
    real = tmp_path / "real"
    publish_document(real / "campaign.json", manifest.to_dict(), "campaign manifest")
    root = repository / "benchmark-output/campaigns"
    root.mkdir(parents=True)
    (root / manifest.campaign_id).symlink_to(real, target_is_directory=True)

    with pytest.raises(CampaignPathError, match="real directory"):
        resolve_campaign_manifest_path(
            manifest=None,
            campaign_id=manifest.campaign_id,
            repository_root=repository,
        )


def test_publication_discovery_selects_completed_campaign_directories(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest = _manifest()
    directory = campaign_directory(manifest.campaign_id, repository_root=repository)
    publish_document(directory / "campaign.json", manifest.to_dict(), "campaign manifest")
    publish_document(
        directory / "report-resolution.json",
        {"status": "accepted"},
        "report resolution",
    )
    incomplete = directory.parent / ("campaign-" + "f" * 32)
    incomplete.mkdir()
    (directory.parent / "notes").mkdir()

    assert discover_publication_campaign_ids(
        repository_root=repository,
    ) == (manifest.campaign_id,)


def test_publication_discovery_refuses_unsafe_or_empty_roots(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    campaign_root = repository / "campaigns"
    campaign_root.mkdir()

    with pytest.raises(CampaignPathError, match="no reportable campaigns"):
        discover_publication_campaign_ids(
            repository_root=repository,
            campaign_root=campaign_root,
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(CampaignPathError, match="inside the repository"):
        discover_publication_campaign_ids(
            repository_root=repository,
            campaign_root=outside,
        )

    manifest = _manifest()
    directory = campaign_root / manifest.campaign_id
    publish_document(directory / "campaign.json", manifest.to_dict(), "campaign manifest")
    resolution = tmp_path / "resolution.json"
    publish_document(resolution, {"status": "accepted"}, "report resolution")
    (directory / "report-resolution.json").symlink_to(resolution)
    with pytest.raises(CampaignPathError, match="non-symlink"):
        discover_publication_campaign_ids(
            repository_root=repository,
            campaign_root=campaign_root,
        )


def test_private_campaign_root_is_derived_outside_the_repository(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    campaign_id = _manifest().campaign_id

    assert private_campaign_root(
        campaign_id,
        repository_root=repository,
        private_data_root=tmp_path / "private",
    ) == (tmp_path / "private" / campaign_id).resolve()
    with pytest.raises(CampaignPathError, match="outside"):
        private_campaign_root(
            campaign_id,
            repository_root=repository,
            private_data_root=repository / "private",
        )
