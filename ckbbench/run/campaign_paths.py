"""Resolve campaign artifacts and their reviewed configuration inputs."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from ckbbench.run.campaign import CampaignManifest, load_campaign
from ckbbench.run.model_profile import ModelProfile, ModelProfileError, load_model_profile
from ckbbench.run.model_qualification import (
    ModelQualification,
    ModelQualificationError,
    load_model_qualification,
)
from ckbbench.run.suite_release import (
    CampaignReleaseBinding,
    SuiteReleaseError,
    load_chain_profile,
    load_suite_release,
    load_treatment_profile,
    validate_campaign_release,
)


CAMPAIGN_ROOT = Path("benchmark-output") / "campaigns"
_CAMPAIGN_ID = re.compile(r"campaign-[0-9a-f]{32}\Z")


class CampaignPathError(ValueError):
    """A campaign path or automatically discovered binding is ambiguous or unsafe."""


def campaign_directory(
    campaign_id: str,
    *,
    repository_root: Path | str = ".",
    campaign_root: Path | str = CAMPAIGN_ROOT,
) -> Path:
    if not isinstance(campaign_id, str) or _CAMPAIGN_ID.fullmatch(campaign_id) is None:
        raise CampaignPathError("campaign ID must use the generated opaque format")
    repository = Path(repository_root).resolve(strict=True)
    root_input = Path(campaign_root)
    root = root_input if root_input.is_absolute() else repository / root_input
    try:
        resolved_root = root.resolve(strict=False)
    except OSError as exc:
        raise CampaignPathError("campaign root cannot be resolved") from exc
    if resolved_root == repository or not resolved_root.is_relative_to(repository):
        raise CampaignPathError("campaign root must be inside the repository")
    return resolved_root / campaign_id


def resolve_campaign_manifest_path(
    *,
    manifest: Path | str | None,
    campaign_id: str | None,
    repository_root: Path | str = ".",
    campaign_root: Path | str = CAMPAIGN_ROOT,
) -> Path:
    if (manifest is None) == (campaign_id is None):
        raise CampaignPathError("provide exactly one of --manifest or --campaign")
    if manifest is not None:
        return Path(manifest)
    directory = campaign_directory(
        campaign_id,
        repository_root=repository_root,
        campaign_root=campaign_root,
    )
    try:
        if directory.is_symlink() or not stat.S_ISDIR(directory.lstat().st_mode):
            raise CampaignPathError("campaign directory must be a real directory")
        candidate = directory / "campaign.json"
        if candidate.is_symlink() or not stat.S_ISREG(candidate.lstat().st_mode):
            raise CampaignPathError("campaign manifest must be a regular non-symlink file")
        if candidate.resolve(strict=True).parent != directory:
            raise CampaignPathError("campaign manifest escapes its campaign directory")
    except FileNotFoundError:
        raise CampaignPathError("campaign manifest is missing") from None
    except OSError as exc:
        raise CampaignPathError("campaign manifest cannot be resolved") from exc
    loaded = load_campaign(candidate)
    if loaded.campaign_id != campaign_id:
        raise CampaignPathError("campaign directory and manifest identity differ")
    return candidate


def private_campaign_root(
    campaign_id: str,
    *,
    repository_root: Path | str = ".",
    private_data_root: Path | str | None = None,
) -> Path:
    if not isinstance(campaign_id, str) or _CAMPAIGN_ID.fullmatch(campaign_id) is None:
        raise CampaignPathError("campaign ID must use the generated opaque format")
    configured = private_data_root
    if configured is None:
        configured = os.getenv("CKBBENCH_PRIVATE_DATA_ROOT")
    base = Path(configured) if configured is not None else Path.home() / ".local/share/ckb-ai-bench"
    if not base.is_absolute():
        raise CampaignPathError("private data root must be absolute")
    repository = Path(repository_root).resolve(strict=True)
    try:
        resolved = base.resolve(strict=False)
    except OSError as exc:
        raise CampaignPathError("private data root cannot be resolved") from exc
    if resolved == repository or resolved.is_relative_to(repository):
        raise CampaignPathError("private data root must stay outside the repository")
    return resolved / campaign_id


def discover_release_binding(
    manifest: CampaignManifest,
    *,
    repository_root: Path | str = ".",
) -> CampaignReleaseBinding:
    repository = Path(repository_root).resolve(strict=True)
    releases = []
    for candidate in sorted((repository / "suites").glob("*/manifest.json")):
        try:
            release = load_suite_release(candidate.parent)
        except (OSError, SuiteReleaseError):
            continue
        if (
            release.suite.suite_semver == manifest.suite_semver
            and release.freeze_sha256 == manifest.suite_freeze_sha256
        ):
            releases.append(release)
    if len(releases) != 1:
        raise CampaignPathError("campaign suite release could not be discovered uniquely")

    chain_keys = {(slot.chain_profile_id, slot.chain_profile_sha256) for slot in manifest.slots}
    chains = {}
    for candidate in sorted((repository / "configs/chains").glob("*.json")):
        try:
            profile = load_chain_profile(candidate)
        except (OSError, SuiteReleaseError):
            continue
        key = profile.profile_id, profile.sha256
        if key in chain_keys:
            if key in chains:
                raise CampaignPathError("campaign chain profile is duplicated")
            chains[key] = profile
    if set(chains) != chain_keys:
        raise CampaignPathError("campaign chain profiles could not be discovered")

    treatment_keys = {
        (slot.treatment_profile_id, slot.treatment_profile_sha256) for slot in manifest.slots
    }
    treatments = {}
    for candidate in sorted((repository / "configs").glob("**/*.json")):
        try:
            profile = load_treatment_profile(candidate)
        except (OSError, SuiteReleaseError):
            continue
        key = profile.profile_id, profile.sha256
        if key in treatment_keys:
            if key in treatments:
                raise CampaignPathError("campaign treatment profile is duplicated")
            treatments[key] = profile
    if set(treatments) != treatment_keys:
        raise CampaignPathError("campaign treatment profiles could not be discovered")
    try:
        return validate_campaign_release(
            manifest,
            releases[0],
            chain_profiles=tuple(chains[key] for key in sorted(chains)),
            treatment_profiles=tuple(treatments[key] for key in sorted(treatments)),
        )
    except SuiteReleaseError as exc:
        raise CampaignPathError("discovered campaign release inputs are invalid") from exc


def discover_model_profile(
    manifest: CampaignManifest,
    *,
    repository_root: Path | str = ".",
) -> ModelProfile:
    keys = {(slot.model_profile_id, slot.model_profile_sha256) for slot in manifest.slots}
    if len(keys) != 1:
        raise CampaignPathError("automatic execution supports one model profile per campaign")
    expected = next(iter(keys))
    matches = []
    for candidate in sorted((Path(repository_root) / "configs/models").glob("*.json")):
        try:
            profile = load_model_profile(candidate)
        except ModelProfileError:
            continue
        if (profile.profile_id, profile.sha256) == expected:
            matches.append(profile)
    if len(matches) != 1:
        raise CampaignPathError("campaign model profile could not be discovered uniquely")
    return matches[0]


def discover_model_qualification(
    manifest: CampaignManifest,
    *,
    manifest_path: Path | str,
    repository_root: Path | str = ".",
) -> tuple[Path, ModelQualification]:
    if len(manifest.model_qualifications) != 1:
        raise CampaignPathError("automatic execution needs one model qualification")
    expected = manifest.model_qualifications[0]
    repository = Path(repository_root).resolve(strict=True)
    local = Path(manifest_path).resolve(strict=True).parent
    roots = (local, repository / "benchmark-output/model-qualifications")
    matches: list[tuple[Path, ModelQualification]] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for candidate in sorted(root.glob("*.json")):
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                record = load_model_qualification(candidate)
            except ModelQualificationError:
                continue
            if (
                record.qualification_id == expected.qualification_id
                and record.sha256 == expected.qualification_sha256
            ):
                matches.append((candidate, record))
    if not matches:
        raise CampaignPathError("campaign model qualification could not be discovered")
    return matches[0]
