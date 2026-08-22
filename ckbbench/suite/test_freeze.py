"""Suite freeze tests: reproducible hashes and sensitivity to Task changes (ADR-0008)."""

from __future__ import annotations

import hashlib
import json
import subprocess
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
    assert set(a["stage_prompt_sha256"]) == {"task-a", "task-b"}
    assert all(len(value) == 64 for value in a["stage_prompt_sha256"].values())
    assert len(a["pointer_prompt_sha256"]) == 64
    assert a["task_order"] == [task.id for task in suite.tasks]
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
    assert before["stage_prompt_sha256"]["task-a"] != after["stage_prompt_sha256"]["task-a"]
    assert before["stage_prompt_sha256"]["task-b"] == after["stage_prompt_sha256"]["task-b"]


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
        manifest_overrides={"toolchain_versions": {}},
    )
    manifest = json.loads((root / "manifest.json").read_text())
    del manifest["agent_image_digest"]
    del manifest["verifier_image_digest"]
    manifest["toolchain_versions"] = {}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    suite = load_suite(root)
    pins = freeze(suite, root)["pins"]
    assert "agent_image_digest" not in pins
    assert "verifier_image_digest" not in pins
    assert "toolchain_versions" not in pins


def test_changing_meta_json_changes_task_dir_hash(tmp_path: Path):
    # ADR-0008: the task_dir hash must cover the whole authored Task, not just prompt.txt. A
    # meta.json edit (e.g. a different score or verifier spec) must change the task_dir hash.
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    before = freeze(suite, root)["tasks"]["task-a"]["task_dir_sha256"]
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["score"] = meta["score"] + 1
    (root / "task-a" / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    after = freeze(load_suite(root), root)["tasks"]["task-a"]["task_dir_sha256"]
    assert before != after


def test_known_junk_does_not_change_hash(tmp_path: Path):
    # Platform stability (grok-build): a stray .DS_Store / __pycache__ / .git / .pyc appearing at
    # freeze time must NOT change "what the agent saw". This is a NARROW denylist of known junk.
    root = build_registry(tmp_path / "reg")
    before = hash_task_dir(root / "task-a")
    (root / "task-a" / ".DS_Store").write_bytes(b"junk")
    (root / "task-a" / "__pycache__").mkdir()
    (root / "task-a" / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
    (root / "task-a" / ".git").mkdir()
    (root / "task-a" / ".git" / "HEAD").write_text("ref: x")
    (root / "task-a" / "stray.pyc").write_bytes(b"\x00")
    after = hash_task_dir(root / "task-a")
    assert before == after


def test_authored_dotfile_does_change_hash(tmp_path: Path):
    # codex round-2 hole: skipping ALL dotfiles let authored hidden content (e.g. a .config the
    # agent reads) escape the freeze. A legitimate authored dotfile MUST change the hash.
    root = build_registry(tmp_path / "reg")
    before = hash_task_dir(root / "task-a")
    (root / "task-a" / ".config").write_text("agent-visible setting")
    after = hash_task_dir(root / "task-a")
    assert before != after


def test_hash_is_unambiguous_for_nul_content(tmp_path: Path):
    # Length-prefixed framing (codex blocker): a rename + content-swap that would collide under a
    # NUL-delimited framing must produce different hashes. Build two dirs whose (path, content)
    # bytes differ only in where the boundary falls.
    a = tmp_path / "a"
    a.mkdir()
    (a / "f").write_bytes(b"x\x00y")
    b = tmp_path / "b"
    b.mkdir()
    (b / "f").write_bytes(b"x\x00z")
    assert hash_task_dir(a) != hash_task_dir(b)


def test_oversized_task_file_is_rejected(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "big.bin").write_bytes(b"0" * ((1 << 20) + 1))
    with pytest.raises(ValueError, match="over the"):
        hash_task_dir(root / "task-a")


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
    assert pins["agent_image_digest"] == "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert pins["verifier_image_digest"] == "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert pins["mcp_tools_digest"] == "sha256:mcp"
    assert pins["scoring_schema_version"] == "1"
    assert pins["custom_pin"] == "value"


def test_authored_build_file_changes_hash(tmp_path: Path):
    """`build` is authored content, not a denylist entry: a file under it changes the digest."""
    root = build_registry(tmp_path / "reg")
    task = root / "task-a"
    before = hash_task_dir(task)
    fixture = task / "build" / "release" / "hashlock"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"authored-build-content")
    assert hash_task_dir(task) != before


V1_TASK_05 = Path(__file__).resolve().parents[2] / "suites" / "ckb-v1" / "task-05-hashlock"


def _tracked_files(task_dir: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", str(task_dir)],
        cwd=task_dir.parents[2], capture_output=True, text=True, check=True,
    )
    return sorted(p for p in proc.stdout.split("\0") if p)


def test_real_task_05_hash_covers_tracked_content_only():
    """The hashlock digest must cover tracked content only.

    Rebuilt independently from ``git ls-files`` and the production framing, so an ignored or
    untracked file under the task directory makes this disagree with ``hash_task_dir``.
    """
    tracked = _tracked_files(V1_TASK_05)
    assert tracked, "expected tracked files under task-05-hashlock"

    digest = hashlib.sha256()
    for repo_rel in tracked:
        path = V1_TASK_05.parents[2] / repo_rel
        rel = str(Path(repo_rel).relative_to(V1_TASK_05.relative_to(V1_TASK_05.parents[2])))
        content = path.read_bytes()
        rel_bytes = rel.encode()
        digest.update(len(rel_bytes).to_bytes(8, "big"))
        digest.update(rel_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)

    assert digest.hexdigest() == hash_task_dir(V1_TASK_05), (
        "hash_task_dir(task-05) does not match a tracked-file-only digest; an untracked or "
        "ignored artifact under the task directory is contributing to the suite freeze"
    )


def test_changing_the_agent_pin_changes_the_freeze(tmp_path: Path):
    """Each role pin is part of the immutable suite identity, so it must move the freeze."""
    other = "sha256:" + "c" * 64
    base = freeze(load_suite(build_registry(tmp_path / "a")), tmp_path / "a")
    moved_root = build_registry(tmp_path / "b", manifest_overrides={"agent_image_digest": other})
    moved = freeze(load_suite(moved_root), moved_root)
    assert base["pins"]["agent_image_digest"] != moved["pins"]["agent_image_digest"]
    assert base != moved


def test_changing_the_verifier_pin_changes_the_freeze(tmp_path: Path):
    other = "sha256:" + "d" * 64
    base = freeze(load_suite(build_registry(tmp_path / "a")), tmp_path / "a")
    moved_root = build_registry(tmp_path / "b", manifest_overrides={"verifier_image_digest": other})
    moved = freeze(load_suite(moved_root), moved_root)
    assert base["pins"]["verifier_image_digest"] != moved["pins"]["verifier_image_digest"]
    assert base != moved
