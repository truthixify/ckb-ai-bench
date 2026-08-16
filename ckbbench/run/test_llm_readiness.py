"""Operator LLM readiness: authenticated, single-attempt, and safe to print.

Task 18's authorized pilot stopped before either benchmark cell because this check sent no
credential. These tests drive the real request-building boundary through `httpx.MockTransport`, so
a regression is caught by behavior rather than by grepping the source for a header name.

The credential canary must never reach argv, output, a diagnostic, a formatted traceback, or a test
failure message, so every assertion below renders redacted values only.
"""

from __future__ import annotations

import json
import traceback

import httpx
import pytest

from ckbbench.run.llm_readiness import (
    AUTH_REJECTED,
    HTTP_FAILURE,
    READY,
    TIMEOUT_SECONDS,
    UNREACHABLE,
    UNSAFE_BASE,
    Readiness,
    check_llm_readiness,
    main,
    models_url,
)

KEY = "sk-live-do-not-log"
LEGACY_KEY = "sk-legacy-do-not-log"
BODY_CANARY = "raw-server-body-do-not-log"
ENDPOINT_VARS = ("CKBBENCH_LLM_API_KEY", "BENCH_API_KEY",
                 "CKBBENCH_LLM_API_BASE", "BENCH_API_BASE")


class _Recorder:
    """Serves one canned response and records the requests it was asked to send."""

    def __init__(self, *, status=200, error=None, headers=None, body=b""):
        self.status = status
        self.error = error
        self.headers = headers or {}
        self.body = body
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, content=self.body, headers=self.headers)

    @property
    def count(self) -> int:
        return len(self.requests)


def _client(recorder: _Recorder) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(recorder.handler), follow_redirects=False)


def _check(recorder: _Recorder, **kwargs) -> Readiness:
    with _client(recorder) as client:
        return check_llm_readiness(api_base="https://proxy.example/v1", api_key=KEY,
                                   client=client, **kwargs)


# --- the request itself ---------------------------------------------------------------------------

@pytest.mark.parametrize("base,expected", [
    ("https://proxy.example", "https://proxy.example/models"),
    ("https://proxy.example/", "https://proxy.example/models"),
    ("https://proxy.example/v1", "https://proxy.example/v1/models"),
    ("https://proxy.example/v1/", "https://proxy.example/v1/models"),
])
def test_a_root_or_v1_base_maps_to_one_models_url(base, expected):
    assert models_url(base) == expected

    recorder = _Recorder()
    with _client(recorder) as client:
        check_llm_readiness(api_base=base, api_key=KEY, client=client)
    assert [str(r.url) for r in recorder.requests] == [expected]


def test_a_ready_endpoint_sends_exactly_one_authenticated_get_with_no_body():
    recorder = _Recorder(status=200)
    result = _check(recorder)

    assert result.state == READY and result.ready is True
    assert recorder.count == 1, "exactly one request"
    request = recorder.requests[0]
    assert request.method == "GET"
    assert request.content == b"", "a readiness check sends no body"
    header_matches = request.headers.get("authorization") == f"Bearer {KEY}"
    assert header_matches, "the authorization header did not carry the expected credential"
    assert sum(1 for k in request.headers if k.lower() == "authorization") == 1


@pytest.mark.parametrize("base", [
    "https://proxy.example/models",
    "https://proxy.example/v1/models",
    "https://proxy.example/responses",
    "ftp://proxy.example/v1",
    "https://user:pass@proxy.example/v1",
    "https://proxy.example/v1?x=1",
    "",
])
def test_an_unsafe_or_endpoint_shaped_base_is_refused_before_any_request(base):
    """A configured `.../models` must not become `.../models/models`."""
    recorder = _Recorder()
    with _client(recorder) as client:
        result = check_llm_readiness(api_base=base, api_key=KEY, client=client)
    assert result.state == UNSAFE_BASE
    assert recorder.count == 0


def test_the_default_timeout_is_no_weaker_than_the_bound_it_replaced():
    assert TIMEOUT_SECONDS <= 5.0


