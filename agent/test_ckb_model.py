"""Usage-ledger tests for the fork's phase-one model (ADR-0014).

The ledger is the run's token evidence. It must count every raw provider attempt, keep a response
that later fails to parse, refuse to guess a missing field, and hold no provider text at all.
"""

from __future__ import annotations

import json
import traceback
import types

import httpx
import pytest

from ckb_model import (
    CkbLitellmModel,
    ProviderCallError,
    ProviderAttempt,
    UsageLedger,
    _openrouter_responses_client,
    _read_model,
    _read_usage,
)

CANARIES = ("sk-live-do-not-log", "raw-server-body", "tok-abc123", "echo secret-command")


def _openrouter_route():
    return {
        "provider": {
            "order": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }


def test_the_openrouter_adapter_merges_only_the_reviewed_route_at_the_request_root():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"object": "response"})

    raw = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    adapter = _openrouter_responses_client(
        expected_url="https://openrouter.ai/api/v1/responses",
        expected_model="openai/gpt-5-mini",
        expected_extra_body=_openrouter_route(),
        client=raw,
    )
    response = adapter.post(
        "https://openrouter.ai/api/v1/responses",
        json={
            "model": "openai/gpt-5-mini",
            "input": [{"role": "user", "content": "x"}],
        },
    )

    assert response.status_code == 200 and len(seen) == 1
    assert response.json() == {"object": "response", "user": None}
    body = json.loads(seen[0].content)
    assert body["provider"] == _openrouter_route()["provider"]
    assert "extra_body" not in body
    assert body["model"] == "openai/gpt-5-mini"


@pytest.mark.parametrize("document", [
    {"object": "response", "user": "provider-value"},
    {"object": "other"},
    ["not", "a", "response", "object"],
])
def test_the_openrouter_adapter_only_defaults_an_omitted_responses_user(document):
    raw = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=document)
    ))
    adapter = _openrouter_responses_client(
        expected_url="https://openrouter.ai/api/v1/responses",
        expected_model="openai/gpt-5-mini",
        expected_extra_body=_openrouter_route(),
        client=raw,
    )

    response = adapter.post(
        "https://openrouter.ai/api/v1/responses",
        json={"model": "openai/gpt-5-mini", "input": []},
    )

    assert response.json() == document


@pytest.mark.parametrize("status,document,category", [
    (401, {"error": {"message": CANARIES[1]}}, "authentication"),
    (402, {"error": {"code": 402, "message": CANARIES[1]}}, "authorization"),
    (429, {"error": {"message": CANARIES[1]}}, "rate_limit"),
    (502, {"error": {"message": CANARIES[1]}}, "server"),
    (400, {"error": {"type": "context_length_exceeded", "message": CANARIES[1]}},
     "context_window"),
])
def test_openrouter_http_failures_become_closed_categories(status, document, category):
    from ckb_model import OpenRouterProviderError

    raw = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=document)
    ))
    adapter = _openrouter_responses_client(
        expected_url="https://openrouter.ai/api/v1/responses",
        expected_model="openai/gpt-5-mini",
        expected_extra_body=_openrouter_route(),
        client=raw,
    )

    with pytest.raises(OpenRouterProviderError) as exc:
        adapter.post(
            "https://openrouter.ai/api/v1/responses",
            json={"model": "openai/gpt-5-mini", "input": []},
        )

    assert exc.value.category == category
    assert CANARIES[1] not in str(exc.value) + repr(exc.value.args)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_openrouter_completed_http_exchange_with_failed_response_is_retryable_and_sanitized():
    from ckb_model import OpenRouterProviderError

    raw = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "object": "response", "status": "failed",
        "error": {"type": "server_error", "message": CANARIES[1]},
    })))
    adapter = _openrouter_responses_client(
        expected_url="https://openrouter.ai/api/v1/responses",
        expected_model="openai/gpt-5-mini",
        expected_extra_body=_openrouter_route(),
        client=raw,
    )
    response = adapter.post(
        "https://openrouter.ai/api/v1/responses",
        json={"model": "openai/gpt-5-mini", "input": []},
    )

    with pytest.raises(OpenRouterProviderError) as exc:
        response.json()

    assert exc.value.category == "server"
    assert CANARIES[1] not in str(exc.value) + repr(exc.value.args)


@pytest.mark.parametrize("error_type,category", [
    ("authentication", "authentication"),
    ("payment_required", "authorization"),
    ("rate_limit_exceeded", "rate_limit"),
    ("provider_overloaded", "server"),
    ("provider_unavailable", "server"),
    ("timeout", "timeout"),
    ("context_length_exceeded", "context_window"),
    ("invalid_prompt", "request"),
    ("unsupported_image_format", "unsupported"),
    ("unmapped", "other_provider"),
])
def test_openrouter_failed_response_prefers_the_documented_typed_error(error_type, category):
    from ckb_model import OpenRouterProviderError

    raw = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "object": "response",
        "status": "failed",
        "error_type": error_type,
        "error": {"code": "server_error", "message": CANARIES[1]},
    })))
    adapter = _openrouter_responses_client(
        expected_url="https://openrouter.ai/api/v1/responses",
        expected_model="openai/gpt-5-mini",
        expected_extra_body=_openrouter_route(),
        client=raw,
    )
    response = adapter.post(
        "https://openrouter.ai/api/v1/responses",
        json={"model": "openai/gpt-5-mini", "input": []},
    )

    with pytest.raises(OpenRouterProviderError) as exc:
        response.json()

    assert exc.value.category == category
    assert error_type not in str(exc.value) + repr(exc.value.args)
    assert CANARIES[1] not in str(exc.value) + repr(exc.value.args)


@pytest.mark.parametrize("url,model,route,extra", [
    ("https://other.invalid/responses", "openai/gpt-5-mini", None, {}),
    ("https://openrouter.ai/api/v1/responses", "openai/gpt-5", None, {}),
    ("https://openrouter.ai/api/v1/responses", "openai/gpt-5-mini",
     {"provider": {"order": ["other"]}}, {}),
    ("https://openrouter.ai/api/v1/responses", "openai/gpt-5-mini", None,
     {"provider": {"unreviewed": True}}),
])
def test_the_openrouter_adapter_refuses_drift_before_http(url, model, route, extra):
    opens = []
    raw = httpx.Client(transport=httpx.MockTransport(
        lambda request: opens.append(request) or httpx.Response(200)
    ))
    adapter = _openrouter_responses_client(
        expected_url="https://openrouter.ai/api/v1/responses",
        expected_model="openai/gpt-5-mini",
        expected_extra_body=_openrouter_route(),
        client=raw,
    )
    body = {"model": model, "input": [], **extra}
    if route is not None:
        body["extra_body"] = route
    with pytest.raises(RuntimeError):
        adapter.post(url, json=body)
    assert opens == []


def test_litellm_172_reaches_openrouter_with_the_profile_route_at_the_root():
    import litellm

    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={
            "id": "resp_test",
            "object": "response",
            "created_at": 1,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "metadata": {},
            "status": "completed",
            "model": "openai/gpt-5-mini",
            "output": [],
            "parallel_tool_calls": True,
            "temperature": None,
            "tool_choice": "auto",
            "tools": [],
            "top_p": None,
            "max_output_tokens": None,
            "previous_response_id": None,
            "reasoning": {"effort": "medium"},
            "text": None,
            "truncation": "disabled",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        })

    raw = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    adapter = _openrouter_responses_client(
        expected_url="https://openrouter.ai/api/v1/responses",
        expected_model="openai/gpt-5-mini",
        expected_extra_body=_openrouter_route(),
        client=raw,
    )
    response = litellm.responses(
        model="openai/openai/gpt-5-mini",
        api_base="https://openrouter.ai/api/v1",
        api_key="sk-test-canary",
        input=[{"role": "user", "content": "x"}],
        stream=False,
        store=False,
        truncation="disabled",
        reasoning={"effort": "medium"},
        extra_body=_openrouter_route(),
        client=adapter,
        num_retries=0,
        timeout=300,
    )

    assert response.model == "openai/gpt-5-mini" and response.user is None and len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["model"] == "openai/gpt-5-mini"
    assert body["provider"] == _openrouter_route()["provider"]
    assert body["reasoning"] == {"effort": "medium"}
    assert body["store"] is False and body["stream"] is False
    assert body["truncation"] == "disabled"
    assert "temperature" not in body
    assert "extra_body" not in body


def _usage(prompt=30, completion=20, total=50):
    """Responses-native usage. `input_tokens`/`output_tokens` are what the provider sends."""
    return types.SimpleNamespace(input_tokens=prompt, output_tokens=completion,
                                 total_tokens=total)


def _response(*, model="gpt-x", usage=None):
    return types.SimpleNamespace(model=model, usage=_usage() if usage is None else usage)


def test_multiple_valid_responses_sum_exactly():
    ledger = UsageLedger()
    for _ in range(3):
        ledger.record_turn()
        ledger.record_response(_response(usage=_usage(10, 5, 15)))
    assert ledger.totals() == (30, 15, 45)
    assert (ledger.turn_count, ledger.attempt_count, ledger.response_count) == (3, 3, 3)
    assert ledger.is_complete() is True
    assert ledger.is_correctness_complete() is True


