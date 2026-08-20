"""Projection, observer and outcome tests for the bounded provider diagnostic."""

from __future__ import annotations

import hashlib
import json
import types

import httpx
import pytest
from litellm import exceptions as litellm_exceptions

from ckbbench.run.diagnostic import (
    EXPECTED_SEAM_CODE_SHA256,
    EXPECTED_SEAM_MODULE,
    EXPECTED_SEAM_QUALNAME,
    EXPECTED_SEAM_SIGNATURE,
    ITEM_TYPES,
    MAX_ARTIFACT_BYTES,
    MAX_RECORDS,
    MAX_SEQUENCE,
    OUTCOMES,
    SUPPORTED_HTTPX,
    SUPPORTED_LITELLM,
    TRANSPORT_STATES,
    DiagnosticLimitReached,
    DiagnosticSession,
    InstrumentationError,
    TransportObserver,
    artifact_bytes,
    build_record,
    code_digest,
    false_envelope,
    http_status_of,
    input_shape,
    outcome_of,
    seam_identity,
    validate_artifact_bytes,
)

RUN_ID = "2.0.0-devnet-B-diagnostic-s1-1786900000"

CANARIES = (
    "SYS-CANARY", "TASK-CANARY", "REASON-CANARY", "CMD-CANARY", "ARGS-CANARY", "CALLID-CANARY",
    "OUT-CANARY", "MCP-CANARY", "URL-CANARY", "KEY-CANARY", "RESPID-CANARY", "EXCMSG-CANARY",
)
CANARY_MESSAGE = " ".join(CANARIES)


def _start():
    return [{"role": "system", "content": "SYS-CANARY"},
            {"role": "user", "content": "TASK-CANARY"}]


def _reasoning(encrypted=None):
    item = {"type": "reasoning", "id": "REASON-CANARY", "summary": ["REASON-CANARY"]}
    if encrypted is not None:
        item["encrypted_content"] = encrypted
    return item


def _call(cid="CALLID-CANARY", name="bash", args='{"command": "CMD-CANARY"}'):
    return {"type": "function_call", "call_id": cid, "name": name,
            "arguments": args, "status": "completed"}


def _output(cid="CALLID-CANARY", text="OUT-CANARY"):
    return {"type": "function_call_output", "call_id": cid, "output": text}


HISTORIES = {
    "initial": _start(),
    "reasoning_and_call": _start() + [_reasoning(), _call()],
    "matching_output": _start() + [_reasoning(), _call(), _output()],
    "nonzero_output": _start() + [_reasoning(), _call(), _output(text="OUT-CANARY")],
    "two_turns": _start() + [_reasoning(), _call("A"), _output("A"),
                             _reasoning(), _call("B"), _output("B")],
    "two_calls": _start() + [_reasoning(), _call("A"), _call("B"), _output("A"), _output("B")],
    "format_feedback": _start() + [_reasoning(),
                                   {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "fixed"}]}],
    "mcp_shaped": _start() + [_reasoning(), _call(), _output(text="MCP-CANARY")],
    "unknown_type": _start() + [{"type": "web_search_call", "id": "URL-CANARY"}, _call()],
    "blank_ids": _start() + [_call(""), {"type": "function_call", "name": "bash",
                                         "arguments": "{}", "status": "completed"}],
    "duplicate_call_ids": _start() + [_call("SAME"), _call("SAME")],
    "unmatched_output": _start() + [_output("ORPHAN")],
    "answered_twice": _start() + [_call("A"), _output("A"), _output("A")],
    "missing_status": _start() + [{"id": "RESPID-CANARY"}],
}


@pytest.mark.parametrize("label", sorted(HISTORIES))
def test_every_history_projects_inside_the_closed_vocabulary(label):
    shape = input_shape(HISTORIES[label])
    assert set(shape["type_sequence"]) <= set(ITEM_TYPES)
    assert set(shape["type_histogram"]) == set(ITEM_TYPES)


