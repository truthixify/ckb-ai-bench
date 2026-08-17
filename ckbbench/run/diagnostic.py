"""Bounded, content-free provider request-failure diagnostic (Task 23 design, review revision 6).

Task 22 collapsed two distinct failures into `provider_failure_category: "request"` and could not say
whether a request ever reached the network. This module projects, per provider attempt, three things
that answer that without retaining any content:

- `outcome`: which exception family the attempt ended in, chosen by type only;
- `transport_state`: whether the pinned HTTPX handler was entered, returned, or could not be
  observed; and
- `input_shape`: the structure of the Responses input we were about to send.

Nothing here is accepted benchmark evidence. The artifact is written only by `./bench diagnose`, is
never read by a report, and never enters a `RunResult`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import types
from typing import Any, Callable

DIAGNOSTIC_SCHEMA_VERSION = "2.0.0"

ITEM_TYPES: tuple[str, ...] = (
    "system", "user", "assistant_message", "reasoning",
    "function_call", "function_call_output", "other",
)
OUTCOMES: tuple[str, ...] = (
    "responded", "bad_request", "not_found", "request_other", "other_failure",
)
# `handler_entered_no_response` is deliberately not "sent": entering the handler proves the transport
# handler was entered, not that bytes left the host. DNS, connect, TLS or write failure all occur
# after entry.
TRANSPORT_STATES: tuple[str, ...] = (
    "not_started", "handler_entered_no_response", "response_seen", "unobserved",
)

MAX_RECORDS = 16
MAX_PROVIDER_REQUESTS = 16
MAX_SEQUENCE = 64
MAX_COUNT = 4096
MAX_TURN = 80
MAX_ATTEMPT = 3
MAX_ARTIFACT_BYTES = 32768

# The reviewed synchronous seam. LiteLLM 1.72.0 reaches it in both configurations: HTTPHandler builds
# `httpx.Client(transport=self._create_sync_transport())`, which returns an `HTTPTransport` when
# `litellm.force_ipv4` and otherwise None, in which case httpx installs its default `HTTPTransport`.
SUPPORTED_LITELLM = "1.72.0"
SUPPORTED_HTTPX = "0.28.1"
EXPECTED_SEAM_MODULE = "httpx._transports.default"
EXPECTED_SEAM_QUALNAME = "HTTPTransport.handle_request"
EXPECTED_SEAM_SIGNATURE = "(self, request: 'Request') -> 'Response'"
# The FULL code object, not `sha256(co_code)`: that opcode stream excludes co_names, co_consts,
# defaults, closure and co_exceptiontable, so a global-name swap or an emptied exception table both
# produce a behaviour-changing callable with an identical opcode digest. Also NOT marshal, which is
# refcount-sensitive: CPython emits FLAG_REF only when Py_REFCNT > 1, so an unrelated reference
# elsewhere in the process changes the bytes and would fail a fixed constant for no real reason.
EXPECTED_SEAM_CODE_SHA256 = "cd1ef1d401a0940b263a21ddbb6c3150373a345ff63a2694dc24010cc396161e"

_CODE_FIELDS = (
    "co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals", "co_stacksize",
    "co_flags", "co_names", "co_varnames", "co_freevars", "co_cellvars",
)

_TOP_LEVEL_KEYS = frozenset({
    "diagnostic_schema_version", "run_id", "instrumentation_ok", "records", "records_dropped",
})
_RECORD_KEYS = frozenset({
    "turn_index", "attempt_index", "outcome", "transport_state", "input_shape",
})
_SHAPE_KEYS = frozenset({
    "item_count", "type_sequence", "type_sequence_truncated", "type_histogram", "pairing",
    "invariants",
})
_PAIRING_KEYS = frozenset({
    "call_items", "output_items", "valid_call_ids", "valid_output_ids",
    "blank_or_missing_call_ids", "blank_or_missing_output_ids",
    "duplicate_call_ids", "duplicate_output_ids",
    "ids_matched", "ids_matched_exactly_once", "unmatched_call_ids", "unmatched_output_ids",
})
_INVARIANT_KEYS = frozenset({
    "first_item_is_system", "last_item_is_function_call_output",
    "every_call_item_has_a_valid_id", "every_output_item_has_a_valid_id",
    "every_call_id_unique", "every_output_id_unique",
    "every_call_named_bash", "every_call_arguments_is_string",
    "any_item_missing_type", "any_reasoning_item_present", "every_call_paired_exactly_once",
})


class InstrumentationError(Exception):
    """A harness-controlled value was invalid.

    Provider-controlled values reduce to a safe bounded token; harness-controlled state must fail so
    a defect cannot be published as a healthy-looking fact.
    """


class DiagnosticLimitReached(Exception):
    """The diagnostic request ceiling was reached. Raised before any transport."""


def code_digest(code: Any) -> str | None:
    """Refcount-independent structural digest of a code object, nested code included.

    Includes `co_exceptiontable`: on CPython 3.12 it controls exception-handler ranges and targets,
    and the pinned method carries one for `map_httpcore_exceptions()`.
    """

    def canonical(obj: Any) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for field in _CODE_FIELDS:
            value = getattr(obj, field)
            body[field] = list(value) if isinstance(value, tuple) else value
        body["co_code"] = obj.co_code.hex()
        body["co_exceptiontable"] = obj.co_exceptiontable.hex()
        body["co_consts"] = [
            ["<code>", canonical(v)] if isinstance(v, types.CodeType)
            else [type(v).__name__, repr(v)]
            for v in obj.co_consts
        ]
        return body

    try:
        return hashlib.sha256(
            json.dumps(canonical(code), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except Exception:
        return None


def seam_identity(func: Any) -> dict[str, Any]:
    """Identity of a callable, for comparison against the reviewed literals above.

    A failure leaves the digest None, which fails validation and therefore yields `unobserved`
    rather than a false accept.
    """
    out: dict[str, Any] = {
        "module": None, "qualname": None, "signature": None, "code_sha256": None,
        "has_defaults": True, "has_kwdefaults": True, "has_closure": True,
    }
    try:
        out["module"] = getattr(func, "__module__", None)
        out["qualname"] = getattr(func, "__qualname__", None)
        out["signature"] = str(inspect.signature(func))
        out["has_defaults"] = getattr(func, "__defaults__", None) is not None
        out["has_kwdefaults"] = getattr(func, "__kwdefaults__", None) is not None
        out["has_closure"] = getattr(func, "__closure__", None) is not None
        out["code_sha256"] = code_digest(func.__code__)
    except Exception:
        out["code_sha256"] = None
    return out


def client_has_custom_transport() -> bool:
    """Derive, not assume, whether LiteLLM's configured sync route bypasses the pinned class.

    `HTTPHandler._create_sync_transport()` returns an `HTTPTransport` when `litellm.force_ipv4` and
    otherwise `None`, in which case httpx installs its default `HTTPTransport`. Both are the pinned
    class. Anything else — or an unreadable route — is treated as custom, so the observer refuses to
    arm rather than reporting `not_started` for a request it never saw.
    """
    try:
        import httpx
        from litellm.llms.custom_httpx.http_handler import HTTPHandler

        transport = HTTPHandler()._create_sync_transport()
    except Exception:
        return True
    if transport is None:
        return False
    return type(transport) is not httpx.HTTPTransport


class TransportObserver:
    """Passive pass-through counter on the pinned synchronous HTTPX seam.

    Reads nothing from the request or response and returns the callee's value unchanged, so request
    bytes are identical with it installed.

    `not_started` means exactly one event: the provider call raised without the pinned handler being
    entered — a LiteLLM-side rejection before dispatch. It is permitted only when every identity
    check passed AND the class attribute is still this observer's wrapper at attempt end. Anything
    else, including a route this observer cannot see, is `unobserved`.

    Attempt state is per-observer and the accepted path is synchronous with one call in flight. A
    concurrent caller would need context-local state instead.
    """

    def __init__(self) -> None:
        self.validated = False
        self.installed_wrapper: Callable | None = None
        self.active = False
        self.entered = 0
        self.returned = 0
        self.raised = 0
        self.failed_checks: list[str] = []

    def validate_and_install(
        self, *, litellm_version: str, httpx_version: str, seam_func: Any,
        client_has_custom_transport: bool, install: Callable[[Callable], None],
    ) -> bool:
        ident = seam_identity(seam_func)
        checks = {
            "litellm_version": litellm_version == SUPPORTED_LITELLM,
            "httpx_version": httpx_version == SUPPORTED_HTTPX,
            "seam_module": ident["module"] == EXPECTED_SEAM_MODULE,
            "seam_qualname": ident["qualname"] == EXPECTED_SEAM_QUALNAME,
            "seam_signature": ident["signature"] == EXPECTED_SEAM_SIGNATURE,
            "seam_code_digest": ident["code_sha256"] == EXPECTED_SEAM_CODE_SHA256,
            "seam_no_defaults": not ident["has_defaults"],
            "seam_no_kwdefaults": not ident["has_kwdefaults"],
            "seam_no_closure": not ident["has_closure"],
            "no_custom_transport": not client_has_custom_transport,
        }
        self.failed_checks = sorted(name for name, ok in checks.items() if not ok)
        if self.failed_checks:
            self.validated = False
            return False
        self.installed_wrapper = self.wrap(seam_func)
        try:
            install(self.installed_wrapper)
        except Exception:
            self.validated = False
            self.failed_checks = ["install"]
            return False
        self.validated = True
        return True

    def begin_attempt(self) -> None:
        self.entered = self.returned = self.raised = 0
        self.active = True

    def end_attempt(self, *, current_seam: Any) -> str:
        """`current_seam` is re-read from the class: a replacement after installation must not be
        reported as a pre-transport failure."""
        self.active = False
        if not self.validated:
            return "unobserved"
        if current_seam is not self.installed_wrapper:
            self.failed_checks = ["wrapper_replaced_after_install"]
            return "unobserved"
        if self.entered == 0:
            return "not_started"
        if self.returned > 0:
            return "response_seen"
        return "handler_entered_no_response"

    def wrap(self, inner: Callable) -> Callable:
        def observed(transport, request):
            if self.active:
                self.entered += 1
            try:
                response = inner(transport, request)
            except BaseException:
                if self.active:
                    self.raised += 1
                raise
            if self.active:
                self.returned += 1
            return response

        return observed


def _provider_count(value: Any) -> int:
    """Provider-influenced counts clamp; a hostile count must not become an oversized field."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return MAX_COUNT if value > MAX_COUNT else value


