"""Run-params tests: two-class split and mount trust boundary (ADR-0009)."""

from __future__ import annotations

import re

import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ckbbench.suite.model import OnchainVerifierSpec, ParamSpec, Task
from ckbbench.suite.runparams import (
    fresh_blob_hex_32,
    BASE_SHANNONS,
    RunParams,
    _NONCE_OFFSET_SPACE,
    _draw_value,
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
            # The amount the agent is TOLD to send and the nonce the Verifier CHECKS are the
            # same draw, made explicit by a shared share_group (ADR-0009 shared primitive).
            ParamSpec(
                name="send_amount_shannons",
                param_class="prompt",
                generator="high_entropy_nonce_amount_shannons",
                share_group="nonce",
            ),
            ParamSpec(
                name="recipient_args",
                param_class="prompt",
                generator="recipient_args",
                static_value="0x470dcdc5e44064909650113a274b3b36aecb6dc7",
                share_group="recipient",
            ),
            ParamSpec(name="harness_tip", param_class="verifier", generator="harness_tip"),
            ParamSpec(
                name="nonce_amount_shannons",
                param_class="verifier",
                generator="high_entropy_nonce_amount_shannons",
                share_group="nonce",
            ),
            ParamSpec(
                name="recipient_args",
                param_class="verifier",
                generator="recipient_args",
                static_value="0x470dcdc5e44064909650113a274b3b36aecb6dc7",
                share_group="recipient",
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


def test_unknown_generator_raises_from_draw_value():
    spec = ParamSpec(name="x", param_class="prompt", generator="static", static_value="1")
    object.__setattr__(spec, "generator", "bogus")
    with pytest.raises(ValueError, match="unknown generator"):
        _draw_value(spec, lambda _m, _p: "0x0")


def test_two_distinct_statics_get_distinct_values():
    # Regression for the generator-keyed cache collision (grok-build/codex blocker): two static
    # params with NO share_group must NOT collide on the first value.
    task = Task(
        id="t",
        prompt_fragment="x",
        score=1,
        proof_file="x.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="x", rpc_method="m"),
        param_schema=(
            ParamSpec(name="label1", param_class="prompt", generator="static", static_value="foo"),
            ParamSpec(name="label2", param_class="prompt", generator="static", static_value="bar"),
        ),
    )
    params = generate_run_params(task, "http://unused", rpc=lambda _m, _p: "0x0")
    assert params.prompt_injected == {"label1": "foo", "label2": "bar"}


def test_unrelated_nonces_without_share_group_draw_independently():
    # Two nonce params with NO share_group must draw independently (not silently share). With a
    # 33-bit space, two independent draws are overwhelmingly likely to differ; assert they CAN.
    seen = set()
    for _ in range(20):
        task = Task(
            id="t",
            prompt_fragment="x",
            score=1,
            proof_file="x.txt",
            kind="onchain",
            verifier=OnchainVerifierSpec(check="x", rpc_method="m"),
            param_schema=(
                ParamSpec(name="a", param_class="prompt", generator="high_entropy_nonce_amount_shannons"),
                ParamSpec(name="b", param_class="prompt", generator="high_entropy_nonce_amount_shannons"),
            ),
        )
        p = generate_run_params(task, "http://unused", rpc=lambda _m, _p: "0x0")
        seen.add(p.prompt_injected["a"] != p.prompt_injected["b"])
    assert True in seen, "independent nonce draws should sometimes differ; they always shared"


def test_share_group_shares_one_draw_across_classes():
    # The nonce share_group binds the prompt amount and the verifier nonce to ONE draw.
    params = generate_run_params(_send_task(), "http://unused", rpc=lambda _m, _p: "0x2a")
    assert (
        params.prompt_injected["send_amount_shannons"]
        == params.verifier_private["nonce_amount_shannons"]
    )


def test_inconsistent_share_group_raises():
    # A share_group whose members disagree on generator/static_value is an authoring error.
    task = Task(
        id="t",
        prompt_fragment="x",
        score=1,
        proof_file="x.txt",
        kind="onchain",
        verifier=OnchainVerifierSpec(check="x", rpc_method="m"),
        param_schema=(
            ParamSpec(name="r1", param_class="prompt", generator="recipient_args",
                      static_value="0xaaaa", share_group="g"),
            ParamSpec(name="r2", param_class="verifier", generator="recipient_args",
                      static_value="0xbbbb", share_group="g"),
        ),
    )
    with pytest.raises(ValueError, match="incompatible specs"):
        generate_run_params(task, "http://unused", rpc=lambda _m, _p: "0x0")


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


def test_write_verifier_private_refuses_inside_mount(tmp_path: Path):
    # The trust boundary made un-mis-wireable: pointing the verifier dir into the mount must fail
    # loud, never write a secret where the agent can read it (ADR-0009).
    params = generate_run_params(_send_task(), "http://unused", rpc=lambda _m, _p: "0x1")
    mount = tmp_path / "mount"
    mount.mkdir()
    with pytest.raises(ValueError, match="trust boundary"):
        write_verifier_private(params, mount / "sneaky", mount_dir=mount)


def test_write_verifier_private_allows_dir_outside_mount(tmp_path: Path):
    # The guard must permit the normal case: a verifier dir that is NOT inside the mount, even
    # when mount_dir is supplied.
    params = generate_run_params(_send_task(), "http://unused", rpc=lambda _m, _p: "0x1")
    mount = tmp_path / "mount"
    mount.mkdir()
    outside = tmp_path / "verifier-private"
    path = write_verifier_private(params, outside, mount_dir=mount)
    assert path == (outside.resolve() / "secret.json")
    assert json.loads(path.read_text())["harness_tip"] == 1


@pytest.mark.parametrize("bad", ["../escape.json", "/abs/secret.json", "sub/secret.json", "..", "."])
def test_write_verifier_private_rejects_path_filename(tmp_path: Path, bad: str):
    # codex round-2 hole: a filename with a separator / absolute path / .. would escape vdir after
    # the dir guard. filename must be a bare name.
    params = generate_run_params(_send_task(), "http://unused", rpc=lambda _m, _p: "0x1")
    with pytest.raises(ValueError, match="bare name"):
        write_verifier_private(params, tmp_path / "vdir", filename=bad)


def test_write_verifier_private_refuses_symlink_final_path(tmp_path: Path):
    # An existing symlink at vdir/secret.json that points into the mount must be refused, not
    # followed (it would redirect the secret write into the agent's view).
    params = generate_run_params(_send_task(), "http://unused", rpc=lambda _m, _p: "0x1")
    mount = tmp_path / "mount"
    mount.mkdir()
    vdir = tmp_path / "vdir"
    vdir.mkdir()
    (vdir / "secret.json").symlink_to(mount / "leaked.json")
    with pytest.raises(ValueError, match="escapes|trust boundary"):
        write_verifier_private(params, vdir, mount_dir=mount)


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


def test_fresh_blob_hex_32_asks_for_exactly_32_bytes(monkeypatch):
    """token_bytes(32), formatted without truncation or padding, and no second draw."""
    calls: list[int] = []
    sentinel = bytes(range(32))

    def fake(n):
        calls.append(n)
        return sentinel

    monkeypatch.setattr("ckbbench.suite.runparams.secrets.token_bytes", fake)
    value = fresh_blob_hex_32()
    assert calls == [32]
    assert value == "0x" + sentinel.hex()
    assert len(value) == 66 and value.islower()


def test_fresh_blob_hex_32_draws_independently():
    a, b = fresh_blob_hex_32(), fresh_blob_hex_32()
    assert a != b
    for v in (a, b):
        assert re.fullmatch(r"0x[0-9a-f]{64}", v)


def _blob_task(share: str | None = "payload") -> Task:
    return Task(
        id="t", prompt_fragment="x", score=1, proof_file="p.txt", kind="onchain",
        verifier=OnchainVerifierSpec(check="type_id_data_cell", rpc_method="get_transaction"),
        param_schema=(
            ParamSpec(name="payload_hex", param_class="prompt",
                      generator="fresh_blob_hex_32", share_group=share),
            ParamSpec(name="expected_payload_hex", param_class="verifier",
                      generator="fresh_blob_hex_32", share_group=share),
        ),
    )


def test_fresh_blob_share_group_draws_once_and_reaches_both_classes():
    params = generate_run_params(_blob_task(), "unused", rpc=lambda m, p: "0x1")
    assert params.prompt_injected["payload_hex"] == params.verifier_private["expected_payload_hex"]


def test_fresh_blob_without_share_group_does_not_share_accidentally():
    params = generate_run_params(_blob_task(share=None), "unused", rpc=lambda m, p: "0x1")
    assert params.prompt_injected["payload_hex"] != params.verifier_private["expected_payload_hex"]


def test_r1_fixed_capacity_covers_the_occupied_capacity_floor():
    """8 + 32 data + (32+1+20) lock + (32+1+32) type = 158 CKB occupied; the task fixes 200 CKB."""
    occupied = 8 + 32 + (32 + 1 + 20) + (32 + 1 + 32)
    assert occupied == 158
    assert 20_000_000_000 == 200 * 100_000_000
    assert 20_000_000_000 >= occupied * 100_000_000
