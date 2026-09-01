"""Bind campaign and preflight inputs to one immutable independent-task suite release."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ckbbench.run.campaign import (
    STOPPING_RULE_ID,
    STOPPING_RULE_SHA256,
    CampaignBatch,
    CampaignError,
    CampaignManifest,
    CampaignSlot,
    execution_plan_sha256,
    publish_document,
    validate_intent_for_slot,
)
from ckbbench.run.chain_profile import ChainProfile, ChainProfileError
from ckbbench.run.model_profile import ModelProfileError, model_variant_id
from ckbbench.run.retry_policy import RETRY_POLICY_ID, RETRY_POLICY_SHA256
from ckbbench.run.task_attempt import (
    CONCURRENCY_CONTRACT,
    AttemptSchemaError,
    ExecutionSource,
    TaskAttemptIntent,
    TaskBudget,
    artifact_sha256,
)
from ckbbench.run.task_preflight import FundingRequirement, TaskPreflightRequirements
from ckbbench.run.treatment_surface import TreatmentSurfaceError, TreatmentSurfaceProfile
from ckbbench.suite.execution_contract import (
    BudgetBasisEvidence,
    TaskExecutionContract,
    TaskExecutionContractError,
    TreatmentRequirement,
)
from ckbbench.suite.freeze import execution_ceilings, freeze, freeze_sha256
from ckbbench.suite.model import Suite, Task
from ckbbench.suite.registry import RegistryError, load_suite

_MAX_FREEZE_BYTES = 1 << 20
CAMPAIGN_DRAFT_SCHEMA_VERSION = "ckbbench-campaign-draft-v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SuiteReleaseError(ValueError):
    """Release bytes or derived runtime inputs disagree with their immutable suite."""


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise SuiteReleaseError(f"{label} must be a bounded public identifier")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SuiteReleaseError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _exact(document: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != fields:
        raise SuiteReleaseError(f"{label} must contain exactly the reviewed fields")
    return document


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise SuiteReleaseError("release input contains a duplicate JSON key")
        document[key] = value
    return document


def _read_json_object(path: Path | str, label: str) -> dict[str, Any]:
    source = Path(path)
    try:
        if source.is_symlink() or not source.is_file():
            raise SuiteReleaseError(f"{label} must be a regular non-symlink file")
        if source.stat().st_size > _MAX_FREEZE_BYTES:
            raise SuiteReleaseError(f"{label} exceeds its byte limit")
        payload = source.read_bytes()
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except SuiteReleaseError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SuiteReleaseError(f"{label} is unreadable") from exc
    if not isinstance(document, dict):
        raise SuiteReleaseError(f"{label} must be a JSON object")
    return document


def _budget(contract: TaskExecutionContract) -> TaskBudget:
    budget = contract.budget
    return TaskBudget(
        profile_id=budget.profile_id,
        profile_sha256=budget.sha256,
        step_limit=budget.step_limit,
        wall_time_limit_seconds=budget.wall_time_limit_seconds,
        provider_call_limit=budget.provider_call_limit,
        output_token_limit=budget.output_token_limit,
    )


def _funding(contract: TaskExecutionContract) -> FundingRequirement | None:
    funding = contract.funding
    if funding is None:
        return None
    return FundingRequirement(
        maximum_transfer_shannons=funding.maximum_transfer_shannons,
        fee_reserve_shannons=funding.fee_reserve_shannons,
        safety_margin_shannons=funding.safety_margin_shannons,
        minimum_cell_count=funding.minimum_cell_count,
        minimum_confirmations=funding.minimum_confirmations,
    )


def toolchain_profile_sha256(suite: Suite) -> str:
    return artifact_sha256({
        "toolchain_versions": dict(sorted(suite.pins.toolchain_versions.items())),
    })


def treatment_satisfies(
    requirement: TreatmentRequirement,
    profile: TreatmentSurfaceProfile,
) -> bool:
    return (
        profile.claims_live_chain == requirement.claims_live_chain
        and set(requirement.required_tools) <= set(profile.allowed_tools)
        and set(requirement.required_resource_prefixes)
        <= set(profile.allowed_resource_prefixes)
    )


def _control_surface_satisfies(
    requirement: TreatmentRequirement,
    profile: TreatmentSurfaceProfile,
) -> bool:
    return (
        profile.claims_live_chain == requirement.claims_live_chain
        and profile.allowed_tools == ()
        and profile.allowed_resource_prefixes == ()
    )


def _matched_treatment_infrastructure(
    control: TreatmentSurfaceProfile,
    treatment: TreatmentSurfaceProfile,
) -> bool:
    return (
        control.server_name,
        control.server_version,
        control.claims_live_chain,
        control.controller_identity_tools,
        control.tool_catalog_sha256,
        control.resource_catalog_sha256,
        control.catalog_sha256,
    ) == (
        treatment.server_name,
        treatment.server_version,
        treatment.claims_live_chain,
        treatment.controller_identity_tools,
        treatment.tool_catalog_sha256,
        treatment.resource_catalog_sha256,
        treatment.catalog_sha256,
    )


@dataclass(frozen=True)
class SuiteRelease:
    registry_root: Path
    suite: Suite
    freeze_document: dict[str, Any]
    freeze_sha256: str
    budget_basis: tuple[tuple[str, BudgetBasisEvidence], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.registry_root, Path) or not isinstance(self.suite, Suite):
            raise SuiteReleaseError("suite release contains untyped registry data")
        if self.suite.task_execution_schema_version is None:
            raise SuiteReleaseError("suite release does not declare task execution contracts")
        if any(task.execution is None for task in self.suite.tasks):
            raise SuiteReleaseError("suite release contains a task without an execution contract")
        if freeze_sha256(self.freeze_document) != self.freeze_sha256:
            raise SuiteReleaseError("suite release freeze digest is inconsistent")
        task_ids = tuple(task.id for task in self.suite.tasks)
        if tuple(task_id for task_id, _basis in self.budget_basis) != task_ids:
            raise SuiteReleaseError("suite release budget evidence is incomplete or reordered")
        if not all(
            type(basis) is BudgetBasisEvidence for _task_id, basis in self.budget_basis
        ):
            raise SuiteReleaseError("suite release budget evidence must be typed")

    @property
    def tasks(self) -> dict[str, Task]:
        return {task.id: task for task in self.suite.tasks}

    def task_content_sha256(self, task_id: str) -> str:
        try:
            value = self.freeze_document["tasks"][task_id]["task_dir_sha256"]
        except (KeyError, TypeError):
            raise SuiteReleaseError("suite freeze is missing task content identity") from None
        if not isinstance(value, str):
            raise SuiteReleaseError("suite freeze task identity is malformed")
        return value

    def budget_basis_for(self, task_id: str) -> BudgetBasisEvidence:
        try:
            return dict(self.budget_basis)[task_id]
        except KeyError:
            raise SuiteReleaseError("suite release is missing Task budget evidence") from None


def _load_budget_basis(root: Path, task: Task) -> BudgetBasisEvidence:
    if task.execution is None:
        raise SuiteReleaseError("suite release task is missing its execution contract")
    document = _read_json_object(root / task.id / "budget-basis.json", "budget basis evidence")
    try:
        basis = BudgetBasisEvidence.from_dict(document)
    except (TaskExecutionContractError, TypeError) as exc:
        raise SuiteReleaseError("budget basis evidence is invalid") from exc
    contract = task.execution
    calibration = contract.calibration
    if basis.to_dict() != document:
        raise SuiteReleaseError("budget basis evidence is not in canonical schema form")
    if (
        basis.task_id != task.id
        or basis.budget_profile_id != contract.budget.profile_id
        or basis.budget_profile_sha256 != contract.budget.sha256
        or basis.status != calibration.status
        or basis.observed_max_steps != calibration.observed_max_steps
        or basis.observed_max_wall_seconds != calibration.observed_max_wall_seconds
        or basis.observed_max_provider_calls != calibration.observed_max_provider_calls
        or calibration.evidence_sha256s != (basis.sha256,)
    ):
        raise SuiteReleaseError("budget basis evidence differs from its Task contract")
    return basis


def _validate_release_tree(root: Path) -> None:
    try:
        if root.is_symlink() or not root.is_dir():
            raise SuiteReleaseError("suite release root must be a real directory")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise SuiteReleaseError("suite release cannot contain symlinks")
    except SuiteReleaseError:
        raise
    except OSError as exc:
        raise SuiteReleaseError("suite release tree is unreadable") from exc


def load_suite_release(registry_root: Path | str) -> SuiteRelease:
    root = Path(registry_root)
    _validate_release_tree(root)
    try:
        suite = load_suite(root)
    except (RegistryError, OSError) as exc:
        raise SuiteReleaseError("suite registry is invalid") from exc
    freeze_path = root / "suite.freeze.json"
    try:
        if freeze_path.is_symlink() or not freeze_path.is_file():
            raise SuiteReleaseError("suite release needs a regular suite.freeze.json")
        if freeze_path.stat().st_size > _MAX_FREEZE_BYTES:
            raise SuiteReleaseError("suite freeze exceeds its byte limit")
        payload = freeze_path.read_bytes()
        tracked = json.loads(payload.decode("ascii"), object_pairs_hook=_unique_object)
    except SuiteReleaseError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SuiteReleaseError("suite freeze is unreadable") from exc
    expected = freeze(suite, root)
    if tracked != expected:
        raise SuiteReleaseError("tracked suite freeze does not match the registry")
    expected_payload = (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode("ascii")
    if payload != expected_payload:
        raise SuiteReleaseError("tracked suite freeze bytes are not canonical")
    budget_basis = tuple((task.id, _load_budget_basis(root, task)) for task in suite.tasks)
    return SuiteRelease(root, suite, expected, freeze_sha256(expected), budget_basis)


def _profile_maps(
    chain_profiles: tuple[ChainProfile, ...],
    treatment_profiles: tuple[TreatmentSurfaceProfile, ...],
) -> tuple[dict[tuple[str, str], ChainProfile], dict[tuple[str, str], TreatmentSurfaceProfile]]:
    if not all(type(profile) is ChainProfile for profile in chain_profiles):
        raise SuiteReleaseError("chain profiles must be immutable typed records")
    if not all(type(profile) is TreatmentSurfaceProfile for profile in treatment_profiles):
        raise SuiteReleaseError("treatment profiles must be immutable typed records")
    chains = {(profile.profile_id, profile.sha256): profile for profile in chain_profiles}
    treatments = {(profile.profile_id, profile.sha256): profile for profile in treatment_profiles}
    if (
        len(chains) != len(chain_profiles)
        or len(treatments) != len(treatment_profiles)
        or len({profile.profile_id for profile in chain_profiles}) != len(chain_profiles)
        or len({profile.profile_id for profile in treatment_profiles}) != len(treatment_profiles)
    ):
        raise SuiteReleaseError("release profile identities must be unique")
    return chains, treatments


@dataclass(frozen=True)
class CampaignTrial:
    """Arm-neutral inputs that cannot be derived from an immutable suite release."""

    batch_id: str
    trial_id: str
    task_id: str
    control_slot_id: str
    treatment_slot_id: str
    requested_model: str
    thinking_level: str
    model_profile_id: str
    model_profile_sha256: str
    trial_challenge_id: str
    trial_challenge_sha256: str
    control_profile_id: str
    control_profile_sha256: str
    treatment_profile_id: str
    treatment_profile_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "batch_id",
            "trial_id",
            "task_id",
            "control_slot_id",
            "treatment_slot_id",
            "requested_model",
            "thinking_level",
            "model_profile_id",
            "trial_challenge_id",
            "control_profile_id",
            "treatment_profile_id",
        ):
            _identifier(getattr(self, field), f"campaign trial {field}")
        for field in (
            "model_profile_sha256",
            "trial_challenge_sha256",
            "control_profile_sha256",
            "treatment_profile_sha256",
        ):
            _sha(getattr(self, field), f"campaign trial {field}")
        if self.control_slot_id == self.treatment_slot_id:
            raise SuiteReleaseError("matched campaign slots need distinct identities")
        if (
            self.control_profile_id == self.treatment_profile_id
            or self.control_profile_sha256 == self.treatment_profile_sha256
        ):
            raise SuiteReleaseError("matched campaign arms need distinct surface profiles")

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "control_profile_id": self.control_profile_id,
            "control_profile_sha256": self.control_profile_sha256,
            "control_slot_id": self.control_slot_id,
            "model_profile_id": self.model_profile_id,
            "model_profile_sha256": self.model_profile_sha256,
            "requested_model": self.requested_model,
            "task_id": self.task_id,
            "thinking_level": self.thinking_level,
            "treatment_profile_id": self.treatment_profile_id,
            "treatment_profile_sha256": self.treatment_profile_sha256,
            "treatment_slot_id": self.treatment_slot_id,
            "trial_challenge_id": self.trial_challenge_id,
            "trial_challenge_sha256": self.trial_challenge_sha256,
            "trial_id": self.trial_id,
        }

    @classmethod
    def from_dict(cls, document: Any) -> CampaignTrial:
        return cls(**_exact(document, {
            "batch_id",
            "control_profile_id",
            "control_profile_sha256",
            "control_slot_id",
            "model_profile_id",
            "model_profile_sha256",
            "requested_model",
            "task_id",
            "thinking_level",
            "treatment_profile_id",
            "treatment_profile_sha256",
            "treatment_slot_id",
            "trial_challenge_id",
            "trial_challenge_sha256",
            "trial_id",
        }, "campaign trial"))


@dataclass(frozen=True)
class CampaignDraft:
    campaign_id: str
    created_utc: str
    execution_plan_id: str
    repository_revision: str
    source_tree_sha256: str
    trials: tuple[CampaignTrial, ...]
    schema_version: str = CAMPAIGN_DRAFT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.campaign_id, "campaign draft ID")
        _identifier(self.execution_plan_id, "campaign draft execution plan ID")
        _sha(self.source_tree_sha256, "campaign draft source tree digest")
        if not isinstance(self.created_utc, str) or not self.created_utc.endswith("Z"):
            raise SuiteReleaseError("campaign draft creation time must be UTC")
        if not isinstance(self.repository_revision, str) or not re.fullmatch(
            r"[0-9a-f]{40}", self.repository_revision
        ):
            raise SuiteReleaseError("campaign draft repository revision must be a full commit")
        if not isinstance(self.trials, tuple) or not self.trials or not all(
            type(trial) is CampaignTrial for trial in self.trials
        ):
            raise SuiteReleaseError("campaign draft trials must be immutable typed records")
        if self.schema_version != CAMPAIGN_DRAFT_SCHEMA_VERSION:
            raise SuiteReleaseError("campaign draft schema version is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "created_utc": self.created_utc,
            "execution_plan_id": self.execution_plan_id,
            "repository_revision": self.repository_revision,
            "schema_version": self.schema_version,
            "source_tree_sha256": self.source_tree_sha256,
            "trials": [trial.to_dict() for trial in self.trials],
        }

    @classmethod
    def from_dict(cls, document: Any) -> CampaignDraft:
        raw = dict(_exact(document, {
            "campaign_id",
            "created_utc",
            "execution_plan_id",
            "repository_revision",
            "schema_version",
            "source_tree_sha256",
            "trials",
        }, "campaign draft"))
        if not isinstance(raw["trials"], list):
            raise SuiteReleaseError("campaign draft trials must be an array")
        raw["trials"] = tuple(CampaignTrial.from_dict(item) for item in raw["trials"])
        return cls(**raw)


def load_campaign_draft(path: Path | str) -> CampaignDraft:
    document = _read_json_object(path, "campaign draft")
    try:
        draft = CampaignDraft.from_dict(document)
    except (TypeError, SuiteReleaseError) as exc:
        raise SuiteReleaseError("campaign draft is invalid") from exc
    if draft.to_dict() != document:
        raise SuiteReleaseError("campaign draft does not use its canonical schema representation")
    return draft


def load_chain_profile(path: Path | str) -> ChainProfile:
    document = _read_json_object(path, "chain profile")
    try:
        profile = ChainProfile.from_dict(document)
    except (ChainProfileError, TypeError) as exc:
        raise SuiteReleaseError("chain profile is invalid") from exc
    if profile.to_dict() != document:
        raise SuiteReleaseError("chain profile does not use its canonical schema representation")
    return profile


def load_treatment_profile(path: Path | str) -> TreatmentSurfaceProfile:
    document = _read_json_object(path, "treatment profile")
    try:
        profile = TreatmentSurfaceProfile.from_dict(document)
    except (TreatmentSurfaceError, TypeError) as exc:
        raise SuiteReleaseError("treatment profile is invalid") from exc
    if profile.to_dict() != document:
        raise SuiteReleaseError("treatment profile does not use its canonical schema representation")
    return profile


def _execution_source(
    release: SuiteRelease,
    repository_revision: str,
    source_tree_sha256: str,
) -> ExecutionSource:
    suite = release.suite
    if suite.pins.agent_image_digest is None or suite.pins.verifier_image_digest is None:
        raise SuiteReleaseError("suite release is missing immutable role-image identities")
    try:
        return ExecutionSource(
            repository_revision=repository_revision,
            source_tree_sha256=source_tree_sha256,
            agent_image_digest=suite.pins.agent_image_digest,
            verifier_image_digest=suite.pins.verifier_image_digest,
            toolchain_sha256=toolchain_profile_sha256(suite),
        )
    except AttemptSchemaError as exc:
        raise SuiteReleaseError("campaign execution-source identity is invalid") from exc


def build_campaign_from_release(
    release: SuiteRelease,
    *,
    campaign_id: str,
    created_utc: str,
    execution_plan_id: str,
    repository_revision: str,
    source_tree_sha256: str,
    trials: tuple[CampaignTrial, ...],
    chain_profiles: tuple[ChainProfile, ...],
    treatment_profiles: tuple[TreatmentSurfaceProfile, ...],
) -> CampaignManifest:
    """Derive every released Task field while preserving arm-neutral trial inputs."""
    if not isinstance(trials, tuple) or not trials or not all(
        type(trial) is CampaignTrial for trial in trials
    ):
        raise SuiteReleaseError("campaign trials must be non-empty immutable typed records")
    chains, treatments = _profile_maps(chain_profiles, treatment_profiles)
    source = _execution_source(release, repository_revision, source_tree_sha256)
    slots: list[CampaignSlot] = []
    batch_order: list[str] = []
    batch_slots: dict[str, list[str]] = {}
    seen_trials: set[tuple[str, str, str]] = set()
    closed_batches: set[str] = set()
    current_batch: str | None = None

    for index, trial in enumerate(trials):
        if current_batch is not None and trial.batch_id != current_batch:
            closed_batches.add(current_batch)
        if trial.batch_id in closed_batches:
            raise SuiteReleaseError("campaign trials for one batch must be contiguous")
        current_batch = trial.batch_id
        task = release.tasks.get(trial.task_id)
        if task is None or task.execution is None or not task.scored:
            raise SuiteReleaseError("campaign trial names an unreleased scored Task")
        contract = task.execution
        chain = chains.get((contract.chain_profile_id, contract.chain_profile_sha256))
        control = treatments.get((trial.control_profile_id, trial.control_profile_sha256))
        treatment = treatments.get((trial.treatment_profile_id, trial.treatment_profile_sha256))
        if chain is None or control is None or treatment is None:
            raise SuiteReleaseError("campaign trial lacks an exact release profile")
        try:
            variant = model_variant_id(
                requested_model=trial.requested_model,
                thinking_level=trial.thinking_level,
                profile_id=trial.model_profile_id,
                profile_sha256=trial.model_profile_sha256,
            )
        except ModelProfileError as exc:
            raise SuiteReleaseError("campaign trial model variant is invalid") from exc
        pair_identity = trial.trial_id, trial.task_id, variant
        if pair_identity in seen_trials:
            raise SuiteReleaseError("campaign repeats one Task trial and model variant")
        seen_trials.add(pair_identity)
        common = {
            "batch_id": trial.batch_id,
            "trial_id": trial.trial_id,
            "task_id": task.id,
            "task_content_sha256": release.task_content_sha256(task.id),
            "chain_track": contract.chain_track,
            "chain_profile_id": chain.profile_id,
            "chain_profile_sha256": chain.sha256,
            "requested_model": trial.requested_model,
            "thinking_level": trial.thinking_level,
            "model_variant_id": variant,
            "model_profile_id": trial.model_profile_id,
            "model_profile_sha256": trial.model_profile_sha256,
            "budget": _budget(contract),
            "max_score": task.score,
            "trial_challenge_id": trial.trial_challenge_id,
            "trial_challenge_sha256": trial.trial_challenge_sha256,
            "run_params_derivation": contract.run_params_derivation,
            "resource_equivalence_policy_id": contract.resource_equivalence_policy_id,
            "resource_equivalence_policy_sha256": contract.resource_equivalence_policy_sha256,
        }
        control_slot = CampaignSlot(
            slot_id=trial.control_slot_id,
            arm="B",
            treatment_profile_id=control.profile_id,
            treatment_profile_sha256=control.sha256,
            **common,
        )
        treatment_slot = CampaignSlot(
            slot_id=trial.treatment_slot_id,
            arm="C",
            treatment_profile_id=treatment.profile_id,
            treatment_profile_sha256=treatment.sha256,
            **common,
        )
        pair = (control_slot, treatment_slot) if index % 2 == 0 else (
            treatment_slot,
            control_slot,
        )
        slots.extend(pair)
        if trial.batch_id not in batch_slots:
            batch_order.append(trial.batch_id)
            batch_slots[trial.batch_id] = []
        batch_slots[trial.batch_id].extend(slot.slot_id for slot in pair)

    batches = tuple(
        CampaignBatch(batch_id, tuple(batch_slots[batch_id]))
        for batch_id in batch_order
    )
    immutable_slots = tuple(slots)
    try:
        manifest = CampaignManifest(
            campaign_id=campaign_id,
            created_utc=created_utc,
            suite_semver=release.suite.suite_semver,
            suite_freeze_sha256=release.freeze_sha256,
            execution_plan_id=execution_plan_id,
            execution_plan_sha256=execution_plan_sha256(batches, immutable_slots),
            retry_policy_id=RETRY_POLICY_ID,
            retry_policy_sha256=RETRY_POLICY_SHA256,
            retry_limit=1,
            stopping_rule_id=STOPPING_RULE_ID,
            stopping_rule_sha256=STOPPING_RULE_SHA256,
            concurrency_contract=CONCURRENCY_CONTRACT,
            execution_source=source,
            batches=batches,
            slots=immutable_slots,
        )
    except CampaignError as exc:
        raise SuiteReleaseError("derived campaign manifest is invalid") from exc
    CampaignReleaseBinding(release, chain_profiles, treatment_profiles).validate_manifest(manifest)
    return manifest


def freeze_campaign_from_release(
    draft_path: Path | str,
    output_path: Path | str,
    *,
    suite_root: Path | str,
    chain_profile_paths: tuple[Path | str, ...],
    treatment_profile_paths: tuple[Path | str, ...],
) -> tuple[CampaignManifest, CampaignReleaseBinding]:
    """Publish a manifest whose released fields come only from immutable reviewed inputs."""
    draft = load_campaign_draft(draft_path)
    release = load_suite_release(suite_root)
    if not chain_profile_paths or not treatment_profile_paths:
        raise SuiteReleaseError("release campaign freezing needs chain and treatment profiles")
    chains = tuple(load_chain_profile(path) for path in chain_profile_paths)
    treatments = tuple(load_treatment_profile(path) for path in treatment_profile_paths)
    manifest = build_campaign_from_release(
        release,
        campaign_id=draft.campaign_id,
        created_utc=draft.created_utc,
        execution_plan_id=draft.execution_plan_id,
        repository_revision=draft.repository_revision,
        source_tree_sha256=draft.source_tree_sha256,
        trials=draft.trials,
        chain_profiles=chains,
        treatment_profiles=treatments,
    )
    binding = validate_campaign_release(
        manifest,
        release,
        chain_profiles=chains,
        treatment_profiles=treatments,
    )
    publish_document(output_path, manifest.to_dict(), "campaign manifest")
    return manifest, binding


@dataclass(frozen=True)
class CampaignReleaseBinding:
    release: SuiteRelease
    chain_profiles: tuple[ChainProfile, ...]
    treatment_profiles: tuple[TreatmentSurfaceProfile, ...]

    def execution_contract_for(self, slot: CampaignSlot) -> TaskExecutionContract:
        task = self.release.tasks.get(slot.task_id)
        if task is None or task.execution is None:
            raise SuiteReleaseError("campaign slot lacks a released execution contract")
        return task.execution

    def campaign_ceilings(self, manifest: CampaignManifest) -> dict[str, Any]:
        self.validate_manifest(manifest)
        contracts = tuple(
            self.execution_contract_for(slot) for slot in manifest.ordered_slots
        )
        return execution_ceilings(
            contracts,
            arm_count=len({slot.arm for slot in manifest.slots}),
            scope="scheduled-campaign",
        )

    def validate_manifest(self, manifest: CampaignManifest) -> None:
        chains, treatments = _profile_maps(self.chain_profiles, self.treatment_profiles)
        suite = self.release.suite
        if (
            manifest.suite_semver != suite.suite_semver
            or manifest.suite_freeze_sha256 != self.release.freeze_sha256
        ):
            raise SuiteReleaseError("campaign does not bind the suite release")
        if (
            manifest.execution_source.agent_image_digest != suite.pins.agent_image_digest
            or manifest.execution_source.verifier_image_digest != suite.pins.verifier_image_digest
            or manifest.execution_source.toolchain_sha256 != toolchain_profile_sha256(suite)
        ):
            raise SuiteReleaseError("campaign execution source differs from the suite release")
        tasks = self.release.tasks
        slots_by_pair: dict[tuple[str, str, str], dict[str, CampaignSlot]] = {}
        for slot in manifest.slots:
            task = tasks.get(slot.task_id)
            if task is None or task.execution is None or not task.scored:
                raise SuiteReleaseError("campaign slot names an unreleased task")
            contract = task.execution
            expected = (
                self.release.task_content_sha256(task.id),
                task.score,
                contract.chain_track,
                contract.chain_profile_id,
                contract.chain_profile_sha256,
                _budget(contract),
                contract.run_params_derivation,
                contract.resource_equivalence_policy_id,
                contract.resource_equivalence_policy_sha256,
            )
            observed = (
                slot.task_content_sha256,
                slot.max_score,
                slot.chain_track,
                slot.chain_profile_id,
                slot.chain_profile_sha256,
                slot.budget,
                slot.run_params_derivation,
                slot.resource_equivalence_policy_id,
                slot.resource_equivalence_policy_sha256,
            )
            if observed != expected:
                raise SuiteReleaseError("campaign slot differs from its released task contract")
            chain = chains.get((slot.chain_profile_id, slot.chain_profile_sha256))
            if chain is None or chain.chain_track != contract.chain_track:
                raise SuiteReleaseError("campaign slot lacks its exact released chain profile")
            surface = treatments.get((
                slot.treatment_profile_id,
                slot.treatment_profile_sha256,
            ))
            if surface is None or surface.server_version != suite.mcp_server_version:
                raise SuiteReleaseError("campaign slot lacks its exact treatment profile")
            if slot.arm == "B" and not _control_surface_satisfies(contract.treatment, surface):
                raise SuiteReleaseError("campaign control surface is not treatment-free")
            if slot.arm == "C" and not treatment_satisfies(contract.treatment, surface):
                raise SuiteReleaseError(
                    "campaign treatment does not satisfy its released task requirement"
                )
            pair_key = slot.trial_id, slot.task_id, slot.model_variant_id
            slots_by_pair.setdefault(pair_key, {})[slot.arm] = slot

        for pair in slots_by_pair.values():
            control_slot = pair["B"]
            treatment_slot = pair["C"]
            control = treatments[(
                control_slot.treatment_profile_id,
                control_slot.treatment_profile_sha256,
            )]
            treatment = treatments[(
                treatment_slot.treatment_profile_id,
                treatment_slot.treatment_profile_sha256,
            )]
            if not _matched_treatment_infrastructure(control, treatment):
                raise SuiteReleaseError(
                    "matched B and C surfaces differ outside model-visible treatment"
                )

    def validate_preflight(
        self,
        manifest: CampaignManifest,
        slot: CampaignSlot,
        intent: TaskAttemptIntent,
        requirements: TaskPreflightRequirements,
    ) -> None:
        self.validate_manifest(manifest)
        try:
            validate_intent_for_slot(manifest, slot, intent)
        except CampaignError as exc:
            raise SuiteReleaseError("preflight intent differs from its campaign slot") from exc
        self._validate_preflight_requirements(slot, intent, requirements)

    def validate_calibration_preflight(
        self,
        manifest: CampaignManifest,
        slot: CampaignSlot,
        intent: TaskAttemptIntent,
        requirements: TaskPreflightRequirements,
    ) -> None:
        self.validate_manifest(manifest)
        self._validate_preflight_requirements(slot, intent, requirements)

    def _validate_preflight_requirements(
        self,
        slot: CampaignSlot,
        intent: TaskAttemptIntent,
        requirements: TaskPreflightRequirements,
    ) -> None:
        contract = self.execution_contract_for(slot)
        chains, treatments = _profile_maps(self.chain_profiles, self.treatment_profiles)
        chain = chains.get((slot.chain_profile_id, slot.chain_profile_sha256))
        if chain is None:
            raise SuiteReleaseError("preflight lacks its exact chain profile")
        treatment = treatments.get((slot.treatment_profile_id, slot.treatment_profile_sha256))
        if treatment is None:
            raise SuiteReleaseError("preflight lacks its exact treatment profile")
        expected_chain = None if chain.chain_track == "local-hermetic" else chain.chain_id
        expected_genesis = None if chain.chain_track == "local-hermetic" else chain.genesis_hash
        expected_surface = (
            None
            if treatment is None
            else (
                treatment.profile_id,
                treatment.sha256,
                treatment.server_version,
                treatment.catalog_sha256,
                treatment.claims_live_chain,
            )
        )
        observed_surface = (
            requirements.ckb_ai_surface_id,
            requirements.ckb_ai_surface_sha256,
            requirements.ckb_ai_server_version,
            requirements.ckb_ai_catalog_sha256,
            requirements.ckb_ai_claims_live_chain,
        )
        if observed_surface != expected_surface:
            raise SuiteReleaseError("preflight treatment differs from the campaign profile")
        resource_kinds = tuple(sorted({
            kind for kind, _resource_id in requirements.required_resource_claims
        }))
        output_kinds = tuple(sorted({
            kind for kind, _resource_id in requirements.expected_output_resources
        }))
        spendable_inputs = sum(
            kind == "spendable-input"
            for kind, _resource_id in requirements.required_resource_claims
        )
        minimum_inputs = 0 if contract.funding is None else contract.funding.minimum_cell_count
        if (
            requirements.intent_sha256 != intent.sha256
            or requirements.expected_chain_id != expected_chain
            or requirements.expected_genesis_hash != expected_genesis
            or requirements.signer_required != contract.signer_required
            or requirements.signing_policy_id != contract.signing_policy_id
            or requirements.funding != _funding(contract)
            or requirements.required_dependencies != contract.dependency_evidence
            or resource_kinds != contract.required_resource_kinds
            or output_kinds != contract.expected_output_resource_kinds
            or spendable_inputs < minimum_inputs
        ):
            raise SuiteReleaseError("preflight requirements differ from the released task contract")


def validate_campaign_release(
    manifest: CampaignManifest,
    release: SuiteRelease,
    *,
    chain_profiles: tuple[ChainProfile, ...],
    treatment_profiles: tuple[TreatmentSurfaceProfile, ...],
) -> CampaignReleaseBinding:
    try:
        binding = CampaignReleaseBinding(release, chain_profiles, treatment_profiles)
        binding.validate_manifest(manifest)
    except CampaignError as exc:
        raise SuiteReleaseError("campaign manifest is invalid") from exc
    return binding
