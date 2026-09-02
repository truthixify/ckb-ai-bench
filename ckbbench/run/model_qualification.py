"""Bounded model and provider qualification before an accepted campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence, TextIO

from ckbbench.config import resolve_llm_api_key
from ckbbench.run.campaign import CampaignError, publish_document
from ckbbench.run.model_profile import (
    REPO_ROOT,
    ModelProfile,
    ModelProfileError,
    load_run_profile,
    model_id,
    publishable,
    safe_api_base,
)
from ckbbench.run.provider_probe import (
    CompletionEvidence,
    ErrorStatusResponse,
    NonJsonResponse,
    OneRequestTransport,
    ProbeError,
    completion_payload,
    prepare_destination,
    probe_completion,
    validate_completion_payload,
)
from ckbbench.run.task_attempt import AttemptSchemaError, artifact_sha256, canonical_json_bytes
from ckbbench.run.task_preflight import MAX_MODEL_EVIDENCE_AGE_SECONDS, QUALIFICATION_KIND


QUALIFICATION_SCHEMA_VERSION = "ckbbench-model-qualification-v1"
QUALIFICATION_REQUEST_LIMIT = 3
QUALIFICATION_ROOT = REPO_ROOT / "benchmark-output" / "model-qualifications"
MAX_QUALIFICATION_BYTES = 64 * 1024

_OUTCOMES = frozenset({"qualified", "rejected"})
_SAMPLE_OUTCOMES = frozenset({"passed", "failed"})
_STATUS_CLASSES = frozenset({"none", "1xx", "2xx", "3xx", "4xx", "5xx"})
_FAILURE_KINDS = frozenset({
    "http_status",
    "incomplete_response",
    "model_drift",
    "non_json",
    "protocol_contract",
    "tool_contract",
    "transport",
    "usage_contract",
})
_QUALIFICATION_ID = re.compile(r"qualification-[0-9a-f]{32}\Z")


class ModelQualificationError(ValueError):
    """A qualification record or invocation violates the accepted contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ModelQualificationError(f"{field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ModelQualificationError(f"{field} must be an RFC3339 UTC timestamp") from None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ModelQualificationError(f"{field} must be UTC")
    return parsed


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ModelQualificationError(f"{field} must be a lowercase SHA-256 digest")
    if any(character not in "0123456789abcdef" for character in value):
        raise ModelQualificationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelQualificationError(f"{field} must be a non-negative integer")
    return value


def _optional_count(value: Any, field: str) -> int | None:
    return None if value is None else _count(value, field)


def _request_payload_sha256(profile: ModelProfile) -> str:
    payload = validate_completion_payload(completion_payload(profile), profile=profile)
    return artifact_sha256({"request": payload})


def _request_extensions_sha256(profile: ModelProfile) -> str:
    return artifact_sha256({"request_body_extensions": profile.request_body_extensions})


def _retry_policy_sha256(profile: ModelProfile) -> str:
    return artifact_sha256({
        "litellm_num_retries": profile.litellm_num_retries,
        "max_agent_query_attempts": profile.max_agent_query_attempts,
        "provider_request_timeout_seconds": profile.provider_request_timeout_seconds,
        "provider_retry_backoff_seconds": list(profile.provider_retry_backoff_seconds),
        "retryable_provider_failure_categories": list(
            profile.retryable_provider_failure_categories
        ),
    })


@dataclass(frozen=True)
class QualificationSample:
    ordinal: int
    request_sent: bool
    outcome: str
    failure_kind: str | None
    status_class: str
    returned_model: str | None
    response_completed: bool
    exactly_one_expected_tool_call: bool
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    token_identity_holds: bool

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or not (
            1 <= self.ordinal <= QUALIFICATION_REQUEST_LIMIT
        ):
            raise ModelQualificationError("sample ordinal is outside the qualification window")
        if type(self.request_sent) is not bool:
            raise ModelQualificationError("sample request_sent must be boolean")
        if self.outcome not in _SAMPLE_OUTCOMES:
            raise ModelQualificationError("sample outcome is unsupported")
        if self.status_class not in _STATUS_CLASSES:
            raise ModelQualificationError("sample status class is unsupported")
        if self.failure_kind is not None and self.failure_kind not in _FAILURE_KINDS:
            raise ModelQualificationError("sample failure kind is unsupported")
        if self.returned_model is not None:
            model_id(self.returned_model, field="sample.returned_model")
        for field in (
            "response_completed",
            "exactly_one_expected_tool_call",
            "token_identity_holds",
        ):
            if type(getattr(self, field)) is not bool:
                raise ModelQualificationError(f"sample {field} must be boolean")
        tokens = (
            _optional_count(self.input_tokens, "sample.input_tokens"),
            _optional_count(self.output_tokens, "sample.output_tokens"),
            _optional_count(self.total_tokens, "sample.total_tokens"),
        )
        if self.outcome == "passed":
            if (
                not self.request_sent
                or self.failure_kind is not None
                or self.status_class != "2xx"
                or self.returned_model is None
                or not self.response_completed
                or not self.exactly_one_expected_tool_call
                or not self.token_identity_holds
                or any(value is None for value in tokens)
                or tokens[0] + tokens[1] != tokens[2]
            ):
                raise ModelQualificationError("a passed sample does not satisfy the wire contract")
        elif self.failure_kind is None:
            raise ModelQualificationError("a failed sample must use one fixed failure kind")
        elif not self.request_sent:
            if (
                self.failure_kind != "transport"
                or self.status_class != "none"
                or self.returned_model is not None
                or self.response_completed
                or self.exactly_one_expected_tool_call
                or any(value is not None for value in tokens)
                or self.token_identity_holds
            ):
                raise ModelQualificationError("an unsent failed sample carries response evidence")
        elif self.failure_kind == "http_status" and self.status_class in {"none", "2xx"}:
            raise ModelQualificationError("an HTTP-status failure must record a non-success class")
        elif self.failure_kind == "non_json" and self.status_class != "2xx":
            raise ModelQualificationError("a non-JSON failure must record a successful status class")
        elif self.failure_kind in {
            "incomplete_response",
            "model_drift",
            "tool_contract",
            "usage_contract",
        } and self.status_class != "2xx":
            raise ModelQualificationError("a response-contract failure must record a success class")

    def to_dict(self) -> dict[str, Any]:
        return {
            "exactly_one_expected_tool_call": self.exactly_one_expected_tool_call,
            "failure_kind": self.failure_kind,
            "input_tokens": self.input_tokens,
            "ordinal": self.ordinal,
            "outcome": self.outcome,
            "output_tokens": self.output_tokens,
            "request_sent": self.request_sent,
            "response_completed": self.response_completed,
            "returned_model": self.returned_model,
            "status_class": self.status_class,
            "token_identity_holds": self.token_identity_holds,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_dict(cls, document: Any) -> QualificationSample:
        fields = {
            "exactly_one_expected_tool_call",
            "failure_kind",
            "input_tokens",
            "ordinal",
            "outcome",
            "output_tokens",
            "request_sent",
            "response_completed",
            "returned_model",
            "status_class",
            "token_identity_holds",
            "total_tokens",
        }
        if not isinstance(document, dict) or set(document) != fields:
            raise ModelQualificationError("qualification sample uses an unsupported schema")
        try:
            return cls(**document)
        except TypeError:
            raise ModelQualificationError("qualification sample is malformed") from None


@dataclass(frozen=True)
class ModelQualification:
    qualification_id: str
    profile_id: str
    profile_sha256: str
    model_variant_id: str
    api_base: str
    api_style: str
    requested_model: str
    probed_response_model: str
    thinking_level: str
    request_payload_sha256: str
    request_extensions_sha256: str
    retry_policy_sha256: str
    usage_contract: str
    created_utc: str
    completed_utc: str
    request_limit: int
    requests_sent: int
    outcome: str
    samples: tuple[QualificationSample, ...]
    qualification_kind: str = QUALIFICATION_KIND
    schema_version: str = QUALIFICATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.qualification_id, str) or not _QUALIFICATION_ID.fullmatch(
            self.qualification_id
        ):
            raise ModelQualificationError("qualification_id must use the generated opaque format")
        publishable(self.profile_id, field="profile_id")
        _sha(self.profile_sha256, "profile_sha256")
        publishable(self.model_variant_id, field="model_variant_id")
        safe_api_base(self.api_base)
        publishable(self.api_style, field="api_style")
        model_id(self.requested_model)
        model_id(self.probed_response_model, field="probed_response_model")
        publishable(self.thinking_level, field="thinking_level")
        for field in (
            "request_payload_sha256",
            "request_extensions_sha256",
            "retry_policy_sha256",
        ):
            _sha(getattr(self, field), field)
        publishable(self.usage_contract, field="usage_contract")
        created = _utc(self.created_utc, "created_utc")
        completed = _utc(self.completed_utc, "completed_utc")
        if completed < created:
            raise ModelQualificationError("qualification completion precedes its creation")
        if self.qualification_kind != QUALIFICATION_KIND:
            raise ModelQualificationError("qualification kind is unsupported")
        if self.schema_version != QUALIFICATION_SCHEMA_VERSION:
            raise ModelQualificationError("qualification schema version is unsupported")
        if self.request_limit != QUALIFICATION_REQUEST_LIMIT:
            raise ModelQualificationError("qualification request limit is unsupported")
        requests_sent = _count(self.requests_sent, "requests_sent")
        if self.outcome not in _OUTCOMES:
            raise ModelQualificationError("qualification outcome is unsupported")
        if not isinstance(self.samples, tuple) or not self.samples or not all(
            type(sample) is QualificationSample for sample in self.samples
        ):
            raise ModelQualificationError("qualification samples must be immutable typed records")
        expected_ordinals = tuple(range(1, len(self.samples) + 1))
        if tuple(sample.ordinal for sample in self.samples) != expected_ordinals:
            raise ModelQualificationError("qualification sample ordinals are not contiguous")
        if len(self.samples) > self.request_limit:
            raise ModelQualificationError("qualification exceeds its request limit")
        if requests_sent != sum(sample.request_sent for sample in self.samples):
            raise ModelQualificationError("qualification request count contradicts its samples")
        passed = tuple(sample.outcome == "passed" for sample in self.samples)
        if self.outcome == "qualified":
            if (
                len(self.samples) != self.request_limit
                or requests_sent != self.request_limit
                or not all(passed)
                or any(sample.returned_model != self.probed_response_model for sample in self.samples)
            ):
                raise ModelQualificationError("qualified evidence lacks three stable clean samples")
        elif passed[-1] or not all(passed[:-1]):
            raise ModelQualificationError("rejected evidence must stop at its first failed sample")

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_base": self.api_base,
            "api_style": self.api_style,
            "completed_utc": self.completed_utc,
            "created_utc": self.created_utc,
            "model_variant_id": self.model_variant_id,
            "outcome": self.outcome,
            "probed_response_model": self.probed_response_model,
            "profile_id": self.profile_id,
            "profile_sha256": self.profile_sha256,
            "qualification_id": self.qualification_id,
            "qualification_kind": self.qualification_kind,
            "request_extensions_sha256": self.request_extensions_sha256,
            "request_limit": self.request_limit,
            "request_payload_sha256": self.request_payload_sha256,
            "requests_sent": self.requests_sent,
            "retry_policy_sha256": self.retry_policy_sha256,
            "samples": [sample.to_dict() for sample in self.samples],
            "schema_version": self.schema_version,
            "thinking_level": self.thinking_level,
            "usage_contract": self.usage_contract,
            "requested_model": self.requested_model,
        }

    @classmethod
    def from_dict(cls, document: Any) -> ModelQualification:
        fields = {
            "api_base",
            "api_style",
            "completed_utc",
            "created_utc",
            "model_variant_id",
            "outcome",
            "probed_response_model",
            "profile_id",
            "profile_sha256",
            "qualification_id",
            "qualification_kind",
            "request_extensions_sha256",
            "request_limit",
            "request_payload_sha256",
            "requested_model",
            "requests_sent",
            "retry_policy_sha256",
            "samples",
            "schema_version",
            "thinking_level",
            "usage_contract",
        }
        if not isinstance(document, dict) or set(document) != fields:
            raise ModelQualificationError("qualification uses an unsupported exact schema")
        if not isinstance(document["samples"], list):
            raise ModelQualificationError("qualification samples must be an array")
        values = dict(document)
        values["samples"] = tuple(QualificationSample.from_dict(row) for row in values["samples"])
        try:
            return cls(**values)
        except (TypeError, ModelProfileError):
            raise ModelQualificationError("qualification is malformed") from None

    def validate_for_profile(
        self,
        profile: ModelProfile,
        *,
        checked_utc: str,
        max_age_seconds: int = MAX_MODEL_EVIDENCE_AGE_SECONDS,
    ) -> None:
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or not (
            0 < max_age_seconds <= MAX_MODEL_EVIDENCE_AGE_SECONDS
        ):
            raise ModelQualificationError("qualification age limit is invalid")
        expected = (
            profile.profile_id,
            profile.sha256,
            profile.model_variant_id,
            profile.api_base,
            profile.api_style,
            profile.requested_model,
            profile.probed_response_model,
            profile.thinking_level,
            _request_payload_sha256(profile),
            _request_extensions_sha256(profile),
            _retry_policy_sha256(profile),
            profile.usage_contract,
        )
        observed = (
            self.profile_id,
            self.profile_sha256,
            self.model_variant_id,
            self.api_base,
            self.api_style,
            self.requested_model,
            self.probed_response_model,
            self.thinking_level,
            self.request_payload_sha256,
            self.request_extensions_sha256,
            self.retry_policy_sha256,
            self.usage_contract,
        )
        if observed != expected:
            raise ModelQualificationError("qualification differs from the selected model profile")
        if self.outcome != "qualified":
            raise ModelQualificationError("the model profile has no accepted qualification")
        checked = _utc(checked_utc, "checked_utc")
        age = int((checked - _utc(self.completed_utc, "completed_utc")).total_seconds())
        if age < 0 or age > max_age_seconds:
            raise ModelQualificationError("model qualification evidence is stale")


