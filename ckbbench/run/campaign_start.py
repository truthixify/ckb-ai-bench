"""Compose campaign creation, provisioning, and accepted execution."""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ckbbench.config import LLM_API_KEY_DEFAULT, resolve_llm_api_key
from ckbbench.run.attempt_store import AttemptEnvelope, AttemptStore
from ckbbench.run.campaign import CampaignManifest, publish_document
from ckbbench.run.campaign_paths import (
    CAMPAIGN_ROOT,
    campaign_directory,
    discover_model_profile,
    discover_model_qualification,
    discover_release_binding,
    private_campaign_root,
    resolve_campaign_manifest_path,
)
from ckbbench.run.campaign_report import resolve_report_builder_source
from ckbbench.run.campaign_runtime import ProductionCampaignRuntime
from ckbbench.run.model_profile import ModelProfile, load_run_profile, resolve_run_profile_path
from ckbbench.run.model_qualification import (
    ModelQualification,
    load_model_qualification,
    run_model_qualification,
)
from ckbbench.run.signer_provisioning import provision_signer_pool
from ckbbench.run.suite_release import (
    CampaignReleaseBinding,
    build_campaign_draft,
    freeze_campaign_from_release,
    load_chain_profile,
    load_suite_release,
    load_treatment_profile,
    validate_campaign_model_qualification,
)


DEFAULT_SUITE = Path("suites/ckb-core-v2")
DEFAULT_CHAIN_PROFILES = (
    Path("configs/chains/local-hermetic-v1.json"),
    Path("configs/chains/ckb-testnet-pudge-v1.json"),
)
DEFAULT_TREATMENT_PROFILES = (
    Path("configs/ckb-ai-surfaces-v1/ckb-ai-control-local-v1.json"),
    Path("configs/ckb-ai-surfaces-v1/ckb-ai-control-testnet-v1.json"),
    Path("configs/ckb-ai-surfaces-v1/ckb-ai-treatment-local-v1.json"),
    Path("configs/ckb-ai-surfaces-v1/ckb-ai-treatment-testnet-v1.json"),
)


class CampaignStartError(RuntimeError):
    """A composed campaign could not advance safely."""


@dataclass(frozen=True)
class CampaignStartResult:
    manifest: CampaignManifest
    manifest_path: Path
    signer_pool_path: Path | None
    envelopes: tuple[AttemptEnvelope, ...]
    complete: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paths(
    values: tuple[str, ...],
    defaults: tuple[Path, ...],
    repository_root: Path,
) -> tuple[Path, ...]:
    selected = tuple(Path(value) for value in values) if values else defaults
    return tuple(path if path.is_absolute() else repository_root / path for path in selected)