def test_stateless_replay_strips_rejected_metadata_without_mutating_history():
    model = _ResponseRecorder(model_name="openai/gpt-x", model_kwargs={},
                              max_query_attempts=1, cost_tracking="ignore_errors")
    messages = [
        {"role": "system", "content": "system", "extra": {"local": True}},
        {
            "object": "response",
            "output": [
                {
                    "type": "reasoning", "id": "rs-1", "status": "completed",
                    "encrypted_content": "ciphertext", "summary": [],
                    "format": "openai-responses-v1", "signature": "signature",
                    "extra": {"local": True},
                },
                {
                    "type": "message", "id": "msg-1", "status": "completed",
                    "role": "assistant", "content": [{"type": "output_text", "text": "x"}],
                },
                {
                    "type": "function_call", "id": "fc-1", "status": "completed",
                    "call_id": "call-1", "name": "bash", "arguments": '{"command":"pwd"}',
                    "caller": None, "namespace": None,
                },
            ],
            "extra": {"local": True},
        },
        {"type": "function_call_output", "call_id": "call-1", "output": "ok",
         "extra": {"local": True}},
    ]
    before = json.loads(json.dumps(messages))

    prepared = model._prepare_messages_for_api(messages)

    assert messages == before
    assert [item["type"] for item in prepared[1:]] == [
        "reasoning", "message", "function_call", "function_call_output",
    ]
    assert all("status" not in item for item in (prepared[1], *prepared[3:]))
    assert prepared[1] == {
        "type": "reasoning", "id": "rs-1", "encrypted_content": "ciphertext", "summary": [],
        "format": "openai-responses-v1", "signature": "signature",
    }
    assert prepared[2]["id"] == "msg-1" and prepared[2]["status"] == "completed"
    assert prepared[2]["content"][0]["text"] == "x"
    assert prepared[3]["call_id"] == "call-1" and prepared[3]["arguments"] == '{"command":"pwd"}'
    assert "namespace" not in prepared[3] and "caller" not in prepared[3]
    assert prepared[4] == {"type": "function_call_output", "call_id": "call-1", "output": "ok"}
    assert prepared[0] == {"role": "system", "content": "system"}


@pytest.mark.parametrize("mutation", [
    lambda item: item.update(provider_extension="unsafe"),
    lambda item: item.update(type="unknown_provider_item"),
    lambda item: item.pop("id"),
    lambda item: item.pop("call_id"),
    lambda item: item.update(arguments={"command": "pwd"}),
    lambda item: item.update(caller={"type": "direct"}),
    lambda item: item.update(namespace="unreviewed"),
    lambda item: item.update(status="incomplete"),
])
def test_response_history_refuses_schema_drift_without_echoing_values(mutation):
    from ckb_model import ResponseHistoryError

    item = {
        "type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "bash",
        "arguments": '{"command":"pwd"}', "status": "completed",
    }
    mutation(item)
    messages = [
        {"role": "user", "content": "start"},
        {"object": "response", "output": [item]},
        {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
    ]

    with pytest.raises(ResponseHistoryError) as exc:
        _response_model()._prepare_messages_for_api(messages)

    assert "unsafe" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


@pytest.mark.parametrize("mutation", [
    lambda item: item.update(format="unreviewed-format"),
    lambda item: item.update(signature=""),
    lambda item: item.update(signature={"opaque": True}),
    lambda item: item.update(provider_extension=None),
])
def test_reasoning_replay_refuses_unreviewed_extensions(mutation):
    from ckb_model import ResponseHistoryError

    item = {
        "type": "reasoning", "id": "rs-1", "summary": [], "status": "completed",
        "encrypted_content": "ciphertext", "format": "openai-responses-v1",
    }
    mutation(item)
    with pytest.raises(ResponseHistoryError, match="reviewed schema"):
        _response_model()._prepare_messages_for_api([
            {"role": "user", "content": "start"},
            {"object": "response", "output": [item]},
        ])


@pytest.mark.parametrize("mutation", [
    lambda item: item.pop("id"),
    lambda item: item.pop("status"),
    lambda item: item.update(status="incomplete"),
])
def test_assistant_response_messages_require_replay_identity_and_completion(mutation):
    from ckb_model import ResponseHistoryError

    item = {
        "type": "message", "id": "msg-1", "status": "completed", "role": "assistant",
        "content": [{"type": "output_text", "text": "done"}],
    }
    mutation(item)
    with pytest.raises(ResponseHistoryError, match="reviewed schema"):
        _response_model()._prepare_messages_for_api([
            {"role": "user", "content": "start"},
            {"object": "response", "output": [item]},
        ])


def _replay_group(index: int, output_size: int = 24):
    call_id = f"call-{index}"
    return [
        {"object": "response", "output": [
            {"type": "reasoning", "id": f"rs-{index}", "summary": [], "content": None},
            {"type": "function_call", "id": f"fc-{index}", "call_id": call_id, "name": "bash",
             "arguments": '{"command":"pwd"}', "caller": None},
        ]},
        {"type": "function_call_output", "call_id": call_id, "output": "x" * output_size},
    ]


def test_fixed_replay_budget_keeps_prefix_and_newest_complete_groups_deterministically():
    from ckb_model import _COMPACTION_NOTICE, _history_bytes, _prepare_response_history

    messages = [{"role": "system", "content": "fixed"}]
    for index in range(4):
        messages.extend(_replay_group(index, output_size=80))
    latest_only, _ = _prepare_response_history(
        [messages[0], *messages[-2:]], policy="all-turns", max_bytes=0
    )
    limit = _history_bytes([latest_only[0], _COMPACTION_NOTICE, *latest_only[1:]])

    first, facts = _prepare_response_history(
        messages, policy="prefix-tail-groups-v1", max_bytes=limit
    )
    second, repeated = _prepare_response_history(
        messages, policy="prefix-tail-groups-v1", max_bytes=limit
    )

    assert first == second
    assert facts == repeated
    assert first[0] == {"role": "system", "content": "fixed"}
    assert first[1] == _COMPACTION_NOTICE
    assert [item.get("call_id") for item in first if "call_id" in item] == ["call-3", "call-3"]
    assert facts.compacted is True and facts.dropped_groups == 3 and facts.dropped_items == 9
    assert facts.prepared_bytes <= limit
    assert all("content" not in item or item["content"] is not None for item in first)
    assert all("caller" not in item or item["caller"] is not None for item in first)


def test_replay_budget_never_splits_or_drops_the_newest_tool_exchange():
    from ckb_model import _COMPACTION_NOTICE, ResponseHistoryError, _history_bytes
    from ckb_model import _prepare_response_history

    messages = [{"role": "user", "content": "fixed"}, *_replay_group(1, output_size=200)]
    base_limit = _history_bytes([messages[0], _COMPACTION_NOTICE]) + 1
    with pytest.raises(ResponseHistoryError, match="newest response group"):
        _prepare_response_history(messages, policy="prefix-tail-groups-v1", max_bytes=base_limit)

    incomplete = messages[:-1]
    with pytest.raises(ResponseHistoryError, match="incomplete tool exchange"):
        _prepare_response_history(incomplete, policy="prefix-tail-groups-v1", max_bytes=10000)


def test_an_untouched_ledger_is_not_complete():
    assert UsageLedger().is_complete() is False
    assert UsageLedger().is_correctness_complete() is False
    assert UsageLedger().totals() is None


@pytest.mark.parametrize("usage", [
    types.SimpleNamespace(),
    types.SimpleNamespace(input_tokens=10, output_tokens=5),
    types.SimpleNamespace(input_tokens=None, output_tokens=5, total_tokens=15),
    types.SimpleNamespace(input_tokens=True, output_tokens=5, total_tokens=15),
    types.SimpleNamespace(input_tokens=-1, output_tokens=5, total_tokens=4),
    types.SimpleNamespace(input_tokens=1.5, output_tokens=5, total_tokens=6.5),
    types.SimpleNamespace(input_tokens="10", output_tokens="5", total_tokens="15"),
    types.SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=99),
    # The chat vocabulary is no longer the wire contract and must not be silently accepted.
    types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
])
def test_malformed_usage_is_incomplete_rather_than_guessed(usage):
    """A missing total is never derived and a missing component is never replaced with zero."""
    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_response(_response(usage=usage))
    assert ledger.is_complete() is False
    assert ledger.totals() is None
    assert ledger.response_count == 1


def test_a_provider_exception_counts_an_attempt_without_fabricating_anything():
    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_failure(OSError("raw-server-body sk-live-do-not-log"))
    assert (ledger.attempt_count, ledger.response_count) == (1, 0)
    assert ledger.totals() is None
    assert ledger.is_complete() is False
    assert ledger.attempts[0] == ProviderAttempt(
        responded=False, error="OSError", failure_category="connection"
    )