def _failure_sample(
    ordinal: int,
    sender: Any,
    exc: ProbeError,
) -> QualificationSample:
    failure_kind = "transport"
    status_class = "none"
    if isinstance(exc, ErrorStatusResponse):
        failure_kind = "http_status"
        status_class = exc.facts.status_class
    elif isinstance(exc, NonJsonResponse):
        failure_kind = "non_json"
        status_class = exc.facts.status_class
    return QualificationSample(
        ordinal=ordinal,
        request_sent=sender.requests_sent == 1,
        outcome="failed",
        failure_kind=failure_kind,
        status_class=status_class,
        returned_model=None,
        response_completed=False,
        exactly_one_expected_tool_call=False,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        token_identity_holds=False,
    )


def _completion_sample(
    ordinal: int,
    sender: OneRequestTransport,
    evidence: CompletionEvidence,
    expected_model: str,
    expected_requested_model: str,
) -> QualificationSample:
    failure_kind = None
    if (
        evidence.requests_sent != 1
        or sender.requests_sent != 1
        or not evidence.status_ok
        or evidence.status_class != "2xx"
        or evidence.requested_model != expected_requested_model
    ):
        failure_kind = "protocol_contract"
    elif evidence.returned_model != expected_model:
        failure_kind = "model_drift"
    elif not evidence.response_completed:
        failure_kind = "incomplete_response"
    elif not evidence.exactly_one_expected_tool_call:
        failure_kind = "tool_contract"
    elif not evidence.token_identity_holds:
        failure_kind = "usage_contract"
    outcome = "passed" if failure_kind is None else "failed"
    return QualificationSample(
        ordinal=ordinal,
        request_sent=sender.requests_sent == 1,
        outcome=outcome,
        failure_kind=failure_kind,
        status_class=evidence.status_class,
        returned_model=evidence.returned_model,
        response_completed=evidence.response_completed,
        exactly_one_expected_tool_call=evidence.exactly_one_expected_tool_call,
        input_tokens=evidence.input_tokens,
        output_tokens=evidence.output_tokens,
        total_tokens=evidence.total_tokens,
        token_identity_holds=evidence.token_identity_holds,
    )


