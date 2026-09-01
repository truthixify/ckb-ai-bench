"""Client-boundary tests for the native MCP client (no network).

The parser is the sensitive part: `text/event-stream` carries no charset, so a body decoded by
requests' fallback would mis-read multi-byte characters, and `str.splitlines()` would then treat a
decoded payload character as a frame boundary and truncate valid JSON.
"""

from __future__ import annotations

import json

import pytest

from ckb_mcp import CkbMcpClient, McpError


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
        chunks: list[bytes] | None = None,
    ):
        self.content = body
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.closed = False
        self.chunks = [body] if chunks is None else chunks

    @property
    def text(self) -> str:  # only a mis-decode trap; production must not use it
        return self.content.decode("iso-8859-1")

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.posts: list[dict] = []

    def post(self, url, *, headers, data, timeout, allow_redirects, stream):
        self.posts.append({
            "url": url,
            "headers": headers,
            "data": json.loads(data),
            "timeout": timeout,
            "allow_redirects": allow_redirects,
            "stream": stream,
        })
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _client(response, **kwargs) -> CkbMcpClient:
    client = CkbMcpClient(url="https://example.invalid/mcp", **kwargs)
    object.__setattr__(client, "_session", FakeSession(response))
    return client


def _envelope(result: dict) -> bytes:
    # ensure_ascii=False so a check mark reaches the wire as real UTF-8 bytes, as the server sends
    # it -- an escaped \u2705 would not reproduce the decoding defect.
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}, ensure_ascii=False).encode()


def test_plain_json_response_parses():
    client = _client(FakeResponse(_envelope({"ok": True})))
    assert client._rpc("initialize", {}) == {"ok": True}


def test_single_line_sse_response_parses():
    client = _client(FakeResponse(b"data: " + _envelope({"ok": True})))
    assert client._rpc("initialize", {}) == {"ok": True}


@pytest.mark.parametrize("framing", [b"\n", b"\r\n", b"\r"])
def test_sse_framing_variants_parse(framing):
    body = b"event: message" + framing + b"data: " + _envelope({"ok": True}) + framing
    assert _client(FakeResponse(body))._rpc("initialize", {}) == {"ok": True}


def test_utf8_payload_is_not_truncated():
    """A check mark in the payload must survive intact."""
    text = "sUDT ✅ 0x5e7a36a7 ✅ done"
    body = b"data: " + _envelope({"contents": [{"text": text}]})
    result = _client(FakeResponse(body))._rpc("resources/read", {"uri": "ckb://x"})
    assert result["contents"][0]["text"] == text


def test_iso_8859_1_u0085_regression_cannot_truncate():
    """The exact historical defect: UTF-8 U+2705 decoded as ISO-8859-1 ends in U+0085, which
    `str.splitlines()` treats as a line break. Byte-level UTF-8 decoding plus CR/LF-only framing
    must keep the envelope whole."""
    text = "before ✅ after"
    body = b"data: " + _envelope({"contents": [{"text": text}]})

    mis_decoded = body.decode("iso-8859-1")
    assert "" in mis_decoded
    assert len(mis_decoded.splitlines()) > 1, "fixture must reproduce the historical split"

    result = _client(FakeResponse(body))._rpc("resources/read", {"uri": "ckb://x"})
    assert result["contents"][0]["text"] == text