def test_blank_ids_are_not_reported_as_healthy():
    """Counting only valid IDs once made two blank-ID calls look like zero calls, all-invariants-true."""
    pairing = input_shape(HISTORIES["blank_ids"])["pairing"]
    invariants = input_shape(HISTORIES["blank_ids"])["invariants"]
    assert pairing["call_items"] == 2
    assert pairing["valid_call_ids"] == 0
    assert pairing["blank_or_missing_call_ids"] == 2
    assert invariants["every_call_item_has_a_valid_id"] is False
    assert invariants["every_call_paired_exactly_once"] is False


def test_a_call_answered_twice_preserves_multiplicity():
    shape = input_shape(HISTORIES["answered_twice"])
    assert shape["pairing"]["output_items"] == 2
    assert shape["pairing"]["duplicate_output_ids"] == 1
    assert shape["pairing"]["ids_matched"] == 1
    assert shape["pairing"]["ids_matched_exactly_once"] == 0
    assert shape["invariants"]["every_output_id_unique"] is False


def test_duplicate_call_ids_are_counted():
    shape = input_shape(HISTORIES["duplicate_call_ids"])
    assert shape["pairing"]["duplicate_call_ids"] == 1
    assert shape["invariants"]["every_call_id_unique"] is False


def test_unmatched_output_is_visible():
    shape = input_shape(HISTORIES["unmatched_output"])
    assert shape["pairing"]["unmatched_output_ids"] == 1


@pytest.mark.parametrize("encrypted, expected", [
    (None, False), ("", False), (7, False), ("ENCRYPTED-CANARY", True),
])
def test_reasoning_replayability_is_reported_without_retaining_the_value(encrypted, expected):
    shape = input_shape(_start() + [_reasoning(encrypted)])
    assert shape["invariants"]["every_reasoning_item_has_encrypted_content"] is expected
    assert "ENCRYPTED-CANARY" not in json.dumps(shape)


def test_no_reasoning_item_is_vacuously_replayable():
    shape = input_shape(_start())
    assert shape["invariants"]["every_reasoning_item_has_encrypted_content"] is True


@pytest.mark.parametrize("hostile", [None, "not a list", 12345, [{"type": "KEY-CANARY"}],
                                     [None, 7, "x"], [{"type": {"nested": 1}}]])
def test_hostile_provider_values_reduce_without_choosing_keys(hostile):
    shape = input_shape(hostile)
    assert set(shape["type_sequence"]) <= set(ITEM_TYPES)
    assert set(shape["type_histogram"]) == set(ITEM_TYPES)


def test_type_sequence_truncates_at_the_ceiling():
    long = _start() + [_call(f"c{i}") for i in range(200)]
    shape = input_shape(long)
    assert len(shape["type_sequence"]) == MAX_SEQUENCE
    assert shape["type_sequence_truncated"] is True


# --- outcome classification -----------------------------------------------------------------------


def _litellm(name, message=CANARY_MESSAGE):
    cls = getattr(litellm_exceptions, name)
    return cls(message=message, model="m", llm_provider="p")


def test_not_found_is_classified_before_bad_request():
    """Both live in the same collapsed `request` family, so ordering decides which value survives."""
    assert outcome_of(_litellm("NotFoundError")) == "not_found"
    assert outcome_of(_litellm("BadRequestError")) == "bad_request"


def test_a_success_is_responded():
    assert outcome_of(None) == "responded"


@pytest.mark.parametrize("exc", [
    OSError(CANARY_MESSAGE),
    TimeoutError(CANARY_MESSAGE),
    RuntimeError(CANARY_MESSAGE),
    ValueError(CANARY_MESSAGE),
])
def test_non_request_families_never_become_request_other(exc):
    assert outcome_of(exc) == "other_failure"


@pytest.mark.parametrize("name", ["ServiceUnavailableError", "Timeout", "InternalServerError"])
def test_litellm_non_request_families_never_become_request_other(name):
    cls = getattr(litellm_exceptions, name, None)
    if cls is None:
        pytest.skip(f"litellm has no {name}")
    assert outcome_of(_litellm(name)) == "other_failure"