def _fresh_campaign(
    *,
    profile_selection: str,
    trials_per_task: int,
    repository_root: Path,
    campaign_root: Path | str,
    suite_path: Path,
    chain_paths: tuple[Path, ...],
    treatment_paths: tuple[Path, ...],
    qualification_path: Path | None,
    qualification_runner: Callable[..., ModelQualification],
    clock: Callable[[], str],
    token_hex: Callable[[int], str],
) -> tuple[CampaignManifest, Path, ModelProfile, Path, CampaignReleaseBinding]:
    profile = load_run_profile(profile_selection)
    profile_path = resolve_run_profile_path(profile_selection)
    release = load_suite_release(suite_path)
    chains = tuple(load_chain_profile(path) for path in chain_paths)
    treatments = tuple(load_treatment_profile(path) for path in treatment_paths)
    source = resolve_report_builder_source(repository_root)
    campaign_id = f"campaign-{token_hex(16)}"
    directory = campaign_directory(
        campaign_id,
        repository_root=repository_root,
        campaign_root=campaign_root,
    )
    if directory.exists():
        raise CampaignStartError("generated campaign destination already exists")
    destination_qualification = directory / "model-qualification.json"
    if qualification_path is None:
        api_key = resolve_llm_api_key(
            profile.credential_env,
            default=LLM_API_KEY_DEFAULT,
        )
        qualification = qualification_runner(profile, api_key=api_key)
        publish_document(
            destination_qualification,
            qualification.to_dict(),
            "model qualification evidence",
        )
        if qualification.outcome != "qualified":
            raise CampaignStartError(
                f"model qualification rejected campaign {campaign_id}"
            )
    else:
        qualification = load_model_qualification(qualification_path)
        publish_document(
            destination_qualification,
            qualification.to_dict(),
            "model qualification evidence",
        )

    created_utc = clock()
    qualification.validate_for_profile(profile, checked_utc=created_utc)
    draft = build_campaign_draft(
        release,
        campaign_id=campaign_id,
        created_utc=created_utc,
        execution_plan_id=f"execution-plan-{token_hex(16)}",
        repository_revision=source.repository_revision,
        source_tree_sha256=source.source_tree_sha256,
        trials_per_task=trials_per_task,
        model_profiles=(profile,),
        chain_profiles=chains,
        treatment_profiles=treatments,
        challenge_sha256_factory=lambda: token_hex(32),
    )
    draft_path = directory / "campaign-draft.json"
    manifest_path = directory / "campaign.json"
    publish_document(draft_path, draft.to_dict(), "campaign draft")
    manifest, binding = freeze_campaign_from_release(
        draft_path,
        manifest_path,
        suite_root=suite_path,
        chain_profile_paths=chain_paths,
        treatment_profile_paths=treatment_paths,
        model_profile_paths=(profile_path,),
        model_qualification_paths=(destination_qualification,),
    )
    return manifest, manifest_path, profile, destination_qualification, binding


