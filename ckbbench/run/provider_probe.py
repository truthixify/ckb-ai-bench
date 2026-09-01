"""Two bounded provider checks for phase-one model readiness (ADR-0014).

The catalog check proves the selected model is exposed. The completion check proves the production
tool-call request works and returns the exact usage shape the harness records. Each is a single
request under an explicit one-use authorization: an instance sends at most one, and a failure
consumes that allowance rather than retrying.

Nothing here executes a returned tool call, and nothing retains a credential, an authorization
header, a raw body, prompt text, completion content, tool arguments, a response ID, or raw exception
text. Diagnostics name the phase and the exception class only.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ckbbench.run.model_profile import (
    REPO_ROOT,
    PROVIDER_REQUEST_TIMEOUT_SECONDS,
    ModelProfile,
    ModelProfileError,
    is_publishable,
    load_run_profile,
    publishable,
    safe_api_base,
)
from ckbbench.config import resolve_llm_api_key

# urllib's default `Python-urllib/x.y` is refused outright by common WAF bot rules, which turns a
# working endpoint into an unexplained 403. The production path (litellm/httpx) sends its own agent
# and is unaffected, so the probe states plainly what it is rather than imitating a browser.
USER_AGENT = "ckbbench-provider-probe/1.0"

CATALOG_PATH = "/models"
# The phase-one wire contract is the OpenAI Responses API (ADR-0014). Attempt 5 of 2026-08-16
# established that this deployment's root /chat/completions answers 2xx `text/html`, so the chat
# contract was replaced rather than re-aimed.
RESPONSES_PATH = "/responses"
# PROBE-ONLY safety ceiling. Production deliberately sends no per-turn cap (ADR-0014); this bounds
# one compatibility request while leaving room for configured reasoning plus a completed tool call. The
# completed-status gate below still fails closed if it is exhausted.
MAX_COMPLETION_TOKENS = 4096
# A response is read once, under a bound. An endpoint that streams forever must not be able to
# exhaust this process, and a body past the bound is classified rather than buffered.
MAX_RESPONSE_BYTES = 1 << 20
REQUEST_TIMEOUT_SECONDS = PROVIDER_REQUEST_TIMEOUT_SECONDS
# Written when a completion returns something that is not a JSON document, so the next authorized
# request is spent on a known cause instead of a guess.
# A distinct path: 17-completion-diagnostic.json is the retained negative evidence for the
# abandoned chat contract and must never be overwritten.
RESPONSES_DIAGNOSTIC_PATH = REPO_ROOT / "research" / "handoff" / "17-responses-diagnostic.json"

# Normalized to one of these or to "other". A media type is provider-controlled text, so it is
# matched against a fixed set rather than recorded verbatim.
CONTENT_TYPES: frozenset[str] = frozenset({
    "application/json", "application/problem+json", "application/x-ndjson",
    "text/event-stream", "text/html", "text/plain",
})
CONTENT_ENCODINGS: frozenset[str] = frozenset({"identity", "gzip", "deflate", "br", "zstd"})
BODY_KINDS: tuple[str, ...] = (
    "json", "sse", "html", "empty", "utf8_bom", "plain_text", "invalid_utf8", "oversized", "other",
)
# Control characters that make a body something other than plain text. Tab/CR/LF are ordinary text.
_BODY_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# One harmless fixed call the model is asked to emit. It is never executed.
PROBE_COMMAND = "echo ckbbench-probe"
PROBE_INSTRUCTION = f"Call the bash tool exactly once with the command: {PROBE_COMMAND}"
EXPECTED_TOOL = "bash"
# The protocol owns these request fields. Profile extensions are checked separately, and changing
# an API key or base URL cannot alter them.
BASE_PAYLOAD_KEYS: tuple[str, ...] = (
    "model", "input", "tools", "stream", "store", "reasoning", "max_output_tokens",
)
# Responses reports usage under its own names. Local evidence keeps them so the wire shape is not
# obscured; the harness's public prompt/completion names are mapped once, in the usage ledger.
NATIVE_USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
EXPECTED_ARGUMENTS = {"command": PROBE_COMMAND}
# Catalog IDs that plausibly belong to the GPT/OpenAI family. Candidate discovery only: the
# selection rule and the user decide, and a name alone never establishes snapshot stability.
_GPT_FAMILY = re.compile(r"(^|[-_/])gpt", re.IGNORECASE)
_SAFE_METADATA = ("owned_by", "created", "object", "root", "parent")


class ProbeError(RuntimeError):
    """A sanitized probe failure. Carries a phase and an exception class, never provider text."""


@dataclass(frozen=True)
class CatalogCandidate:
    """One sanitized catalog entry. Only non-secret identification metadata is kept."""

    model_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogEvidence:
    requests_sent: int
    status_ok: bool
    status_class: str
    candidate_count: int
    candidates: tuple[CatalogCandidate, ...]


@dataclass(frozen=True)
class CompletionEvidence:
    requests_sent: int
    status_ok: bool
    status_class: str
    requested_model: str
    returned_model: str | None
    response_completed: bool
    exactly_one_expected_tool_call: bool
    # Native Responses names, deliberately: this file records the provider's own vocabulary.
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    token_identity_holds: bool
    # Written offline after the profile file exists; never a second request.
    model_profile_sha256: str | None = None


@dataclass(frozen=True)
class ResponseFacts:
    """Everything the probe may know about a response: shape and size, never content.

    The parsed document is returned alongside these facts rather than stored on them, so a body can
    never be serialized by accident through the object that gets written to evidence.
    """

    status_class: str
    content_type: str
    content_encoding: str
    # DECODED response bytes observed through httpx (it content-decodes before yielding), not
    # network transfer bytes and not the full body length: for an oversized response this is a
    # truthful lower bound that includes the crossing chunk, which is never buffered.
    byte_count: int
    body_kind: str

    @property
    def status_ok(self) -> bool:
        return self.status_class == "2xx"


class SanitizedResponse(ProbeError):
    """A response this probe can describe but must not use. Carries sanitized facts only."""

    def __init__(self, message: str, facts: ResponseFacts, *, requests_sent: int,
                 requested_model: str | None, api_base: str) -> None:
        super().__init__(message)
        self.facts = facts
        self.requests_sent = requests_sent
        self.requested_model = requested_model
        self.api_base = api_base


class NonJsonResponse(SanitizedResponse):
    """A successful response whose body is not a JSON document."""

    def __init__(self, facts: ResponseFacts, **kwargs: Any) -> None:
        super().__init__(f"the endpoint returned a non-JSON body ({facts.body_kind})", facts,
                         **kwargs)


class ErrorStatusResponse(SanitizedResponse):
    """A non-2xx response. Its body is classified for the diagnostic and never inspected.

    A WAF page, a routing error and a proxy failure are exactly the cases a bare status code cannot
    distinguish, so they are described the same way a non-JSON success is.
    """

    def __init__(self, facts: ResponseFacts, *, status: int, **kwargs: Any) -> None:
        super().__init__(f"the endpoint returned HTTP {status}", facts, **kwargs)
        self.status = status


def _normalized(value: str | None, allowed: frozenset[str], *, default: str) -> str:
    """A header value reduced to one allowlisted token, or "other". Never echoed verbatim."""
    if not isinstance(value, str) or not value.strip():
        return default
    token = value.split(";")[0].strip().lower()
    return token if token in allowed else "other"


def classify_body(raw: bytes, *, content_type: str, truncated: bool) -> tuple[str, Any]:
    """Classify a response body without retaining or echoing any of it.

    Returns the body kind and, only for a JSON document, the parsed value. Every other kind returns
    None: the probe must learn the shape of an unusable response without keeping it.
    """
    if truncated:
        return "oversized", None
    if not raw:
        return "empty", None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid_utf8", None
    # A BOM is reported before parsing: json.loads accepts some BOM-prefixed input, and an endpoint
    # emitting one is a finding rather than a detail to normalize away.
    if text.startswith("\ufeff"):
        return "utf8_bom", None
    try:
        return "json", json.loads(text)
    except json.JSONDecodeError:
        pass
    stripped = text.lstrip()
    if content_type == "text/event-stream" or stripped.startswith(("data:", "event:", "retry:")):
        return "sse", None
    if content_type == "text/html" or stripped[:1] == "<":
        return "html", None
    if not _BODY_CONTROL.search(text):
        return "plain_text", None
    return "other", None


def _default_client(timeout: float) -> Any:
    """httpx with redirects off and no retries.

    Production LiteLLM speaks over httpx, so what this proves about an endpoint is what the
    benchmark will actually meet. urllib proved something else once already.
    """
    import httpx

    return httpx.Client(
        transport=httpx.HTTPTransport(retries=0), follow_redirects=False, timeout=timeout
    )


class OneRequestTransport:
    """Sends at most one HTTP request. A failed send still consumes the allowance."""

    def __init__(self, *, client: Any | None = None, max_bytes: int = MAX_RESPONSE_BYTES,
                 timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._timeout = timeout
        self.requests_sent = 0
        self.last_status: int | None = None

    def send(self, *, method: str, url: str, api_key: str,
             payload: dict[str, Any] | None = None) -> tuple[ResponseFacts, Any]:
        # Nothing about the request is retained: the prompt and tool schema stay in the request
        # object only. A test inspects the injected mock transport instead.
        if self.requests_sent:
            raise ProbeError("this probe is authorized for exactly one request")
        self.requests_sent += 1
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"

        import httpx

        client, owned = (self._client, False) if self._client is not None else (
            _default_client(self._timeout), True
        )
        try:
            with client.stream(method, url, headers=headers, content=body) as response:
                # A redirect is refused unread: it already names the forbidden condition, and its
                # body would describe an origin this authorization never covered.
                if 300 <= response.status_code < 400:
                    raise ProbeError(
                        f"the endpoint attempted a redirect (HTTP {response.status_code})"
                    )
                raw, observed, truncated = self._read_bounded(response)
                content_type = _normalized(
                    response.headers.get("content-type"), CONTENT_TYPES, default="other"
                )
                content_encoding = _normalized(
                    response.headers.get("content-encoding"), CONTENT_ENCODINGS,
                    default="identity",
                )
                kind, document = classify_body(
                    raw, content_type=content_type, truncated=truncated
                )
                facts = ResponseFacts(
                    status_class=_status_class(response.status_code),
                    content_type=content_type,
                    content_encoding=content_encoding,
                    byte_count=observed,
                    body_kind=kind,
                )
                self.last_status = response.status_code
                # An error body is classified but never handed back: a 4xx/5xx document is
                # diagnostic evidence, never completion evidence.
                return facts, (document if facts.status_ok else None)
        except ProbeError:
            raise
        except httpx.HTTPError as exc:
            raise ProbeError(f"the request failed ({type(exc).__name__})") from None
        except Exception as exc:
            raise ProbeError(f"the request failed ({type(exc).__name__})") from None
        finally:
            if owned:
                client.close()

    def _read_bounded(self, response: Any) -> tuple[bytes, int, bool]:
        """Read once, up to the bound.

        Returns the buffered prefix, the number of DECODED bytes observed, and whether the bound
        was crossed. httpx content-decodes before yielding, so this counts decoded bytes rather than
        network transfer bytes. The count includes the crossing chunk so an oversized report states a
        truthful lower bound; that chunk is never buffered or classified.
        """
        chunks: list[bytes] = []
        observed = 0
        for chunk in response.iter_bytes():
            observed += len(chunk)
            if observed > self._max_bytes:
                return b"".join(chunks), observed, True
            chunks.append(chunk)
        return b"".join(chunks), observed, False


def _endpoint(api_base: str, path: str) -> str:
    """The one strict safe-base rule, shared with the profile loader."""
    try:
        base = safe_api_base(api_base)
    except ModelProfileError as exc:
        raise ProbeError(f"unsafe api base: {exc}") from None
    return f"{base}{path}"


def _status_class(status: int) -> str:
    return f"{status // 100}xx"


def _sanitized_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    """Permitted field names AND publishable values: a server controls both.

    An unpublishable value is dropped, never echoed into an error.
    """
    out: dict[str, Any] = {}
    for key in _SAFE_METADATA:
        value = entry.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            out[key] = value
        elif is_publishable(value):
            out[key] = value
    return out


def gpt_candidates(payload: Any) -> tuple[CatalogCandidate, ...]:
    """The GPT-family entries of a catalog body, sanitized. Non-candidates are discarded."""
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ProbeError("the catalog response has no data list")
    found: list[CatalogCandidate] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        # An ID this project may not publish is not a candidate: it would be printed, offered for
        # selection, and written into tracked provenance.
        if not is_publishable(model_id) or not _GPT_FAMILY.search(model_id):
            continue
        found.append(CatalogCandidate(model_id=model_id, metadata=_sanitized_metadata(entry)))
    return tuple(sorted(found, key=lambda c: c.model_id))


def probe_catalog(*, api_base: str, api_key: str,
                  transport: OneRequestTransport | None = None) -> CatalogEvidence:
    """One authenticated GET of the catalog. Retains only sanitized GPT candidates."""
    sender = transport or OneRequestTransport()
    facts, payload = sender.send(
        method="GET", url=_endpoint(api_base, CATALOG_PATH), api_key=api_key
    )
    _reject_unusable(facts, sender, requested_model=None, api_base=safe_api_base(api_base))
    candidates = gpt_candidates(payload)
    return CatalogEvidence(
        requests_sent=sender.requests_sent,
        status_ok=facts.status_ok,
        status_class=facts.status_class,
        candidate_count=len(candidates),
        candidates=candidates,
    )


def canonical_bash_tool() -> dict[str, Any]:
    """A deep copy of the production Responses tool schema (flat: no nested `function` key).

    The imported object is mutable and shared. Handing it straight to a request made the payload and
    the schema it is checked against the same object, so a nested mutation was invisible to both.
    """
    return copy.deepcopy(_bash_tool())


def canonical_request_extensions(profile: ModelProfile) -> dict[str, Any]:
    """A deep copy of the selected profile's validated request extensions."""
    return profile.request_body_extensions


