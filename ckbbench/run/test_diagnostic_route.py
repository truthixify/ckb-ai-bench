"""The pinned wrapper must sit on the route a real client dispatch actually takes.

No network and no sockets: the layer BELOW the pinned seam is replaced in process, so httpx's own
client and transport machinery runs for real while nothing opens a connection.

`handle_request` is assigned and restored directly rather than through `monkeypatch`: mixing the two
on one attribute makes teardown order decide the final value, which silently re-installed a wrapper
into later tests.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from ckbbench.run.diagnostic import (
    EXPECTED_SEAM_CODE_SHA256,
    TransportObserver,
    client_has_custom_transport,
    code_digest,
)


@pytest.fixture
def no_sockets(monkeypatch):
    """Any real connection attempt fails this test immediately."""

    def deny(*_a, **_k):
        raise AssertionError("the route test attempted a real socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def pinned_seam():
    """Restore the genuine pinned method whatever the test does to it."""
    original = httpx.HTTPTransport.handle_request
    assert code_digest(original.__code__) == EXPECTED_SEAM_CODE_SHA256, (
        "the installed httpx is not the pinned reviewed method"
    )
    yield original
    httpx.HTTPTransport.handle_request = original


def _arm(original):
    """Install the observer exactly as the worker does, over the genuine pinned method."""
    from importlib.metadata import version

    observer = TransportObserver()

    def install(wrapper):
        httpx.HTTPTransport.handle_request = wrapper

    ok = observer.validate_and_install(
        litellm_version=version("litellm"),
        httpx_version=version("httpx"),
        seam_func=original,
        client_has_custom_transport=client_has_custom_transport(),
        install=install,
    )
    return observer, ok


def _delegate_to(observer, inner):
    """Re-wrap `inner` and register it as the installed wrapper, keeping identity consistent."""
    wrapper = observer.wrap(inner)
    httpx.HTTPTransport.handle_request = wrapper
    observer.installed_wrapper = wrapper


def test_the_route_litellm_uses_is_the_pinned_default_transport(no_sockets):
    """Derived from LiteLLM's own client construction, not asserted as a literal."""
    assert client_has_custom_transport() is False


def test_the_observer_arms_against_the_genuine_pinned_method(no_sockets, pinned_seam):
    observer, ok = _arm(pinned_seam)
    assert ok is True, observer.failed_checks
    assert observer.failed_checks == []
    assert httpx.HTTPTransport.handle_request is observer.installed_wrapper


def test_a_real_client_dispatch_enters_the_wrapper(no_sockets, pinned_seam):
    """A genuine `httpx.Client` request over the default transport reaches the pinned wrapper."""
    observer, ok = _arm(pinned_seam)
    assert ok
    reached = {"n": 0}

    def below_the_seam(self, request):
        reached["n"] += 1
        return httpx.Response(200, json={"ok": True}, request=request)

    _delegate_to(observer, below_the_seam)

    observer.begin_attempt()
    with httpx.Client() as client:
        response = client.post("https://example.invalid/responses", json={"input": []})
    state = observer.end_attempt(current_seam=httpx.HTTPTransport.handle_request)

    assert response.status_code == 200
    assert reached["n"] == 1, "the request never reached the transport layer"
    assert state == "response_seen"


def test_a_transport_failure_is_handler_entered_no_response(no_sockets, pinned_seam):
    observer, ok = _arm(pinned_seam)
    assert ok

    def below_the_seam(self, request):
        raise httpx.ConnectError("synthetic transport failure")

    _delegate_to(observer, below_the_seam)

    observer.begin_attempt()
    with pytest.raises(httpx.ConnectError):
        with httpx.Client() as client:
            client.post("https://example.invalid/responses", json={"input": []})
    assert observer.end_attempt(
        current_seam=httpx.HTTPTransport.handle_request
    ) == "handler_entered_no_response"


def test_a_call_that_never_dispatches_is_not_started(no_sockets, pinned_seam):
    """The LiteLLM-side rejection shape: armed, but the handler was never entered."""
    observer, ok = _arm(pinned_seam)
    assert ok
    observer.begin_attempt()
    assert observer.end_attempt(current_seam=httpx.HTTPTransport.handle_request) == "not_started"


def test_a_dispatch_is_never_retried_by_the_client(no_sockets, pinned_seam):
    """One request must produce exactly one transport entry."""
    observer, ok = _arm(pinned_seam)
    assert ok
    entries = {"n": 0}

    def below_the_seam(self, request):
        entries["n"] += 1
        return httpx.Response(500, json={"error": "synthetic"}, request=request)

    _delegate_to(observer, below_the_seam)
    observer.begin_attempt()
    with httpx.Client() as client:
        client.post("https://example.invalid/responses", json={"input": []})
    assert entries["n"] == 1
    assert observer.end_attempt(current_seam=httpx.HTTPTransport.handle_request) == "response_seen"