def test_a_generic_api_error_is_other_failure_through_the_real_classifier():
    exc = litellm_exceptions.APIError(
        status_code=500, message=CANARY_MESSAGE, llm_provider="p", model="m"
    )
    assert outcome_of(exc) == "other_failure"


@pytest.mark.parametrize("status", [100, 200, 403, 413, 429, 500, 502, 599])
def test_a_litellm_api_error_retains_only_its_bounded_http_status(status):
    exc = litellm_exceptions.APIError(
        status_code=status, message=CANARY_MESSAGE, llm_provider="p", model="m"
    )
    assert http_status_of(exc) == status
    assert _record(exc=exc)["http_status"] == status


@pytest.mark.parametrize("name,status", [
    ("BadRequestError", 400),
    ("NotFoundError", 404),
    ("AuthenticationError", 401),
    ("RateLimitError", 429),
    ("InternalServerError", 500),
    ("ServiceUnavailableError", 503),
])
def test_litellm_api_error_subclasses_retain_their_bounded_status(name, status):
    exc = _litellm(name)
    assert http_status_of(exc) == status


@pytest.mark.parametrize("status", [None, True, False, 99, 600, -1, 500.0, "500", CANARY_MESSAGE])
def test_an_invalid_api_error_status_reduces_to_null(status):
    exc = litellm_exceptions.APIError(
        status_code=500, message=CANARY_MESSAGE, llm_provider="p", model="m"
    )
    exc.status_code = status
    assert http_status_of(exc) is None


def test_an_integer_subclass_status_reduces_to_null():
    class HostileInt(int):
        pass

    exc = litellm_exceptions.APIError(
        status_code=500, message=CANARY_MESSAGE, llm_provider="p", model="m"
    )
    exc.status_code = HostileInt(500)
    assert http_status_of(exc) is None


def test_an_unrelated_exception_status_property_is_never_read():
    class HostileError(Exception):
        @property
        def status_code(self):
            raise AssertionError("unrelated status property was read")

    assert http_status_of(HostileError(CANARY_MESSAGE)) is None


def test_an_unlisted_api_error_subclass_status_property_is_never_read():
    class HostileApiError(litellm_exceptions.APIError):
        @property
        def status_code(self):
            raise AssertionError("unlisted status property was read")

        @status_code.setter
        def status_code(self, _value):
            pass

    exc = HostileApiError(
        status_code=500, message=CANARY_MESSAGE, llm_provider="p", model="m"
    )
    assert http_status_of(exc) is None


def test_a_success_has_no_http_error_status():
    assert http_status_of(None) is None
    assert _record(exc=None)["http_status"] is None


def test_request_other_is_reachable_only_through_the_trusted_classifier():
    """Defensive future-member branch: unreachable today, exercised through an injected classifier."""
    exc = litellm_exceptions.APIError(
        status_code=418, message=CANARY_MESSAGE, llm_provider="p", model="m"
    )
    assert outcome_of(exc, classifier=lambda _e: "request") == "request_other"
    assert outcome_of(exc, classifier=lambda _e: "connection") == "other_failure"


def test_a_broken_classifier_fails_safe():
    def boom(_exc):
        raise RuntimeError(CANARY_MESSAGE)

    assert outcome_of(OSError(CANARY_MESSAGE), classifier=boom) == "other_failure"


# --- record and artifact bounds ---------------------------------------------------------------------


def _record(turn=1, attempt=0, exc=None, prepared=None, state="response_seen"):
    return build_record(turn_index=turn, attempt_index=attempt, exc=exc,
                        prepared=prepared if prepared is not None else HISTORIES["two_turns"],
                        transport_state=state)


@pytest.mark.parametrize("turn", [-1, True, "3", 81, None, 1.5])
def test_invalid_harness_indices_fail_rather_than_clamp(turn):
    with pytest.raises(InstrumentationError):
        _record(turn=turn)


def test_an_unknown_transport_state_fails():
    with pytest.raises(InstrumentationError):
        _record(state="made_up")