def _bash_tool() -> dict[str, Any]:
    """The production Responses tool schema from the agent fork.

    The fork is a sibling directory, not an installed package: `scripts/run-matrix.sh` puts it on
    PYTHONPATH. A caller that forgot to must not discover it by spending an authorized request, so
    the path is added here instead of failing at the send boundary.
    """
    try:
        from minisweagent.models.utils.actions_toolcall_response import BASH_TOOL_RESPONSE_API
    except ModuleNotFoundError:
        fork = str(REPO_ROOT / "agent")
        if fork not in sys.path:
            sys.path.insert(0, fork)
        try:
            from minisweagent.models.utils.actions_toolcall_response import BASH_TOOL_RESPONSE_API
        except ModuleNotFoundError:
            raise ProbeError("the agent fork is not importable; no request was sent") from None
    return BASH_TOOL_RESPONSE_API


def completion_payload(profile: ModelProfile) -> dict[str, Any]:
    """The production-shaped minimal Responses request.

    Same optional parameters as the production model, so what this proves accepted is the request
    shape the benchmark sends. Chat-only `messages`, `n` and `max_tokens` are absent by contract.
    """
    payload: dict[str, Any] = {
        "model": profile.requested_model,
        "input": [{"role": "user", "content": PROBE_INSTRUCTION}],
        "tools": [canonical_bash_tool()],
        "stream": False,
        "store": profile.store,
        "reasoning": profile.reasoning(),
        "max_output_tokens": MAX_COMPLETION_TOKENS,
    }
    payload.update(canonical_request_extensions(profile))
    if profile.temperature is not None:
        payload["temperature"] = profile.temperature
    return payload