def test_the_wrapper_does_not_change_the_request(no_sockets, pinned_seam):
    """Diagnostic-on and diagnostic-off must put identical bytes on the transport."""
    seen: list[tuple] = []

    def below_the_seam(self, request):
        seen.append((request.method, str(request.url), request.content,
                     request.headers.get("x-probe")))
        return httpx.Response(200, json={"ok": True}, request=request)

    def post():
        with httpx.Client() as client:
            client.post("https://example.invalid/responses", json={"input": [{"a": 1}]},
                        headers={"x-probe": "value"})

    httpx.HTTPTransport.handle_request = below_the_seam
    post()

    observer, ok = _arm(pinned_seam)
    assert ok
    _delegate_to(observer, below_the_seam)
    observer.begin_attempt()
    post()
    observer.end_attempt(current_seam=httpx.HTTPTransport.handle_request)

    assert seen[0] == seen[1]


def test_the_pinned_digest_still_matches_the_installed_httpx(no_sockets, pinned_seam):
    assert code_digest(pinned_seam.__code__) == EXPECTED_SEAM_CODE_SHA256


# --- the real litellm.responses() dispatch ---------------------------------------------------------
#
# The tests above prove the wrapper sits on httpx's default transport. These prove the PRODUCTION
# call reaches it: real `litellm.responses()`, real provider selection and request construction, with
# only `httpcore.ConnectionPool.handle_request` replaced in process and every socket denied.


@pytest.fixture
def litellm_route(no_sockets, pinned_seam, monkeypatch):
    """Arm the observer, then replace the layer below httpx entirely in memory."""
    import httpcore

    observer, ok = _arm(pinned_seam)
    assert ok, observer.failed_checks

    entries = {"n": 0}
    behaviour = {"mode": "response"}

    def pool_handle(self, request):
        entries["n"] += 1
        if behaviour["mode"] == "raise":
            raise httpcore.ConnectError("synthetic transport failure")
        body = json.dumps(_completed_response()).encode()
        return httpcore.Response(
            200, headers=[(b"content-type", b"application/json"),
                          (b"content-length", str(len(body)).encode())],
            content=body,
        )

    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", pool_handle)
    yield observer, entries, behaviour


def _completed_response() -> dict:
    """A complete in-memory Responses result: LiteLLM validates every required field."""
    return {
        "id": "resp_diagnostic", "object": "response", "status": "completed", "model": "gpt-x",
        "created_at": 0, "error": None, "incomplete_details": None, "instructions": None,
        "metadata": {}, "parallel_tool_calls": False, "temperature": 0.0, "tool_choice": "auto",
        "tools": [], "top_p": 1.0, "max_output_tokens": None, "previous_response_id": None,
        "reasoning": None, "text": None, "truncation": "disabled", "user": None,
        "output": [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "bash",
                    "arguments": "{\"command\": \"ls\"}", "status": "completed"}],
        "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
    }


def _responses_call():
    """The production wire shape: the same tool schema `CkbLitellmResponseModel` sends."""
    import litellm
    from minisweagent.models.utils.actions_toolcall_response import BASH_TOOL_RESPONSE_API

    return litellm.responses(
        model="openai/gpt-x",
        input=[{"role": "user", "content": "CANARY-PROMPT"}],
        tools=[BASH_TOOL_RESPONSE_API],
        api_base="https://example.invalid",
        api_key="sk-CANARY-KEY",
        num_retries=0,
    )


def test_a_real_litellm_responses_call_enters_the_pinned_wrapper(litellm_route):
    observer, entries, behaviour = litellm_route
    behaviour["mode"] = "response"

    observer.begin_attempt()
    _responses_call()
    state = observer.end_attempt(current_seam=httpx.HTTPTransport.handle_request)

    assert entries["n"] == 1, "the production call did not reach the transport layer"
    assert state == "response_seen"


def test_a_real_litellm_transport_failure_is_handler_entered(litellm_route):
    observer, entries, behaviour = litellm_route
    behaviour["mode"] = "raise"

    observer.begin_attempt()
    with pytest.raises(Exception):
        _responses_call()
    state = observer.end_attempt(current_seam=httpx.HTTPTransport.handle_request)

    assert entries["n"] == 1
    assert state == "handler_entered_no_response"


def test_a_real_litellm_call_is_dispatched_exactly_once(litellm_route):
    """`num_retries=0` is the accepted profile: one dispatch, never a silent retry."""
    observer, entries, behaviour = litellm_route
    behaviour["mode"] = "raise"

    observer.begin_attempt()
    with pytest.raises(Exception):
        _responses_call()
    observer.end_attempt(current_seam=httpx.HTTPTransport.handle_request)
    assert entries["n"] == 1


def test_no_canary_from_the_real_call_reaches_the_projection(litellm_route, capsys):
    from ckbbench.run.diagnostic import DiagnosticSession

    observer, _entries, behaviour = litellm_route
    behaviour["mode"] = "raise"

    session = DiagnosticSession()
    observer.begin_attempt()
    failure = None
    try:
        _responses_call()
    except Exception as exc:  # noqa: BLE001 - the projection must survive any provider failure
        failure = exc
    state = observer.end_attempt(current_seam=httpx.HTTPTransport.handle_request)
    session.record(turn_index=0, attempt_index=0, exc=failure,
                   prepared=[{"role": "user", "content": "CANARY-PROMPT"}],
                   transport_state=state)

    blob = session.to_bytes("2.0.0-devnet-B-m-s1-1").decode() + capsys.readouterr().out
    for canary in ("CANARY-PROMPT", "sk-CANARY-KEY", "example.invalid", "resp_diagnostic"):
        assert canary not in blob
