"""The reviewed phase-one model profile (ADR-0014).

An accepted phase-one result must identify the exact requested model configuration it ran under. The
launch CLI used to take any `--models` string and the endpoint came from an environment default, so
two rows could differ in model, endpoint or retry policy while looking comparable.

This module is the strict reader for the selectable profiles under `configs/models/`. Profiles are
separate from the frozen suite: they record model paths, not tasks. A profile digest is taken from
its exact tracked bytes, so a reformatted file is a different profile even when it parses the same.

Nothing here reads an API key. The credential stays in the environment and never enters the profile,
a result, or a diagnostic.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_PROFILE_DIR = REPO_ROOT / "configs" / "models"
DEFAULT_PROFILE_ALIAS = "gpt-5.6-luna"
PROFILE_PATH = MODEL_PROFILE_DIR / f"{DEFAULT_PROFILE_ALIAS}.json"

PROFILE_SCHEMA_VERSION = "9"
API_STYLE = "openai-responses"
USAGE_CONTRACT = "openai-responses-usage-v1"
PROFILE_CREDENTIAL_ENV = "CKBBENCH_LLM_API_KEY"
DIRECT_QUALIFICATION_KIND = "direct-evidence-v1"
MIGRATED_QUALIFICATION_KIND = "schema-8-semantic-migration-v1"
LITELLM_NUM_RETRIES = 0
MAX_AGENT_QUERY_ATTEMPTS = 4
PROVIDER_REQUEST_TIMEOUT_SECONDS = 300
PROVIDER_RETRY_BACKOFF_SECONDS = (4, 8, 16)
RETRYABLE_PROVIDER_FAILURE_CATEGORIES = (
    "rate_limit",
    "timeout",
    "connection",
    "server",
    "protocol",
    "other_provider",
)
# Reasoning effort is sent explicitly rather than inherited from a moving alias default. The context
# value describes the harness's stateless replay policy; it is not sent as a nested provider
# reasoning parameter.
REASONING_EFFORT = "medium"
REASONING_CONTEXT = "prefix_tail_groups"
THINKING_LEVEL_PROVIDER_DEFAULT = "provider-default"
THINKING_LEVEL_UNSUPPORTED = "unsupported"
# Routed Responses endpoints may be stateless and need not support OpenAI's server-side
# context_management contract. Keep the original instructions plus a contiguous tail of complete
# response/observation groups under one deterministic serialized-byte ceiling instead.
REPLAY_POLICY = "prefix-tail-groups-v1"
REPLAY_MAX_BYTES = 128 * 1024
# Tool output is untrusted and can be arbitrarily large. Preserve a deterministic head and tail
# within this per-turn budget before the output enters stateless replay.
OBSERVATION_MAX_BYTES = 32 * 1024
# The harness owns context reduction. Profiles may explicitly disable provider truncation or omit
# the field for compatible endpoints that do not support it.
TRUNCATION_MODES = frozenset({"disabled", "omitted"})
# The benchmark replays every Responses output item itself. Explicitly disabling provider-side
# storage makes that stateless contract unambiguous and avoids mixing manual history with an
# endpoint-managed conversation.
STORE_RESPONSES = False
# Production sends NO per-turn output ceiling. A cap would silently truncate a real coding turn and
# bias the five-task result; its absence is the phase-one behavior, not an oversight.
STABILITIES: frozenset[str] = frozenset({"dated_snapshot", "moving_alias", "unknown"})

REQUIRED_KEYS: frozenset[str] = frozenset({
    "api_base", "api_style", "drop_unsupported_params", "evidence_utc", "litellm_num_retries",
    "max_agent_query_attempts", "model_stability", "observation_max_bytes",
    "probed_response_model", "profile_id", "credential_env", "qualification_source",
    "request_body_extensions",
    "provider_request_timeout_seconds", "provider_retry_backoff_seconds",
    "reasoning_context", "reasoning_effort", "replay_max_bytes", "replay_policy",
    "requested_model",
    "retryable_provider_failure_categories", "schema_version", "store", "temperature",
    "truncation", "usage_contract",
})

# A base is an OpenAI-compatible root, not a specific operation. Committing a `/chat/completions`
# or `/models` URL would make the recorded provenance describe the wrong thing.
_ENDPOINT_SUFFIXES = (
    "/models", "/responses", "/chat/completions", "/completions", "/embeddings",
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Substrings that suggest a credential reached a tracked file. The profile is published provenance.
_SECRET_MARKERS = (
    "sk-", "api_key", "apikey", "authorization", "bearer ", "password", "secret", "token=",
    "access_key", "private_key",
)


class ModelProfileError(ValueError):
    """Raised when the tracked profile is absent, malformed, or unsafe to publish."""


@dataclass(frozen=True)
class ModelProfile:
    """One immutable phase-one model configuration, bound to the bytes it was read from."""

    profile_id: str
    schema_version: str
    requested_model: str
    api_base: str
    api_style: str
    credential_env: str
    qualification_kind: str
    qualification_source_profile_sha256: str | None
    qualification_source_evidence_sha256: str | None
    temperature: int | float | None
    drop_unsupported_params: bool
    litellm_num_retries: int
    max_agent_query_attempts: int
    provider_request_timeout_seconds: int
    provider_retry_backoff_seconds: tuple[int, ...]
    retryable_provider_failure_categories: tuple[str, ...]
    model_stability: str
    probed_response_model: str
    reasoning_effort: str
    reasoning_context: str
    replay_policy: str
    replay_max_bytes: int
    observation_max_bytes: int
    truncation: str
    store: bool
    _request_body_extensions_json: str
    usage_contract: str
    evidence_utc: str
    sha256: str

    @property
    def litellm_model_name(self) -> str:
        """Select LiteLLM's OpenAI Responses adapter while preserving the configured model ID."""
        return f"openai/{self.requested_model}"

    def model_kwargs(self) -> dict[str, Any]:
        """Provider call settings. Deliberately credential-free: the key is supplied at call time."""
        settings: dict[str, Any] = {
            "api_base": self.api_base,
            "drop_params": self.drop_unsupported_params,
            "num_retries": self.litellm_num_retries,
            "timeout": self.provider_request_timeout_seconds,
            "stream": False,
            "store": self.store,
            # Deliberately NO max_output_tokens: a per-turn ceiling would truncate a real coding
            # turn. The controlled probe carries one because it is a single compatibility request.
        }
        reasoning = self.reasoning()
        if reasoning is not None:
            settings["reasoning"] = reasoning
        extensions = self.request_body_extensions
        if extensions:
            settings["extra_body"] = extensions
        if self.truncation != "omitted":
            settings["truncation"] = self.truncation
        if self.temperature is not None:
            settings["temperature"] = self.temperature
        return settings

    def reasoning(self) -> dict[str, str] | None:
        """The explicit reasoning setting to send, or none when the provider owns the setting."""
        if self.thinking_level in {
            THINKING_LEVEL_PROVIDER_DEFAULT,
            THINKING_LEVEL_UNSUPPORTED,
        }:
            return None
        return {"effort": self.thinking_level}

    @property
    def thinking_level(self) -> str:
        """Benchmark vocabulary for the profile's stored reasoning-effort contract."""
        return self.reasoning_effort

    @property
    def model_variant_id(self) -> str:
        """Canonical identity for the requested model and every reviewed profile byte."""
        return model_variant_id(
            requested_model=self.requested_model,
            thinking_level=self.thinking_level,
            profile_id=self.profile_id,
            profile_sha256=self.sha256,
        )

    @property
    def run_id_variant(self) -> str:
        """Filename-safe, recognizable model-variant component for new run IDs."""
        return f"think-{self.thinking_level}-mv-{self.model_variant_id.removeprefix('mv1-')[:12]}"

    @property
    def request_body_extensions(self) -> dict[str, Any]:
        """A fresh copy of the reviewed top-level request extensions."""
        return deepcopy(json.loads(self._request_body_extensions_json))

    def summary_lines(self) -> list[str]:
        """Operator-facing provenance. Contains no credential and no endpoint query."""
        qualification = f"qualification: {self.qualification_kind}"
        if (
            self.qualification_source_profile_sha256 is not None
            and self.qualification_source_evidence_sha256 is not None
        ):
            qualification += (
                f" profile={self.qualification_source_profile_sha256[:12]}…"
                f" evidence={self.qualification_source_evidence_sha256[:12]}…"
            )
        return [
            f"model profile: {self.profile_id} ({self.sha256[:12]}…)",
            f"requested model: {self.requested_model} ({self.model_stability})",
            f"api base: {self.api_base}",
            f"retries: litellm={self.litellm_num_retries} agent_attempts="
            f"{self.max_agent_query_attempts} | temperature="
            f"{'omitted' if self.temperature is None else self.temperature}",
            "retry backoff: " + ",".join(
                f"{seconds}s" for seconds in self.provider_retry_backoff_seconds
            ),
            "retryable failures: " + ",".join(self.retryable_provider_failure_categories),
            f"provider request timeout: {self.provider_request_timeout_seconds}s",
            f"api style: {self.api_style} (root /responses)",
            qualification,
            "request extensions: "
            + (",".join(sorted(self.request_body_extensions)) or "none"),
            f"thinking level: {self.thinking_level} | reasoning context={self.reasoning_context} "
            f"store={str(self.store).lower()}",
            f"replay: {self.replay_policy} max_bytes={self.replay_max_bytes} "
            f"observation_max_bytes={self.observation_max_bytes} "
            f"provider_truncation={self.truncation}",
            f"usage contract: {self.usage_contract}",
        ]