def run_model_qualification(
    profile: ModelProfile,
    *,
    api_key: str,
    transport_factory: Callable[[], OneRequestTransport] = OneRequestTransport,
    probe: Callable[..., CompletionEvidence] = probe_completion,
    clock: Callable[[], str] = _utc_now,
    qualification_id: str | None = None,
) -> ModelQualification:
    """Run up to three independent checks, stopping on the first invalid observation."""
    _request_payload_sha256(profile)
    selected_id = qualification_id or f"qualification-{secrets.token_hex(16)}"
    created_utc = clock()
    samples: list[QualificationSample] = []
    for ordinal in range(1, QUALIFICATION_REQUEST_LIMIT + 1):
        try:
            sender = transport_factory()
        except Exception:
            sender = SimpleNamespace(requests_sent=0)
            sample = _failure_sample(
                ordinal,
                sender,
                ProbeError("the provider transport could not be constructed"),
            )
        else:
            try:
                evidence = probe(profile=profile, api_key=api_key, transport=sender)
            except ProbeError as exc:
                sample = _failure_sample(ordinal, sender, exc)
            except Exception:
                sample = _failure_sample(
                    ordinal,
                    sender,
                    ProbeError("the provider adapter failed internally"),
                )
            else:
                sample = _completion_sample(
                    ordinal,
                    sender,
                    evidence,
                    profile.probed_response_model,
                    profile.requested_model,
                )
        samples.append(sample)
        if sample.outcome == "failed":
            break
    completed_utc = clock()
    immutable_samples = tuple(samples)
    return ModelQualification(
        qualification_id=selected_id,
        profile_id=profile.profile_id,
        profile_sha256=profile.sha256,
        model_variant_id=profile.model_variant_id,
        api_base=profile.api_base,
        api_style=profile.api_style,
        requested_model=profile.requested_model,
        probed_response_model=profile.probed_response_model,
        thinking_level=profile.thinking_level,
        request_payload_sha256=_request_payload_sha256(profile),
        request_extensions_sha256=_request_extensions_sha256(profile),
        retry_policy_sha256=_retry_policy_sha256(profile),
        usage_contract=profile.usage_contract,
        created_utc=created_utc,
        completed_utc=completed_utc,
        request_limit=QUALIFICATION_REQUEST_LIMIT,
        requests_sent=sum(sample.request_sent for sample in immutable_samples),
        outcome=(
            "qualified"
            if len(immutable_samples) == QUALIFICATION_REQUEST_LIMIT
            and all(sample.outcome == "passed" for sample in immutable_samples)
            else "rejected"
        ),
        samples=immutable_samples,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelQualificationError("qualification contains a duplicate JSON key")
        result[key] = value
    return result


def load_model_qualification(path: Path | str) -> ModelQualification:
    source = Path(path)
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError:
        raise ModelQualificationError("model qualification evidence is missing") from None
    if not stat.S_ISREG(mode):
        raise ModelQualificationError("model qualification evidence must be a regular file")
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ModelQualificationError(
                    "model qualification evidence must be a regular file"
                )
            payload = handle.read(MAX_QUALIFICATION_BYTES + 1)
    except OSError as exc:
        raise ModelQualificationError("model qualification evidence is unreadable") from exc
    if len(payload) > MAX_QUALIFICATION_BYTES:
        raise ModelQualificationError("model qualification evidence exceeds the size limit")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ModelQualificationError("model qualification evidence is not valid JSON") from None
    qualification = ModelQualification.from_dict(document)
    try:
        canonical = canonical_json_bytes(qualification.to_dict())
    except AttemptSchemaError as exc:
        raise ModelQualificationError("model qualification evidence is not canonical") from exc
    if payload != canonical:
        raise ModelQualificationError("model qualification evidence bytes are not canonical")
    return qualification


def load_accepted_model_qualification(
    path: Path | str,
    profile: ModelProfile,
    *,
    checked_utc: str | None = None,
    max_age_seconds: int = MAX_MODEL_EVIDENCE_AGE_SECONDS,
) -> ModelQualification:
    qualification = load_model_qualification(path)
    qualification.validate_for_profile(
        profile,
        checked_utc=checked_utc or _utc_now(),
        max_age_seconds=max_age_seconds,
    )
    return qualification


def qualification_sha256(path: Path | str) -> str:
    qualification = load_model_qualification(path)
    return hashlib.sha256(canonical_json_bytes(qualification.to_dict())).hexdigest()


def _output_path(value: str) -> Path:
    destination = Path(value)
    try:
        root = QUALIFICATION_ROOT.resolve(strict=False)
        resolved = destination.resolve(strict=False)
    except OSError as exc:
        raise ModelQualificationError("qualification output path is invalid") from exc
    if resolved == root or not resolved.is_relative_to(root):
        raise ModelQualificationError("qualification output must be a file under benchmark-output")
    return resolved


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ModelQualificationError("invalid qualification command arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="ckbbench qualify")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--authorized-by-user", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., ModelQualification] = run_model_qualification,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        args = _parser().parse_args(argv)
        profile = load_run_profile(args.profile)
        destination = _output_path(args.output)
        prepare_destination(destination, label="qualification evidence")
        _request_payload_sha256(profile)
        if not args.authorized_by_user:
            raise ModelQualificationError(
                "model qualification needs explicit live authorization for this invocation"
            )
        api_key = resolve_llm_api_key(profile.credential_env)
        qualification = runner(profile, api_key=api_key)
        publish_document(
            destination,
            qualification.to_dict(),
            "model qualification evidence",
        )
        print(
            f"{qualification.qualification_id}\t{qualification.outcome}\t"
            f"requests={qualification.requests_sent}\tsha256={qualification.sha256}",
            file=stdout,
        )
        return 0 if qualification.outcome == "qualified" else 1
    except (CampaignError, ModelProfileError, ModelQualificationError, ProbeError, ValueError):
        print("FAIL: model qualification was refused or could not be retained", file=stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
