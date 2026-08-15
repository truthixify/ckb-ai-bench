"""Loader and freeze tests for the v1 seed suite registry (Phase 6)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ckbbench.suite.compose import compose

from ckbbench.suite.freeze import freeze, write_freeze
from ckbbench.suite.model import OnchainVerifierSpec
from ckbbench.suite.registry import load_suite
from ckbbench.verify.onchain import _ONCHAIN_CHECKS

V1_SUITE_ROOT = Path(__file__).resolve().parents[2] / "suites" / "ckb-v1"

REAL_TASK_IDS = (
    "task-01-tip",
    "task-04-send-tx",
    "task-05-hashlock",
    "task-06-sudt-script",
    "task-08-type-id-data-cell",
)

# Retired by the Card 7 suite cut: absent from the manifest, the tree, the composed prompt, and
# the freeze. Kept here as an explicit absence contract, not as suite members.
RETIRED_TASK_IDS = ("task-02-epoch", "task-03-blockhash", "task-07-spore-script")

# The canonical Simple UDT mainnet type script, established independently in the Task 06 audit.
SUDT_CODE_HASH = (
    "0x5e7a36a77e68eecc013dfa2fe6a23f3b6c344b04005808694ae6dd45eea4cfd5"
)
ONCHAIN_EXPECTED: dict[str, dict[str, object]] = {
    "task-01-tip": {
        "check": "tip_block_identity",
        "rpc_method": "get_tip_block_number",
        "rpc_params": (),
    },
    "task-04-send-tx": {
        "check": "tx_proof",
        "rpc_method": "get_transaction",
        "rpc_params": (),
    },
    "task-08-type-id-data-cell": {
        "check": "type_id_data_cell",
        "rpc_method": "get_transaction",
        "rpc_params": (),
    },
    "task-06-sudt-script": {
        "check": "script_identity",
        "rpc_method": "constant",
        "rpc_params": (SUDT_CODE_HASH, "type"),
    },
}


@pytest.fixture(scope="module")
def v1_suite():
    return load_suite(V1_SUITE_ROOT)


ROLE_PIN_RE = re.compile(r"sha256:[0-9a-f]{64}")


def test_v1_suite_loads_and_manifest_pins(v1_suite):
    assert v1_suite.suite_semver == "2.0.0"
    assert v1_suite.chain_profile == "devnet"
    assert v1_suite.mcp_server_version == "1.6.13"
    agent_pin = v1_suite.pins.agent_image_digest
    verifier_pin = v1_suite.pins.verifier_image_digest
    assert agent_pin and ROLE_PIN_RE.fullmatch(agent_pin), agent_pin
    assert verifier_pin and ROLE_PIN_RE.fullmatch(verifier_pin), verifier_pin
    assert agent_pin != verifier_pin
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


def test_v1_sudt_task_contract(v1_suite):
    """Task 06 asks for a two-field Simple UDT identity graded by its own checker."""
    task = next(t for t in v1_suite.tasks if t.id == "task-06-sudt-script")
    assert task.scored is True
    assert task.score == 10
    assert task.proof_file == "proof_sudt_script.txt"
    assert task.verifier.check == "script_identity"
    assert task.verifier.rpc_method == "constant"
    assert task.verifier.rpc_params == (SUDT_CODE_HASH, "type")
    meta = json.loads((V1_SUITE_ROOT / "task-06-sudt-script" / "meta.json").read_text())
    assert meta.get("scored") is True
    assert "PLACEHOLDER" not in meta.get("note", "")


@pytest.mark.parametrize("retired", RETIRED_TASK_IDS)
def test_v1_retired_tasks_are_absent_from_the_registry(v1_suite, retired):
    assert retired not in {t.id for t in v1_suite.tasks}


@pytest.mark.parametrize("retired", RETIRED_TASK_IDS)
def test_v1_retired_task_directories_are_gone(retired):
    assert not (V1_SUITE_ROOT / retired).exists()


@pytest.mark.parametrize("retired", RETIRED_TASK_IDS)
def test_v1_retired_tasks_are_absent_from_the_manifest(retired):
    manifest = json.loads((V1_SUITE_ROOT / "manifest.json").read_text())
    assert retired not in manifest["tasks"]


@pytest.mark.parametrize("retired", RETIRED_TASK_IDS)
def test_v1_composed_prompt_has_no_retired_fragment(v1_suite, retired):
    assert retired not in compose(v1_suite)


@pytest.mark.parametrize("retired", RETIRED_TASK_IDS)
def test_v1_freeze_has_no_retired_task(retired):
    doc = json.loads((V1_SUITE_ROOT / "suite.freeze.json").read_text())
    assert retired not in doc["tasks"]


def test_v1_sudt_prompt_asks_for_the_two_fields_without_leaking_the_answer(v1_suite):
    task = next(t for t in v1_suite.tasks if t.id == "task-06-sudt-script")
    prompt = task.prompt_fragment
    for required in ("Simple UDT", "mainnet", "type script", "code_hash", "hash_type",
                     "proof_sudt_script.txt"):
        assert required in prompt, required
    assert "xUDT" in prompt, "the prompt must distinguish the requested protocol by name"
    for forbidden in (SUDT_CODE_HASH, SUDT_CODE_HASH.upper(), "5e7a36a7"):
        assert forbidden not in prompt, "the prompt must not contain the answer"


@pytest.mark.parametrize(
    "forbidden", ["mcp_call", "resources/read", "CKB AI", "MCP"],
)
def test_v1_sudt_prompt_is_arm_neutral(v1_suite, forbidden):
    task = next(t for t in v1_suite.tasks if t.id == "task-06-sudt-script")
    assert forbidden not in task.prompt_fragment


def test_v1_registry_has_no_xudt_identity_left(v1_suite):
    assert not (V1_SUITE_ROOT / "task-06-xudt-script").exists()
    assert all(t.id != "task-06-xudt-script" for t in v1_suite.tasks)
    assert all(t.proof_file != "proof_xudt_code_hash.txt" for t in v1_suite.tasks)


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


# --- Task 01: run-bound tip + block-hash identity (Card 4) ---


def test_v1_task_01_run_bound_contract(v1_suite):
    task = next(t for t in v1_suite.tasks if t.id == "task-01-tip")
    assert task.score == 10
    assert task.scored
    assert task.proof_file == "proof_tip.txt"
    assert task.kind == "onchain"
    assert task.verifier.check == "tip_block_identity"
    assert task.verifier.rpc_method == "get_tip_block_number"


def test_v1_task_01_declares_only_a_verifier_private_harness_tip(v1_suite):
    """harness_tip is the run-start lower bound; exposing it to the prompt would hand over the
    answer's lower bound and let a model pass without reading the chain."""
    schema = next(t for t in v1_suite.tasks if t.id == "task-01-tip").param_schema
    assert len(schema) == 1
    entry = schema[0]
    assert entry.name == "harness_tip"
    assert entry.param_class == "verifier"
    assert entry.generator == "harness_tip"
    assert not [p for p in schema if p.param_class == "prompt"]


