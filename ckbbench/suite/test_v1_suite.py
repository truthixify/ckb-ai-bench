"""Loader and freeze tests for the v1 seed suite registry (Phase 6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ckbbench.suite.freeze import freeze, write_freeze
from ckbbench.suite.model import OnchainVerifierSpec
from ckbbench.suite.registry import load_suite
from ckbbench.verify.onchain import _ONCHAIN_CHECKS

V1_SUITE_ROOT = Path(__file__).resolve().parents[2] / "suites" / "ckb-v1"

REAL_TASK_IDS = (
    "task-01-tip",
    "task-02-epoch",
    "task-03-blockhash",
    "task-04-send-tx",
    "task-05-hashlock",
    "task-06-xudt-script",
    "task-07-spore-script",
)

SIMPLE_UDT_CODE_HASH = (
    "0xc35396b3053610327a1d7638567a6e7e04d5e7f378e7f189c3e550e8c3bee42"
)
SPORE_LOCK_CODE_HASH = (
    "0x9c23a6097b2c27e5cb47d1dade5ebb5acaa8a4233a204b6eeaa741eb6de49e0a"
)

ONCHAIN_EXPECTED: dict[str, dict[str, object]] = {
    "task-01-tip": {
        "check": "tip_hex",
        "rpc_method": "get_tip_block_number",
        "rpc_params": (),
    },
    "task-02-epoch": {
        "check": "epoch_number",
        "rpc_method": "get_current_epoch",
        "rpc_params": (),
    },
    "task-03-blockhash": {
        "check": "block_hash",
        "rpc_method": "get_block_hash",
        "rpc_params": (1,),
    },
    "task-04-send-tx": {
        "check": "tx_proof",
        "rpc_method": "get_transaction",
        "rpc_params": (),
    },
    "task-06-xudt-script": {
        "check": "constant_hex",
        "rpc_method": "constant",
        "rpc_params": (SIMPLE_UDT_CODE_HASH,),
    },
    "task-07-spore-script": {
        "check": "constant_hex",
        "rpc_method": "constant",
        "rpc_params": (SPORE_LOCK_CODE_HASH,),
    },
}


@pytest.fixture(scope="module")
def v1_suite():
    return load_suite(V1_SUITE_ROOT)


def test_v1_suite_loads_and_manifest_pins(v1_suite):
    assert v1_suite.suite_semver == "1.0.0"
    assert v1_suite.chain_profile == "devnet"
    assert v1_suite.mcp_server_version == "1.6.12"
    digest = v1_suite.pins.docker_image_digest
    assert digest and (
        digest.startswith("TO_BE_FILLED:") or digest.startswith("sha256:")
    )
    assert v1_suite.pins.toolchain_versions == {
        "nodejs": "22.14.0",
        "python": "3.12.8",
        "rust": "1.95.0",
    }


def test_v1_task_list_order_and_uniqueness(v1_suite):
    ids = [t.id for t in v1_suite.tasks]
    assert ids == list(REAL_TASK_IDS)
    assert len(ids) == len(set(ids))


def test_v1_all_scores_positive(v1_suite):
    for task in v1_suite.tasks:
        assert task.score > 0


def test_v1_onchain_checks_match_verifier(v1_suite):
    for task in v1_suite.tasks:
        if task.kind != "onchain":
            continue
        assert isinstance(task.verifier, OnchainVerifierSpec)
        if task.id in ONCHAIN_EXPECTED:
            want = ONCHAIN_EXPECTED[task.id]
            assert task.verifier.check == want["check"]
            assert task.verifier.rpc_method == want["rpc_method"]
            assert task.verifier.rpc_params == want["rpc_params"]
            assert task.verifier.check in _ONCHAIN_CHECKS


def test_v1_send_tx_param_schema_share_groups(v1_suite):
    send = next(t for t in v1_suite.tasks if t.id == "task-04-send-tx")
    prompt = [s for s in send.param_schema if s.param_class == "prompt"]
    verifier = [s for s in send.param_schema if s.param_class == "verifier"]
    assert {s.name for s in prompt} == {"send_amount_shannons", "recipient_args"}
    assert {s.name for s in verifier} == {
        "harness_tip",
        "nonce_amount_shannons",
        "recipient_args",
    }
    nonce_prompt = next(s for s in prompt if s.name == "send_amount_shannons")
    nonce_verifier = next(s for s in verifier if s.name == "nonce_amount_shannons")
    assert nonce_prompt.share_group == nonce_verifier.share_group == "nonce"
    recip_prompt = next(s for s in prompt if s.name == "recipient_args")
    recip_verifier = next(s for s in verifier if s.name == "recipient_args")
    assert recip_prompt.share_group == recip_verifier.share_group == "recipient"
    assert recip_prompt.static_value == "0x470dcdc5e44064909650113a274b3b36aecb6dc7"


def test_v1_code_task_hidden_suite_exists(v1_suite):
    code = next(t for t in v1_suite.tasks if t.id == "task-05-hashlock")
    assert code.kind == "code"
    assert code.proof_file == "build/release/hashlock"
    assert code.verifier == "hidden"
    hidden = V1_SUITE_ROOT / code.id / "hidden"
    assert hidden.is_dir()
    assert (hidden / "Cargo.toml").is_file()
    assert (hidden / "src" / "tests.rs").is_file()


def test_v1_protocol_script_tasks_are_scored(v1_suite):
    for tid in ("task-06-xudt-script", "task-07-spore-script"):
        task = next(t for t in v1_suite.tasks if t.id == tid)
        assert task.scored is True
        assert task.score == 10
        assert task.verifier.check == "constant_hex"
        meta = json.loads((V1_SUITE_ROOT / tid / "meta.json").read_text())
        assert meta.get("scored") is True
        assert "PLACEHOLDER" not in meta.get("note", "")
        assert "PLACEHOLDER" not in task.prompt_fragment


def test_v1_prompt_fragments_are_arm_neutral(v1_suite):
    """Shared fragments feed every arm; MCP steering belongs only in the C/D arm preamble."""
    for task in v1_suite.tasks:
        assert "Use the MCP tool" not in task.prompt_fragment
        assert "mcp_call" not in task.prompt_fragment
        assert "rpc_get_" not in task.prompt_fragment


def test_v1_freeze_is_deterministic(v1_suite):
    a = freeze(v1_suite, V1_SUITE_ROOT)
    b = freeze(v1_suite, V1_SUITE_ROOT)
    assert a == b
    assert len(a["composed_prompt_sha256"]) == 64


def test_v1_suite_freeze_file_matches_regeneration(v1_suite):
    freeze_path = V1_SUITE_ROOT / "suite.freeze.json"
    assert freeze_path.is_file(), "run scripts/freeze-v1-suite.sh to generate suite.freeze.json"
    on_disk = json.loads(freeze_path.read_text())
    assert on_disk == freeze(v1_suite, V1_SUITE_ROOT)