def validate_completion_payload(payload: Any, *, profile: ModelProfile) -> dict[str, Any]:
    """Prove the request is the reviewed shape before it can consume the authorization.

    Everything here is checkable offline. A payload defect discovered at the send boundary costs a
    grant; discovered here it costs nothing.
    """
    if not isinstance(payload, dict):
        raise ProbeError("the completion payload is not an object")
    if not is_publishable(payload.get("model")):
        raise ProbeError("the completion payload names no publishable model")
    if payload["model"] != profile.requested_model:
        raise ProbeError("the completion payload model differs from the selected profile")
    extension_fields = canonical_request_extensions(profile)
    if profile.temperature is None and "temperature" in payload:
        raise ProbeError("the completion payload must omit unsupported temperature")
    if profile.temperature is not None and payload.get("temperature") != profile.temperature:
        raise ProbeError(
            f"the completion payload must set temperature to {profile.temperature!r}"
        )
    for field_name, expected in (("stream", False), ("store", profile.store),
                                 ("reasoning", profile.reasoning()),
                                 *extension_fields.items(),
                                 ("max_output_tokens", MAX_COMPLETION_TOKENS)):
        value = payload.get(field_name)
        if isinstance(value, bool) != isinstance(expected, bool) or value != expected:
            raise ProbeError(f"the completion payload must set {field_name} to {expected!r}")
    payload_input = payload.get("input")
    if (not isinstance(payload_input, list) or len(payload_input) != 1
            or payload_input[0] != {"role": "user", "content": PROBE_INSTRUCTION}):
        raise ProbeError("the completion payload must carry exactly the fixed probe instruction")
    tools = payload.get("tools")
    if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict):
        raise ProbeError("the completion payload must offer exactly one tool")
    # Responses tools are FLAT: name and parameters sit at the top level, with no nested
    # `function` object. A nested chat-shaped tool is the wrong contract, not a variant.
    tool = tools[0]
    if tool.get("type") != "function" or tool.get("name") != EXPECTED_TOOL:
        raise ProbeError(f"the completion payload's one tool must be the {EXPECTED_TOOL} function")
    # Byte-level equality with the production schema: a changed description, parameter schema,
    # required list or extra function key all alter what the model is actually offered.
    if tool != canonical_bash_tool():
        raise ProbeError("the completion payload's tool is not the production bash schema")
    # Only the model varies. Any other top-level key changes what was authorized.
    expected_keys = set(BASE_PAYLOAD_KEYS) | set(extension_fields)
    if profile.temperature is not None:
        expected_keys.add("temperature")
    if set(payload) != expected_keys:
        unexpected = len(set(payload) - expected_keys)
        missing = sorted(expected_keys - set(payload))
        raise ProbeError(
            "the completion payload must carry exactly the reviewed top-level keys "
            f"({unexpected} unexpected, missing {missing})"
        )
    return payload