def test_known_tokens_survive_a_later_failure_but_stay_incomplete():
    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_response(_response(usage=_usage(10, 5, 15)))
    ledger.record_turn()
    ledger.record_failure(TimeoutError("boom"))
    assert ledger.totals() == (10, 5, 15)
    assert ledger.is_complete() is False
    assert ledger.is_correctness_complete() is False


def test_a_recovered_attempt_preserves_correctness_but_not_efficiency():
    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_failure(OSError("transport"))
    ledger.record_response(_response(usage=_usage(10, 5, 15)))
    assert (ledger.turn_count, ledger.attempt_count, ledger.response_count) == (1, 2, 1)
    assert ledger.is_correctness_complete() is True
    assert ledger.is_complete() is False


@pytest.mark.parametrize("models,complete", [
    (("gpt-x", "gpt-x"), True),
    (("gpt-x", "gpt-y"), False),
    (("gpt-x", None), False),
    ((None, None), False),
])
def test_returned_model_absence_or_drift_is_incomplete(models, complete):
    ledger = UsageLedger()
    for model in models:
        ledger.record_turn()
        ledger.record_response(_response(model=model))
    assert ledger.is_complete() is complete


def test_the_ledger_holds_no_provider_text():
    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_failure(RuntimeError(" ".join(CANARIES)))
    ledger.record_turn()
    ledger.record_response(types.SimpleNamespace(
        model="gpt-x", usage=_usage(), id="resp-secret", choices=[CANARIES[3]],
    ))
    rendered = repr(ledger.attempts)
    for canary in CANARIES:
        assert canary not in rendered
    assert "resp-secret" not in rendered


def test_usage_and_model_are_read_from_mappings_too():
    assert _read_usage({"usage": {"input_tokens": 1, "output_tokens": 2,
                                  "total_tokens": 3}}) == (1, 2, 3)
    assert _read_model({"model": "gpt-x"}) == "gpt-x"
    assert _read_model({"model": "  "}) is None
    assert _read_usage({}) is None


# --- the model's own attempt policy ---------------------------------------------------------------

class _Recorder(CkbLitellmModel):
    """Replaces only the raw provider call, so the real ledger and retry path are exercised."""

    def __init__(self, *, responses=None, errors=None, **kwargs):
        super().__init__(**kwargs)
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.raw_calls = 0

    def _raw(self):
        self.raw_calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._responses.pop(0)


def _model(**kwargs):
    return _Recorder(model_name="openai/gpt-x", model_kwargs={},
                     cost_tracking="ignore_errors", **kwargs)


def test_the_provider_boundary_records_before_anything_can_discard_it(monkeypatch):
    """A response is in the ledger the moment the provider returns, before cost or parsing."""
    model = _model(responses=[_response()])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    returned = model._query([{"role": "user", "content": "x"}])
    assert returned.model == "gpt-x"
    assert model.usage_ledger.response_count == 1
    assert model.usage_ledger.totals() == (30, 20, 50)


def test_a_response_that_later_fails_to_parse_stays_in_the_ledger(monkeypatch):
    """A FormatError response consumed tokens; dropping it would understate the run."""
    from minisweagent.exceptions import FormatError

    model = _model(responses=[_response()])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    monkeypatch.setattr(type(model), "_calculate_cost", lambda self, response: {"cost": 0.0})

    def boom(self, response):
        raise FormatError({"extra": {}})

    monkeypatch.setattr(type(model), "_parse_actions", boom)
    with pytest.raises(FormatError):
        model.query([{"role": "user", "content": "x"}])
    assert model.usage_ledger.response_count == 1
    assert model.usage_ledger.totals() == (30, 20, 50)
    assert model.usage_ledger.is_complete() is True


def test_a_provider_exception_is_recorded_and_not_retried(monkeypatch):
    model = _model(errors=[OSError("transport")])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    with pytest.raises(ProviderCallError):
        model.query([{"role": "user", "content": "x"}])
    assert model.raw_calls == 1, "one attempt means one raw provider call"
    assert model.usage_ledger.attempt_count == 1
    assert model.usage_ledger.response_count == 0
    assert model.usage_ledger.turn_count == 1


def test_the_recorded_failure_keeps_no_exception_text(monkeypatch):
    model = _model(errors=[OSError(" ".join(CANARIES))])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    with pytest.raises(ProviderCallError) as exc:
        model.query([{"role": "user", "content": "x"}])
    tb = "".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__))
    for canary in CANARIES:
        assert canary not in str(exc.value)
        assert canary not in tb
    # Raised outside the handler, so the original exception is not reachable at all -- stronger
    # than `from None`, which only suppresses its display.
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    rendered = repr(model.usage_ledger.attempts)
    for canary in CANARIES:
        assert canary not in rendered
    assert model.usage_ledger.attempts[0].error == "OSError"


def test_the_default_attempt_policy_is_one():
    assert _model().config.max_query_attempts == 1


# --- provider faults are separated from harness bugs, and no path leaks provider text -------------

@pytest.mark.parametrize("exc,is_provider", [
    (OSError("transport"), True),
    (TimeoutError("slow"), True),
    (ConnectionResetError("reset"), True),
    (RuntimeError("an unclassified fault"), False),
    (TypeError("harness bug"), False),
    (AttributeError("harness bug"), False),
    (KeyError("harness bug"), False),
    (AssertionError("harness bug"), False),
])
def test_a_harness_bug_is_not_reported_as_provider_health(exc, is_provider):
    """A harness bug must not become a failed provider attempt in the published health numbers."""
    from ckb_model import is_provider_fault

    assert is_provider_fault(exc) is is_provider
    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_failure(exc)
    assert ledger.attempt_count == (1 if is_provider else 0)
    assert ledger.internal_errors == (0 if is_provider else 1)
    assert ledger.is_complete() is False


def test_the_rendered_and_serialized_model_is_credential_free():
    """`serialize()`/`get_template_vars()` reach the trajectory; a key there would be published."""
    model = _model()
    model.config.model_kwargs = {"api_base": "https://proxy.example/v1",
                                 "api_key": "sk-live-do-not-log"}
    rendered = repr(model.serialize()) + repr(model.get_template_vars())
    assert "sk-live-do-not-log" not in rendered
    assert "(redacted)" in rendered


def test_the_production_builder_keeps_the_key_out_of_the_rendered_config(monkeypatch):
    """The end-to-end path the factory actually builds, not a hand-made config."""
    import ckbbench.run.agent_factory as factory_mod
    from ckbbench.run.model_profile import parse_model_profile

    profile = parse_model_profile({
        "api_base": "https://proxy.example/v1",
        "api_style": "openai-responses", "drop_unsupported_params": True,
        "evidence_utc": "2026-08-15T09:30:00Z", "litellm_num_retries": 0,
        "max_agent_query_attempts": 4, "model_stability": "moving_alias",
        "probed_response_model": "openai/gpt-x", "observation_max_bytes": 32768,
        "profile_id": "phase1-gpt-v10",
        "provider": "openrouter", "provider_allow_fallbacks": False,
        "provider_order": ["openai"], "provider_require_parameters": True,
        "provider_request_timeout_seconds": 300,
        "provider_retry_backoff_seconds": [4, 8, 16],
        "reasoning_context": "prefix_tail_groups",
        "reasoning_effort": "medium", "replay_max_bytes": 131072,
        "replay_policy": "prefix-tail-groups-v1", "store": False,
        "requested_model": "openai/gpt-x",
        "retryable_provider_failure_categories": [
            "rate_limit", "timeout", "connection", "server", "protocol", "other_provider",
        ],
        "schema_version": "8",
        "temperature": None, "truncation": "disabled",
        "usage_contract": "openai-responses-usage-v1",
    }, sha256="a" * 64)
    built = factory_mod._profile_model_builder(profile, "sk-live-do-not-log")
    rendered = repr(built.serialize()) + repr(built.get_template_vars()) + repr(
        built.config.model_dump()
    )
    assert "sk-live-do-not-log" not in rendered
    assert built._call_secrets == {"api_key": "sk-live-do-not-log"}
    assert "api_key" not in built.config.model_kwargs
    assert built.config.model_kwargs["timeout"] == 300
    assert built.config.observation_max_bytes == 32768


def test_the_production_timeout_reaches_litellm_responses(monkeypatch):
    import ckbbench.run.agent_factory as factory_mod
    from ckbbench.run.model_profile import load_reviewed_profile

    seen = {}

    def fake_responses(**kwargs):
        seen.update(kwargs)
        return _responses_body()

    monkeypatch.setattr(
        "minisweagent.models.litellm_response_model.litellm.responses", fake_responses
    )
    profile = load_reviewed_profile()
    model = factory_mod._profile_model_builder(profile, "sk-live-do-not-log")
    model._query([{"role": "user", "content": "x"}])

    assert seen["timeout"] == 300
    assert seen["model"] == profile.litellm_model_name == "openai/gpt-5.6-sol"
    assert "client" not in seen
    assert "extra_body" not in model.config.model_kwargs
    assert "truncation" not in model.config.model_kwargs
    assert seen["api_key"] == "sk-live-do-not-log"
    assert model.usage_ledger.attempt_count == model.usage_ledger.response_count == 1