def test_the_artifact_keeps_sixteen_records_and_reports_the_rest():
    payload = artifact_bytes(RUN_ID, [_record() for _ in range(40)])
    document = json.loads(payload)
    assert len(document["records"]) == MAX_RECORDS
    assert document["records_dropped"] == 24
    assert document["instrumentation_ok"] is True


def test_serialization_is_exact_and_deterministic():
    first = artifact_bytes(RUN_ID, [_record()])
    second = artifact_bytes(RUN_ID, [_record()])
    assert first == second
    assert not first.endswith(b"\n")
    assert b", " not in first and b": " not in first


def test_the_false_envelope_is_fixed():
    document = json.loads(false_envelope(RUN_ID))
    assert document == {
        "diagnostic_schema_version": "2.2.0",
        "run_id": RUN_ID,
        "instrumentation_ok": False,
        "records": [],
        "records_dropped": 0,
    }


def test_an_oversized_document_drops_records_and_stays_honest():
    huge = [_record(prepared=_start() + [_call(f"c{i}") for i in range(200)])
            for _ in range(MAX_RECORDS)]
    payload = artifact_bytes(RUN_ID, huge)
    document = json.loads(payload)
    assert len(payload) <= MAX_ARTIFACT_BYTES
    if not document["instrumentation_ok"]:
        assert document["records"] == []
        assert document["records_dropped"] == len(huge)


def test_validation_accepts_what_this_module_produces():
    payload = artifact_bytes(RUN_ID, [_record()])
    assert validate_artifact_bytes(payload, run_id=RUN_ID)["run_id"] == RUN_ID


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=1),
    lambda d: d.pop("records_dropped"),
    lambda d: d.update(diagnostic_schema_version="1.0.0"),
    lambda d: d.update(instrumentation_ok="yes"),
    lambda d: d.update(records_dropped=-1),
    lambda d: d["records"][0].update(extra=1),
    lambda d: d["records"][0].update(outcome="made_up"),
    lambda d: d["records"][0].update(transport_state="sent"),
    lambda d: d["records"][0].pop("http_status"),
    lambda d: d["records"][0].update(http_status=True),
    lambda d: d["records"][0].update(http_status=99),
    lambda d: d["records"][0].update(http_status=600),
    lambda d: d["records"][0].update(http_status="500"),
    lambda d: d["records"][0].update(turn_index=999),
    lambda d: d["records"][0]["input_shape"].update(item_count=99999),
    lambda d: d["records"][0]["input_shape"]["type_sequence"].append("nonsense"),
    lambda d: d["records"][0]["input_shape"]["pairing"].update(call_items=True),
    lambda d: d["records"][0]["input_shape"]["invariants"].update(every_call_id_unique=1),
])
def test_validation_refuses_a_tampered_candidate(mutate):
    document = json.loads(artifact_bytes(RUN_ID, [_record()]))
    mutate(document)
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(InstrumentationError):
        validate_artifact_bytes(payload, run_id=RUN_ID)


def test_validation_refuses_non_canonical_whitespace():
    document = json.loads(artifact_bytes(RUN_ID, [_record()]))
    with pytest.raises(InstrumentationError):
        validate_artifact_bytes(json.dumps(document).encode(), run_id=RUN_ID)


def test_validation_refuses_a_foreign_run_id():
    with pytest.raises(InstrumentationError):
        validate_artifact_bytes(artifact_bytes(RUN_ID, []), run_id="another-run")


def test_no_canary_reaches_the_artifact():
    payload = artifact_bytes(RUN_ID, [
        _record(exc=_litellm("BadRequestError"), prepared=HISTORIES[label])
        for label in sorted(HISTORIES)
    ])
    text = payload.decode()
    for canary in CANARIES:
        assert canary not in text


# --- request ceiling ----------------------------------------------------------------------------


def test_request_seventeen_is_refused_before_transport():
    session = DiagnosticSession()
    for _ in range(16):
        session.reserve_request()
    with pytest.raises(DiagnosticLimitReached):
        session.reserve_request()
    assert session.requests_started == 16


