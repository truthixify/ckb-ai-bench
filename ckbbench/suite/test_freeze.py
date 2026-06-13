"""Suite freeze tests: reproducible hashes and sensitivity to Task changes (ADR-0008)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ckbbench.suite.freeze import freeze, hash_task_dir, write_freeze
from ckbbench.suite.registry import load_suite
from ckbbench.suite.test_registry import build_registry


def test_identical_input_produces_identical_freeze(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    a = freeze(suite, root)
    b = freeze(suite, root)
    assert a == b
    assert len(a["composed_prompt_sha256"]) == 64
    assert set(a["tasks"]) == {"task-a", "task-b"}


def test_changing_task_file_changes_freeze_hash(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    before = freeze(suite, root)
    (root / "task-a" / "prompt.txt").write_text("Changed fragment.\n")
    suite2 = load_suite(root)
    after = freeze(suite2, root)
    assert before["tasks"]["task-a"]["prompt_fragment_sha256"] != after["tasks"]["task-a"]["prompt_fragment_sha256"]
    assert before["tasks"]["task-a"]["task_dir_sha256"] != after["tasks"]["task-a"]["task_dir_sha256"]
    assert before["composed_prompt_sha256"] != after["composed_prompt_sha256"]


def test_write_freeze_to_directory(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    doc = freeze(suite, root)
    path = write_freeze(doc, root)
    assert path == root / "suite.freeze.json"
    loaded = json.loads(path.read_text())
    assert loaded["suite_semver"] == "1.0.0"


def test_write_freeze_to_explicit_file(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    dest = tmp_path / "custom.freeze.json"
    path = write_freeze(freeze(suite, root), dest)
    assert path == dest
    assert path.is_file()


def test_hash_task_dir_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        hash_task_dir(tmp_path / "missing")


def test_hash_task_dir_skips_non_files(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "nested").mkdir()
    digest = hash_task_dir(root / "task-a")
    assert len(digest) == 64


def test_freeze_pins_omit_unset_optional_fields(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        manifest_overrides={
            "docker_image_digest": None,
            "toolchain_versions": {},
        },
    )
    manifest = json.loads((root / "manifest.json").read_text())
    del manifest["docker_image_digest"]
    manifest["toolchain_versions"] = {}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    suite = load_suite(root)
    pins = freeze(suite, root)["pins"]
    assert "docker_image_digest" not in pins
    assert "toolchain_versions" not in pins


def test_freeze_pins_include_all_optional_fields(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        manifest_overrides={
            "mcp_tools_digest": "sha256:mcp",
            "scoring_schema_version": "1",
            "custom_pin": "value",
        },
    )
    suite = load_suite(root)
    doc = freeze(suite, root)
    pins = doc["pins"]
    assert pins["docker_image_digest"] == "sha256:abc"
    assert pins["mcp_tools_digest"] == "sha256:mcp"
    assert pins["scoring_schema_version"] == "1"
    assert pins["custom_pin"] == "value"