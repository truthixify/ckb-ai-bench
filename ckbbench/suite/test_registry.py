"""Registry load/validate tests (ADR-0008 strict independence, fail-loud contract)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ckbbench.suite.registry import RegistryError, load_suite


def build_registry(
    root: Path,
    *,
    tasks: list[dict] | None = None,
    manifest_overrides: dict | None = None,
) -> Path:
    """Build a minimal valid registry fixture (no spikes/ dependency)."""
    default_tasks = [
        {
            "id": "task-a",
            "proof_file": "proof_a.txt",
            "score": 10,
            "kind": "onchain",
            "check": "tip_hex",
            "rpc_method": "get_tip_block_number",
            "fragment": "Write tip to proof_a.txt.",
        },
        {
            "id": "task-b",
            "proof_file": "proof_b.txt",
            "score": 5,
            "kind": "onchain",
            "check": "epoch_number",
            "rpc_method": "get_current_epoch",
            "fragment": "Write epoch to proof_b.txt.",
        },
    ]
    task_defs = tasks if tasks is not None else default_tasks
    manifest = {
        "suite_semver": "1.0.0",
        "chain_profile": "devnet",
        "mcp_server_version": "1.6.12",
        "docker_image_digest": "sha256:abc",
        "toolchain_versions": {"rust": "1.85.0"},
        "tasks": [t["id"] for t in task_defs],
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)

    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    for t in task_defs:
        tdir = root / t["id"]
        tdir.mkdir()
        meta = {k: v for k, v in t.items() if k not in ("fragment",)}
        (tdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        (tdir / "prompt.txt").write_text(t.get("fragment", f"Do {t['id']}.\n"))

    return root


def test_good_registry_loads_ordered_tasks(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    assert suite.suite_semver == "1.0.0"
    assert suite.chain_profile == "devnet"
    assert [t.id for t in suite.tasks] == ["task-a", "task-b"]
    assert suite.tasks[0].prompt_fragment.startswith("Write tip")
    assert suite.pins.docker_image_digest == "sha256:abc"
    assert suite.pins.toolchain_versions["rust"] == "1.85.0"


def test_missing_task_dir_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    shutil.rmtree(root / "task-b")
    with pytest.raises(RegistryError, match="has no directory"):
        load_suite(root)


def test_duplicate_id_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "a.txt",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a", "task-a"]},
    )
    with pytest.raises(RegistryError, match="duplicate task id"):
        load_suite(root)


def test_non_positive_score_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "a.txt",
                "score": 0,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a"]},
    )
    with pytest.raises(RegistryError, match="score must be a positive integer"):
        load_suite(root)


def test_missing_proof_file_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "   ",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a"]},
    )
    with pytest.raises(RegistryError, match="missing proof_file"):
        load_suite(root)


def test_fragment_referencing_other_proof_file_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "proof_a.txt",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "Write proof_a.txt only.",
            },
            {
                "id": "task-b",
                "proof_file": "proof_b.txt",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "Also read proof_a.txt for context.",
            },
        ],
    )
    with pytest.raises(RegistryError, match="strict independence violated"):
        load_suite(root)


def test_independence_does_not_false_positive_on_substring(tmp_path: Path):
    # Word-boundary match (grok-build): a fragment that contains another proof name only as a
    # SUBSTRING of a longer token must NOT be flagged. task-a's proof is "out.txt"; task-b
    # legitimately mentions its own "checkout.txt"-like token, which contains "out.txt".
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a", "proof_file": "out.txt", "score": 1, "kind": "onchain",
                "check": "x", "rpc_method": "m", "fragment": "Write to out.txt.",
            },
            {
                "id": "task-b", "proof_file": "result.txt", "score": 1, "kind": "onchain",
                "check": "x", "rpc_method": "m", "fragment": "Write your checkout.txt summary to result.txt.",
            },
        ],
    )
    # Must load cleanly: "out.txt" appears only inside "checkout.txt", not as a standalone token.
    suite = load_suite(root)
    assert [t.id for t in suite.tasks] == ["task-a", "task-b"]


def test_oversized_prompt_file_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "prompt.txt").write_text("x" * ((1 << 20) + 1))
    with pytest.raises(RegistryError, match="over the"):
        load_suite(root)


def test_non_utf8_meta_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "meta.json").write_bytes(b"\xff\xfe not utf8")
    with pytest.raises(RegistryError, match="not valid UTF-8"):
        load_suite(root)


def test_missing_manifest_raises(tmp_path: Path):
    with pytest.raises(RegistryError, match="missing manifest.json"):
        load_suite(tmp_path / "nope")


def test_manifest_missing_required_field_raises(tmp_path: Path):
    root = tmp_path / "reg"
    root.mkdir()
    (root / "manifest.json").write_text('{"suite_semver": "1.0.0"}\n')
    with pytest.raises(RegistryError, match="missing required field"):
        load_suite(root)


def test_empty_tasks_list_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg", manifest_overrides={"tasks": []})
    with pytest.raises(RegistryError, match="non-empty ordered list"):
        load_suite(root)


def test_code_task_loads_verifier_dir(tmp_path: Path):
    root = tmp_path / "reg"
    build_registry(
        root,
        tasks=[
            {
                "id": "code-1",
                "proof_file": "out.rbc",
                "score": 20,
                "kind": "code",
                "verifier_dir": "hidden",
                "fragment": "Build contract.",
            },
        ],
        manifest_overrides={"tasks": ["code-1"]},
    )
    (root / "code-1" / "hidden").mkdir()
    suite = load_suite(root)
    assert suite.tasks[0].kind == "code"
    assert suite.tasks[0].verifier == "hidden"


def test_code_task_missing_verifier_dir_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "code-1",
                "proof_file": "out.rbc",
                "score": 20,
                "kind": "code",
                "fragment": "Build contract.",
            },
        ],
        manifest_overrides={"tasks": ["code-1"]},
    )
    with pytest.raises(RegistryError, match="missing verifier_dir"):
        load_suite(root)


def test_param_schema_parsed(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "send-1",
                "proof_file": "tx_id.txt",
                "score": 15,
                "kind": "onchain",
                "check": "tx",
                "rpc_method": "get_transaction",
                "param_schema": [
                    {
                        "name": "send_amount_shannons",
                        "class": "prompt",
                        "generator": "high_entropy_nonce_amount_shannons",
                    },
                    {
                        "name": "harness_tip",
                        "class": "verifier",
                        "generator": "harness_tip",
                    },
                ],
                "fragment": "Send CKB.",
            },
        ],
        manifest_overrides={"tasks": ["send-1"]},
    )
    suite = load_suite(root)
    assert len(suite.tasks[0].param_schema) == 2
    assert suite.tasks[0].param_schema[0].param_class == "prompt"


def test_invalid_param_schema_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "a.txt",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "param_schema": [{"name": "x", "class": "nope", "generator": "static"}],
                "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a"]},
    )
    with pytest.raises(RegistryError, match="class must be"):
        load_suite(root)


def test_meta_id_must_match_directory_name(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["id"] = "wrong-id"
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="must match directory name"):
        load_suite(root)


def test_invalid_json_raises(tmp_path: Path):
    root = tmp_path / "reg"
    root.mkdir()
    (root / "manifest.json").write_text("{not json\n")
    with pytest.raises(RegistryError, match="not valid JSON"):
        load_suite(root)


def test_invalid_manifest_task_id_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg", manifest_overrides={"tasks": [""]})
    with pytest.raises(RegistryError, match="invalid task id"):
        load_suite(root)


def test_missing_prompt_txt_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "prompt.txt").unlink()
    with pytest.raises(RegistryError, match="missing prompt.txt"):
        load_suite(root)


def test_meta_not_object_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "meta.json").write_text("[]\n")
    with pytest.raises(RegistryError, match="must be a JSON object"):
        load_suite(root)


def test_meta_missing_required_field_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    del meta["kind"]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="missing required field"):
        load_suite(root)


def test_invalid_kind_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["kind"] = "wasm"
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="kind must be"):
        load_suite(root)


def test_onchain_missing_check_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    del meta["check"]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="missing 'check'"):
        load_suite(root)


def test_onchain_invalid_rpc_params_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["rpc_params"] = "nope"
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="rpc_params must be a list"):
        load_suite(root)


def test_code_verifier_dir_missing_on_disk_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "code-1",
                "proof_file": "out.rbc",
                "score": 20,
                "kind": "code",
                "verifier_dir": "hidden",
                "fragment": "Build contract.",
            },
        ],
        manifest_overrides={"tasks": ["code-1"]},
    )
    with pytest.raises(RegistryError, match="verifier_dir .* not found"):
        load_suite(root)


def test_param_schema_must_be_list_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = {}
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="param_schema must be a list"):
        load_suite(root)


def test_param_schema_entry_must_be_object_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = ["bad"]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="must be an object"):
        load_suite(root)


def test_param_schema_missing_field_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = [{"class": "prompt", "generator": "static", "static_value": "x"}]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="missing 'name'"):
        load_suite(root)


def test_param_schema_unknown_generator_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = [{"name": "x", "class": "prompt", "generator": "magic"}]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="unknown generator"):
        load_suite(root)


def test_param_schema_static_requires_value_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = [{"name": "x", "class": "prompt", "generator": "static"}]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="static generator requires static_value"):
        load_suite(root)


def test_param_schema_recipient_static_value_type_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = [
        {"name": "r", "class": "prompt", "generator": "recipient_args", "static_value": 1},
    ]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="must be a string"):
        load_suite(root)


def test_invalid_toolchain_versions_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg", manifest_overrides={"toolchain_versions": "bad"})
    with pytest.raises(RegistryError, match="toolchain_versions must be an object"):
        load_suite(root)


def test_non_integer_score_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["score"] = "10"
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="score must be a positive integer"):
        load_suite(root)