def _secret_payload(choices=None):
    """A response whose every raw surface outside the assistant message carries a canary."""
    return types.SimpleNamespace(
        model="gpt-x", usage=_usage(), id="resp-secret-id",
        choices=[CANARIES[3]] if choices is None else choices,
        model_dump=lambda **_kw: {"secret": "sk-live-do-not-log", "body": "raw-server-body"},
    )


def test_a_format_error_response_reaches_neither_the_ledger_nor_the_agent_message(monkeypatch):
    """Upstream attaches the raw response to the FormatError message; that message is a diagnostic."""
    from minisweagent.exceptions import FormatError

    model = _model(responses=[_secret_payload()])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    monkeypatch.setattr(type(model), "_calculate_cost", lambda self, response: {"cost": 0.0})
    monkeypatch.setattr(type(model), "_parse_actions",
                        lambda self, response: (_ for _ in ()).throw(FormatError({"extra": {}})))
    with pytest.raises(FormatError) as exc:
        model.query([{"role": "user", "content": "x"}])
    tb = "".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__))
    rendered = repr(model.usage_ledger.attempts) + repr(exc.value.messages) + str(exc.value) + tb
    for canary in (*CANARIES, "resp-secret-id"):
        assert canary not in rendered
    assert exc.value.messages[0]["extra"]["response"] == {
        "model": "gpt-x",
        "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
    }
    assert model.usage_ledger.is_complete() is True


def test_the_message_a_successful_query_returns_carries_no_raw_response(monkeypatch):
    """That message becomes an agent turn and is serialized into the trajectory."""
    choice = types.SimpleNamespace(
        finish_reason=CANARIES[2],
        message=types.SimpleNamespace(
            model_dump=lambda **_kw: {"role": "assistant", "content": "run ls"}
        ),
    )
    model = _model(responses=[_secret_payload(choices=[choice])])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    monkeypatch.setattr(type(model), "_calculate_cost", lambda self, response: {"cost": 0.0})
    monkeypatch.setattr(type(model), "_parse_actions", lambda self, response: [{"action": "ls"}])
    monkeypatch.setattr(type(model), "_prepare_messages_for_api", lambda self, messages: messages)
    monkeypatch.setattr(
        type(model.config), "model_dump", lambda self, **kw: {"model_name": "gpt-x"}, raising=False
    )
    message = model.query([{"role": "user", "content": "x"}])
    rendered = repr(message) + repr(model.serialize()) + repr(model.get_template_vars())
    for canary in (*CANARIES, "resp-secret-id"):
        assert canary not in rendered
    assert message["extra"]["response"]["usage"]["total_tokens"] == 50
    assert message["content"] == "run ls"
    assert message["extra"]["actions"] == [{"action": "ls"}]


def test_an_unsafe_response_model_never_reaches_the_ledger_or_the_message(monkeypatch):
    """The model ID is server-controlled and published as provenance, so it obeys the same rule."""
    choice = types.SimpleNamespace(
        message=types.SimpleNamespace(
            model_dump=lambda **_kw: {"role": "assistant", "content": "run ls"}
        ),
    )
    payload = types.SimpleNamespace(
        model=f"gpt-4o {CANARIES[0]}", usage=_usage(), id="resp-secret-id", choices=[choice],
        model_dump=lambda **_kw: {"body": "raw-server-body"},
    )
    model = _model(responses=[payload])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    monkeypatch.setattr(type(model), "_calculate_cost", lambda self, response: {"cost": 0.0})
    monkeypatch.setattr(type(model), "_parse_actions", lambda self, response: [])
    monkeypatch.setattr(type(model), "_prepare_messages_for_api", lambda self, messages: messages)
    message = model.query([{"role": "user", "content": "x"}])

    rendered = repr(model.usage_ledger.attempts) + repr(message)
    assert CANARIES[0] not in rendered
    assert model.usage_ledger.attempts[0].model is None
    # The tokens are real; only the unusable identity is dropped, and that makes the run incomplete.
    assert model.usage_ledger.totals() == (30, 20, 50)
    assert model.usage_ledger.is_complete() is False
    assert message["extra"]["response"]["model"] is None


# --- the phase-one model speaks the pinned Responses contract ------------------------------------

from ckb_model import CkbLitellmResponseModel  # noqa: E402


class _ResponseRecorder(CkbLitellmResponseModel):
    """Replaces only the raw provider call, so the real ledger and retry path are exercised."""

    def __init__(self, *, responses=None, errors=None, **kwargs):
        super().__init__(**kwargs)
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.raw_calls = 0

    def _raw(self):
        self.raw_calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._responses.pop(0)


def _response_model(**kwargs):
    attempts = int(kwargs.get("max_query_attempts", 1))
    kwargs.setdefault("retry_backoff_seconds", (4, 8, 16)[:attempts - 1])
    kwargs.setdefault("retryable_failure_categories", (
        "rate_limit", "timeout", "connection", "server", "protocol", "other_provider",
    ))
    kwargs.setdefault("observation_max_bytes", 32768)
    return _ResponseRecorder(model_name="openai/gpt-5.6-sol", model_kwargs={},
                             cost_tracking="ignore_errors", **kwargs)


_DEFAULT = object()


def _responses_body(*, model="gpt-5.6-sol", output=None, usage=_DEFAULT, status="completed"):
    """A Responses body whose every surface outside the protocol items carries a canary."""
    return types.SimpleNamespace(
        id="resp-secret-id", object="response", status=status, model=model,
        output=output if output is not None else [
            {"type": "reasoning", "id": "rs-1",
             "summary": [{"type": "summary_text", "text": CANARIES[1]}],
             "status": "completed", "format": "openai-responses-v1"},
            {"type": "message", "id": "msg-1", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "secret-completion-text"}]},
            {"type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "bash",
             "arguments": '{"command": "ls"}', "status": "completed",
             "caller": None, "namespace": None},
        ],
        usage=_usage() if usage is _DEFAULT else usage,
        model_dump=lambda **_kw: {"secret": CANARIES[0], "body": CANARIES[1]},
    )


def _wire_raw(monkeypatch, model):
    monkeypatch.setattr(
        "minisweagent.models.litellm_response_model.LitellmResponseModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    monkeypatch.setattr(type(model), "_calculate_cost", lambda self, response: {"cost": 0.0})
    monkeypatch.setattr("ckb_model.time.sleep", lambda _seconds: None)


def test_a_responses_turn_keeps_only_the_protocol_items_and_provenance(monkeypatch):
    """The message becomes the next stateless turn, so the calls survive and the body does not."""
    model = _response_model(responses=[_responses_body()])
    _wire_raw(monkeypatch, model)
    message = model.query([{"role": "user", "content": "x"}])

    assert message["object"] == "response"
    # Every item, in order: GPT-5.6 persists reasoning and a manual history must resend all of it.
    assert [item["type"] for item in message["output"]] == [
        "reasoning", "message", "function_call"
    ]
    assert message["extra"]["actions"] == [{"command": "ls", "tool_call_id": "call-1"}]
    assert message["extra"]["response"] == {
        "model": "gpt-5.6-sol",
        "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
    }
    # Protocol history is in-memory conversation, not published provenance: the wrapper, response
    # ID, status and usage object stay out of the ledger and every retained surface.
    assert set(message) == {"object", "output", "extra"}
    published = (repr(model.usage_ledger.attempts) + repr(model.usage_ledger.last_provenance())
                 + repr(model.serialize()) + repr(model.get_template_vars()))
    for canary in (*CANARIES, "resp-secret-id", "secret-completion-text"):
        assert canary not in published
    assert "resp-secret-id" not in repr(message)


def test_native_usage_is_mapped_to_the_public_names_at_one_boundary(monkeypatch):
    """input->prompt and output->completion happen once, in the ledger, and nowhere else."""
    from ckb_model import NATIVE_TO_PUBLIC

    assert NATIVE_TO_PUBLIC == {"input_tokens": "prompt_tokens",
                                "output_tokens": "completion_tokens",
                                "total_tokens": "total_tokens"}
    model = _response_model(responses=[_responses_body(usage=_usage(11, 7, 18))])
    _wire_raw(monkeypatch, model)
    model.query([{"role": "user", "content": "x"}])
    assert model.usage_ledger.totals() == (11, 7, 18)
    assert model.usage_ledger.last_provenance()["usage"] == {
        "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18
    }


@pytest.mark.parametrize("usage,label", [
    (types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15), "chat names"),
    (types.SimpleNamespace(input_tokens=10, output_tokens=5), "missing total"),
    (types.SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=99), "broken identity"),
    (None, "no usage block"),
])
def test_missing_or_malformed_native_usage_is_incomplete(usage, label, monkeypatch):
    model = _response_model(responses=[_responses_body(usage=usage)])
    _wire_raw(monkeypatch, model)
    model.query([{"role": "user", "content": "x"}])
    assert model.usage_ledger.is_complete() is False, label
    assert model.usage_ledger.totals() is None
    assert model.usage_ledger.response_count == 1