# --- credential precedence, exactly as the production model resolves it -----------------------------

@pytest.fixture(autouse=True)
def _clean_endpoint_environment(monkeypatch):
    for name in ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("env,expected", [
    ({"CKBBENCH_LLM_API_KEY": KEY, "BENCH_API_KEY": LEGACY_KEY}, KEY),
    ({"BENCH_API_KEY": LEGACY_KEY}, LEGACY_KEY),
    ({}, "sk-noauth"),
    # `_env()` selects the first name that is SET; an explicit empty value is set.
    ({"CKBBENCH_LLM_API_KEY": "", "BENCH_API_KEY": LEGACY_KEY}, ""),
    ({"BENCH_API_KEY": ""}, ""),
])
def test_the_credential_precedence_matches_config_env_semantics(env, expected, monkeypatch):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    recorder = _Recorder(status=200)
    with _client(recorder) as client:
        check_llm_readiness(api_base="https://proxy.example/v1", client=client)
    header_matches = recorder.requests[0].headers.get("authorization") == f"Bearer {expected}"
    assert header_matches, "readiness selected a different credential than config precedence"


def test_the_resolver_is_the_one_the_production_model_uses(monkeypatch):
    """Two resolvers could disagree about the same environment and certify the wrong endpoint."""
    from ckbbench.config import resolve_llm_api_key

    monkeypatch.setenv("BENCH_API_KEY", LEGACY_KEY)
    recorder = _Recorder(status=200)
    with _client(recorder) as client:
        check_llm_readiness(api_base="https://proxy.example/v1", client=client)
    header_matches = (recorder.requests[0].headers.get("authorization")
                      == f"Bearer {resolve_llm_api_key()}")
    assert header_matches, "readiness and the production resolver disagreed"


def test_a_development_no_auth_endpoint_still_passes_under_the_placeholder():
    recorder = _Recorder(status=200)
    with _client(recorder) as client:
        result = check_llm_readiness(api_base="http://localhost:18321/v1", client=client)
    assert result.ready is True
    uses_placeholder = recorder.requests[0].headers.get("authorization") == "Bearer sk-noauth"
    assert uses_placeholder, "the development placeholder was not used"


# --- classification -------------------------------------------------------------------------------

@pytest.mark.parametrize("status,state", [
    (200, READY), (204, READY), (299, READY),
    (401, AUTH_REJECTED), (403, AUTH_REJECTED),
    (301, HTTP_FAILURE), (302, HTTP_FAILURE), (307, HTTP_FAILURE), (308, HTTP_FAILURE),
    (400, HTTP_FAILURE), (404, HTTP_FAILURE), (429, HTTP_FAILURE),
    (500, HTTP_FAILURE), (502, HTTP_FAILURE), (503, HTTP_FAILURE),
])
def test_every_status_class_is_distinguished(status, state):
    recorder = _Recorder(status=status,
                         headers={"Location": "https://elsewhere.example/v1/models"},
                         body=BODY_CANARY.encode())
    result = _check(recorder)
    assert result.state == state
    assert result.status == status
    assert recorder.count == 1, "one attempt, never a retry"


@pytest.mark.parametrize("error", [
    httpx.ConnectError("connection refused"),
    httpx.ConnectTimeout("timed out"),
    httpx.ReadTimeout("timed out"),
    httpx.TooManyRedirects("too many"),
    httpx.RemoteProtocolError("bad protocol"),
    OSError("dns failure"),
])
def test_transport_failures_classify_as_unreachable_without_their_text(error):
    recorder = _Recorder(error=error)
    result = _check(recorder)
    assert result.state == UNREACHABLE
    assert result.status is None
    assert recorder.count == 1, "one attempt, never a retry"
    assert type(error).__name__ in result.line()
    assert str(error) not in result.line()