def _usage_ints(payload: Any) -> tuple[int | None, int | None, int | None]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None, None, None
    out: list[int | None] = []
    for field_name in NATIVE_USAGE_FIELDS:
        value = usage.get(field_name)
        out.append(None if isinstance(value, bool) or not isinstance(value, int) else value)
    return out[0], out[1], out[2]


def response_completed(payload: Any) -> bool:
    """Whether the provider says it finished. Recorded explicitly, never folded into another flag."""
    return isinstance(payload, dict) and payload.get("status") == "completed"


def expected_tool_call(payload: Any) -> bool:
    """Exactly one COMPLETED Responses `function_call` for bash with the exact probe command.

    Responses returns a flat `output` list rather than `choices[].message.tool_calls`. Type, name and
    arguments are not enough: a call with no `call_id` cannot be linked to its result, and a call
    emitted inside an unfinished response may be truncated. Both are refused here, so the one
    authorized request certifies a shape the benchmark can actually execute against.

    The returned arguments are compared, never retained, printed, or executed.
    """
    if not response_completed(payload):
        return False
    output = payload.get("output")
    if not isinstance(output, list):
        return False
    calls = [item for item in output
             if isinstance(item, dict) and item.get("type") == "function_call"]
    if len(calls) != 1:
        return False
    call = calls[0]
    if call.get("status") != "completed":
        return False
    call_id = call.get("call_id")
    if not isinstance(call_id, str) or not call_id.strip():
        return False
    if call.get("name") != EXPECTED_TOOL:
        return False
    raw = call.get("arguments")
    if not isinstance(raw, str):
        return False
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return arguments == EXPECTED_ARGUMENTS


