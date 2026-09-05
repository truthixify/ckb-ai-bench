from __future__ import annotations

from pathlib import Path

import pytest

from ckbbench.run.campaign import publish_document
from ckbbench.run.campaign_paths import (
    CampaignPathError,
    campaign_directory,
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
