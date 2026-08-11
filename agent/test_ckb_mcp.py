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
    def __init__(self, body: bytes, *, status: int = 200):
        self.content = body
        self.status_code = status

    @property
    def text(self) -> str:  # only a mis-decode trap; production must not use it
        return self.content.decode("iso-8859-1")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.posts: list[dict] = []

    def post(self, url, *, headers, data, timeout):
        self.posts.append({"url": url, "headers": headers, "data": json.loads(data), "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _client(response) -> CkbMcpClient:
    client = CkbMcpClient(url="https://example.invalid/mcp")
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
        ({"jsonrpc": "2.0", "id": 1, "result": None}, "result is NoneType"),
        ({"jsonrpc": "2.0", "id": 1, "result": [1, 2]}, "result is list"),
        ({"jsonrpc": "2.0", "id": 1, "result": "text"}, "result is str"),
    ],
    ids=["no-result-or-error", "both", "no-version", "wrong-version", "wrong-id",
         "null-result", "list-result", "str-result"],
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
    with pytest.raises(RuntimeError, match="HTTP 500"):
        _client(FakeResponse(_envelope({}), status=500))._rpc("initialize", {})


def test_read_resource_uses_the_exact_method_and_params():
    client = _client(FakeResponse(_envelope({"contents": [{"text": "body"}]})))
    client.read_resource("ckb://docs/reference/token-script-hashes")
    sent = client._session.posts[-1]["data"]
    assert sent["method"] == "resources/read"
    assert sent["params"] == {"uri": "ckb://docs/reference/token-script-hashes"}


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