def test_an_incomplete_responses_output_is_a_format_error_not_a_leak(monkeypatch):
    """No function_call means no action; the FormatError message must still carry no body."""
    from minisweagent.exceptions import FormatError

    model = _response_model(responses=[_responses_body(
        status="incomplete",
        output=[{"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "secret-completion-text"}]}],
    )])
    _wire_raw(monkeypatch, model)
    with pytest.raises(FormatError) as exc:
        model.query([{"role": "user", "content": "x"}])
    tb = "".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__))
    rendered = repr(exc.value.messages) + repr(model.usage_ledger.attempts) + tb
    for canary in (*CANARIES, "resp-secret-id", "secret-completion-text"):
        assert canary not in rendered
    assert exc.value.messages[0]["extra"]["response"]["model"] == "gpt-5.6-sol"
    # The provider answered and its usage was valid: that is agent behavior, not infrastructure.
    assert model.usage_ledger.is_complete() is True


def test_a_valid_response_then_an_action_format_failure_stays_in_the_ledger(monkeypatch):
    from minisweagent.exceptions import FormatError

    model = _response_model(responses=[_responses_body(
        output=[{"type": "function_call", "call_id": "c", "name": "rm", "arguments": "{}",
                 "status": "completed"}]
    )])
    _wire_raw(monkeypatch, model)
    with pytest.raises(FormatError):
        model.query([{"role": "user", "content": "x"}])
    assert model.usage_ledger.response_count == 1
    assert model.usage_ledger.totals() == (30, 20, 50)
    assert model.usage_ledger.is_complete() is True


@pytest.mark.parametrize("models,complete", [
    (("gpt-5.6-sol", "gpt-5.6-sol"), True),
    (("gpt-5.6-sol", "gpt-5.6-luna"), False),
    (("gpt-5.6-sol", None), False),
])
def test_returned_model_drift_is_unchanged_under_responses(models, complete, monkeypatch):
    model = _response_model(responses=[_responses_body(model=m) for m in models])
    _wire_raw(monkeypatch, model)
    for _ in models:
        model.query([{"role": "user", "content": "x"}])
    assert model.usage_ledger.is_complete() is complete


def test_an_unsafe_responses_model_identity_is_dropped(monkeypatch):
    model = _response_model(responses=[_responses_body(model=f"gpt {CANARIES[0]}")])
    _wire_raw(monkeypatch, model)
    message = model.query([{"role": "user", "content": "x"}])
    assert model.usage_ledger.attempts[0].model is None
    assert model.usage_ledger.is_complete() is False
    assert CANARIES[0] not in repr(message) + repr(model.usage_ledger.attempts)


def test_a_provider_failure_on_the_responses_path_is_sanitized_and_not_retried(monkeypatch):
    model = _response_model(errors=[OSError(" ".join(CANARIES))])
    _wire_raw(monkeypatch, model)
    with pytest.raises(ProviderCallError) as exc:
        model.query([{"role": "user", "content": "x"}])
    tb = "".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__))
    for canary in CANARIES:
        assert canary not in str(exc.value) and canary not in tb
    assert model.raw_calls == 1
    assert model.usage_ledger.attempt_count == 1 and model.usage_ledger.response_count == 0


def test_one_provider_fault_is_retried_once_and_the_response_is_usable(monkeypatch):
    model = _response_model(
        errors=[OSError("transport sk-live-do-not-log")],
        responses=[_responses_body()],
        max_query_attempts=2,
    )
    _wire_raw(monkeypatch, model)

    message = model.query([{"role": "user", "content": "x"}])

    assert message["extra"]["actions"] == [{"command": "ls", "tool_call_id": "call-1"}]
    assert model.raw_calls == 2
    assert (model.usage_ledger.turn_count, model.usage_ledger.attempt_count,
            model.usage_ledger.response_count) == (1, 2, 1)
    assert model.usage_ledger.provider_failure_category == "connection"
    assert model.usage_ledger.provider_failure_counts == {"connection": 1}
    assert model.usage_ledger.retry_count == 1
    assert model.usage_ledger.retry_delay_seconds == 4
    assert model.usage_ledger.is_correctness_complete() is True
    assert model.usage_ledger.is_complete() is False
    assert "sk-live-do-not-log" not in repr(model.usage_ledger.attempts) + repr(message)


def test_two_provider_faults_exhaust_the_bounded_retry(monkeypatch):
    model = _response_model(
        errors=[OSError("first"), TimeoutError("second")], max_query_attempts=2
    )
    _wire_raw(monkeypatch, model)

    with pytest.raises(ProviderCallError):
        model.query([{"role": "user", "content": "x"}])

    assert model.raw_calls == 2
    assert (model.usage_ledger.turn_count, model.usage_ledger.attempt_count,
            model.usage_ledger.response_count) == (1, 2, 0)
    assert model.usage_ledger.provider_failure_category == "multiple"
    assert model.usage_ledger.provider_failure_counts == {"connection": 1, "timeout": 1}
    assert model.usage_ledger.retry_count == 1
    assert model.usage_ledger.retry_delay_seconds == 4
    assert model.usage_ledger.is_correctness_complete() is False


def test_an_internal_error_is_never_retried_even_when_two_attempts_are_configured(monkeypatch):
    model = _response_model(errors=[RuntimeError("harness bug")], max_query_attempts=2)
    _wire_raw(monkeypatch, model)

    with pytest.raises(RuntimeError, match="harness bug"):
        model.query([{"role": "user", "content": "x"}])

    assert model.raw_calls == 1
    assert model.usage_ledger.attempt_count == 0
    assert model.usage_ledger.internal_errors == 1


def test_a_local_preparation_failure_reaches_no_provider_or_retry(monkeypatch):
    delays = []
    model = _response_model(errors=[OSError("transport")], max_query_attempts=2)
    _wire_raw(monkeypatch, model)
    monkeypatch.setattr("ckb_model.time.sleep", delays.append)
    monkeypatch.setattr(
        "ckb_model._prepare_response_history",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("local preparation failed")),
    )

    with pytest.raises(RuntimeError, match="local preparation failed"):
        model.query([{"role": "user", "content": "x"}])

    assert model.raw_calls == 0
    assert delays == []
    assert model.usage_ledger.retry_count == 0


def test_a_retry_reuses_one_canonical_payload_byte_for_byte(monkeypatch):
    from ckb_model import ReplayFacts

    delays = []
    prepared_calls = []
    sent = []
    model = _response_model(
        errors=[OSError("transport")], responses=[_responses_body()], max_query_attempts=2
    )

    def prepare(*args, **kwargs):
        prepared_calls.append((args, kwargs))
        return ([{"role": "user", "content": "canonical"}], ReplayFacts(47, False))

    def raw(self, messages, **kwargs):
        sent.append(json.dumps(messages, sort_keys=True, separators=(",", ":")))
        return self._raw()

    monkeypatch.setattr("ckb_model._prepare_response_history", prepare)
    monkeypatch.setattr(
        "minisweagent.models.litellm_response_model.LitellmResponseModel._query", raw
    )
    monkeypatch.setattr(type(model), "_calculate_cost", lambda self, response: {"cost": 0.0})
    monkeypatch.setattr("ckb_model.time.sleep", delays.append)

    model.query([{"role": "user", "content": "original"}])

    assert len(prepared_calls) == 1
    assert sent == [sent[0], sent[0]]
    assert model.raw_calls == 2 and delays == [4]
    assert model.usage_ledger.replays == [ReplayFacts(47, False)]


def test_an_internal_query_error_after_retries_preserves_completed_wait_evidence(monkeypatch):
    delays = []
    model = _response_model(
        errors=[OSError("first"), OSError("second"), RuntimeError("local bug")],
        max_query_attempts=4,
    )
    _wire_raw(monkeypatch, model)
    monkeypatch.setattr("ckb_model.time.sleep", delays.append)

    with pytest.raises(RuntimeError, match="local bug"):
        model.query([{"role": "user", "content": "x"}])

    assert model.raw_calls == 3
    assert delays == [4, 8]
    assert model.usage_ledger.attempt_count == 2
    assert model.usage_ledger.response_count == 0
    assert model.usage_ledger.internal_errors == 1
    assert model.usage_ledger.retry_count == 2
    assert model.usage_ledger.retry_delay_seconds == 12
    assert model.usage_ledger.provider_failure_counts == {"connection": 2}


@pytest.mark.parametrize("kwargs,match", [
    ({"retry_backoff_seconds": (-1,)}, "positive integer"),
    ({"retryable_failure_categories": ("authentication",)}, "transient-only"),
])
def test_runtime_config_cannot_expand_the_reviewed_retry_boundary(kwargs, match, monkeypatch):
    model = _response_model(max_query_attempts=2, **kwargs)
    _wire_raw(monkeypatch, model)

    with pytest.raises(ValueError, match=match):
        model.query([{"role": "user", "content": "x"}])

    assert model.raw_calls == 0