def _harness_index(value: Any, hi: int, name: str) -> int:
    """Harness-controlled indices fail rather than clamp."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > hi:
        raise InstrumentationError(f"invalid {name}")
    return value


def _item_type(item: Any) -> str:
    """Closed vocabulary. An unrecognized item is `other`, never its observed value."""
    if not isinstance(item, dict):
        return "other"
    raw = item.get("type")
    if raw in ("reasoning", "function_call", "function_call_output"):
        return raw
    if raw == "message" or raw is None:
        role = item.get("role")
        if role == "system":
            return "system"
        if role == "user":
            return "user"
        if role == "assistant":
            return "assistant_message"
    return "other"


def input_shape(prepared: Any) -> dict[str, Any]:
    """Content-free structure of the exact list about to be sent.

    Item counts are separate from valid-ID counts: counting only non-blank IDs would report two
    blank-ID calls as zero calls with every pairing invariant true, hiding the exact anomaly this
    exists to surface. Call IDs are compared in memory only and are never retained.
    """
    items = prepared if isinstance(prepared, list) else []
    types_seen = [_item_type(item) for item in items]

    call_items = output_items = 0
    blank_call_ids = blank_output_ids = 0
    call_ids: list[str] = []
    output_ids: list[str] = []
    named_bash = args_are_strings = True
    missing_type = False

    for item in items:
        if not isinstance(item, dict):
            missing_type = True
            continue
        if item.get("type") is None and item.get("role") is None:
            missing_type = True
        if item.get("type") == "function_call":
            call_items += 1
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id.strip():
                call_ids.append(call_id)
            else:
                blank_call_ids += 1
            if item.get("name") != "bash":
                named_bash = False
            if not isinstance(item.get("arguments"), str):
                args_are_strings = False
        elif item.get("type") == "function_call_output":
            output_items += 1
            output_id = item.get("call_id")
            if isinstance(output_id, str) and output_id.strip():
                output_ids.append(output_id)
            else:
                blank_output_ids += 1

    unique_calls, unique_outputs = set(call_ids), set(output_ids)
    duplicate_calls = len(call_ids) - len(unique_calls)
    duplicate_outputs = len(output_ids) - len(unique_outputs)
    matched = unique_calls & unique_outputs
    # Multiplicity preserved: a call answered twice is an anomaly, not a match.
    matched_once = sum(
        1 for call_id in matched
        if call_ids.count(call_id) == 1 and output_ids.count(call_id) == 1
    )

    return {
        "item_count": _provider_count(len(items)),
        "type_sequence": types_seen[:MAX_SEQUENCE],
        "type_sequence_truncated": len(types_seen) > MAX_SEQUENCE,
        "type_histogram": {t: _provider_count(types_seen.count(t)) for t in ITEM_TYPES},
        "pairing": {
            "call_items": _provider_count(call_items),
            "output_items": _provider_count(output_items),
            "valid_call_ids": _provider_count(len(call_ids)),
            "valid_output_ids": _provider_count(len(output_ids)),
            "blank_or_missing_call_ids": _provider_count(blank_call_ids),
            "blank_or_missing_output_ids": _provider_count(blank_output_ids),
            "duplicate_call_ids": _provider_count(duplicate_calls),
            "duplicate_output_ids": _provider_count(duplicate_outputs),
            "ids_matched": _provider_count(len(matched)),
            "ids_matched_exactly_once": _provider_count(matched_once),
            "unmatched_call_ids": _provider_count(len(unique_calls - unique_outputs)),
            "unmatched_output_ids": _provider_count(len(unique_outputs - unique_calls)),
        },
        "invariants": {
            "first_item_is_system": bool(types_seen) and types_seen[0] == "system",
            "last_item_is_function_call_output": (
                bool(types_seen) and types_seen[-1] == "function_call_output"
            ),
            "every_call_item_has_a_valid_id": call_items == len(call_ids),
            "every_output_item_has_a_valid_id": output_items == len(output_ids),
            "every_call_id_unique": duplicate_calls == 0,
            "every_output_id_unique": duplicate_outputs == 0,
            "every_call_named_bash": named_bash,
            "every_call_arguments_is_string": args_are_strings,
            "any_item_missing_type": missing_type,
            "any_reasoning_item_present": "reasoning" in types_seen,
            "every_call_paired_exactly_once": call_items > 0 and matched_once == call_items,
        },
    }


def ledger_category(exc: BaseException) -> str | None:
    """The tracked type-only classifier, imported lazily (agent fork is on the run path only)."""
    from ckb_model import provider_failure_category

    return provider_failure_category(exc)


def outcome_of(exc: BaseException | None, *, classifier: Callable | None = None) -> str:
    """Closed outcome by type only. Message, status, response and class name are never read.

    `request_other` is reserved for a future member the tracked classifier positively assigns to the
    `request` family. Today that family is exactly `BadRequestError` and `NotFoundError`, so the
    value is unreachable in production; everything else that failed is `other_failure`, matching the
    ledger's own `other_provider` / `connection` / `timeout` / `server` split.
    """
    if exc is None:
        return "responded"
    from litellm import exceptions as litellm_exceptions

    if isinstance(exc, litellm_exceptions.NotFoundError):
        return "not_found"
    if isinstance(exc, litellm_exceptions.BadRequestError):
        return "bad_request"
    try:
        category = (classifier or ledger_category)(exc)
    except Exception:
        return "other_failure"
    return "request_other" if category == "request" else "other_failure"


def build_record(
    *, turn_index: Any, attempt_index: Any, exc: BaseException | None, prepared: Any,
    transport_state: str, classifier: Callable | None = None,
) -> dict[str, Any]:
    if transport_state not in TRANSPORT_STATES:
        raise InstrumentationError("invalid transport_state")
    return {
        "turn_index": _harness_index(turn_index, MAX_TURN, "turn_index"),
        "attempt_index": _harness_index(attempt_index, MAX_ATTEMPT, "attempt_index"),
        "outcome": outcome_of(exc, classifier=classifier),
        "transport_state": transport_state,
        "input_shape": input_shape(prepared),
    }


def _document(run_id: str, records: list[dict[str, Any]], *, ok: bool, dropped: int) -> dict:
    return {
        "diagnostic_schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "run_id": run_id,
        "instrumentation_ok": ok,
        "records": records,
        "records_dropped": _provider_count(dropped),
    }


def _serialize(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def false_envelope(run_id: str) -> bytes:
    """The fixed envelope published whenever evidence cannot be trusted."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise InstrumentationError("invalid run_id")
    return _serialize(_document(run_id, [], ok=False, dropped=0))


