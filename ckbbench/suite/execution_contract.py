"""Canonical execution contracts for independently scheduled benchmark Tasks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

TASK_EXECUTION_SCHEMA_VERSION = "ckbbench-task-execution-contract-v1"
CALIBRATION_SCHEMA_VERSION = "ckbbench-task-budget-calibration-v1"
BUDGET_BASIS_SCHEMA_VERSION = "ckbbench-budget-basis-evidence-v1"

ChainTrack = Literal["testnet", "local-hermetic"]
CalibrationStatus = Literal["calibrated", "owner-approved-exception"]

_CHAIN_TRACKS = frozenset({"testnet", "local-hermetic"})
_CALIBRATION_STATUSES = frozenset({"calibrated", "owner-approved-exception"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HASH32 = re.compile(r"^0x[0-9a-f]{64}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]{0,127}$")
_RESOURCE_KIND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CHAIN_RESOURCE_KINDS = frozenset({
    "data-cell",
    "signer",
    "spendable-input",
    "transaction",
})
_CHAIN_WRITE_RESOURCE_KINDS = frozenset({"data-cell", "transaction"})

MAX_STEP_LIMIT = 1_000
MAX_AGENT_WALL_SECONDS = 14_400
MAX_PROVIDER_CALL_LIMIT = 4_000
MAX_OUTPUT_TOKEN_LIMIT = 10_000_000
MAX_HARNESS_DEADLINE_SECONDS = 3_600


class TaskExecutionContractError(ValueError):
    """A Task execution contract is malformed or internally inconsistent."""


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise TaskExecutionContractError(f"{label} must contain exactly the reviewed fields")
    return value


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise TaskExecutionContractError(f"{label} must be a bounded public identifier")
    return value


def _sha(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or _SHA256.fullmatch(value) is None
        or value == "0" * 64
    ):
        raise TaskExecutionContractError(f"{label} must be a nonzero lowercase SHA-256 digest")
    return value


def _positive(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TaskExecutionContractError(f"{label} must be a positive integer")
    if value > maximum:
        raise TaskExecutionContractError(f"{label} exceeds its hard ceiling")
    return value


def _nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TaskExecutionContractError(f"{label} must be a non-negative integer")
    return value


def _optional_positive(value: Any, label: str, maximum: int) -> int | None:
    return None if value is None else _positive(value, label, maximum)


def _public_text(value: Any, label: str, maximum: int = 2_000) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise TaskExecutionContractError(f"{label} must be bounded non-empty text")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise TaskExecutionContractError(f"{label} contains unsupported control characters")
    return value


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise TaskExecutionContractError("execution contract is not canonical JSON data") from None
    return (payload + "\n").encode("ascii")


def contract_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(document)).hexdigest()


def _sorted_unique_resource_kinds(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TaskExecutionContractError(f"{label} must be immutable")
    if any(not isinstance(item, str) or _RESOURCE_KIND.fullmatch(item) is None for item in value):
        raise TaskExecutionContractError(f"{label} contains an invalid resource kind")
    if value != tuple(sorted(set(value))):
        raise TaskExecutionContractError(f"{label} must be unique and sorted")
    return value


def _resource_prefix(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 2_048:
        raise TaskExecutionContractError(f"{label} must be a bounded resource URI prefix")
    parsed = urlsplit(value)
    decoded = unquote(parsed.path)
    if (
        not parsed.scheme
        or value.split(":", 1)[0] != parsed.scheme
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in decoded
        or decoded.count("/") != parsed.path.count("/")
        or any(segment in {".", ".."} for segment in decoded.split("/"))
        or not value.endswith("/")
    ):
        raise TaskExecutionContractError(f"{label} must be an absolute canonical prefix")
    return value


@dataclass(frozen=True)
class TaskBudgetProfile:
    profile_id: str
    step_limit: int
    wall_time_limit_seconds: int
    provider_call_limit: int | None
    output_token_limit: int | None

    def __post_init__(self) -> None:
        _id(self.profile_id, "budget.profile_id")
        _positive(self.step_limit, "budget.step_limit", MAX_STEP_LIMIT)
        _positive(
            self.wall_time_limit_seconds,
            "budget.wall_time_limit_seconds",
            MAX_AGENT_WALL_SECONDS,
        )
        provider_limit = _optional_positive(
            self.provider_call_limit,
            "budget.provider_call_limit",
            MAX_PROVIDER_CALL_LIMIT,
        )
        _optional_positive(
            self.output_token_limit,
            "budget.output_token_limit",
            MAX_OUTPUT_TOKEN_LIMIT,
        )
        if provider_limit is not None and provider_limit < self.step_limit:
            raise TaskExecutionContractError(
                "budget.provider_call_limit cannot be lower than the agent step limit"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_token_limit": self.output_token_limit,
            "profile_id": self.profile_id,
            "provider_call_limit": self.provider_call_limit,
            "step_limit": self.step_limit,
            "wall_time_limit_seconds": self.wall_time_limit_seconds,
        }

    @property
    def sha256(self) -> str:
        return contract_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> TaskBudgetProfile:
        return cls(**_exact(document, {
            "output_token_limit",
            "profile_id",
            "provider_call_limit",
            "step_limit",
            "wall_time_limit_seconds",
        }, "Task budget"))


@dataclass(frozen=True)
class HarnessDeadlines:
    preflight_seconds: int
    setup_seconds: int
    grading_seconds: int
    teardown_seconds: int

    def __post_init__(self) -> None:
        for field in (
            "preflight_seconds",
            "setup_seconds",
            "grading_seconds",
            "teardown_seconds",
        ):
            _positive(
                getattr(self, field),
                f"harness_deadlines.{field}",
                MAX_HARNESS_DEADLINE_SECONDS,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "grading_seconds": self.grading_seconds,
            "preflight_seconds": self.preflight_seconds,
            "setup_seconds": self.setup_seconds,
            "teardown_seconds": self.teardown_seconds,
        }

    @classmethod
    def from_dict(cls, document: Any) -> HarnessDeadlines:
        return cls(**_exact(document, {
            "grading_seconds",
            "preflight_seconds",
            "setup_seconds",
            "teardown_seconds",
        }, "harness deadlines"))


@dataclass(frozen=True)
class FundingPolicy:
    maximum_transfer_shannons: int
    fee_reserve_shannons: int
    safety_margin_shannons: int
    minimum_cell_count: int
    minimum_confirmations: int

    def __post_init__(self) -> None:
        for field in (
            "maximum_transfer_shannons",
            "fee_reserve_shannons",
            "safety_margin_shannons",
        ):
            _nonnegative(getattr(self, field), f"funding.{field}")
        _positive(self.minimum_cell_count, "funding.minimum_cell_count", 64)
        _positive(self.minimum_confirmations, "funding.minimum_confirmations", 100_000)
        if self.required_capacity_shannons <= 0:
            raise TaskExecutionContractError("funding must reserve positive capacity")

    @property
    def required_capacity_shannons(self) -> int:
        return (
            self.maximum_transfer_shannons
            + self.fee_reserve_shannons
            + self.safety_margin_shannons
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fee_reserve_shannons": self.fee_reserve_shannons,
            "maximum_transfer_shannons": self.maximum_transfer_shannons,
            "minimum_cell_count": self.minimum_cell_count,
            "minimum_confirmations": self.minimum_confirmations,
            "safety_margin_shannons": self.safety_margin_shannons,
        }

    @classmethod
    def from_dict(cls, document: Any) -> FundingPolicy:
        return cls(**_exact(document, {
            "fee_reserve_shannons",
            "maximum_transfer_shannons",
            "minimum_cell_count",
            "minimum_confirmations",
            "safety_margin_shannons",
        }, "funding policy"))


@dataclass(frozen=True)
class DeploymentPin:
    dependency_id: str
    transaction_hash: str
    output_index: int
    expected_cell_sha256: str

    def __post_init__(self) -> None:
        _id(self.dependency_id, "dependency.dependency_id")
        if not isinstance(self.transaction_hash, str) or _HASH32.fullmatch(
            self.transaction_hash
        ) is None:
            raise TaskExecutionContractError(
                "dependency.transaction_hash must be a 32-byte chain hash"
            )
        index = _nonnegative(self.output_index, "dependency.output_index")
        if index > 0xFFFFFFFF:
            raise TaskExecutionContractError("dependency.output_index exceeds uint32")
        _sha(self.expected_cell_sha256, "dependency.expected_cell_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency_id": self.dependency_id,
            "expected_cell_sha256": self.expected_cell_sha256,
            "output_index": self.output_index,
            "transaction_hash": self.transaction_hash,
        }

    @classmethod
    def from_dict(cls, document: Any) -> DeploymentPin:
        return cls(**_exact(document, {
            "dependency_id",
            "expected_cell_sha256",
            "output_index",
            "transaction_hash",
        }, "deployed dependency"))


@dataclass(frozen=True)
class TreatmentRequirement:
    requirement_id: str
    claims_live_chain: bool
    required_tools: tuple[str, ...]
    required_resource_prefixes: tuple[str, ...]

    def __post_init__(self) -> None:
        _id(self.requirement_id, "treatment.requirement_id")
        if not isinstance(self.claims_live_chain, bool):
            raise TaskExecutionContractError("treatment.claims_live_chain must be boolean")
        if not isinstance(self.required_tools, tuple):
            raise TaskExecutionContractError("treatment.required_tools must be immutable")
        tools = tuple(
            item
            for item in self.required_tools
            if isinstance(item, str) and _TOOL_NAME.fullmatch(item) is not None
        )
        if len(tools) != len(self.required_tools):
            raise TaskExecutionContractError("treatment.required_tools contains an invalid name")
        if tools != tuple(sorted(set(tools))):
            raise TaskExecutionContractError("treatment.required_tools must be unique and sorted")
        if not isinstance(self.required_resource_prefixes, tuple):
            raise TaskExecutionContractError(
                "treatment.required_resource_prefixes must be immutable"
            )
        prefixes = tuple(
            _resource_prefix(value, "treatment resource prefix")
            for value in self.required_resource_prefixes
        )
        if prefixes != tuple(sorted(set(prefixes))):
            raise TaskExecutionContractError(
                "treatment.required_resource_prefixes must be unique and sorted"
            )
        if not tools and not prefixes:
            raise TaskExecutionContractError("a CKB AI treatment requirement cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims_live_chain": self.claims_live_chain,
            "required_resource_prefixes": list(self.required_resource_prefixes),
            "required_tools": list(self.required_tools),
            "requirement_id": self.requirement_id,
        }

    @classmethod
    def from_dict(cls, document: Any) -> TreatmentRequirement:
        raw = dict(_exact(document, {
            "claims_live_chain",
            "required_resource_prefixes",
            "required_tools",
            "requirement_id",
        }, "treatment requirement"))
        for field in ("required_resource_prefixes", "required_tools"):
            if not isinstance(raw[field], list):
                raise TaskExecutionContractError(f"treatment.{field} must be an array")
            raw[field] = tuple(raw[field])
        return cls(**raw)


@dataclass(frozen=True)
class BudgetCalibration:
    status: CalibrationStatus
    evidence_sha256s: tuple[str, ...]
    observed_max_steps: int | None
    observed_max_wall_seconds: int | None
    observed_max_provider_calls: int | None
    schema_version: str = CALIBRATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in _CALIBRATION_STATUSES:
            raise TaskExecutionContractError("calibration status is unsupported")
        if not isinstance(self.evidence_sha256s, tuple):
            raise TaskExecutionContractError("calibration evidence must be immutable")
        evidence = tuple(_sha(value, "calibration evidence digest") for value in self.evidence_sha256s)
        if evidence != tuple(sorted(set(evidence))) or not evidence:
            raise TaskExecutionContractError("calibration evidence must be non-empty, unique and sorted")
        for field in (
            "observed_max_steps",
            "observed_max_wall_seconds",
            "observed_max_provider_calls",
        ):
            value = getattr(self, field)
            if self.status == "calibrated":
                _positive(value, f"calibration.{field}", 100_000_000)
            elif value is not None:
                raise TaskExecutionContractError(
                    "an owner-approved exception cannot invent observed calibration values"
                )
        if self.schema_version != CALIBRATION_SCHEMA_VERSION:
            raise TaskExecutionContractError("calibration schema version is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_sha256s": list(self.evidence_sha256s),
            "observed_max_provider_calls": self.observed_max_provider_calls,
            "observed_max_steps": self.observed_max_steps,
            "observed_max_wall_seconds": self.observed_max_wall_seconds,
            "schema_version": self.schema_version,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, document: Any) -> BudgetCalibration:
        raw = dict(_exact(document, {
            "evidence_sha256s",
            "observed_max_provider_calls",
            "observed_max_steps",
            "observed_max_wall_seconds",
            "schema_version",
            "status",
        }, "budget calibration"))
        if not isinstance(raw["evidence_sha256s"], list):
            raise TaskExecutionContractError("calibration.evidence_sha256s must be an array")
        raw["evidence_sha256s"] = tuple(raw["evidence_sha256s"])
        return cls(**raw)


@dataclass(frozen=True)
class BudgetBasisEvidence:
    status: CalibrationStatus
    task_id: str
    budget_profile_id: str
    budget_profile_sha256: str
    recorded_utc: str
    observed_max_steps: int | None
    observed_max_wall_seconds: int | None
    observed_max_provider_calls: int | None
    attempt_result_sha256s: tuple[str, ...]
    decision_reference: str | None
    approved_by_role: str | None
    rationale: str
    schema_version: str = BUDGET_BASIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in _CALIBRATION_STATUSES:
            raise TaskExecutionContractError("budget basis status is unsupported")
        _id(self.task_id, "budget basis task ID")
        _id(self.budget_profile_id, "budget basis profile ID")
        _sha(self.budget_profile_sha256, "budget basis profile digest")
        if not isinstance(self.recorded_utc, str) or _UTC.fullmatch(self.recorded_utc) is None:
            raise TaskExecutionContractError("budget basis time must be canonical UTC")
        if not isinstance(self.attempt_result_sha256s, tuple):
            raise TaskExecutionContractError("budget basis attempt results must be immutable")
        attempts = tuple(
            _sha(value, "budget basis attempt result digest")
            for value in self.attempt_result_sha256s
        )
        if attempts != tuple(sorted(set(attempts))):
            raise TaskExecutionContractError(
                "budget basis attempt results must be unique and sorted"
            )
        _public_text(self.rationale, "budget basis rationale")
        if self.status == "calibrated":
            if not attempts:
                raise TaskExecutionContractError(
                    "calibrated budget basis needs attempt result evidence"
                )
            if self.decision_reference is not None or self.approved_by_role is not None:
                raise TaskExecutionContractError(
                    "calibrated budget basis cannot claim an approval exception"
                )
            for field in (
                "observed_max_steps",
                "observed_max_wall_seconds",
                "observed_max_provider_calls",
            ):
                _positive(getattr(self, field), f"budget basis {field}", 100_000_000)
        else:
            if attempts:
                raise TaskExecutionContractError(
                    "an approval exception cannot claim calibration attempts"
                )
            if self.decision_reference is None or self.approved_by_role is None:
                raise TaskExecutionContractError(
                    "an approval exception needs a decision reference and approver role"
                )
            _id(self.decision_reference, "budget basis decision reference")
            _id(self.approved_by_role, "budget basis approver role")
            if any(
                getattr(self, field) is not None
                for field in (
                    "observed_max_steps",
                    "observed_max_wall_seconds",
                    "observed_max_provider_calls",
                )
            ):
                raise TaskExecutionContractError(
                    "an approval exception cannot invent observed calibration values"
                )
        if self.schema_version != BUDGET_BASIS_SCHEMA_VERSION:
            raise TaskExecutionContractError("budget basis schema version is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved_by_role": self.approved_by_role,
            "attempt_result_sha256s": list(self.attempt_result_sha256s),
            "budget_profile_id": self.budget_profile_id,
            "budget_profile_sha256": self.budget_profile_sha256,
            "decision_reference": self.decision_reference,
            "observed_max_provider_calls": self.observed_max_provider_calls,
            "observed_max_steps": self.observed_max_steps,
            "observed_max_wall_seconds": self.observed_max_wall_seconds,
            "rationale": self.rationale,
            "recorded_utc": self.recorded_utc,
            "schema_version": self.schema_version,
            "status": self.status,
            "task_id": self.task_id,
        }

    @property
    def sha256(self) -> str:
        return contract_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> BudgetBasisEvidence:
        raw = dict(_exact(document, {
            "approved_by_role",
            "attempt_result_sha256s",
            "budget_profile_id",
            "budget_profile_sha256",
            "decision_reference",
            "observed_max_provider_calls",
            "observed_max_steps",
            "observed_max_wall_seconds",
            "rationale",
            "recorded_utc",
            "schema_version",
            "status",
            "task_id",
        }, "budget basis evidence"))
        if not isinstance(raw["attempt_result_sha256s"], list):
            raise TaskExecutionContractError(
                "budget basis attempt_result_sha256s must be an array"
            )
        raw["attempt_result_sha256s"] = tuple(raw["attempt_result_sha256s"])
        return cls(**raw)


@dataclass(frozen=True)
class TaskExecutionContract:
    contract_id: str
    chain_track: ChainTrack
    chain_profile_id: str
    chain_profile_sha256: str
    budget: TaskBudgetProfile
    harness_deadlines: HarnessDeadlines
    treatment: TreatmentRequirement
    signer_required: bool
    signing_policy_id: str | None
    funding: FundingPolicy | None
    required_dependencies: tuple[DeploymentPin, ...]
    required_resource_kinds: tuple[str, ...]
    expected_output_resource_kinds: tuple[str, ...]
    run_params_derivation: str
    resource_equivalence_policy_id: str
    calibration: BudgetCalibration
    schema_version: str = TASK_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _id(self.contract_id, "execution.contract_id")
        if self.chain_track not in _CHAIN_TRACKS:
            raise TaskExecutionContractError("execution chain track is unsupported")
        _id(self.chain_profile_id, "execution.chain_profile_id")
        _sha(self.chain_profile_sha256, "execution.chain_profile_sha256")
        if not isinstance(self.budget, TaskBudgetProfile):
            raise TaskExecutionContractError("execution budget must be typed")
        if not isinstance(self.harness_deadlines, HarnessDeadlines):
            raise TaskExecutionContractError("execution harness deadlines must be typed")
        if not isinstance(self.treatment, TreatmentRequirement):
            raise TaskExecutionContractError("execution treatment requirement must be typed")
        if not isinstance(self.signer_required, bool):
            raise TaskExecutionContractError("execution.signer_required must be boolean")
        if self.signing_policy_id is not None:
            _id(self.signing_policy_id, "execution.signing_policy_id")
        if self.funding is not None and not isinstance(self.funding, FundingPolicy):
            raise TaskExecutionContractError("execution funding must be typed")
        if not isinstance(self.required_dependencies, tuple) or not all(
            type(item) is DeploymentPin for item in self.required_dependencies
        ):
            raise TaskExecutionContractError(
                "execution.required_dependencies must be immutable deployment pins"
            )
        dependency_ids = tuple(item.dependency_id for item in self.required_dependencies)
        if dependency_ids != tuple(sorted(set(dependency_ids))):
            raise TaskExecutionContractError(
                "execution.required_dependencies must be unique and sorted by ID"
            )
        claims = _sorted_unique_resource_kinds(
            self.required_resource_kinds,
            "execution.required_resource_kinds",
        )
        outputs = _sorted_unique_resource_kinds(
            self.expected_output_resource_kinds,
            "execution.expected_output_resource_kinds",
        )
        _id(self.run_params_derivation, "execution.run_params_derivation")
        _id(
            self.resource_equivalence_policy_id,
            "execution.resource_equivalence_policy_id",
        )
        if not isinstance(self.calibration, BudgetCalibration):
            raise TaskExecutionContractError("execution calibration must be typed")
        if self.budget.provider_call_limit is None:
            raise TaskExecutionContractError(
                "an independent Task budget needs an enforceable provider-call limit"
            )
        if self.calibration.status == "calibrated":
            if self.calibration.observed_max_steps > self.budget.step_limit:
                raise TaskExecutionContractError("calibration exceeded the declared step limit")
            if self.calibration.observed_max_wall_seconds > self.budget.wall_time_limit_seconds:
                raise TaskExecutionContractError("calibration exceeded the declared wall-time limit")
            if (
                self.calibration.observed_max_provider_calls
                > self.budget.provider_call_limit
            ):
                raise TaskExecutionContractError(
                    "calibration exceeded the declared provider-call limit"
                )
        if not {"runtime-name", "workspace"} <= set(claims):
            raise TaskExecutionContractError(
                "execution resources must reserve a runtime name and workspace"
            )
        if not set(outputs) <= set(claims):
            raise TaskExecutionContractError(
                "every expected output resource kind must be reserved"
            )
        if self.chain_track == "local-hermetic":
            if self.chain_profile_id != "local-hermetic-v1":
                raise TaskExecutionContractError(
                    "local-hermetic execution needs the canonical local chain profile"
                )
            if (
                self.signer_required
                or self.signing_policy_id is not None
                or self.funding is not None
                or self.required_dependencies
            ):
                raise TaskExecutionContractError(
                    "local-hermetic execution cannot carry signer, funding or chain dependencies"
                )
            if self.treatment.claims_live_chain:
                raise TaskExecutionContractError(
                    "a local-hermetic treatment cannot claim a live chain"
                )
            if _CHAIN_RESOURCE_KINDS & set(claims):
                raise TaskExecutionContractError(
                    "local-hermetic execution cannot reserve chain resources"
                )
        else:
            if self.chain_track == "testnet" and self.chain_profile_id == "local-hermetic-v1":
                raise TaskExecutionContractError("TestNet execution needs a TestNet chain profile")
            if not self.treatment.claims_live_chain:
                raise TaskExecutionContractError(
                    "a chain-aware Task needs a treatment that attests chain identity"
                )
            if self.signer_required:
                if self.signing_policy_id is None:
                    raise TaskExecutionContractError(
                        "signed execution needs a signing policy identity"
                    )
                if self.funding is None:
                    raise TaskExecutionContractError("signed execution needs a funding policy")
                if not {"signer", "spendable-input"} <= set(claims):
                    raise TaskExecutionContractError(
                        "signed execution must reserve a signer and spendable input"
                    )
            elif (
                self.signing_policy_id is not None
                or self.funding is not None
                or {"signer", "spendable-input"} & set(claims)
            ):
                raise TaskExecutionContractError(
                    "read-only chain execution cannot carry signer or funding resources"
                )
            elif _CHAIN_WRITE_RESOURCE_KINDS & set(claims):
                raise TaskExecutionContractError(
                    "read-only chain execution cannot reserve write resources"
                )
        if self.schema_version != TASK_EXECUTION_SCHEMA_VERSION:
            raise TaskExecutionContractError("Task execution schema version is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget.to_dict(),
            "calibration": self.calibration.to_dict(),
            "chain_profile_id": self.chain_profile_id,
            "chain_profile_sha256": self.chain_profile_sha256,
            "chain_track": self.chain_track,
            "contract_id": self.contract_id,
            "expected_output_resource_kinds": list(self.expected_output_resource_kinds),
            "funding": None if self.funding is None else self.funding.to_dict(),
            "harness_deadlines": self.harness_deadlines.to_dict(),
            "required_dependencies": [item.to_dict() for item in self.required_dependencies],
            "required_resource_kinds": list(self.required_resource_kinds),
            "run_params_derivation": self.run_params_derivation,
            "resource_equivalence_policy_id": self.resource_equivalence_policy_id,
            "schema_version": self.schema_version,
            "signer_required": self.signer_required,
            "signing_policy_id": self.signing_policy_id,
            "treatment": self.treatment.to_dict(),
        }

    @property
    def sha256(self) -> str:
        return contract_sha256(self.to_dict())

    @property
    def resource_equivalence_policy(self) -> dict[str, Any]:
        return {
            "chain_profile_sha256": self.chain_profile_sha256,
            "expected_output_resource_kinds": list(self.expected_output_resource_kinds),
            "funding": None if self.funding is None else self.funding.to_dict(),
            "id": self.resource_equivalence_policy_id,
            "required_dependencies": [item.to_dict() for item in self.required_dependencies],
            "required_resource_kinds": list(self.required_resource_kinds),
            "signing_policy_id": self.signing_policy_id,
        }

    @property
    def resource_equivalence_policy_sha256(self) -> str:
        return contract_sha256(self.resource_equivalence_policy)

    @property
    def dependency_evidence(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (item.dependency_id, item.expected_cell_sha256)
            for item in self.required_dependencies
        )

    @classmethod
    def from_dict(cls, document: Any) -> TaskExecutionContract:
        raw = dict(_exact(document, {
            "budget",
            "calibration",
            "chain_profile_id",
            "chain_profile_sha256",
            "chain_track",
            "contract_id",
            "expected_output_resource_kinds",
            "funding",
            "harness_deadlines",
            "required_dependencies",
            "required_resource_kinds",
            "run_params_derivation",
            "resource_equivalence_policy_id",
            "schema_version",
            "signer_required",
            "signing_policy_id",
            "treatment",
        }, "Task execution contract"))
        raw["budget"] = TaskBudgetProfile.from_dict(raw["budget"])
        raw["harness_deadlines"] = HarnessDeadlines.from_dict(raw["harness_deadlines"])
        raw["treatment"] = TreatmentRequirement.from_dict(raw["treatment"])
        raw["calibration"] = BudgetCalibration.from_dict(raw["calibration"])
        raw["funding"] = (
            None if raw["funding"] is None else FundingPolicy.from_dict(raw["funding"])
        )
        for field in (
            "expected_output_resource_kinds",
            "required_resource_kinds",
        ):
            if not isinstance(raw[field], list):
                raise TaskExecutionContractError(f"execution.{field} must be an array")
            raw[field] = tuple(raw[field])
        dependencies = raw["required_dependencies"]
        if not isinstance(dependencies, list):
            raise TaskExecutionContractError(
                "execution.required_dependencies must be an array"
            )
        raw["required_dependencies"] = tuple(
            DeploymentPin.from_dict(item) for item in dependencies
        )
        return cls(**raw)