def test_diagnostic_records_the_real_retry_attempt_index(monkeypatch):
    records = []

    class Session:
        def reserve_request(self):
            return None

        def poison(self):
            raise AssertionError("diagnostic was poisoned")

        def record(self, **kwargs):
            records.append(kwargs)

    class Seam:
        def begin_attempt(self):
            return None

        def end_attempt(self):
            return "response_seen"

    model = _response_model(
        errors=[OSError("transport")], responses=[_responses_body()], max_query_attempts=2
    )
    _wire_raw(monkeypatch, model)
    model.attach_diagnostic(Session(), Seam())

    model.query([{"role": "user", "content": "x"}])

    assert [(record["turn_index"], record["attempt_index"]) for record in records] == [
        (0, 0), (0, 1)
    ]


def test_a_serialization_failure_cannot_publish_the_response(monkeypatch):
    """A hostile item serializer must not put its text into any diagnostic. No edited assertions."""
    from ckb_model import ResponseConversionError

    class _Hostile:
        type = "function_call"

        def model_dump(self):
            raise RuntimeError(f"cannot serialize {CANARIES[0]}")

        def __repr__(self):
            return f"<hostile {CANARIES[1]}>"

    model = _response_model(responses=[_responses_body(output=[_Hostile()])])
    _wire_raw(monkeypatch, model)
    with pytest.raises(ResponseConversionError) as exc:
        model.query([{"role": "user", "content": "x"}])
    tb = "".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__))
    rendered = str(exc.value) + tb + repr(model.usage_ledger.attempts)
    for canary in CANARIES:
        assert canary not in rendered
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    # The response was recorded before this failure; its usage is still honest.
    assert model.usage_ledger.response_count == 1
    assert model.usage_ledger.totals() == (30, 20, 50)


def test_a_cost_accounting_failure_is_sanitized_and_keeps_the_ledger(monkeypatch):
    from ckb_model import ResponseConversionError

    model = _response_model(responses=[_responses_body()])
    monkeypatch.setattr(
        "minisweagent.models.litellm_response_model.LitellmResponseModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    monkeypatch.setattr(type(model), "_calculate_cost",
                        lambda self, response: (_ for _ in ()).throw(
                            RuntimeError(f"pricing blew up {CANARIES[0]}")))
    with pytest.raises(ResponseConversionError) as exc:
        model.query([{"role": "user", "content": "x"}])
    tb = "".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__))
    for canary in CANARIES:
        assert canary not in str(exc.value) + tb
    assert model.usage_ledger.response_count == 1


@pytest.mark.parametrize("name", ["rm", "TOOL-CANARY-VALUE", "", None])
def test_a_hostile_tool_name_never_reaches_a_format_error(name, monkeypatch):
    """Upstream interpolates the returned name; the agent stores that string as a diagnostic."""
    from ckb_model import NO_TOOL_CALL, UNFINISHED_RESPONSE, UNUSABLE_TOOL_CALL
    from minisweagent.exceptions import FormatError

    model = _response_model(responses=[_responses_body(output=[
        {"type": "function_call", "call_id": "c", "name": name,
         "arguments": '{"command": "ls"}', "status": "completed"},
    ])])
    _wire_raw(monkeypatch, model)
    with pytest.raises(FormatError) as exc:
        model.query([{"role": "user", "content": "x"}])
    # The strongest property available: the message IS one of the fixed constants, so nothing
    # provider-controlled can be in it by construction.
    text = exc.value.messages[0]["content"][0]["text"]
    assert text in (UNFINISHED_RESPONSE, NO_TOOL_CALL, UNUSABLE_TOOL_CALL)
    assert "TOOL-CANARY-VALUE" not in repr(exc.value.messages)


@pytest.mark.parametrize("arguments,label", [
    ('{"command": "' + CANARIES[0] + '" TRAILING GARBAGE', "malformed json holding a canary"),
    ("not json at all", "not json"),
    ('{"cmd": "ls"}', "wrong key"),
    ('{"command": 17}', "non-string command"),
    ('["ls"]', "not an object"),
    ('{"command": "ls"' + CANARIES[1] + '}', "truncated json holding a canary"),
])
def test_malformed_arguments_leave_no_reachable_provider_value(arguments, label, monkeypatch):
    """A JSONDecodeError keeps the whole argument string in `.doc`; that must not be reachable."""
    from ckb_model import NO_TOOL_CALL, UNFINISHED_RESPONSE, UNUSABLE_TOOL_CALL
    from minisweagent.exceptions import FormatError

    model = _response_model(responses=[_responses_body(output=[
        {"type": "function_call", "call_id": "c", "name": "bash",
         "arguments": arguments, "status": "completed"},
    ])])
    _wire_raw(monkeypatch, model)
    with pytest.raises(FormatError) as exc:
        model.query([{"role": "user", "content": "x"}])

    error = exc.value
    assert error.__cause__ is None, label
    assert error.__context__ is None, label
    text = error.messages[0]["content"][0]["text"]
    assert text in (UNFINISHED_RESPONSE, NO_TOOL_CALL, UNUSABLE_TOOL_CALL)

    surfaces = "".join((
        str(error), repr(error.messages), repr(error.__cause__), repr(error.__context__),
        repr(getattr(error.__context__, "doc", "")),
        "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        repr(model.usage_ledger.attempts), repr(model.usage_ledger.last_provenance()),
    ))
    for canary in CANARIES:
        assert canary not in surfaces, f"{canary!r} reachable via {label}"

    assert model.usage_ledger.response_count == 1
    assert model.usage_ledger.is_complete() is True


def test_a_malformed_argument_canary_never_reaches_the_agent(monkeypatch, tmp_path):
    """The full stored-diagnostic surface, through the agent that persists str(e) and a traceback."""
    from minisweagent.agents.default import AgentConfig, DefaultAgent
    from minisweagent.environments.local import LocalEnvironment

    model = _response_model(responses=[_responses_body(output=[
        {"type": "function_call", "call_id": "c", "name": "bash", "status": "completed",
         "arguments": '{"command": "' + CANARIES[0] + '" TRAILING GARBAGE'},
    ])])
    _wire_raw(monkeypatch, model)
    agent = DefaultAgent(
        model, LocalEnvironment(cwd=str(tmp_path)),
        config_class=AgentConfig, system_template="s", instance_template="i", step_limit=1,
        max_consecutive_format_errors=1,
    )
    executed = []
    # The environment's own execute() is the seam an action would reach.
    monkeypatch.setattr(LocalEnvironment, "execute",
                        lambda self, command, **kw: executed.append(command))
    agent.run("task")

    assert executed == [], "a malformed call must never become an executed action"
    rendered = json.dumps(agent.messages, default=str) + json.dumps(agent.serialize(), default=str)
    for canary in CANARIES:
        assert canary not in rendered



# --- an unusable call never reaches the execution environment ------------------------------------

UNEXECUTABLE = {
    "response-incomplete": ("incomplete", [
        {"type": "function_call", "call_id": "c", "name": "bash",
         "arguments": '{"command": "ls"}', "status": "completed"}]),
    "call-incomplete": ("completed", [
        {"type": "function_call", "call_id": "c", "name": "bash",
         "arguments": '{"command": "ls"}', "status": "incomplete"}]),
    "missing-call-id": ("completed", [
        {"type": "function_call", "name": "bash",
         "arguments": '{"command": "ls"}', "status": "completed"}]),
    "blank-call-id": ("completed", [
        {"type": "function_call", "call_id": "   ", "name": "bash",
         "arguments": '{"command": "ls"}', "status": "completed"}]),
    "non-string-call-id": ("completed", [
        {"type": "function_call", "call_id": 7, "name": "bash",
         "arguments": '{"command": "ls"}', "status": "completed"}]),
    "duplicate-call-ids": ("completed", [
        {"type": "function_call", "call_id": "same", "name": "bash",
         "arguments": '{"command": "ls"}', "status": "completed"},
        {"type": "function_call", "call_id": "same", "name": "bash",
         "arguments": '{"command": "pwd"}', "status": "completed"}]),
    "non-string-command": ("completed", [
        {"type": "function_call", "call_id": "c", "name": "bash",
         "arguments": '{"command": ["ls"]}', "status": "completed"}]),
}


@pytest.mark.parametrize("case", sorted(UNEXECUTABLE))
def test_an_unusable_call_yields_no_action_and_reaches_no_execution_seam(case, monkeypatch):
    """The agent runs actions as soon as it has them, so an unlinkable call must never become one."""
    from minisweagent.exceptions import FormatError

    status, output = UNEXECUTABLE[case]
    model = _response_model(responses=[_responses_body(status=status, output=output)])
    _wire_raw(monkeypatch, model)
    executed = []
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: executed.append(a) or pytest.fail("a command ran"))
    with pytest.raises(FormatError) as exc:
        model.query([{"role": "user", "content": "x"}])
    assert executed == []
    # A post-response format failure: the provider answered, so its usage stays honest.
    assert model.usage_ledger.response_count == 1
    assert model.usage_ledger.is_complete() is True
    rendered = repr(exc.value.messages)
    for canary in (*CANARIES, "resp-secret-id", "secret-completion-text"):
        assert canary not in rendered