def artifact_bytes(run_id: str, records: list[dict[str, Any]], *, dropped: int = 0) -> bytes:
    """Serialize the bounded artifact, or the false envelope when it cannot be published honestly."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise InstrumentationError("invalid run_id")
    kept = records[:MAX_RECORDS]
    total_dropped = dropped + max(0, len(records) - MAX_RECORDS)
    payload = _serialize(_document(run_id, kept, ok=True, dropped=total_dropped))
    if len(payload) > MAX_ARTIFACT_BYTES:
        # Honest about how many records existed rather than silently shrinking the evidence.
        return _serialize(_document(run_id, [], ok=False, dropped=dropped + len(records)))
    return payload


def _exact_keys(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise InstrumentationError(f"{label} keys must be exactly {sorted(expected)}")
    return value


def _bounded_int(value: Any, hi: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > hi:
        raise InstrumentationError(f"{label} must be an int in 0..{hi}")
    return value


def validate_artifact_bytes(payload: bytes, *, run_id: str) -> dict[str, Any]:
    """Strictly validate a candidate before the parent publishes it.

    Raises `InstrumentationError` on anything unexpected; the caller publishes the false envelope.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise InstrumentationError("candidate must be bytes")
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise InstrumentationError("candidate exceeds the artifact byte ceiling")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstrumentationError("candidate is not readable UTF-8 JSON") from None
    if payload != _serialize(document):
        raise InstrumentationError("candidate is not the exact canonical serialization")
    _exact_keys(document, _TOP_LEVEL_KEYS, "artifact")
    if document["diagnostic_schema_version"] != DIAGNOSTIC_SCHEMA_VERSION:
        raise InstrumentationError("unexpected diagnostic schema version")
    if document["run_id"] != run_id:
        raise InstrumentationError("candidate run_id does not match this run")
    if not isinstance(document["instrumentation_ok"], bool):
        raise InstrumentationError("instrumentation_ok must be a bool")
    _bounded_int(document["records_dropped"], MAX_COUNT, "records_dropped")
    records = document["records"]
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise InstrumentationError(f"records must be a list of at most {MAX_RECORDS}")
    if document["instrumentation_ok"] is False and (records or document["records_dropped"]):
        # A false envelope is a fixed shape. Records beside it would be evidence the run just
        # declared untrustworthy.
        raise InstrumentationError("a false envelope must carry no records")
    for record in records:
        _exact_keys(record, _RECORD_KEYS, "record")
        _bounded_int(record["turn_index"], MAX_TURN, "turn_index")
        _bounded_int(record["attempt_index"], MAX_ATTEMPT, "attempt_index")
        if record["outcome"] not in OUTCOMES:
            raise InstrumentationError("unknown outcome")
        if record["transport_state"] not in TRANSPORT_STATES:
            raise InstrumentationError("unknown transport_state")
        shape = _exact_keys(record["input_shape"], _SHAPE_KEYS, "input_shape")
        _bounded_int(shape["item_count"], MAX_COUNT, "item_count")
        sequence = shape["type_sequence"]
        if not isinstance(sequence, list) or len(sequence) > MAX_SEQUENCE:
            raise InstrumentationError("type_sequence is unbounded")
        if any(token not in ITEM_TYPES for token in sequence):
            raise InstrumentationError("type_sequence carries an unknown token")
        if not isinstance(shape["type_sequence_truncated"], bool):
            raise InstrumentationError("type_sequence_truncated must be a bool")
        histogram = _exact_keys(shape["type_histogram"], frozenset(ITEM_TYPES), "type_histogram")
        for key, value in histogram.items():
            _bounded_int(value, MAX_COUNT, f"type_histogram.{key}")
        pairing = _exact_keys(shape["pairing"], _PAIRING_KEYS, "pairing")
        for key, value in pairing.items():
            _bounded_int(value, MAX_COUNT, f"pairing.{key}")
        invariants = _exact_keys(shape["invariants"], _INVARIANT_KEYS, "invariants")
        for key, value in invariants.items():
            if not isinstance(value, bool):
                raise InstrumentationError(f"invariants.{key} must be a bool")
    return document