def test_a_session_with_bad_harness_state_publishes_the_false_envelope():
    session = DiagnosticSession()
    session.record(turn_index=-1, attempt_index=0, exc=None,
                   prepared=HISTORIES["initial"], transport_state="not_started")
    assert session.instrumentation_ok is False
    assert json.loads(session.to_bytes(RUN_ID))["instrumentation_ok"] is False


def test_a_session_drops_beyond_the_record_ceiling():
    session = DiagnosticSession()
    for _ in range(20):
        session.record(turn_index=1, attempt_index=0, exc=None,
                       prepared=HISTORIES["initial"], transport_state="not_started")
    document = json.loads(session.to_bytes(RUN_ID))
    assert len(document["records"]) == MAX_RECORDS
    assert document["records_dropped"] == 4


# --- pinned seam --------------------------------------------------------------------------------


def test_the_installed_seam_matches_every_reviewed_literal():
    identity = seam_identity(httpx.HTTPTransport.handle_request)
    assert identity["module"] == EXPECTED_SEAM_MODULE
    assert identity["qualname"] == EXPECTED_SEAM_QUALNAME
    assert identity["signature"] == EXPECTED_SEAM_SIGNATURE
    assert identity["code_sha256"] == EXPECTED_SEAM_CODE_SHA256
    assert not identity["has_defaults"]
    assert not identity["has_kwdefaults"]
    assert not identity["has_closure"]


def _holder():
    class Seam:
        handle_request = staticmethod(httpx.HTTPTransport.handle_request)

    def install(wrapper):
        Seam.handle_request = wrapper

    return Seam, install


def _validated():
    from importlib.metadata import version

    seam, install = _holder()
    observer = TransportObserver()
    ok = observer.validate_and_install(
        litellm_version=version("litellm"), httpx_version=version("httpx"),
        seam_func=httpx.HTTPTransport.handle_request,
        client_has_custom_transport=False, install=install,
    )
    return observer, seam, ok


def test_the_observer_validates_against_the_real_seam():
    observer, _seam, ok = _validated()
    assert ok is True
    assert observer.failed_checks == []


def test_the_wrapper_is_a_strict_pass_through():
    observer, seam, _ = _validated()
    request, response, transport = object(), object(), object()
    seen = []

    def inner(t, r):
        seen.append((t, r))
        return response

    observer.begin_attempt()
    returned = observer.wrap(inner)(transport, request)
    assert seen[-1][0] is transport and seen[-1][1] is request
    assert returned is response
    assert observer.end_attempt(current_seam=seam.handle_request) == "response_seen"


def test_an_inner_exception_propagates_and_yields_handler_entered():
    observer, seam, _ = _validated()

    class Boom(RuntimeError):
        pass

    def inner(_t, _r):
        raise Boom("inner")

    observer.begin_attempt()
    with pytest.raises(Boom):
        observer.wrap(inner)(object(), object())
    assert observer.end_attempt(current_seam=seam.handle_request) == "handler_entered_no_response"


def test_a_never_entered_attempt_with_an_intact_seam_is_not_started():
    observer, seam, _ = _validated()
    observer.begin_attempt()
    assert observer.end_attempt(current_seam=seam.handle_request) == "not_started"


def test_a_wrapper_replaced_after_install_is_unobserved():
    observer, _seam, _ = _validated()
    observer.begin_attempt()
    assert observer.end_attempt(current_seam=lambda *_: None) == "unobserved"


@pytest.mark.parametrize("kwargs,expected_check", [
    ({"litellm_version": "9.9.9"}, "litellm_version"),
    ({"httpx_version": "9.9.9"}, "httpx_version"),
    ({"client_has_custom_transport": True}, "no_custom_transport"),
])
def test_an_unsupported_environment_refuses_to_arm(kwargs, expected_check):
    seam, install = _holder()
    observer = TransportObserver()
    base = {
        "litellm_version": SUPPORTED_LITELLM, "httpx_version": SUPPORTED_HTTPX,
        "seam_func": httpx.HTTPTransport.handle_request,
        "client_has_custom_transport": False, "install": install,
    }
    assert observer.validate_and_install(**{**base, **kwargs}) is False
    assert expected_check in observer.failed_checks
    observer.begin_attempt()
    assert observer.end_attempt(current_seam=seam.handle_request) == "unobserved"


