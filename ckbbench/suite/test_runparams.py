"""Run-params tests: two-class split and mount trust boundary (ADR-0009)."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ckbbench.suite.model import OnchainVerifierSpec, ParamSpec, Task
from ckbbench.suite.runparams import (
    BASE_SHANNONS,
    RunParams,
    _NONCE_OFFSET_SPACE,
    _generate_value,
    generate_run_params,
    high_entropy_nonce_amount_shannons,
    make_rpc_client,
    write_prompt_injected,
    write_verifier_private,
)


def _send_task() -> Task:
    return Task(
        id="send-tx",
        prompt_fragment="Send CKB.",
        score=15,
        proof_file="tx_id.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="tx", rpc_method="get_transaction"),
        param_schema=(
            ParamSpec(
                name="send_amount_shannons",
                param_class="prompt",
                generator="high_entropy_nonce_amount_shannons",
            ),
            ParamSpec(
                name="recipient_args",
                param_class="prompt",
                generator="recipient_args",
                static_value="0x470dcdc5e44064909650113a274b3b36aecb6dc7",
            ),
            ParamSpec(name="harness_tip", param_class="verifier", generator="harness_tip"),
            ParamSpec(
                name="nonce_amount_shannons",
                param_class="verifier",
                generator="high_entropy_nonce_amount_shannons",
            ),
            ParamSpec(
                name="recipient_args",
                param_class="verifier",
                generator="recipient_args",
                static_value="0x470dcdc5e44064909650113a274b3b36aecb6dc7",
            ),
        ),
    )


def test_prompt_injected_and_verifier_private_are_disjoint():
    def mock_rpc(method: str, params: list) -> object:
        assert method == "get_tip_block_number"
        return "0x2a"

    params = generate_run_params(_send_task(), "http://unused", rpc=mock_rpc)
    assert set(params.prompt_injected) == {"send_amount_shannons", "recipient_args"}
    assert set(params.verifier_private) == {
        "harness_tip",
        "nonce_amount_shannons",
        "recipient_args",
    }
    assert params.prompt_injected["send_amount_shannons"] == params.verifier_private["nonce_amount_shannons"]
    assert params.verifier_private["harness_tip"] == 42


def test_verifier_private_never_written_to_mount(tmp_path: Path):
    def mock_rpc(_method: str, _params: list) -> object:
        return "0x10"

    params = generate_run_params(_send_task(), "http://unused", rpc=mock_rpc)
    mount = tmp_path / "mount"
    verifier = tmp_path / "verifier-private"
    write_prompt_injected(params, mount)
    write_verifier_private(params, verifier)

    mount_text = "\n".join(p.read_text() for p in mount.rglob("*") if p.is_file())
    assert "harness_tip" not in mount_text
    assert "nonce_amount_shannons" not in mount_text
    secret = json.loads((verifier / "secret.json").read_text())
    assert secret["harness_tip"] == 16
    assert "nonce_amount_shannons" in secret


def test_nonce_has_high_entropy_space_and_draws_differ():
    values = {high_entropy_nonce_amount_shannons() for _ in range(32)}
    assert len(values) > 1
    for v in values:
        n = int(v)
        assert n >= BASE_SHANNONS
        assert n < BASE_SHANNONS + _NONCE_OFFSET_SPACE


def test_harness_tip_uses_injected_rpc_not_network():
    seen: list[str] = []

    def mock_rpc(method: str, _params: list) -> object:
        seen.append(method)
        return "0x64"

    task = Task(
        id="tip",
        prompt_fragment="tip",
        score=1,
        proof_file="tip.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="tip", rpc_method="get_tip_block_number"),
        param_schema=(ParamSpec(name="harness_tip", param_class="verifier", generator="harness_tip"),),
    )
    params = generate_run_params(task, "http://must-not-be-called", rpc=mock_rpc)
    assert seen == ["get_tip_block_number"]
    assert params.verifier_private["harness_tip"] == 100


def test_shared_recipient_args_appear_in_both_classes_with_same_value():
    def mock_rpc(_method: str, _params: list) -> object:
        return "0x1"

    params = generate_run_params(_send_task(), "http://unused", rpc=mock_rpc)
    assert params.prompt_injected["recipient_args"] == params.verifier_private["recipient_args"]


def test_verifier_only_secrets_not_in_prompt_injected():
    def mock_rpc(_method: str, _params: list) -> object:
        return "0x1"

    params = generate_run_params(_send_task(), "http://unused", rpc=mock_rpc)
    assert "harness_tip" not in params.prompt_injected
    assert "nonce_amount_shannons" not in params.prompt_injected


def test_static_generator_missing_value_raises():
    task = Task(
        id="t",
        prompt_fragment="x",
        score=1,
        proof_file="x.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="x", rpc_method="m"),
        param_schema=(ParamSpec(name="x", param_class="prompt", generator="static"),),
    )
    with pytest.raises(ValueError, match="static_value"):
        generate_run_params(task, "http://unused", rpc=lambda _m, _p: "0x0")


def test_recipient_args_without_static_raises():
    task = Task(
        id="t",
        prompt_fragment="x",
        score=1,
        proof_file="x.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="x", rpc_method="m"),
        param_schema=(
            ParamSpec(name="r", param_class="prompt", generator="recipient_args"),
        ),
    )
    with pytest.raises(ValueError, match="requires static_value"):
        generate_run_params(task, "http://unused", rpc=lambda _m, _p: "0x0")


def test_make_rpc_client_success_and_errors():
    payload_ok = json.dumps({"result": "0x7"}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = payload_ok
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        client = make_rpc_client("http://127.0.0.1:8114")
        assert client("get_tip_block_number", []) == "0x7"

    err_payload = json.dumps({"error": {"code": -1, "message": "boom"}}).encode()
    mock_err = MagicMock()
    mock_err.read.return_value = err_payload
    mock_err.__enter__.return_value = mock_err
    with patch("urllib.request.urlopen", return_value=mock_err):
        with pytest.raises(RuntimeError, match="RPC get_tip_block_number error"):
            make_rpc_client("http://127.0.0.1:8114")("get_tip_block_number", [])

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        with pytest.raises(RuntimeError, match="failed"):
            make_rpc_client("http://127.0.0.1:8114")("get_tip_block_number", [])


def test_static_param_generates_value():
    task = Task(
        id="t",
        prompt_fragment="x",
        score=1,
        proof_file="x.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="x", rpc_method="m"),
        param_schema=(
            ParamSpec(name="label", param_class="prompt", generator="static", static_value="hello"),
        ),
    )
    params = generate_run_params(task, "http://unused", rpc=lambda _m, _p: "0x0")
    assert params.prompt_injected["label"] == "hello"


def test_generate_run_params_defaults_to_rpc_client():
    task = Task(
        id="t",
        prompt_fragment="x",
        score=1,
        proof_file="x.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="x", rpc_method="m"),
        param_schema=(
            ParamSpec(name="harness_tip", param_class="verifier", generator="harness_tip"),
        ),
    )
    fake_client = lambda _m, _p: "0x5"
    with patch("ckbbench.suite.runparams.make_rpc_client", return_value=fake_client):
        params = generate_run_params(task, "http://127.0.0.1:8114")
    assert params.verifier_private["harness_tip"] == 5


def test_unknown_generator_raises_from_generate_value():
    spec = ParamSpec(name="x", param_class="prompt", generator="static", static_value="1")
    object.__setattr__(spec, "generator", "bogus")
    with pytest.raises(ValueError, match="unknown generator"):
        _generate_value(spec, {}, lambda _m, _p: "0x0")


def test_empty_param_schema_yields_empty_dicts():
    task = Task(
        id="none",
        prompt_fragment="x",
        score=1,
        proof_file="x.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="x", rpc_method="m"),
        param_schema=(),
    )
    params = generate_run_params(task, "http://unused", rpc=lambda _m, _p: "0x0")
    assert params == RunParams(prompt_injected={}, verifier_private={})