def test_a_redirect_is_refused_and_never_followed():
    recorder = _Recorder(status=302,
                         headers={"Location": "https://elsewhere.example/v1/models"})
    result = _check(recorder)
    assert result.state == HTTP_FAILURE and result.status == 302
    assert [str(r.url) for r in recorder.requests] == ["https://proxy.example/v1/models"]
    assert "elsewhere.example" not in result.line()


# --- nothing sensitive escapes ---------------------------------------------------------------------

def _rendered(result: Readiness) -> str:
    return result.line() + result.detail + result.state + repr(result)


@pytest.mark.parametrize("status,error", [
    (200, None), (401, None), (403, None), (302, None), (500, None),
    (None, httpx.ConnectError(f"failed talking to {BODY_CANARY}")),
])
def test_no_credential_body_or_transport_text_reaches_the_operator(status, error):
    recorder = _Recorder(status=status or 200, error=error,
                         headers={"Location": f"https://elsewhere.example/{BODY_CANARY}",
                                  "X-Request-Id": BODY_CANARY},
                         body=json.dumps({"error": BODY_CANARY}).encode())
    result = _check(recorder)
    rendered = _rendered(result)
    escaped = [name for name, value in (("credential", KEY), ("body", BODY_CANARY),
                                        ("scheme", "Bearer"), ("redirect", "elsewhere.example"),
                                        ("header", "X-Request-Id"))
               if value in rendered]
    assert not escaped, f"these escaped into the operator line: {escaped}"


def test_an_unsafe_base_diagnostic_carries_no_credential():
    recorder = _Recorder()
    with _client(recorder) as client:
        result = check_llm_readiness(api_base=f"https://user:{KEY}@proxy.example/v1",
                                     api_key=KEY, client=client)
    assert result.state == UNSAFE_BASE
    assert KEY not in _rendered(result)


def test_a_transport_failure_leaves_no_reachable_exception_text():
    """The operator prints this line; a raised chain would put the text in a traceback."""
    recorder = _Recorder(error=httpx.ConnectError(f"host {BODY_CANARY} refused"))
    try:
        result = _check(recorder)
    except Exception as exc:  # pragma: no cover - the check must not raise
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        pytest.fail(f"readiness raised instead of classifying: {type(exc).__name__} {rendered[:0]}")
    assert result.state == UNREACHABLE
    assert BODY_CANARY not in _rendered(result)


# --- the CLI the operator script calls -------------------------------------------------------------

def test_the_cli_takes_no_arguments_and_reads_its_configuration_from_the_environment(
    monkeypatch, capsys
):
    """Neither the endpoint nor the credential may be an argument: argv is world-readable."""
    import ckbbench.run.llm_readiness as mod

    monkeypatch.setenv("CKBBENCH_LLM_API_KEY", KEY)
    monkeypatch.setenv("CKBBENCH_LLM_API_BASE", "https://proxy.example/v1")
    seen = {}

    def fake(*, api_base=None, api_key=None, client=None, timeout=mod.TIMEOUT_SECONDS):
        seen["api_base_defaulted"] = api_base is None
        seen["api_key_defaulted"] = api_key is None
        return Readiness(READY, "ready", 200, "https://proxy.example/v1")

    monkeypatch.setattr(mod, "check_llm_readiness", fake)
    assert mod.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "https://proxy.example/v1 ready (HTTP 200)"
    leaked = KEY in captured.out + captured.err
    assert not leaked
    assert seen["api_base_defaulted"] and seen["api_key_defaulted"], (
        "the CLI must pass neither endpoint nor credential explicitly"
    )


@pytest.mark.parametrize("argument", ["--api-key", "--api-base", "-x", "anything"])
def test_the_cli_refuses_any_argument_without_echoing_it(argument, capsys):
    """argparse's default unknown-argument error prints the value straight to stderr."""
    canary = "sk-synthetic-argv-canary"
    assert main([argument, canary]) == 2
    captured = capsys.readouterr()
    echoed = canary in captured.out + captured.err
    assert not echoed, "the rejected argument was echoed back"
    assert "takes no arguments" in captured.err