def test_two_turns_replay_every_output_item_in_order(monkeypatch):
    """Turn two's input must carry the reasoning, message, call and its output, in order."""
    model = _response_model(responses=[_responses_body()])
    _wire_raw(monkeypatch, model)
    first = model.query([{"role": "user", "content": "start"}])
    observations = model.format_observation_messages(
        first, [{"output": "ok", "returncode": 0, "exception_info": None}]
    )
    conversation = [{"role": "user", "content": "start"}, first, *observations]
    next_input = model._prepare_messages_for_api(conversation)

    assert [item.get("type") for item in next_input] == [
        None, "reasoning", "message", "function_call", "function_call_output"
    ]
    assert next_input[1]["id"] == "rs-1", "the reasoning item must survive to turn two"
    assert next_input[3]["call_id"] == next_input[4]["call_id"] == "call-1"
    assert all("extra" not in item for item in next_input)


def test_the_default_responses_attempt_policy_is_one():
    assert _response_model().config.max_query_attempts == 1


def test_observation_messages_use_the_responses_function_call_output_shape(monkeypatch):
    model = _response_model(responses=[_responses_body()])
    _wire_raw(monkeypatch, model)
    message = model.query([{"role": "user", "content": "x"}])
    observations = model.format_observation_messages(
        message, [{"output": "ok", "returncode": 0, "exception_info": None}]
    )
    assert observations[0]["type"] == "function_call_output"
    assert observations[0]["call_id"] == "call-1"


def test_large_observations_keep_a_utf8_head_and_tail_under_the_profile_budget(monkeypatch):
    from ckb_model import _OBSERVATION_TRUNCATION_NOTICE

    model = _response_model(responses=[_responses_body()])
    _wire_raw(monkeypatch, model)
    message = model.query([{"role": "user", "content": "x"}])
    raw = "HEAD-" + ("\u03bb" * 40000) + "-TAIL"
    output = {"output": raw, "returncode": 0, "exception_info": None}

    observation = model.format_observation_messages(message, [output])[0]

    assert len(observation["output"].encode("utf-8")) <= 32768
    assert "HEAD-" in observation["output"] and "-TAIL" in observation["output"]
    assert _OBSERVATION_TRUNCATION_NOTICE in observation["output"]
    assert len(observation["extra"]["raw_output"].encode("utf-8")) <= 32768
    assert output["output"] == raw


def test_a_bounded_large_observation_keeps_the_next_responses_turn_replayable(monkeypatch):
    from ckb_model import _history_bytes

    model = _response_model(
        responses=[_responses_body()],
        replay_policy="prefix-tail-groups-v1",
        replay_max_bytes=131072,
    )
    _wire_raw(monkeypatch, model)
    first = model.query([{"role": "user", "content": "start"}])
    observations = model.format_observation_messages(
        first,
        [{"output": "x" * 500000, "returncode": 0, "exception_info": None}],
    )

    prepared = model._prepare_messages_for_api([
        {"role": "user", "content": "start"}, first, *observations,
    ])

    assert _history_bytes(prepared) <= 131072
    assert prepared[-1]["type"] == "function_call_output"
    assert prepared[-1]["call_id"] == "call-1"


def test_multiple_observations_share_one_turn_budget():
    model = _response_model()
    message = {"extra": {"actions": [
        {"command": "one", "tool_call_id": "call-1"},
        {"command": "two", "tool_call_id": "call-2"},
    ]}}
    outputs = [
        {"output": "a" * 40000, "returncode": 0, "exception_info": None},
        {"output": "b" * 40000, "returncode": 0, "exception_info": None},
    ]

    observations = model.format_observation_messages(message, outputs)

    assert sum(len(item["output"].encode("utf-8")) for item in observations) <= 32768
    assert [item["call_id"] for item in observations] == ["call-1", "call-2"]


def test_no_provider_value_reaches_the_agent_exit_diagnostic(monkeypatch, tmp_path):
    """`DefaultAgent.handle_uncaught_exception` stores str(e) AND the formatted traceback."""
    from ckb_model import ResponseConversionError
    from minisweagent.agents.default import AgentConfig, DefaultAgent
    from minisweagent.environments.local import LocalEnvironment

    class _Boom:
        type = "function_call"

        def model_dump(self):
            raise RuntimeError(f"provider text {CANARIES[0]} {CANARIES[1]}")

    model = _response_model(responses=[_responses_body(output=[_Boom()])])
    _wire_raw(monkeypatch, model)
    agent = DefaultAgent(
        model, LocalEnvironment(cwd=str(tmp_path)),
        config_class=AgentConfig, system_template="s", instance_template="i", step_limit=1,
    )
    # Upstream records the diagnostic and re-raises, so the failure is observed here.
    with pytest.raises(ResponseConversionError):
        agent.run("task")

    exit_message = agent.messages[-1]
    assert exit_message["extra"]["exit_status"] == "ResponseConversionError"
    rendered = json.dumps(agent.messages, default=str) + json.dumps(agent.serialize(), default=str)
    for canary in CANARIES:
        assert canary not in rendered, f"{canary!r} reached the agent's stored diagnostics"
    # Both surfaces upstream fills from the exception.
    assert CANARIES[0] not in exit_message["extra"]["exception_str"]
    assert CANARIES[0] not in exit_message["extra"]["traceback"]


# --- provider-failure provenance: one fixed category, never exception material --------------------
#
# The category comes from the exception type; every canary below sits in the message and must never
# survive the sanitization boundary.

PROVIDER_CANARIES = ("sk-live-do-not-log", "https://user:sk-live@proxy.example/v1",
                     "raw-server-body", "resp-secret-id", "Bearer abc123")
CANARY_MESSAGE = " ".join(PROVIDER_CANARIES)


def _litellm_exc(name, message):
    """Build a LiteLLM exception if this fork exposes it, else skip the row."""
    import httpx
    import litellm.exceptions as le

    cls = getattr(le, name, None)
    if cls is None:
        pytest.skip(f"litellm has no {name}")
    response = httpx.Response(400, request=httpx.Request("POST", "https://proxy.example/responses"))
    for kwargs in (
        {"llm_provider": "p", "model": "m"},
        {"llm_provider": "p", "model": "m", "response": response},
        {},
    ):
        try:
            return cls(message, **kwargs)
        except TypeError:
            continue
    # `APIError` takes `status_code` first, so the message cannot be positional there.
    try:
        return cls(status_code=500, message=message, llm_provider="p", model="m")
    except TypeError:
        pytest.skip(f"cannot construct {name}")


@pytest.mark.parametrize("name,expected", [
    ("AuthenticationError", "authentication"),
    ("PermissionDeniedError", "authorization"),
    ("RateLimitError", "rate_limit"),
    ("Timeout", "timeout"),
    ("APIConnectionError", "connection"),
    ("ServiceUnavailableError", "server"),
    ("InternalServerError", "server"),
    ("BadRequestError", "request"),
    ("NotFoundError", "request"),
    ("UnsupportedParamsError", "unsupported"),
    ("ContextWindowExceededError", "context_window"),
])
def test_each_litellm_family_maps_to_its_fixed_category(name, expected):
    from ckb_model import provider_failure_category

    exc = _litellm_exc(name, CANARY_MESSAGE)
    category = provider_failure_category(exc)
    assert category == expected, f"{name} mapped to {category!r}"
    # The category is a fixed literal, so no canary can ride along inside it.
    for canary in PROVIDER_CANARIES:
        assert canary not in str(category)


@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("slow " + CANARY_MESSAGE), "timeout"),
    (OSError("dns " + CANARY_MESSAGE), "connection"),
    (ConnectionResetError("reset " + CANARY_MESSAGE), "connection"),
    (json.JSONDecodeError("bad", "{" + CANARY_MESSAGE, 0), "protocol"),
])
def test_builtin_transport_and_protocol_failures_map_by_type(exc, expected):
    from ckb_model import provider_failure_category

    assert provider_failure_category(exc) == expected


def test_a_generic_api_error_falls_back_to_other_provider(monkeypatch):
    """The last mapping row: a positively allowlisted provider fault with no specific rule.

    `other_provider` is deliberately unnarrowed — narrowing it would mean reading the message.
    """
    from ckb_model import is_provider_fault, provider_failure_category

    exc = _litellm_exc("APIError", CANARY_MESSAGE)
    assert is_provider_fault(exc), "APIError must be positively allowlisted to be categorized"
    assert provider_failure_category(exc) == "other_provider"

    model = _model(errors=[exc])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    with pytest.raises(ProviderCallError) as raised:
        model.query([{"role": "user", "content": "x"}])
    assert model.usage_ledger.provider_failure_category == "other_provider"

    # The class name is retained in the in-memory attempt list by design; what matters is that it
    # cannot cross the boundary the agent and the result see.
    assert model.usage_ledger.attempts[-1].error == "APIError"
    published = (str(raised.value)
                 + "".join(traceback.format_exception(
                     type(raised.value), raised.value, raised.value.__traceback__))
                 + repr(model.usage_ledger.last_provenance())
                 + str(model.usage_ledger.provider_failure_category)
                 + repr(model.serialize()) + repr(model.get_template_vars()))
    assert "APIError" not in published
    for canary in PROVIDER_CANARIES:
        assert canary not in published