def probe_completion(*, profile: ModelProfile, api_key: str,
                     transport: OneRequestTransport | None = None) -> CompletionEvidence:
    """One authenticated minimal completion. The returned tool call is counted, never executed."""
    model = profile.requested_model
    if not is_publishable(model):
        raise ProbeError("the selected model ID is not a publishable identifier")
    # Built and validated before the send boundary exists, so a payload defect cannot spend a grant.
    request_payload = validate_completion_payload(completion_payload(profile), profile=profile)
    url = _endpoint(profile.api_base, RESPONSES_PATH)
    sender = transport or OneRequestTransport()
    facts, payload = sender.send(
        method="POST", url=url, api_key=api_key, payload=request_payload,
    )
    _reject_unusable(facts, sender, requested_model=model, api_base=profile.api_base)
    returned = payload.get("model") if isinstance(payload, dict) else None
    # The returned identity is written into the tracked profile, so it obeys the same rule.
    if not is_publishable(returned):
        returned = None
    prompt, completion, total = _usage_ints(payload)
    identity = (
        None not in (prompt, completion, total)
        and all(v >= 0 for v in (prompt, completion, total))
        and total == prompt + completion
    )
    return CompletionEvidence(
        requests_sent=sender.requests_sent,
        status_ok=facts.status_ok,
        status_class=facts.status_class,
        requested_model=model,
        returned_model=returned,
        response_completed=response_completed(payload),
        exactly_one_expected_tool_call=expected_tool_call(payload),
        input_tokens=prompt,
        output_tokens=completion,
        total_tokens=total,
        token_identity_holds=bool(identity),
    )


