"""RPC client tests: direct CKB JSON-RPC with injectable seam."""

from __future__ import annotations

import json
import urllib.error

import pytest

from ckbbench.verify.rpc import DEFAULT_RPC_TIMEOUT, make_rpc_client


def test_rpc_client_success(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"result": "0x2a"}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    result = make_rpc_client("http://node:8114")("get_tip_block_number", [])
    assert result == "0x2a"


def test_rpc_client_passes_timeout(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"result": "0x1"}).encode()

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    make_rpc_client("http://x", timeout=7.5)("get_tip_block_number", [])
    assert captured["timeout"] == 7.5
    assert DEFAULT_RPC_TIMEOUT == 30.0


def test_rpc_client_url_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    client = make_rpc_client("http://bad")
    with pytest.raises(RuntimeError, match="connection refused"):
        client("get_tip_block_number", [])


def test_rpc_client_json_rpc_error(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"error": {"code": -1, "message": "nope"}}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    client = make_rpc_client("http://node")
    with pytest.raises(RuntimeError, match="nope"):
        client("get_transaction", ["0xabc"])