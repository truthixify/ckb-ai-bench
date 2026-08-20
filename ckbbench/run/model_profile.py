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

PROFILE_ID = "phase1-gpt-v4"
PROFILE_SCHEMA_VERSION = "3"
PROVIDER = "ckbuilders"
API_STYLE = "openai-responses"
USAGE_CONTRACT = "openai-responses-usage-v1"
TEMPERATURE = 0
LITELLM_NUM_RETRIES = 0
MAX_AGENT_QUERY_ATTEMPTS = 1
PROVIDER_REQUEST_TIMEOUT_SECONDS = 300
# GPT-5.6 reasoning is set intentionally rather than inherited from a moving alias default. Both
# values are profile fields, so the profile digest binds them.
REASONING_EFFORT = "medium"
REASONING_CONTEXT = "all_turns"
# The benchmark replays every Responses output item itself. Explicitly disabling provider-side
# storage makes that stateless contract unambiguous and avoids mixing manual history with an
# endpoint-managed conversation.
STORE_RESPONSES = False
# Production sends NO per-turn output ceiling. A cap would silently truncate a real coding turn and
# bias the five-task result; its absence is the phase-one behavior, not an oversight.
STABILITIES: frozenset[str] = frozenset({"dated_snapshot", "moving_alias", "unknown"})

REQUIRED_KEYS: frozenset[str] = frozenset({
    "api_base", "api_style", "drop_unsupported_params", "evidence_utc", "litellm_num_retries",
    "max_agent_query_attempts", "model_stability", "probed_response_model", "profile_id",
    "provider", "provider_request_timeout_seconds", "reasoning_context", "reasoning_effort",
    "requested_model", "schema_version", "store", "temperature", "usage_contract",
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
    temperature: int
    drop_unsupported_params: bool
    litellm_num_retries: int
    max_agent_query_attempts: int
    provider_request_timeout_seconds: int
    model_stability: str
    probed_response_model: str
    reasoning_effort: str
    reasoning_context: str
    store: bool
    usage_contract: str
    evidence_utc: str
    sha256: str

    @property
    def litellm_model_name(self) -> str:
        """The internal LiteLLM name. Exactly one `openai/` prefix, never doubled."""
        return f"openai/{self.requested_model}"

    def model_kwargs(self) -> dict[str, Any]:
        """Provider call settings. Deliberately credential-free: the key is supplied at call time."""
        return {
            "api_base": self.api_base,
            "temperature": self.temperature,
            "drop_params": self.drop_unsupported_params,
            "num_retries": self.litellm_num_retries,
            "timeout": self.provider_request_timeout_seconds,
            "stream": False,
            "store": self.store,
            "reasoning": self.reasoning(),
            # Deliberately NO max_output_tokens: a per-turn ceiling would truncate a real coding
            # turn. The controlled probe carries one because it is a single compatibility request.
        }

    def reasoning(self) -> dict[str, str]:
        """The reasoning settings the probe and the production model both send."""
        return {"effort": self.reasoning_effort, "context": self.reasoning_context}

    def summary_lines(self) -> list[str]:
        """Operator-facing provenance. Contains no credential and no endpoint query."""
        return [
            f"model profile: {self.profile_id} ({self.sha256[:12]}…)",
            f"requested model: {self.requested_model} ({self.model_stability})",
            f"api base: {self.api_base}",
            f"retries: litellm={self.litellm_num_retries} agent_attempts="
            f"{self.max_agent_query_attempts} | temperature={self.temperature}",
            f"provider request timeout: {self.provider_request_timeout_seconds}s",
            f"api style: {self.api_style} (root /responses)",
            f"reasoning: effort={self.reasoning_effort} context={self.reasoning_context} "
            f"store={str(self.store).lower()}",
            f"usage contract: {self.usage_contract}",
        ]


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


def _requested_model(raw: dict[str, Any]) -> str:
    value = _text(raw, "requested_model")
    if value != value.strip():
        raise ModelProfileError("requested_model must not be padded with whitespace")
    if value.startswith("openai/"):
        raise ModelProfileError(
            "requested_model is the catalog ID; the internal openai/ prefix is added at call time"
        )
    if "/" in value and not value.startswith("ft:"):
        raise ModelProfileError("requested_model must be a plain catalog ID")
    return value


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
    return ModelProfile(
        profile_id=_exact(raw, "profile_id", PROFILE_ID),
        schema_version=_exact(raw, "schema_version", PROFILE_SCHEMA_VERSION),
        provider=_exact(raw, "provider", PROVIDER),
        requested_model=_requested_model(raw),
        api_base=_api_base(raw),
        api_style=_exact(raw, "api_style", API_STYLE),
        temperature=_exact(raw, "temperature", TEMPERATURE),
        drop_unsupported_params=_exact(raw, "drop_unsupported_params", True),
        litellm_num_retries=_exact(raw, "litellm_num_retries", LITELLM_NUM_RETRIES),
        max_agent_query_attempts=_exact(raw, "max_agent_query_attempts", MAX_AGENT_QUERY_ATTEMPTS),
        provider_request_timeout_seconds=_exact(
            raw, "provider_request_timeout_seconds", PROVIDER_REQUEST_TIMEOUT_SECONDS
        ),
        model_stability=stability,
        probed_response_model=_text(raw, "probed_response_model"),
        reasoning_effort=_exact(raw, "reasoning_effort", REASONING_EFFORT),
        reasoning_context=_exact(raw, "reasoning_context", REASONING_CONTEXT),
        store=_exact(raw, "store", STORE_RESPONSES),
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