def _reject_unusable(facts: ResponseFacts, sender: OneRequestTransport, *,
                     requested_model: str | None, api_base: str) -> None:
    """Turn any response that cannot be evidence into a sanitized, diagnosable failure."""
    common = {"requests_sent": sender.requests_sent, "requested_model": requested_model,
              "api_base": api_base}
    if not facts.status_ok:
        raise ErrorStatusResponse(facts, status=sender.last_status or 0, **common)
    if facts.body_kind != "json":
        raise NonJsonResponse(facts, **common)


def _evidence_document(kind: str, evidence: Any, *, api_base: str, utc: str) -> dict[str, Any]:
    """The permitted sanitized fields only. Nothing else from the response is representable here."""
    common = {"kind": kind, "utc": utc, "api_base": api_base,
              "requests_sent": evidence.requests_sent, "status_ok": evidence.status_ok,
              "status_class": evidence.status_class}
    if isinstance(evidence, CatalogEvidence):
        return {
            **common,
            "candidate_count": evidence.candidate_count,
            "candidates": [{"model_id": c.model_id, **c.metadata} for c in evidence.candidates],
        }
    return {
        **common,
        "requested_model": evidence.requested_model,
        "returned_model": evidence.returned_model,
        "response_completed": evidence.response_completed,
        "exactly_one_expected_tool_call": evidence.exactly_one_expected_tool_call,
        "input_tokens": evidence.input_tokens,
        "output_tokens": evidence.output_tokens,
        "total_tokens": evidence.total_tokens,
        "token_identity_holds": evidence.token_identity_holds,
        "model_profile_sha256": evidence.model_profile_sha256,
    }


COMPLETION_EVIDENCE_FIELDS = (
    "kind", "utc", "api_base", "requests_sent", "status_ok", "status_class",
    "requested_model", "returned_model", "response_completed", "exactly_one_expected_tool_call",
    "input_tokens", "output_tokens", "total_tokens", "token_identity_holds",
    "model_profile_sha256",
)


def _accepted_completion(document: dict[str, Any]) -> None:
    """Every invariant an accepted phase-one completion must already satisfy.

    Finalization is the moment a document becomes the profile's evidence, so a failed, malformed or
    tampered one must fail here rather than gain a digest that makes it look reviewed.
    """
    # Proved to be an object before any field access: a list or scalar must produce this same
    # sanitized refusal, not an AttributeError past the caller's handler.
    if not isinstance(document, dict):
        raise ProbeError("evidence must be a JSON object")
    if document.get("kind") != "completion":
        raise ProbeError("only completion evidence carries the profile digest")
    if document.get("model_profile_sha256") is not None:
        raise ProbeError("this evidence already carries a profile digest")
    # Counted, never named: an unexpected key is attacker-controlled text like any other value.
    extra = len(set(document) - set(COMPLETION_EVIDENCE_FIELDS))
    if extra:
        raise ProbeError(f"evidence carries {extra} field(s) outside the schema")
    missing = [f for f in COMPLETION_EVIDENCE_FIELDS if f not in document]
    if missing:
        raise ProbeError(f"evidence is missing required fields: {missing}")
    requests_sent = document["requests_sent"]
    if isinstance(requests_sent, bool) or not isinstance(requests_sent, int) or requests_sent != 1:
        raise ProbeError("accepted evidence records exactly one request")
    for flag in ("status_ok", "response_completed", "exactly_one_expected_tool_call",
                 "token_identity_holds"):
        if document[flag] is not True:
            raise ProbeError(f"the completion did not satisfy {flag}")
    if document["status_class"] != "2xx":
        raise ProbeError("accepted evidence records a successful status class")
    tokens = [document[f] for f in NATIVE_USAGE_FIELDS]
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in tokens):
        raise ProbeError("usage must be three non-negative integers")
    if tokens[0] + tokens[1] != tokens[2]:
        raise ProbeError("usage must satisfy total = prompt + completion")
    # One rule, one error type: the caller of this boundary handles ProbeError only.
    try:
        safe_api_base(document["api_base"], field="api_base")
        for field_name in ("requested_model", "returned_model"):
            publishable(document[field_name], field=field_name)
    except ModelProfileError as exc:
        raise ProbeError(str(exc)) from None