class DiagnosticSession:
    """Per-run diagnostic state: the request ceiling and the ordered attempt records.

    Held by the model boundary. Absent in every ordinary run, so normal cells behave exactly as
    before.
    """

    def __init__(self, *, max_requests: int = MAX_PROVIDER_REQUESTS) -> None:
        self.max_requests = int(max_requests)
        self.requests_started = 0
        self.records: list[dict[str, Any]] = []
        self.dropped = 0
        self.instrumentation_ok = True

    def poison(self) -> None:
        """Terminal and atomic: the run can no longer publish evidence, only the false envelope.

        Records are dropped immediately rather than kept, so a later serialization cannot publish a
        partial artifact that looks healthy.
        """
        self.instrumentation_ok = False
        self.records = []
        self.dropped = 0

    def reserve_request(self) -> None:
        """Called before transport. Request `max_requests + 1` never reaches LiteLLM or HTTPX."""
        if self.requests_started >= self.max_requests:
            raise DiagnosticLimitReached(
                f"diagnostic provider request ceiling reached ({self.max_requests})"
            )
        self.requests_started += 1

    def record(self, **kwargs: Any) -> None:
        """Any failure here is an instrumentation failure, not a record to skip."""
        if not self.instrumentation_ok:
            return
        try:
            entry = build_record(**kwargs)
        except Exception:
            self.poison()
            return
        if len(self.records) >= MAX_RECORDS:
            self.dropped += 1
            return
        self.records.append(entry)

    def to_bytes(self, run_id: str) -> bytes:
        if not self.instrumentation_ok:
            return false_envelope(run_id)
        return artifact_bytes(run_id, self.records, dropped=self.dropped)