def test_a_replaced_seam_is_refused_by_the_reviewed_literals():
    """The constants are literals, so a seam replaced before validation cannot validate itself."""
    def replaced(self, other):
        return None

    seam, install = _holder()
    observer = TransportObserver()
    assert observer.validate_and_install(
        litellm_version=SUPPORTED_LITELLM, httpx_version=SUPPORTED_HTTPX,
        seam_func=replaced, client_has_custom_transport=False, install=install,
    ) is False
    assert "seam_code_digest" in observer.failed_checks
    observer.begin_attempt()
    assert observer.end_attempt(current_seam=seam.handle_request) == "unobserved"


def _impostor(**code_kwargs):
    original = httpx.HTTPTransport.handle_request
    impostor_code = original.__code__.replace(**code_kwargs)
    fn = types.FunctionType(impostor_code, original.__globals__, original.__name__)
    fn.__module__ = original.__module__
    fn.__qualname__ = original.__qualname__
    fn.__annotations__ = dict(getattr(original, "__annotations__", {}))
    return fn


def test_a_co_names_swap_is_caught_although_its_opcodes_are_identical():
    original = httpx.HTTPTransport.handle_request.__code__
    names = tuple("_definitely_not_pool" if n == "_pool" else n for n in original.co_names)
    impostor = _impostor(co_names=names)
    assert (hashlib.sha256(impostor.__code__.co_code).hexdigest()
            == hashlib.sha256(original.co_code).hexdigest())
    assert seam_identity(impostor)["code_sha256"] != EXPECTED_SEAM_CODE_SHA256


def test_an_emptied_exception_table_is_caught():
    """CPython 3.12 exception-handler ranges live here, and the pinned method has a 12-byte table."""
    assert len(httpx.HTTPTransport.handle_request.__code__.co_exceptiontable) == 12
    impostor = _impostor(co_exceptiontable=b"")
    assert seam_identity(impostor)["code_sha256"] != EXPECTED_SEAM_CODE_SHA256


def test_a_callable_with_defaults_is_refused():
    def with_default(self, request=None):
        return None

    identity = seam_identity(with_default)
    assert identity["has_defaults"] is True


def test_the_code_digest_is_refcount_independent():
    """The reason this is canonical rather than `marshal.dumps`.

    marshal emits FLAG_REF only when an object's refcount exceeds one, so its bytes depend on how
    many references the process happens to hold. Asserting that instability here would itself be
    refcount-dependent, so this pins the property production needs: the canonical digest does not
    move when unrelated references appear.
    """
    code = httpx.HTTPTransport.handle_request.__code__
    before = code_digest(code)
    holder = [code.co_consts, code.co_names, code] * 3  # noqa: F841 - ordinary extra references
    assert code_digest(code) == before == EXPECTED_SEAM_CODE_SHA256


def test_counters_do_not_bleed_between_attempts():
    observer, seam, _ = _validated()

    def inner(_t, _r):
        return object()

    observer.begin_attempt()
    observer.wrap(inner)(object(), object())
    assert observer.end_attempt(current_seam=seam.handle_request) == "response_seen"
    observer.begin_attempt()
    assert observer.end_attempt(current_seam=seam.handle_request) == "not_started"
    observer.begin_attempt()
    observer.wrap(inner)(object(), object())
    assert observer.end_attempt(current_seam=seam.handle_request) == "response_seen"


def test_traffic_outside_the_attempt_window_is_not_recorded():
    observer, seam, _ = _validated()
    observer.begin_attempt()
    observer.active = False
    observer.wrap(lambda _t, _r: object())(object(), object())
    observer.active = True
    assert observer.end_attempt(current_seam=seam.handle_request) == "not_started"


def test_every_enum_is_closed():
    assert set(OUTCOMES) == {
        "responded", "bad_request", "not_found", "request_other", "other_failure"
    }
    assert set(TRANSPORT_STATES) == {
        "not_started", "handler_entered_no_response", "response_seen", "unobserved"
    }