def start_campaign(
    *,
    profile_selection: str | None,
    trials_per_task: int,
    campaign_id: str | None,
    repository_root: Path | str = ".",
    campaign_root: Path | str = CAMPAIGN_ROOT,
    private_data_root: Path | str | None = None,
    suite: Path | str = DEFAULT_SUITE,
    chain_profiles: tuple[str, ...] = (),
    treatment_profiles: tuple[str, ...] = (),
    model_qualification: Path | str | None = None,
    authorized_by_user: bool,
    qualification_runner: Callable[..., ModelQualification] = run_model_qualification,
    provisioner: Callable[..., Path | None] = provision_signer_pool,
    runtime_factory: Callable[..., ProductionCampaignRuntime] = ProductionCampaignRuntime,
    clock: Callable[[], str] = _utc_now,
    token_hex: Callable[[int], str] = secrets.token_hex,
    retry_wait: Callable[[float], None] = time.sleep,
    coordination_root: Path | str | None = None,
    on_manifest_ready: Callable[[CampaignManifest, Path], None] | None = None,
) -> CampaignStartResult:
    if not authorized_by_user:
        raise CampaignStartError("campaign start needs explicit live authorization")
    if os.getenv("CKBBENCH_DOCKER") != "1":
        raise CampaignStartError("campaign start requires CKBBENCH_DOCKER=1")
    if not isinstance(trials_per_task, int) or isinstance(trials_per_task, bool):
        raise CampaignStartError("trials per Task must be an integer")
    repository = Path(repository_root).resolve(strict=True)
    qualification_input = None if model_qualification is None else Path(model_qualification)
    if campaign_id is None:
        if not profile_selection:
            raise CampaignStartError("a new campaign needs --profile")
        chain_paths = _paths(chain_profiles, DEFAULT_CHAIN_PROFILES, repository)
        treatment_paths = _paths(
            treatment_profiles,
            DEFAULT_TREATMENT_PROFILES,
            repository,
        )
        suite_path = Path(suite)
        if not suite_path.is_absolute():
            suite_path = repository / suite_path
        manifest, manifest_path, profile, qualification_path, binding = _fresh_campaign(
            profile_selection=profile_selection,
            trials_per_task=trials_per_task,
            repository_root=repository,
            campaign_root=campaign_root,
            suite_path=suite_path,
            chain_paths=chain_paths,
            treatment_paths=treatment_paths,
            qualification_path=qualification_input,
            qualification_runner=qualification_runner,
            clock=clock,
            token_hex=token_hex,
        )
    else:
        manifest_path = resolve_campaign_manifest_path(
            manifest=None,
            campaign_id=campaign_id,
            repository_root=repository,
            campaign_root=campaign_root,
        )
        from ckbbench.run.campaign import load_campaign

        manifest = load_campaign(manifest_path)
        binding = discover_release_binding(manifest, repository_root=repository)
        profile = discover_model_profile(manifest, repository_root=repository)
        if profile_selection is not None:
            selected = load_run_profile(profile_selection)
            if selected != profile:
                raise CampaignStartError("selected profile differs from the frozen campaign")
        if qualification_input is None:
            qualification_path, _record = discover_model_qualification(
                manifest,
                manifest_path=manifest_path,
                repository_root=repository,
            )
        else:
            qualification_path = qualification_input

    qualification = validate_campaign_model_qualification(
        manifest,
        profile,
        qualification_path,
        checked_utc=clock(),
    )
    if on_manifest_ready is not None:
        on_manifest_ready(manifest, manifest_path)
    attempt_root = manifest_path.parent / "attempts"
    store = AttemptStore(attempt_root)
    from ckbbench.run.campaign_operator import (
        CampaignOperator,
        DEFAULT_COORDINATION_ROOT,
        inspect_campaign,
    )

    if inspect_campaign(manifest, store).complete:
        return CampaignStartResult(
            manifest=manifest,
            manifest_path=manifest_path,
            signer_pool_path=None,
            envelopes=(),
            complete=True,
        )
    private_root = private_campaign_root(
        manifest.campaign_id,
        repository_root=repository,
        private_data_root=private_data_root,
    )
    pool_path = provisioner(
        manifest,
        binding,
        repository_root=repository,
        private_root=private_root,
        receipt_path=manifest_path.parent / "signer-provisioning.json",
        authorized_by_user=True,
    )
    signer_pool = None
    if pool_path is not None:
        from ckbbench.run.campaign_runtime import load_private_signer_pool

        signer_pool = load_private_signer_pool(pool_path, repository_root=repository)
    runtime = runtime_factory(
        binding,
        profile,
        model_qualification=qualification,
        repository_root=repository,
        private_runtime_root=private_root / "runtime",
        signer_pool=signer_pool,
    )
    operator = CampaignOperator(
        manifest,
        store,
        runtime,
        coordination_root or DEFAULT_COORDINATION_ROOT,
        retry_wait=retry_wait,
        release_binding=binding,
    )
    envelopes = []
    progress = inspect_campaign(manifest, store)
    if progress.current is not None and progress.current.status in {
        "active",
        "cleanup-incomplete",
    }:
        current = progress.current
        interrupted = current.retry or current.original
        if interrupted is None:
            raise CampaignStartError("interrupted campaign state is incomplete")
        recovered = operator.recover(interrupted.intent.attempt_id)
        envelopes.append(recovered)
        if (
            recovered.receipts[-1].status != "complete"
            or (
                manifest.pauses_on_infrastructure_failure
                and recovered.result.outcome == "infra_fail"
            )
        ):
            return CampaignStartResult(
                manifest=manifest,
                manifest_path=manifest_path,
                signer_pool_path=pool_path,
                envelopes=tuple(envelopes),
                complete=False,
            )
    for batch in manifest.batches:
        produced = operator.run_batch(batch.batch_id)
        envelopes.extend(produced)
        progress = inspect_campaign(manifest, store)
        if (
            progress.current is not None
            and progress.current.slot.batch_id == batch.batch_id
        ):
            break
    return CampaignStartResult(
        manifest=manifest,
        manifest_path=manifest_path,
        signer_pool_path=pool_path,
        envelopes=tuple(envelopes),
        complete=inspect_campaign(manifest, store).complete,
    )
