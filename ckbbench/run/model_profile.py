"""The reviewed phase-one model profile (ADR-0014).

An accepted phase-one result must identify the exact requested GPT configuration it ran under. The
launch CLI used to take any `--models` string and the endpoint came from an environment default, so
two rows could differ in model, endpoint or retry policy while looking comparable.

This module is the single strict reader for `configs/phase1-gpt.json`. The profile is deliberately
separate from the frozen suite: it records the model path, not the tasks. Its digest is taken from
the exact tracked bytes, so a reformatted file is a different profile even when it parses the same.

Nothing here reads an API key. The credential stays in the environment and never enters the profile,
a result, or a diagnostic.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = REPO_ROOT / "configs" / "phase1-gpt.json"
REPORT_PROFILE_DIR = REPO_ROOT / "configs" / "model-profiles"

PROFILE_ID = "phase1-gpt-v10"
PROFILE_SCHEMA_VERSION = "8"
API_STYLE = "openai-responses"
USAGE_CONTRACT = "openai-responses-usage-v1"
SUPPORTED_PROVIDERS = frozenset({"ckbuilders", "openrouter"})
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
CKBUILDERS_API_BASE = "https://share-ai.ckbdev.com"
OPENROUTER_TEMPERATURE = None
CKBUILDERS_TEMPERATURE = 0
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
# value describes the harness's stateless replay policy; GPT-5 Mini does not receive it as a nested
# reasoning parameter.
REASONING_EFFORT = "medium"
REASONING_CONTEXT = "prefix_tail_groups"
# OpenRouter's Responses endpoint is stateless and does not document OpenAI's server-side
# context_management contract. Keep the original instructions plus a contiguous tail of complete
# response/observation groups under one deterministic serialized-byte ceiling instead.
REPLAY_POLICY = "prefix-tail-groups-v1"
REPLAY_MAX_BYTES = 128 * 1024
# Tool output is untrusted and can be arbitrarily large. Preserve a deterministic head and tail
# within this per-turn budget before the output enters stateless replay.
OBSERVATION_MAX_BYTES = 32 * 1024
# The harness owns context reduction. OpenRouter supports an explicit disabled value; CKBuilders'
# direct Responses proxy rejects that field, so its omission is an equally explicit profile choice.
OPENROUTER_TRUNCATION = "disabled"
DIRECT_TRUNCATION = "omitted"
# The benchmark replays every Responses output item itself. Explicitly disabling provider-side
# storage makes that stateless contract unambiguous and avoids mixing manual history with an
# endpoint-managed conversation.
STORE_RESPONSES = False
OPENROUTER_PROVIDER_ORDER = ("openai",)
OPENROUTER_ALLOW_FALLBACKS = False
OPENROUTER_REQUIRE_PARAMETERS = True
DIRECT_PROVIDER_ORDER: tuple[str, ...] = ()
DIRECT_ALLOW_FALLBACKS = False
DIRECT_REQUIRE_PARAMETERS = False
# Production sends NO per-turn output ceiling. A cap would silently truncate a real coding turn and
# bias the five-task result; its absence is the phase-one behavior, not an oversight.
STABILITIES: frozenset[str] = frozenset({"dated_snapshot", "moving_alias", "unknown"})

REQUIRED_KEYS: frozenset[str] = frozenset({
    "api_base", "api_style", "drop_unsupported_params", "evidence_utc", "litellm_num_retries",
    "max_agent_query_attempts", "model_stability", "observation_max_bytes",
    "probed_response_model", "profile_id",
    "provider", "provider_allow_fallbacks", "provider_order", "provider_require_parameters",
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
    provider: str
    requested_model: str
    api_base: str
    api_style: str
    temperature: int | None
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
    provider_order: tuple[str, ...]
    provider_allow_fallbacks: bool
    provider_require_parameters: bool
    usage_contract: str
    evidence_utc: str
    sha256: str

    @property
    def litellm_model_name(self) -> str:
        """Use LiteLLM's OpenAI Responses adapter while preserving OpenRouter's catalog ID.

        LiteLLM strips the first ``openai/`` as its provider selector. The remaining
        ``openai/gpt-5-mini`` is the exact model slug OpenRouter receives.
        """
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
            "reasoning": self.reasoning(),
            # Deliberately NO max_output_tokens: a per-turn ceiling would truncate a real coding
            # turn. The controlled probe carries one because it is a single compatibility request.
        }
        route = self.provider_extra_body()
        if route is not None:
            settings["extra_body"] = route
        if self.truncation != DIRECT_TRUNCATION:
            settings["truncation"] = self.truncation
        if self.temperature is not None:
            settings["temperature"] = self.temperature
        return settings

    def reasoning(self) -> dict[str, str]:
        """The reasoning settings the probe and the production model both send."""
        return {"effort": self.reasoning_effort}

    def provider_extra_body(self) -> dict[str, Any] | None:
        """Provider-specific request fields, or None for a direct OpenAI-compatible endpoint."""
        if self.provider != "openrouter":
            return None
        return {
            "provider": {
                "order": list(self.provider_order),
                "allow_fallbacks": self.provider_allow_fallbacks,
                "require_parameters": self.provider_require_parameters,
            }
        }

    def summary_lines(self) -> list[str]:
        """Operator-facing provenance. Contains no credential and no endpoint query."""
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
            (
                "provider route: " + ",".join(self.provider_order)
                + f" fallbacks={str(self.provider_allow_fallbacks).lower()}"
                + f" require_parameters={str(self.provider_require_parameters).lower()}"
                if self.provider_order else "provider route: direct"
            ),
            f"reasoning: effort={self.reasoning_effort} context={self.reasoning_context} "
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


def report_profile(profile: ModelProfile) -> ReportModelProfile:
    return ReportModelProfile(
        profile_id=profile.profile_id,
        sha256=profile.sha256,
        requested_model=profile.requested_model,
        probed_response_model=profile.probed_response_model,
        model_stability=profile.model_stability,
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
_OPENROUTER_OPENAI_MODEL = re.compile(r"^openai/[A-Za-z0-9][A-Za-z0-9._:+-]*$")
_DIRECT_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$")
_PROFILE_ID = re.compile(r"^phase1-gpt-v[1-9][0-9]*$")


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


def is_publishable(value: Any) -> bool:
    """Whether an identifier may be retained. Never raises, never echoes the value."""
    try:
        publishable(value, field="value")
    except ModelProfileError:
        return False
    return True


def openrouter_model_id(value: Any, *, field: str = "requested_model") -> str:
    """One OpenRouter OpenAI catalog ID, suitable for both the probe and tracked profile."""
    model = publishable(value, field=field)
    if not _OPENROUTER_OPENAI_MODEL.fullmatch(model):
        raise ModelProfileError(f"{field} must be one OpenRouter openai/ catalog ID")
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
    match = _PROFILE_ID.fullmatch(value)
    if match is None or int(value.rsplit("v", 1)[1]) < 8:
        raise ModelProfileError("profile_id must be a current phase1-gpt-vN identifier")
    return value


def _provider(raw: dict[str, Any]) -> str:
    value = _text(raw, "provider")
    if value not in SUPPORTED_PROVIDERS:
        raise ModelProfileError(f"provider must be one of {sorted(SUPPORTED_PROVIDERS)}")
    return value


def _requested_model(raw: dict[str, Any], *, provider: str) -> str:
    if provider == "openrouter":
        return openrouter_model_id(raw["requested_model"])
    model = publishable(raw["requested_model"], field="requested_model")
    if not _DIRECT_MODEL.fullmatch(model):
        raise ModelProfileError("requested_model must be one direct-provider catalog ID")
    return model


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
    provider = _provider(raw)
    if provider == "openrouter":
        temperature = OPENROUTER_TEMPERATURE
        provider_order = OPENROUTER_PROVIDER_ORDER
        allow_fallbacks = OPENROUTER_ALLOW_FALLBACKS
        require_parameters = OPENROUTER_REQUIRE_PARAMETERS
        truncation = OPENROUTER_TRUNCATION
    else:
        temperature = CKBUILDERS_TEMPERATURE
        provider_order = DIRECT_PROVIDER_ORDER
        allow_fallbacks = DIRECT_ALLOW_FALLBACKS
        require_parameters = DIRECT_REQUIRE_PARAMETERS
        truncation = DIRECT_TRUNCATION
    return ModelProfile(
        profile_id=_profile_id(raw),
        schema_version=_exact(raw, "schema_version", PROFILE_SCHEMA_VERSION),
        provider=provider,
        requested_model=_requested_model(raw, provider=provider),
        api_base=_api_base(raw),
        api_style=_exact(raw, "api_style", API_STYLE),
        temperature=_exact(raw, "temperature", temperature),
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
        probed_response_model=_text(raw, "probed_response_model"),
        reasoning_effort=_exact(raw, "reasoning_effort", REASONING_EFFORT),
        reasoning_context=_exact(raw, "reasoning_context", REASONING_CONTEXT),
        replay_policy=_exact(raw, "replay_policy", REPLAY_POLICY),
        replay_max_bytes=_exact(raw, "replay_max_bytes", REPLAY_MAX_BYTES),
        observation_max_bytes=_exact(
            raw, "observation_max_bytes", OBSERVATION_MAX_BYTES
        ),
        truncation=_exact(raw, "truncation", truncation),
        store=_exact(raw, "store", STORE_RESPONSES),
        provider_order=_exact_list(raw, "provider_order", provider_order),
        provider_allow_fallbacks=_exact(raw, "provider_allow_fallbacks", allow_fallbacks),
        provider_require_parameters=_exact(raw, "provider_require_parameters", require_parameters),
        usage_contract=_exact(raw, "usage_contract", USAGE_CONTRACT),
        evidence_utc=_evidence_utc(raw),
        sha256=sha256,
    )


def load_reviewed_profile(path: Path | str = PROFILE_PATH) -> ModelProfile:
    """The tracked phase-one profile, by exact bytes.

    A custom path is accepted only when its bytes equal the tracked file's. A schema-equivalent
    file is not the reviewed profile: accepting one would let an arbitrary model and endpoint run
    under the approved profile ID.
    """
    reviewed = load_model_profile(PROFILE_PATH)
    supplied = Path(path)
    if supplied.resolve() == PROFILE_PATH.resolve():
        return reviewed
    candidate = load_model_profile(supplied)
    if candidate.sha256 != reviewed.sha256:
        raise ModelProfileError(
            f"{supplied.name} is not the reviewed phase-one profile; its bytes differ from "
            f"{PROFILE_PATH.name}"
        )
    return reviewed


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


_ARCHIVED_REPORT_PROFILES = {
    "phase1-gpt-v9.json": {
        "sha256": "7d7bca8d95ad655f6dd143373f4a8b5ca3bb0efd9486f2acd8b344bd6fc1617f",
        "profile_id": "phase1-gpt-v9",
        "schema_version": "7",
        "provider": "ckbuilders",
        "requested_model": "gpt-5.6-sol",
        "probed_response_model": "gpt-5.6-sol",
        "max_agent_query_attempts": 4,
        "provider_retry_backoff_seconds": (4, 8, 16),
        "replay_max_bytes": 128 * 1024,
    },
    "phase1-gpt-v8.json": {
        "sha256": "d0021bed7ae2a885933ba11d009ca6f33fdf801dda4940d4844e3f496cdd1362",
        "profile_id": "phase1-gpt-v8",
        "schema_version": "7",
        "provider": "openrouter",
        "requested_model": "openai/gpt-5-mini",
        "probed_response_model": "openai/gpt-5-mini",
        "max_agent_query_attempts": 4,
        "provider_retry_backoff_seconds": (4, 8, 16),
        "replay_max_bytes": 128 * 1024,
    },
    "phase1-gpt-v2.json": {
        "sha256": "117f5d35d699e6200b4d9fb96fce724947b57bfc63c3a5620467f088c90f4ade",
        "profile_id": "phase1-gpt-v2",
        "schema_version": "2",
        "provider": "ckbuilders",
        "requested_model": "gpt-5.6-sol",
        "probed_response_model": "gpt-5.6-sol",
        "max_agent_query_attempts": 1,
        "provider_retry_backoff_seconds": (),
        "replay_max_bytes": 0,
    },
    "phase1-gpt-v6.json": {
        "sha256": "266c77ef67d6954a0daf4d9dfdff87d8d788995930f54769c279dffc58e2a275",
        "profile_id": "phase1-gpt-v6",
        "schema_version": "5",
        "provider": "ckbuilders",
        "requested_model": "gpt-5.6-sol",
        "probed_response_model": "gpt-5.6-sol",
        "max_agent_query_attempts": 4,
        "provider_retry_backoff_seconds": (4, 8, 16),
        "replay_max_bytes": 0,
    },
}


def load_report_profile(path: Path | str) -> ReportModelProfile:
    """Load the current profile or one exact tracked historical reporting profile."""
    profile_path = Path(path)
    if profile_path.resolve() == PROFILE_PATH.resolve():
        return report_profile(load_model_profile(profile_path))
    try:
        relative = profile_path.resolve().relative_to(REPORT_PROFILE_DIR.resolve())
    except ValueError:
        raise ModelProfileError("a report profile must be a tracked reporting profile") from None
    if len(relative.parts) != 1 or relative.name not in _ARCHIVED_REPORT_PROFILES:
        raise ModelProfileError("the requested historical report profile is not supported")
    expected = _ARCHIVED_REPORT_PROFILES[relative.name]
    try:
        payload = profile_path.read_bytes()
    except OSError:
        raise ModelProfileError(f"no model profile at {profile_path.name}") from None
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected["sha256"]:
        raise ModelProfileError("the historical report profile bytes do not match their pin")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ModelProfileError("the historical report profile is not valid UTF-8 JSON") from None
    bound_fields = (
        "profile_id", "schema_version", "provider", "requested_model",
        "probed_response_model", "max_agent_query_attempts",
    )
    if not isinstance(raw, dict) or any(raw.get(key) != expected[key] for key in bound_fields):
        raise ModelProfileError("the historical report profile contract does not match its pin")
    stability = raw.get("model_stability")
    if stability not in STABILITIES:
        raise ModelProfileError("the historical report profile has invalid model stability")
    return ReportModelProfile(
        profile_id=str(expected["profile_id"]),
        sha256=digest,
        requested_model=str(expected["requested_model"]),
        probed_response_model=str(expected["probed_response_model"]),
        model_stability=str(stability),
        max_agent_query_attempts=int(expected["max_agent_query_attempts"]),
        provider_retry_backoff_seconds=tuple(expected["provider_retry_backoff_seconds"]),
        replay_max_bytes=int(expected["replay_max_bytes"]),
    )
