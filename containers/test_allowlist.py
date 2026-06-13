"""Unit tests for the per-arm egress allowlist builder (ADR-0006).

This is load-bearing security logic: a block-mode arm (A/D) must permit EXACTLY the chain RPC,
the proxy, and (on MCP arms) the MCP endpoint, and nothing else; an observe arm (B/C) gets the
permissive list. A test that could not fail if block mode silently became permissive, or if a
host were left unanchored (so a lookalike domain matched), would be worthless, so we assert the
exact emitted lines and the anchoring (Rule 9).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from build_allowlist import build_allowlist  # noqa: E402
from compose_builder import compose_env_for_arm  # noqa: E402


def _lines(text: str) -> list[str]:
    return [ln for ln in text.strip().splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_block_arm_A_permits_only_chain_and_proxy():
    out = build_allowlist(chain_rpc="http://ckbbench-devnet-node:8114", arm="A")
    lines = _lines(out)
    assert "^ckbbench-devnet-node$" in lines
    assert "^ckbbench-proxy$" in lines
    # A has no MCP, so no MCP line and nothing else permitted.
    assert all("mcp" not in ln.lower() for ln in lines)
    assert len(lines) == 2


def test_block_arm_D_permits_chain_proxy_and_mcp():
    out = build_allowlist(
        chain_rpc="http://ckbbench-devnet-node:8114",
        mcp_url="https://mcp.ckbdev.com/ckbai",
        arm="D",
    )
    lines = _lines(out)
    assert "^ckbbench-devnet-node$" in lines
    assert "^ckbbench-proxy$" in lines
    assert r"^mcp\.ckbdev\.com$" in lines  # host escaped + anchored
    assert len(lines) == 3


def test_observe_arms_B_and_C_are_permissive():
    for arm in ("B", "C"):
        out = build_allowlist(chain_rpc="http://unused", arm=arm)
        # the observe allowlist permits web (a catch-all); it must NOT be the 2-line block list.
        assert ".*" in out or "^.*$" in out, f"arm {arm} observe list should be permissive"


def test_block_arm_hosts_are_anchored_and_escaped():
    # A dotted host must be escaped so a regex dot cannot match a lookalike (e.g. mcpXckbdev.com).
    out = build_allowlist(
        chain_rpc="http://192.168.0.73:18114", mcp_url="https://mcp.ckbdev.com/ckbai", arm="D"
    )
    assert r"^192\.168\.0\.73$" in out  # IP dots escaped
    assert r"^mcp\.ckbdev\.com$" in out
    # anchored both ends so a substring host cannot match
    for ln in _lines(out):
        assert ln.startswith("^") and ln.endswith("$")


def test_block_mcp_arm_without_mcp_url_raises():
    with pytest.raises(ValueError, match="requires MCP URL"):
        build_allowlist(chain_rpc="http://node:8114", mcp_url=None, arm="D")


def test_unknown_arm_raises():
    with pytest.raises(ValueError, match="unknown arm"):
        build_allowlist(chain_rpc="http://node:8114", arm="Z")


def test_unparseable_chain_rpc_raises():
    with pytest.raises(ValueError, match="cannot parse host"):
        build_allowlist(chain_rpc="http://:badport", arm="A")


def test_compose_env_for_arm_devnet_uses_service_name(tmp_path, monkeypatch):
    # devnet RPC inside the docker net is the sidecar SERVICE NAME, not the host default.
    monkeypatch.chdir(tmp_path)
    env, allowlist_path = compose_env_for_arm(arm="A", chain="devnet")
    assert "CKBBENCH_ALLOWLIST_FILE=" in env
    content = allowlist_path.read_text()
    assert "^ckbbench-devnet-node$" in content
    allowlist_path.unlink()  # clean the generated artifact


def test_compose_env_for_arm_observe_arm_writes_permissive(tmp_path):
    env, allowlist_path = compose_env_for_arm(arm="C", chain="devnet")
    content = allowlist_path.read_text()
    assert ".*" in content
    allowlist_path.unlink()


def test_compose_env_for_arm_testnet_uses_rpc_url(tmp_path):
    # testnet (block arm) must permit the real TestNet RPC host, not the devnet service name.
    env, allowlist_path = compose_env_for_arm(arm="A", chain="testnet")
    content = allowlist_path.read_text()
    assert "ckbbench-devnet-node" not in content
    assert r"^192\.168\.0\.73$" in content  # the inventory TestNet host
    allowlist_path.unlink()


def test_build_allowlist_main_writes_file(tmp_path, monkeypatch, capsys):
    import build_allowlist as mod

    out = tmp_path / "al.built"
    monkeypatch.setattr(
        sys, "argv",
        ["build_allowlist.py", "--arm", "A", "--chain-rpc", "http://node:8114", "-o", str(out)],
    )
    mod.main()
    assert out.read_text().count("^node$") == 1
    assert str(out) in capsys.readouterr().out


def test_compose_builder_main_writes_env(tmp_path, monkeypatch, capsys):
    import compose_builder as mod

    env_out = tmp_path / ".env.arm"
    monkeypatch.setattr(
        sys, "argv",
        ["compose_builder.py", "--arm", "A", "--chain", "devnet", "-o", str(env_out)],
    )
    mod.main()
    assert "CKBBENCH_ALLOWLIST_FILE=" in env_out.read_text()
    captured = capsys.readouterr().out
    assert "allowlist:" in captured and "env:" in captured
    # clean the generated allowlist artifact the builder wrote next to the package
    for p in (_HERE / "proxy").glob("allowlist.A.devnet.built"):
        p.unlink()