def prepare_destination(path: Path, *, label: str) -> None:
    """Prove this file can be written BEFORE a request exists.

    Every local reason a write could fail is checkable now. Discovering one after the response would
    throw away the only thing the spent grant bought.

    An existing destination is refused rather than overwritten: earlier evidence is the record of a
    request that cannot be repeated.
    """
    if path.exists() or path.is_symlink():
        raise ProbeError(f"the {label} destination already exists; refusing to overwrite evidence")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProbeError(
            f"the {label} destination parent cannot be created ({type(exc).__name__})"
        ) from None
    if not path.parent.is_dir():
        raise ProbeError(f"the {label} destination parent is not a directory")
    # An exclusive random name, removed only if this call created it. A fixed scratch name would
    # truncate and delete an unrelated file that happened to be called that.
    handle, probe_name = None, None
    try:
        handle, probe_name = tempfile.mkstemp(prefix=".ckbbench-writable-", dir=str(path.parent))
    except OSError as exc:
        raise ProbeError(
            f"the {label} destination is not writable ({type(exc).__name__})"
        ) from None
    finally:
        if handle is not None:
            os.close(handle)
        if probe_name is not None:
            try:
                os.unlink(probe_name)
            except OSError:
                pass


def write_json_evidence(path: Path, document: dict[str, Any], *, label: str) -> None:
    """The one writer for every sanitized artifact this probe produces.

    Exclusive creation, so a destination that appeared between preflight and now is refused instead
    of silently replacing a record of an unrepeatable request.
    """
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError as exc:
        raise ProbeError(
            f"the {label} could not be written ({type(exc).__name__})"
        ) from None


def diagnostic_document(exc: SanitizedResponse, *, utc: str) -> dict[str, Any]:
    """The only record kept of an unusable response: shape and size, never content."""
    return {
        "kind": "completion-diagnostic",
        "utc": utc,
        "api_base": exc.api_base,
        "requested_model": exc.requested_model,
        "status_class": exc.facts.status_class,
        "content_type": exc.facts.content_type,
        "content_encoding": exc.facts.content_encoding,
        "byte_count": exc.facts.byte_count,
        "body_kind": exc.facts.body_kind,
        "requests_sent": exc.requests_sent,
    }


def finalize_evidence(document: dict[str, Any], profile: Any) -> dict[str, Any]:
    """Insert the tracked profile digest into already-retained completion evidence. Zero requests.

    The digest cannot be known before the completion, because the profile records that request's UTC
    and returned model. So it is written here, offline, only after the evidence and the profile
    agree about the model path they describe.
    """
    _accepted_completion(document)
    for field_name, expected in (("requested_model", profile.requested_model),
                                 ("returned_model", profile.probed_response_model),
                                 ("api_base", profile.api_base),
                                 ("utc", profile.evidence_utc)):
        if document.get(field_name) != expected:
            raise ProbeError(
                f"the evidence and the profile disagree about {field_name}; "
                "they do not describe one run"
            )
    if not re.fullmatch(r"[0-9a-f]{64}", profile.sha256):
        raise ProbeError("the profile digest is not 64 lowercase hex characters")
    # Rebuilt from the allowed fields, never copied: an unrecognized key cannot survive review by
    # riding along inside the document that the digest then blesses.
    finalized = {name: document[name] for name in COMPLETION_EVIDENCE_FIELDS}
    finalized["model_profile_sha256"] = profile.sha256
    return finalized