@pytest.mark.parametrize("state,detail,status,rc", [
    (READY, "ready", 200, 0),
    (AUTH_REJECTED, "authentication rejected; check CKBBENCH_LLM_API_KEY", 401, 1),
    (HTTP_FAILURE, "endpoint returned an unusable status", 503, 1),
    (UNREACHABLE, "endpoint unreachable (ConnectError)", None, 1),
])
def test_the_cli_exit_code_follows_the_classification(state, detail, status, rc, monkeypatch,
                                                      capsys):
    import ckbbench.run.llm_readiness as mod

    monkeypatch.setattr(mod, "check_llm_readiness",
                        lambda **kw: Readiness(state, detail, status))
    assert mod.main([]) == rc
    assert detail in capsys.readouterr().out


# --- client construction and cleanup are inside the sanitizing boundary ----------------------------

def test_a_client_construction_failure_is_classified_not_raised(monkeypatch):
    """A bad SSL_CERT_FILE or proxy setting must not escape as a traceback carrying that path."""
    import ckbbench.run.llm_readiness as mod

    canary = "/nonexistent/synthetic-tls-canary.pem"

    def explode(timeout):
        raise OSError(f"could not load {canary}")

    monkeypatch.setattr(mod, "_default_client", explode)
    result = mod.check_llm_readiness(api_base="https://proxy.example/v1", api_key=KEY)
    assert result.state == UNREACHABLE
    assert "OSError" in result.line()
    leaked = canary in _rendered(result) or KEY in _rendered(result)
    assert not leaked, "client construction detail escaped into the operator line"


def test_a_client_close_failure_never_overrides_the_classified_result(monkeypatch):
    """Cleanup runs after the verdict; a failure there must not change or leak it."""
    import ckbbench.run.llm_readiness as mod

    class _HostileClose(httpx.Client):
        def close(self):
            raise RuntimeError("close failed with /synthetic-close-canary")

    recorder = _Recorder(status=200)
    monkeypatch.setattr(mod, "_default_client",
                        lambda timeout: _HostileClose(
                            transport=httpx.MockTransport(recorder.handler),
                            follow_redirects=False))
    result = mod.check_llm_readiness(api_base="https://proxy.example/v1", api_key=KEY)
    assert result.state == READY and result.status == 200
    assert "synthetic-close-canary" not in _rendered(result)


def test_the_cli_reports_a_construction_failure_with_one_safe_line(monkeypatch, capsys):
    """CLI level: nonzero, one fixed line, no traceback, no configuration detail."""
    import ckbbench.run.llm_readiness as mod

    canary = "/nonexistent/synthetic-proxy-canary"
    monkeypatch.setenv("CKBBENCH_LLM_API_BASE", "https://proxy.example/v1")
    monkeypatch.setattr(mod, "_default_client",
                        lambda timeout: (_ for _ in ()).throw(OSError(f"proxy {canary}")))
    assert mod.main([]) == 1
    captured = capsys.readouterr()
    assert "endpoint unreachable (OSError)" in captured.out
    assert "Traceback" not in captured.err
    leaked = canary in captured.out + captured.err
    assert not leaked, "configuration detail reached the operator"


def test_the_validated_endpoint_is_shown_not_the_configured_one(monkeypatch):
    """A configured base can carry userinfo; only a validated base may be displayed."""
    recorder = _Recorder(status=200)
    with _client(recorder) as client:
        safe = check_llm_readiness(api_base="https://proxy.example/v1/", api_key=KEY,
                                   client=client)
    assert safe.endpoint == "https://proxy.example/v1"
    assert safe.line() == "https://proxy.example/v1 ready (HTTP 200)"

    unsafe_canary = "sk-synthetic-userinfo-canary"
    with _client(recorder) as client:
        unsafe = check_llm_readiness(api_base=f"https://user:{unsafe_canary}@proxy.example/v1",
                                     api_key=KEY, client=client)
    assert unsafe.state == UNSAFE_BASE and unsafe.endpoint is None
    leaked = unsafe_canary in _rendered(unsafe)
    assert not leaked, "the configured base was echoed back"