@dataclass(frozen=True)
class ReportModelProfile:
    """The profile fields needed to validate already-recorded result rows."""

    profile_id: str
    sha256: str
    requested_model: str
    probed_response_model: str
    model_stability: str
    max_agent_query_attempts: int
    provider_retry_backoff_seconds: tuple[int, ...]
    replay_max_bytes: int
    thinking_level: str
    model_variant_id: str


def report_profile(profile: ModelProfile) -> ReportModelProfile:
    return ReportModelProfile(
        profile_id=profile.profile_id,
        sha256=profile.sha256,
        requested_model=profile.requested_model,
        probed_response_model=profile.probed_response_model,
        model_stability=profile.model_stability,
        thinking_level=profile.thinking_level,
        model_variant_id=profile.model_variant_id,
        max_agent_query_attempts=profile.max_agent_query_attempts,
        provider_retry_backoff_seconds=profile.provider_retry_backoff_seconds,
        replay_max_bytes=profile.replay_max_bytes,
    )


def _reject_secrets(field: str, value: str) -> None:
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise ModelProfileError(
                f"{field} looks credential-bearing; the profile is published provenance"
            )


MAX_PUBLISHABLE = 200
# Identifiers this project may print or write into tracked provenance. Deliberately narrow: these
# values are provider-controlled, and anything outside this set is either a formatting accident or
# an attempt to smuggle content through an identifier field.
_PUBLISHABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]*$")
_MODEL_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:+-]*(?:/[A-Za-z0-9][A-Za-z0-9._:+-]*)*$"
)
_CURRENT_PROFILE_ID = re.compile(
    r"^model-profile-[a-z0-9][a-z0-9-]*-v[1-9][0-9]*$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
THINKING_LEVELS = REASONING_EFFORTS | frozenset({
    THINKING_LEVEL_PROVIDER_DEFAULT,
    THINKING_LEVEL_UNSUPPORTED,
})
_EXTENSION_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_REQUEST_OWNED_KEYS = frozenset({
    "api_key", "extra_body", "input", "max_output_tokens", "model", "reasoning", "store",
    "stream", "temperature", "tools", "truncation",
})
MAX_EXTENSION_BYTES = 4096
MAX_EXTENSION_DEPTH = 4
MAX_EXTENSION_ITEMS = 64
MAX_EXTENSION_NUMBER_ABS = 1_000_000_000_000


def publishable(value: Any, *, field: str) -> str:
    """One rule for every value this project may print, retain, or commit as provenance.

    Applied to profile fields, catalog candidate IDs, retained metadata and returned model IDs, so a
    server cannot smuggle a secret or a body fragment through an identifier.
    """
    if not isinstance(value, str) or not value.strip():
        raise ModelProfileError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ModelProfileError(f"{field} must not be padded with whitespace")
    if len(value) > MAX_PUBLISHABLE:
        raise ModelProfileError(f"{field} is longer than {MAX_PUBLISHABLE} characters")
    if _CONTROL.search(value):
        raise ModelProfileError(f"{field} contains a control character")
    _reject_secrets(field, value)
    if not _PUBLISHABLE.fullmatch(value):
        raise ModelProfileError(f"{field} is not a plain identifier")
    return value


def model_variant_id(
    *,
    requested_model: str,
    thinking_level: str,
    profile_id: str,
    profile_sha256: str,
) -> str:
    """Hash the public model-variant identity without duplicating the full profile in each row."""
    requested_model = model_id(requested_model)
    if not isinstance(thinking_level, str) or thinking_level not in THINKING_LEVELS:
        raise ModelProfileError(
            f"thinking_level must be one of {sorted(THINKING_LEVELS)}"
        )
    profile_id = publishable(profile_id, field="profile_id")
    if not _CURRENT_PROFILE_ID.fullmatch(profile_id):
        raise ModelProfileError("profile_id must use the current versioned profile format")
    if not isinstance(profile_sha256, str) or not _SHA256.fullmatch(profile_sha256):
        raise ModelProfileError("profile_sha256 must be 64 lowercase hex characters")
    material = {
        "profile_id": profile_id,
        "profile_sha256": profile_sha256,
        "requested_model": requested_model,
        "schema": "model-variant-v1",
        "thinking_level": thinking_level,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return f"mv1-{hashlib.sha256(canonical).hexdigest()}"


def is_publishable(value: Any) -> bool:
    """Whether an identifier may be retained. Never raises, never echoes the value."""
    try:
        publishable(value, field="value")
    except ModelProfileError:
        return False
    return True


def model_id(value: Any, *, field: str = "requested_model") -> str:
    """One plain model catalog ID, independent of the endpoint serving it."""
    model = publishable(value, field=field)
    if not _MODEL_ID.fullmatch(model):
        raise ModelProfileError(f"{field} must be a plain model catalog ID")
    return model


def _text(raw: dict[str, Any], field: str) -> str:
    return publishable(raw[field], field=field)


def _exact(raw: dict[str, Any], field: str, expected: Any) -> Any:
    """Diagnostics name the field, the expected value and the got TYPE -- never the got value.

    A profile is operator-supplied, so an invalid value can be a pasted credential; echoing it into
    an error string or a formatted traceback would publish it.
    """
    value = raw[field]
    if isinstance(expected, bool) or isinstance(value, bool):
        if value is not expected:
            raise ModelProfileError(f"{field} must be {expected!r}")
        return value
    if isinstance(expected, int) and (isinstance(value, bool) or not isinstance(value, int)):
        raise ModelProfileError(
            f"{field} must be the integer {expected!r}, got a {type(value).__name__}"
        )
    if value != expected:
        raise ModelProfileError(f"{field} must be {expected!r}")
    return value


def safe_api_base(value: Any, *, field: str = "api_base") -> str:
    """One strict check for every endpoint this project may request or publish.

    The probe runs before a profile exists, so it must not use a weaker rule than the profile: an
    unsafe base would otherwise be requested, printed and written into evidence.

    The ONLY transformation is removing an optional trailing slash. Whitespace padding and a
    non-lowercase scheme are refused rather than silently rewritten, so what is validated is exactly
    what was supplied.
    """
    if not isinstance(value, str) or not value.strip():
        raise ModelProfileError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ModelProfileError(f"{field} must not be padded with whitespace")
    if len(value) > MAX_PUBLISHABLE:
        raise ModelProfileError(f"{field} is longer than {MAX_PUBLISHABLE} characters")
    if _CONTROL.search(value):
        raise ModelProfileError(f"{field} contains a control character")
    _reject_secrets(field, value)
    parts = urlsplit(value)
    if not value.startswith(f"{parts.scheme}://"):
        raise ModelProfileError(f"{field} must use a lowercase scheme exactly as written")
    if parts.netloc != parts.netloc.lower():
        raise ModelProfileError(f"{field} must use a lowercase host exactly as written")
    if parts.scheme not in ("http", "https"):
        raise ModelProfileError(f"{field} must be an absolute http(s) URL")
    if not parts.hostname:
        raise ModelProfileError(f"{field} must name a host")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ModelProfileError(f"{field} must carry no userinfo")
    if parts.query:
        raise ModelProfileError(f"{field} must carry no query string")
    if parts.fragment:
        raise ModelProfileError(f"{field} must carry no fragment")
    # Exactly one optional trailing slash is normalized; host and path meaning are never rewritten.
    if parts.path.endswith("//"):
        raise ModelProfileError(f"{field} must end with at most one slash")
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    if path.endswith(_ENDPOINT_SUFFIXES):
        raise ModelProfileError(
            f"{field} must be the API root, not a specific endpoint such as /models"
        )
    return f"{parts.scheme}://{parts.netloc}{path}"


def _api_base(raw: dict[str, Any]) -> str:
    return safe_api_base(raw["api_base"])


def _profile_id(raw: dict[str, Any]) -> str:
    value = _text(raw, "profile_id")
    if _CURRENT_PROFILE_ID.fullmatch(value) is None:
        raise ModelProfileError("profile_id must use the current provider-neutral namespace")
    return value


def _credential_env(raw: dict[str, Any]) -> str:
    value = raw["credential_env"]
    if not isinstance(value, str):
        raise ModelProfileError("credential_env must name the generic credential channel")
    if value != PROFILE_CREDENTIAL_ENV:
        raise ModelProfileError(f"credential_env must be {PROFILE_CREDENTIAL_ENV!r}")
    return value


def _qualification_source(raw: dict[str, Any]) -> tuple[str, str | None, str | None]:
    value = raw["qualification_source"]
    if not isinstance(value, dict):
        raise ModelProfileError("qualification_source must use a supported exact schema")
    if value == {"kind": DIRECT_QUALIFICATION_KIND}:
        return DIRECT_QUALIFICATION_KIND, None, None
    if set(value) != {"kind", "profile_sha256", "evidence_sha256"}:
        raise ModelProfileError("qualification_source must use a supported exact schema")
    if value["kind"] != MIGRATED_QUALIFICATION_KIND:
        raise ModelProfileError("qualification_source.kind is unsupported")
    profile_sha256 = value["profile_sha256"]
    evidence_sha256 = value["evidence_sha256"]
    if not isinstance(profile_sha256, str) or not _SHA256.fullmatch(profile_sha256):
        raise ModelProfileError(
            "qualification_source.profile_sha256 must be 64 lowercase hex characters"
        )
    if not isinstance(evidence_sha256, str) or not _SHA256.fullmatch(evidence_sha256):
        raise ModelProfileError(
            "qualification_source.evidence_sha256 must be 64 lowercase hex characters"
        )
    return MIGRATED_QUALIFICATION_KIND, profile_sha256, evidence_sha256


def _reasoning_effort(raw: dict[str, Any]) -> str:
    value = _text(raw, "reasoning_effort")
    if value not in THINKING_LEVELS:
        raise ModelProfileError(
            f"reasoning_effort must be one of {sorted(THINKING_LEVELS)}"
        )
    return value


def _temperature(raw: dict[str, Any]) -> int | float | None:
    value = raw["temperature"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelProfileError("temperature must be null or a number")
    if not 0 <= float(value) <= 2:
        raise ModelProfileError("temperature must be between 0 and 2")
    return value


def _request_body_extensions(raw: dict[str, Any]) -> str:
    value = raw["request_body_extensions"]
    if not isinstance(value, dict):
        raise ModelProfileError("request_body_extensions must be a JSON object")
    item_count = 0

    def validate(node: Any, *, depth: int, field: str) -> None:
        nonlocal item_count
        if depth > MAX_EXTENSION_DEPTH:
            raise ModelProfileError("request_body_extensions exceeds the nesting limit")
        if node is None or isinstance(node, bool):
            return
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            if isinstance(node, float) and not math.isfinite(node):
                raise ModelProfileError("request_body_extensions contains a non-finite number")
            if abs(node) > MAX_EXTENSION_NUMBER_ABS:
                raise ModelProfileError("request_body_extensions contains an out-of-range number")
            return
        if isinstance(node, str):
            publishable(node, field=field)
            return
        if isinstance(node, list):
            item_count += len(node)
            if item_count > MAX_EXTENSION_ITEMS:
                raise ModelProfileError("request_body_extensions exceeds the item limit")
            for index, item in enumerate(node):
                validate(item, depth=depth + 1, field=f"{field}[{index}]")
            return
        if isinstance(node, dict):
            item_count += len(node)
            if item_count > MAX_EXTENSION_ITEMS:
                raise ModelProfileError("request_body_extensions exceeds the item limit")
            for key, item in node.items():
                if not isinstance(key, str) or not _EXTENSION_KEY.fullmatch(key):
                    raise ModelProfileError("request_body_extensions contains an invalid key")
                _reject_secrets("request_body_extensions key", key)
                _reject_credential_key(key)
                validate(item, depth=depth + 1, field="request_body_extensions value")
            return
        raise ModelProfileError("request_body_extensions contains a non-JSON value")

    if set(value).intersection(_REQUEST_OWNED_KEYS):
        raise ModelProfileError("request_body_extensions collides with a request-owned field")
    validate(value, depth=0, field="request_body_extensions value")
    try:
        canonical = json.dumps(
            value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
    except (TypeError, ValueError):
        raise ModelProfileError("request_body_extensions is not canonical JSON") from None
    if len(canonical.encode("utf-8")) > MAX_EXTENSION_BYTES:
        raise ModelProfileError("request_body_extensions exceeds the byte limit")
    return canonical


def _reject_credential_key(key: str) -> None:
    """Reject credential-bearing field names without treating words like ``monkey`` as keys."""
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", separated.lower()) if part)
    credential_parts = {
        "auth", "authorization", "credential", "credentials", "key", "password", "secret",
        "token",
    }
    compact = "".join(parts)
    credential_compounds = {
        "accesskey", "accesstoken", "apikey", "authtoken", "bearertoken", "clientsecret",
        "credentialvalue", "passwordvalue", "privatekey", "secretvalue",
    }
    if credential_parts.intersection(parts) or any(
        marker in compact for marker in credential_compounds
    ):
        raise ModelProfileError(
            "request_body_extensions contains a credential-bearing key"
        )


def _evidence_utc(raw: dict[str, Any]) -> str:
    value = _text(raw, "evidence_utc")
    if not value.endswith("Z"):
        raise ModelProfileError("evidence_utc must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ModelProfileError("evidence_utc is not a valid RFC3339 timestamp") from None
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ModelProfileError("evidence_utc must be UTC")
    return value


def _exact_list(raw: dict[str, Any], field: str, expected: tuple[Any, ...]) -> tuple[Any, ...]:
    """Require one exact JSON list without echoing an untrusted supplied value."""
    value = raw[field]
    if not isinstance(value, list) or value != list(expected):
        raise ModelProfileError(f"{field} must be exactly {list(expected)!r}")
    return tuple(value)


def parse_model_profile(raw: Any, *, sha256: str) -> ModelProfile:
    """Validate one already-decoded profile document against the fixed phase-one contract."""
    if not isinstance(raw, dict):
        raise ModelProfileError("the model profile must be a JSON object")
    keys = set(raw)
    missing = sorted(REQUIRED_KEYS - keys)
    extra = sorted(keys - REQUIRED_KEYS)
    if missing or extra:
        raise ModelProfileError(
            f"the model profile needs exactly {sorted(REQUIRED_KEYS)}; "
            f"{len(missing)} missing ({missing}), {len(extra)} unexpected"
        )
    stability = _text(raw, "model_stability")
    if stability not in STABILITIES:
        raise ModelProfileError(f"model_stability must be one of {sorted(STABILITIES)}")
    requested_model = model_id(raw["requested_model"])
    reasoning_effort = _reasoning_effort(raw)
    temperature = _temperature(raw)
    qualification_kind, qualification_profile_sha256, qualification_evidence_sha256 = (
        _qualification_source(raw)
    )
    truncation = _text(raw, "truncation")
    if truncation not in TRUNCATION_MODES:
        raise ModelProfileError(f"truncation must be one of {sorted(TRUNCATION_MODES)}")
    return ModelProfile(
        profile_id=_profile_id(raw),
        schema_version=_exact(raw, "schema_version", PROFILE_SCHEMA_VERSION),
        requested_model=requested_model,
        api_base=_api_base(raw),
        api_style=_exact(raw, "api_style", API_STYLE),
        credential_env=_credential_env(raw),
        qualification_kind=qualification_kind,
        qualification_source_profile_sha256=qualification_profile_sha256,
        qualification_source_evidence_sha256=qualification_evidence_sha256,
        temperature=temperature,
        drop_unsupported_params=_exact(raw, "drop_unsupported_params", True),
        litellm_num_retries=_exact(raw, "litellm_num_retries", LITELLM_NUM_RETRIES),
        max_agent_query_attempts=_exact(raw, "max_agent_query_attempts", MAX_AGENT_QUERY_ATTEMPTS),
        provider_request_timeout_seconds=_exact(
            raw, "provider_request_timeout_seconds", PROVIDER_REQUEST_TIMEOUT_SECONDS
        ),
        provider_retry_backoff_seconds=_exact_list(
            raw, "provider_retry_backoff_seconds", PROVIDER_RETRY_BACKOFF_SECONDS
        ),
        retryable_provider_failure_categories=_exact_list(
            raw,
            "retryable_provider_failure_categories",
            RETRYABLE_PROVIDER_FAILURE_CATEGORIES,
        ),
        model_stability=stability,
        probed_response_model=model_id(raw["probed_response_model"], field="probed_response_model"),
        reasoning_effort=reasoning_effort,
        reasoning_context=_exact(raw, "reasoning_context", REASONING_CONTEXT),
        replay_policy=_exact(raw, "replay_policy", REPLAY_POLICY),
        replay_max_bytes=_exact(raw, "replay_max_bytes", REPLAY_MAX_BYTES),
        observation_max_bytes=_exact(
            raw, "observation_max_bytes", OBSERVATION_MAX_BYTES
        ),
        truncation=truncation,
        store=_exact(raw, "store", STORE_RESPONSES),
        _request_body_extensions_json=_request_body_extensions(raw),
        usage_contract=_exact(raw, "usage_contract", USAGE_CONTRACT),
        evidence_utc=_evidence_utc(raw),
        sha256=sha256,
    )


def resolve_run_profile_path(selection: Path | str = PROFILE_PATH) -> Path:
    """Resolve one supported alias or explicit JSON path inside ``configs/models``."""
    raw = str(selection)
    supplied = (
        MODEL_PROFILE_DIR / (raw if raw.endswith(".json") else f"{raw}.json")
        if "/" not in raw and "\\" not in raw
        else Path(selection)
    )
    try:
        root = MODEL_PROFILE_DIR.resolve(strict=True)
        resolved = supplied.resolve(strict=True)
    except OSError:
        raise ModelProfileError(f"no supported model profile named {Path(raw).name}") from None
    if resolved.parent != root or resolved.suffix != ".json" or supplied.is_symlink():
        raise ModelProfileError("a run profile must be one JSON file under configs/models")
    return resolved


def load_run_profile(selection: Path | str = PROFILE_PATH) -> ModelProfile:
    """Load a selectable, reviewed run profile by alias or path."""
    return load_model_profile(resolve_run_profile_path(selection))


def load_reviewed_profile(path: Path | str = PROFILE_PATH) -> ModelProfile:
    """Compatibility name for callers that predate selectable run profiles."""
    return load_run_profile(path)


def load_model_profile(path: Path | str = PROFILE_PATH) -> ModelProfile:
    """Read, digest and validate the tracked profile. The digest covers the exact file bytes."""
    profile_path = Path(path)
    try:
        payload = profile_path.read_bytes()
    except OSError:
        raise ModelProfileError(f"no model profile at {profile_path.name}") from None
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ModelProfileError(f"{profile_path.name} is not valid UTF-8 JSON") from None
    return parse_model_profile(raw, sha256=hashlib.sha256(payload).hexdigest())


def available_run_profiles() -> tuple[tuple[str, ModelProfile], ...]:
    """All selectable profiles, sorted by their command-line aliases."""
    try:
        paths = sorted(MODEL_PROFILE_DIR.glob("*.json"))
    except OSError:
        raise ModelProfileError("the supported model profile directory is unavailable") from None
    profiles = tuple((path.stem, load_run_profile(path)) for path in paths)
    if not profiles:
        raise ModelProfileError("no supported model profiles are configured")
    return profiles


def configured_model_profile(model: Any) -> ModelProfile:
    """The unique selectable profile for one requested model ID."""
    requested = model_id(model)
    matches = [
        profile
        for _alias, profile in available_run_profiles()
        if profile.requested_model == requested
    ]
    if len(matches) != 1:
        raise ModelProfileError("the model ID must resolve to one supported profile")
    return matches[0]


def load_report_profile(path: Path | str) -> ReportModelProfile:
    """Load one selectable profile for report validation."""
    return report_profile(load_run_profile(path))