def _finalize(out: str, profile_path: str | None) -> int:
    from ckbbench.run.model_profile import load_reviewed_profile

    if not profile_path:
        print("REFUSED: finalize needs --profile", file=sys.stderr)
        return 2
    target = Path(out)
    try:
        document = json.loads(target.read_text())
        # The reviewed loader, not a schema check: only the tracked profile's exact bytes may
        # certify evidence, or an alternate file could bless a run it never described.
        profile = load_reviewed_profile(profile_path)
        finalized = finalize_evidence(document, profile)
    except (OSError, json.JSONDecodeError):
        print(f"REFUSED: cannot read {target.name} as evidence JSON", file=sys.stderr)
        return 2
    except (ProbeError, ModelProfileError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # No traceback and no value: an unforeseen shape in operator-supplied JSON must still leave
        # by this one sanitized exit rather than printing whatever it contained.
        print("REFUSED: the evidence document could not be validated", file=sys.stderr)
        return 1
    target.write_text(json.dumps(finalized, indent=2, sort_keys=True) + "\n")
    print(json.dumps(finalized, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """One authorized provider check: ``catalog``, ``completion``, or offline ``finalize``.

    The API key is read from the environment and never printed. Catalog mode takes an explicit API
    root; completion mode takes it only from the reviewed profile. Exactly one request is sent, and
    a failure consumes it.
    """
    import argparse
    import datetime as _dt

    parser = argparse.ArgumentParser(description="One bounded provider readiness check.")
    parser.add_argument("mode", choices=("catalog", "completion", "finalize"))
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible /v1 root")
    parser.add_argument(
        "--profile", default=None,
        help="completion/finalize: supported profile alias or tracked profile JSON",
    )
    parser.add_argument("--out", required=True, help="Where to write the sanitized evidence JSON")
    parser.add_argument(
        "--diagnostic-out", default=None,
        help="completion: where to write a sanitized failure diagnostic",
    )

    args = parser.parse_args(argv)

    if args.mode == "finalize":
        if args.api_base or args.diagnostic_out:
            print("REFUSED: finalize mode accepts only --profile and --out", file=sys.stderr)
            return 2
        return _finalize(args.out, args.profile)

    if args.mode == "catalog":
        if args.profile or args.diagnostic_out:
            print("REFUSED: catalog mode accepts only --api-base and --out", file=sys.stderr)
            return 2
        if not args.api_base:
            print("REFUSED: --api-base is required", file=sys.stderr)
            return 2
    profile = None
    if args.mode == "completion":
        if args.api_base:
            print("REFUSED: completion mode takes its API base from --profile", file=sys.stderr)
            return 2
        if not args.profile:
            print("REFUSED: completion mode needs --profile", file=sys.stderr)
            return 2
        try:
            profile = load_run_profile(args.profile)
        except ModelProfileError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        args.api_base = profile.api_base
    api_key = resolve_llm_api_key(
        profile.credential_env if profile is not None else None, default=""
    )
    if not api_key:
        print("REFUSED: CKBBENCH_LLM_API_KEY is not set", file=sys.stderr)
        return 2
    diagnostic_path = (
        Path(args.diagnostic_out) if args.diagnostic_out else RESPONSES_DIAGNOSTIC_PATH
    )

    utc = _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        # Both destinations are checked before the request exists. Preflighting only the failure
        # path once let a valid response consume the grant and then be lost to a local path error.
        prepare_destination(Path(args.out), label="evidence")
        if args.mode == "completion":
            prepare_destination(diagnostic_path, label="diagnostic")
    except ProbeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        api_base = safe_api_base(args.api_base)
        if args.mode == "catalog":
            evidence: Any = probe_catalog(api_base=api_base, api_key=api_key)
        else:
            assert profile is not None
            evidence = probe_completion(profile=profile, api_key=api_key)
    except SanitizedResponse as exc:
        # The request is spent either way. Retaining the sanitized classification is what lets the
        # next authorized one be aimed at a known cause instead of a guess. This covers a non-2xx
        # response as well: a WAF page, a routing error and a proxy failure are exactly the cases a
        # bare status code cannot tell apart.
        diagnostic = diagnostic_document(exc, utc=utc)
        try:
            write_json_evidence(diagnostic_path, diagnostic,
                                label="sanitized diagnostic")
            print(f"wrote sanitized diagnostic to {diagnostic_path}", file=sys.stderr)
        except ProbeError as write_failure:
            print(f"DIAGNOSTIC NOT WRITTEN: {write_failure}", file=sys.stderr)
        print(f"PROBE FAILED: {exc}", file=sys.stderr)
        print(json.dumps(diagnostic, indent=2, sort_keys=True))
        return 1
    except (ProbeError, ModelProfileError) as exc:
        print(f"PROBE FAILED: {exc}", file=sys.stderr)
        return 1

    document = _evidence_document(args.mode, evidence, api_base=api_base, utc=utc)
    try:
        write_json_evidence(Path(args.out), document, label="evidence")
    except ProbeError as exc:
        # The request is spent. Say so plainly rather than exiting as if it had been retained.
        print(f"EVIDENCE NOT WRITTEN: {exc}", file=sys.stderr)
        print(json.dumps(document, indent=2, sort_keys=True))
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