def test_invalid_utf8_is_an_mcp_error():
    with pytest.raises(McpError, match="not valid UTF-8"):
        _client(FakeResponse(b"data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":\xff}"))._rpc("x", {})


@pytest.mark.parametrize(
    "body",
    [b"data: {not json}", b"{not json}", b"", b"data: [1,2,3]", b"[1,2,3]"],
    ids=["sse-malformed", "json-malformed", "empty", "sse-not-object", "json-not-object"],
)
def test_malformed_envelopes_fail_without_echoing_the_body(body):
    secret = b"SENSITIVE-BODY-MARKER"
    with pytest.raises(McpError) as excinfo:
        _client(FakeResponse(body + secret))._rpc("x", {})
    assert secret.decode() not in str(excinfo.value)


@pytest.mark.parametrize(
    ("envelope", "match"),
    [
        ({"jsonrpc": "2.0", "id": 1}, "exactly one of result or error"),
        ({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 1}}, "exactly one"),
        ({"id": 1, "result": {}}, "not JSON-RPC 2.0"),
        ({"jsonrpc": "1.0", "id": 1, "result": {}}, "not JSON-RPC 2.0"),
        ({"jsonrpc": "2.0", "id": 99, "result": {}}, "id does not match"),
        ({"jsonrpc": "2.0", "id": 1, "result": {}, "extra": True}, "unexpected fields"),
        ({"jsonrpc": "2.0", "id": 1, "result": None}, "result is NoneType"),
        ({"jsonrpc": "2.0", "id": 1, "result": [1, 2]}, "result is list"),
        ({"jsonrpc": "2.0", "id": 1, "result": "text"}, "result is str"),
    ],
    ids=["no-result-or-error", "both", "no-version", "wrong-version", "wrong-id",
         "extra-field", "null-result", "list-result", "str-result"],
)
def test_invalid_jsonrpc_envelopes_are_rejected(envelope, match):
    with pytest.raises(McpError, match=match):
        _client(FakeResponse(json.dumps(envelope).encode()))._rpc("tools/list", {})


def test_jsonrpc_error_rendering_is_bounded_and_omits_data():
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32602, "message": "m" * 5000, "data": "SENSITIVE-DATA-MARKER"},
    }).encode()
    with pytest.raises(McpError) as excinfo:
        _client(FakeResponse(body))._rpc("tools/call", {})
    rendered = str(excinfo.value)
    assert "SENSITIVE-DATA-MARKER" not in rendered
    assert len(rendered) < 500


def test_jsonrpc_error_is_an_mcp_error():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "nope"}}).encode()
    with pytest.raises(McpError, match="-32602"):
        _client(FakeResponse(body))._rpc("tools/call", {})


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, None), ([], None), ("text", None), (42, None),
        ({"contents": [{"text": "   "}]}, None),
        ({"contents": [{"text": ""}]}, None),
        ({"contents": [{"text": "  "}, {"text": "real"}]}, "real"),
    ],
    ids=["none", "list", "str", "int", "whitespace", "empty", "mixed-usable"],
)
def test_resource_text_is_defensive_about_shape(result, expected):
    assert CkbMcpClient.resource_text(result) == expected


def test_http_status_error_surfaces():
    with pytest.raises(McpError, match="unusable status"):
        _client(FakeResponse(_envelope({}), status=500))._rpc("initialize", {})


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_refused_without_following_the_target(status):
    response = FakeResponse(b"", status=status)
    client = _client(response)
    with pytest.raises(McpError, match="redirect"):
        client._rpc("initialize", {})
    assert client._session.posts[0]["allow_redirects"] is False
    assert client._session.posts[0]["stream"] is True
    assert response.closed


def test_request_ceiling_stops_before_a_second_transport_call():
    client = _client(FakeResponse(_envelope({})), request_limit=1)
    assert client._rpc("initialize", {}) == {}
    with pytest.raises(McpError, match="ceiling"):
        client._rpc("tools/list", {})
    assert client.request_count == 1
    assert len(client._session.posts) == 1


def test_response_body_is_streamed_and_bounded():
    response = FakeResponse(b"", chunks=[b"1234", b"56789"])
    client = _client(response, max_response_bytes=8)
    with pytest.raises(McpError, match="byte limit"):
        client._rpc("initialize", {})
    assert response.closed


def test_content_type_is_allowlisted_before_parsing():
    response = FakeResponse(_envelope({}), content_type="text/html")
    with pytest.raises(McpError, match="content type"):
        _client(response)._rpc("initialize", {})
    assert response.closed


def test_transport_failure_is_counted_once_and_sanitized():
    secret = "SENSITIVE-TRANSPORT-CONTENT"
    client = _client(RuntimeError(secret), request_limit=2)
    with pytest.raises(McpError) as excinfo:
        client._rpc("initialize", {})
    assert client.request_count == 1
    assert len(client._session.posts) == 1
    assert secret not in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.invalid/mcp",
        "https://user:password@example.invalid/mcp",
        "https://example.invalid/mcp?credential=value",
        "https://example.invalid/mcp#fragment",
        "not-a-url",
    ],
)
def test_endpoint_configuration_rejects_unsafe_urls(url):
    with pytest.raises(ValueError, match="unusable"):
        CkbMcpClient(url=url)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("Tools/List", {}),
        ("tools/list", []),
        ("tools/list", {"value": float("nan")}),
        ("tools/list", {"value": "x" * ((1 << 20) + 1)}),
    ],
)
def test_invalid_requests_are_refused_before_the_transport(method, params):
    client = _client(FakeResponse(_envelope({})))
    with pytest.raises(McpError):
        client._rpc(method, params)
    assert client.request_count == 0
    assert client._session.posts == []