def test_context_window_wins_over_its_bad_request_superclass():
    """`ContextWindowExceededError` IS a `BadRequestError`; ordering decides which rule fires."""
    import litellm.exceptions as le

    from ckb_model import provider_failure_category

    assert issubclass(le.ContextWindowExceededError, le.BadRequestError)
    assert provider_failure_category(
        _litellm_exc("ContextWindowExceededError", CANARY_MESSAGE)
    ) == "context_window"
    assert provider_failure_category(_litellm_exc("BadRequestError", CANARY_MESSAGE)) == "request"


def test_timeout_wins_over_its_connection_superclass():
    """LiteLLM's `Timeout` subclasses openai's `APIConnectionError`, not LiteLLM's."""
    import litellm.exceptions as le
    import openai

    from ckb_model import provider_failure_category

    assert issubclass(le.Timeout, openai.APIConnectionError)
    assert provider_failure_category(_litellm_exc("Timeout", CANARY_MESSAGE)) == "timeout"


def test_timeout_wins_over_the_oserror_connection_rule():
    """`TimeoutError` subclasses `OSError`; the broader rule must not swallow it."""
    from ckb_model import provider_failure_category

    assert issubclass(TimeoutError, OSError)
    assert provider_failure_category(TimeoutError("slow")) == "timeout"


def test_a_timed_out_responses_request_is_one_sanitized_unanswered_attempt(monkeypatch):
    model = _response_model(errors=[_litellm_exc("Timeout", CANARY_MESSAGE)])
    _wire_raw(monkeypatch, model)

    with pytest.raises(ProviderCallError) as exc:
        model.query([{"role": "user", "content": "x"}])

    assert model.raw_calls == 1
    assert model.usage_ledger.attempt_count == 1
    assert model.usage_ledger.response_count == 0
    assert model.usage_ledger.provider_failure_category == "timeout"
    assert model.usage_ledger.is_complete() is False
    published = str(exc.value) + "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    for canary in PROVIDER_CANARIES:
        assert canary not in published


def test_the_profile_v6_schedule_waits_4_8_16_and_stops_after_four_attempts(monkeypatch):
    delays = []
    model = _response_model(errors=[OSError("transient")] * 4, max_query_attempts=4)
    _wire_raw(monkeypatch, model)
    monkeypatch.setattr("ckb_model.time.sleep", delays.append)

    with pytest.raises(ProviderCallError):
        model.query([{"role": "user", "content": "x"}])

    assert model.raw_calls == 4
    assert delays == [4, 8, 16]
    assert model.usage_ledger.retry_count == 3
    assert model.usage_ledger.retry_delay_seconds == 28
    assert model.usage_ledger.provider_failure_counts == {"connection": 4}


@pytest.mark.parametrize("exc,category", [
    (_litellm_exc("RateLimitError", CANARY_MESSAGE), "rate_limit"),
    (TimeoutError(CANARY_MESSAGE), "timeout"),
    (OSError(CANARY_MESSAGE), "connection"),
    (_litellm_exc("InternalServerError", CANARY_MESSAGE), "server"),
    (json.JSONDecodeError("bad", "{" + CANARY_MESSAGE, 0), "protocol"),
    (_litellm_exc("APIError", CANARY_MESSAGE), "other_provider"),
])
def test_each_approved_transient_category_gets_one_delayed_recovery(
    exc, category, monkeypatch
):
    delays = []
    model = _response_model(
        errors=[exc], responses=[_responses_body()], max_query_attempts=4
    )
    _wire_raw(monkeypatch, model)
    monkeypatch.setattr("ckb_model.time.sleep", delays.append)

    model.query([{"role": "user", "content": "x"}])

    assert model.raw_calls == 2
    assert delays == [4]
    assert model.usage_ledger.provider_failure_counts == {category: 1}


@pytest.mark.parametrize("name,category", [
    ("AuthenticationError", "authentication"),
    ("PermissionDeniedError", "authorization"),
    ("BadRequestError", "request"),
    ("UnsupportedParamsError", "unsupported"),
    ("ContextWindowExceededError", "context_window"),
])
def test_non_transient_provider_categories_stop_without_sleep(name, category, monkeypatch):
    delays = []
    model = _response_model(
        errors=[_litellm_exc(name, CANARY_MESSAGE)],
        responses=[_responses_body()],
        max_query_attempts=4,
    )
    _wire_raw(monkeypatch, model)
    monkeypatch.setattr("ckb_model.time.sleep", delays.append)

    with pytest.raises(ProviderCallError):
        model.query([{"role": "user", "content": "x"}])

    assert model.raw_calls == 1
    assert delays == []
    assert model.usage_ledger.retry_count == 0
    assert model.usage_ledger.provider_failure_counts == {category: 1}


@pytest.mark.parametrize("exc", [RuntimeError("harness bug"), TypeError("bug"), KeyError("bug")])
def test_an_internal_error_gets_no_provider_category(exc):
    from ckb_model import is_provider_fault, provider_failure_category

    assert is_provider_fault(exc) is False
    assert provider_failure_category(exc) is None


def test_the_ledger_reduces_one_category_and_disagreement_to_multiple():
    from ckb_model import UsageLedger

    empty = UsageLedger()
    assert empty.provider_failure_category is None

    same = UsageLedger()
    for _ in range(3):
        same.record_turn()
        same.record_failure(OSError("transport"))
    assert same.provider_failure_category == "connection"

    mixed = UsageLedger()
    mixed.record_turn()
    mixed.record_failure(OSError("transport"))
    mixed.record_turn()
    mixed.record_failure(TimeoutError("slow"))
    assert mixed.provider_failure_category == "multiple"


def test_an_internal_error_leaves_the_ledger_category_empty():
    from ckb_model import UsageLedger

    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_failure(RuntimeError("harness bug"))
    assert ledger.provider_failure_category is None


def test_a_responded_turn_with_bad_usage_has_no_failure_category():
    """Incomplete because usage was unusable is not the same as an unanswered attempt."""
    from ckb_model import UsageLedger

    ledger = UsageLedger()
    ledger.record_turn()
    ledger.record_response(types.SimpleNamespace(model="gpt-x", usage=types.SimpleNamespace()))
    assert ledger.is_complete() is False
    assert ledger.provider_failure_category is None


def test_the_sanitized_provider_error_names_no_exception_class(monkeypatch):
    """The agent stores `str(e)` and a traceback; the class name is provenance, not a message."""
    model = _model(errors=[OSError(CANARY_MESSAGE)])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    with pytest.raises(ProviderCallError) as exc:
        model.query([{"role": "user", "content": "x"}])
    rendered = str(exc.value) + "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert "OSError" not in rendered, "the exact exception class reached the agent"
    for canary in PROVIDER_CANARIES:
        assert canary not in rendered
    # The provenance still exists where it belongs.
    assert model.usage_ledger.provider_failure_category == "connection"


def test_no_canary_reaches_the_public_ledger_projection(monkeypatch):
    model = _model(errors=[OSError(CANARY_MESSAGE)])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    with pytest.raises(ProviderCallError):
        model.query([{"role": "user", "content": "x"}])
    surfaces = (repr(model.usage_ledger.attempts) + repr(model.usage_ledger.last_provenance())
                + str(model.usage_ledger.provider_failure_category)
                + repr(model.serialize()) + repr(model.get_template_vars()))
    for canary in PROVIDER_CANARIES:
        assert canary not in surfaces


def test_a_canary_failure_reaches_the_report_only_as_its_category(tmp_path, monkeypatch):
    """The whole path: provider exception -> ledger -> metrics -> result JSON -> rendered report."""
    import ckbbench.matrix.store as store
    from ckbbench.matrix.build_site import build_dataset
    from ckbbench.matrix.conftest import synthetic_profile
    from ckbbench.matrix.render import render_ladder_html
    from ckbbench.matrix.store import load_results, validate_results
    from ckbbench.matrix.test_fixtures import synthetic_run_dict
    from ckbbench.run.metrics import collect_metrics_from_agent

    monkeypatch.setattr(store, "_reviewed_profile", lambda: synthetic_profile())
    model = _model(errors=[OSError(CANARY_MESSAGE)])
    monkeypatch.setattr(
        "minisweagent.models.litellm_model.LitellmModel._query",
        lambda self, messages, **kw: self._raw(),
    )
    with pytest.raises(ProviderCallError):
        model.query([{"role": "user", "content": "x"}])

    agent = types.SimpleNamespace(model=model)
    metrics = collect_metrics_from_agent(agent, wall_seconds=1.0)
    assert metrics.provider_failure_category == "connection"

    row = synthetic_run_dict(arm="B", outcome="infra_fail", run_id="b1", metrics=metrics,
                             model_response_id=None)
    results = tmp_path / "2.0.0"
    results.mkdir()
    (results / "b1.json").write_text(json.dumps(row))
    loaded = load_results(results)
    validate_results(loaded)

    published = (results / "b1.json").read_text() + render_ladder_html(build_dataset(loaded))
    assert "connection" in json.loads((results / "b1.json").read_text())["metrics"].values()
    assert "OSError" not in published
    for canary in PROVIDER_CANARIES:
        assert canary not in published