def test_v1_task_01_prompt_states_the_two_line_contract(v1_suite):
    prompt = next(t for t in v1_suite.tasks if t.id == "task-01-tip").prompt_fragment.lower()
    for term in ("tip", "block hash", "proof_tip.txt", "two lines", "0x"):
        assert term in prompt, term
    assert "line 1" in prompt and "line 2" in prompt


def test_v1_task_01_prompt_is_arm_neutral_and_leaks_nothing(v1_suite):
    prompt = next(t for t in v1_suite.tasks if t.id == "task-01-tip").prompt_fragment.lower()
    for banned in ("mcp", "mcp_call", "ckb ai", "resources/read", "web search", "curl",
                   "json-rpc", "harness_tip", "rpc endpoint"):
        assert banned not in prompt, banned


# --- Task 05: lock-script wording and registry identity (Card 5) ---


def test_v1_task_05_registry_contract(v1_suite):
    task = next(t for t in v1_suite.tasks if t.id == "task-05-hashlock")
    assert task.kind == "code"
    assert task.score == 30
    assert task.scored
    assert task.proof_file == "build/release/hashlock"
    assert task.verifier == "hidden"


def test_v1_task_05_prompt_calls_hashlock_a_lock_script(v1_suite):
    """The hidden suite installs the binary as an input lock; calling it a type script in the
    prompt invites the agent to author the wrong semantics and fail a correct oracle."""
    prompt = next(t for t in v1_suite.tasks if t.id == "task-05-hashlock").prompt_fragment.lower()
    assert "lock script" in prompt
    assert "type script" not in prompt



def test_v1_task_08_registry_contract(v1_suite):
    task = next(t for t in v1_suite.tasks if t.id == "task-08-type-id-data-cell")
    assert task.kind == "onchain"
    assert task.score == 25
    assert task.scored
    assert task.proof_file == "proof_type_id_data_cell.txt"
    assert task.verifier.check == "type_id_data_cell"


def test_v1_registry_is_five_scored_tasks_totalling_one_hundred(v1_suite):
    """The frozen phase-one shape: five scored tasks totalling exactly 100 points."""
    scored = [t for t in v1_suite.tasks if t.scored]
    assert len(scored) == 5
    assert len(scored) == len(v1_suite.tasks), "every retained task must be scored"
    assert sum(t.score for t in scored) == 100
    assert [t.score for t in v1_suite.tasks] == [10, 25, 30, 10, 25]


def test_v1_task_08_param_schema_splits_prompt_and_verifier(v1_suite):
    schema = next(t for t in v1_suite.tasks if t.id == "task-08-type-id-data-cell").param_schema
    prompt = {p.name: p for p in schema if p.param_class == "prompt"}
    private = {p.name: p for p in schema if p.param_class == "verifier"}
    assert set(prompt) == {"payload_hex", "recipient_args"}
    assert set(private) == {"expected_payload_hex", "expected_recipient_args", "harness_tip"}
    assert prompt["payload_hex"].generator == "fresh_blob_hex_32"
    assert prompt["payload_hex"].share_group == private["expected_payload_hex"].share_group == "payload"
    assert prompt["recipient_args"].share_group == private["expected_recipient_args"].share_group == "recipient"
    assert private["harness_tip"].share_group is None


def test_v1_task_08_prompt_states_the_contract_without_the_answer(v1_suite):
    prompt = next(t for t in v1_suite.tasks if t.id == "task-08-type-id-data-cell").prompt_fragment
    low = prompt.lower()
    for term in ("payload_hex", "recipient_args", "proof_type_id_data_cell.txt",
                 "20000000000", "type-id", "output at index 0", "two lines"):
        assert term in low, term
    # The Type-ID code_hash is protocol structure and belongs in the prompt; the derived args and
    # script hash are the answer and must not be.
    assert "545950455f4944" in low
    for banned in ("mcp", "ckb ai", "mcp_call", "web search"):
        assert banned not in low, banned