def test_read_resource_uses_the_exact_method_and_params():
    client = _client(FakeResponse(_envelope({"contents": [{"text": "body"}]})))
    client.read_resource("ckb://docs/reference/token-script-hashes")
    sent = client._session.posts[-1]["data"]
    assert sent["method"] == "resources/read"
    assert sent["params"] == {"uri": "ckb://docs/reference/token-script-hashes"}


def test_list_resources_uses_the_exact_method():
    client = _client(FakeResponse(_envelope({"resources": [{"uri": "ckb://docs/reference"}]})))
    assert client.list_resources() == [{"uri": "ckb://docs/reference"}]
    sent = client._session.posts[-1]["data"]
    assert sent["method"] == "resources/list"
    assert sent["params"] == {}


@pytest.mark.parametrize(
    ("method", "result"),
    [
        ("list_tools", {}),
        ("list_tools", {"tools": {}, "nextCursor": "page-2"}),
        ("list_tools", {"tools": [], "nextCursor": "page-2"}),
        ("list_resources", {}),
        ("list_resources", {"resources": {}, "nextCursor": "page-2"}),
        ("list_resources", {"resources": [], "nextCursor": "page-2"}),
    ],
)
def test_catalog_methods_refuse_missing_malformed_or_paginated_results(method, result):
    client = _client(FakeResponse(_envelope(result)))
    with pytest.raises(McpError, match="complete catalog"):
        getattr(client, method)()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"contents": [{"text": "one"}]}, "one"),
        ({"contents": [{"text": "one"}, {"text": "two"}]}, "one\ntwo"),
        ({"contents": [{"text": "a"}, {"blob": "x"}, {"text": "b"}]}, "a\nb"),
        ({"contents": []}, None),
        ({"contents": [{"blob": "x"}]}, None),
        ({"contents": "not-a-list"}, None),
        ({}, None),
    ],
    ids=["one", "ordered", "mixed", "empty", "no-text", "not-a-list", "absent"],
)
def test_resource_text_joins_only_usable_text(result, expected):
    assert CkbMcpClient.resource_text(result) == expected


def test_result_text_still_reads_tool_content():
    assert CkbMcpClient.result_text({"content": [{"type": "text", "text": "hi"}]}) == "hi"


@pytest.mark.parametrize(
    "result",
    [{}, {"content": {}}, {"content": [None]}, {"content": [{"type": "text"}]}],
)
def test_result_text_refuses_malformed_tool_content(result):
    with pytest.raises(McpError, match="malformed"):
        CkbMcpClient.result_text(result)


@pytest.mark.parametrize(
    "bad_id", [True, False, "1", 1.0, None, [1]],
    ids=["true", "false", "string", "float", "null", "list"],
)
def test_non_integer_response_ids_are_rejected(bad_id):
    """Python treats True == 1; JSON-RPC does not allow a boolean id."""
    body = json.dumps({"jsonrpc": "2.0", "id": bad_id, "result": {}}).encode()
    with pytest.raises(McpError, match="id does not match"):
        _client(FakeResponse(body))._rpc("tools/list", {})


@pytest.mark.parametrize(
    "code", ["c" * 5000, {"nested": "object"}, [1, 2], None, True],
    ids=["huge-string", "object", "list", "null", "bool"],
)
def test_invalid_error_codes_are_not_echoed(code):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "m"}}).encode()
    with pytest.raises(McpError) as excinfo:
        _client(FakeResponse(body))._rpc("tools/call", {})
    rendered = str(excinfo.value)
    assert len(rendered) < 300
    assert "(invalid)" in rendered


def test_valid_integer_error_code_is_preserved():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "m"}}).encode()
    with pytest.raises(McpError, match="-32602"):
        _client(FakeResponse(body))._rpc("tools/call", {})
