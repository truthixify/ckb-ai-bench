"""Suite freeze: reproducible hashes of the staged agent delivery (ADR-0008).

Hashes each Task directory, prompt fragment, staged prompt, and suite-level pin so a run can be
tied to an immutable Suite snapshot.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ckbbench.suite.compose import compose_stage, pointer_prompt
from ckbbench.suite.model import Suite, SuitePins


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode())


# Environment/tooling artifacts, not authored Task content. Excluding them keeps the freeze hash
# stable across platforms (a stray .DS_Store or a __pycache__ created at freeze time must not
# change "what the agent saw"). This is a NARROW, explicit denylist of KNOWN junk - NOT a
# blanket "skip all dotfiles" rule: a legitimate authored dotfile (e.g. a .config the agent
# reads) must still be hashed, or it could affect the run without affecting the freeze.
_IGNORED_NAMES = frozenset({".DS_Store", "__pycache__", ".git", "target"})
_MAX_HASHED_FILE_BYTES = 1 << 20  # 1 MiB: a Task file larger than this is an authoring error


def _is_ignored(rel_parts: tuple[str, ...]) -> bool:
    return any(part in _IGNORED_NAMES or part.endswith(".pyc") for part in rel_parts)


def hash_task_dir(task_dir: Path) -> str:
    """Deterministic, platform-stable sha256 over the authored files in a Task directory.

    Framing is length-prefixed (path length + path + content length + content) so it is
    unambiguous even when file content contains NUL bytes - a delimiter-only framing would let a
    rename + content-swap collide. A NARROW denylist of known tooling junk (.DS_Store,
    __pycache__, .git, *.pyc) is skipped so a stray artifact created at freeze time does not
    change the hash; authored content, including authored dotfiles, IS hashed. Symlinks are not
    followed (only regular files contribute), so a symlink swap cannot inject foreign bytes.
    """
    if not task_dir.is_dir():
        raise FileNotFoundError(task_dir)
    digest = hashlib.sha256()
    for path in sorted(task_dir.rglob("*")):
        rel_parts = path.relative_to(task_dir).parts
        if _is_ignored(rel_parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        if size > _MAX_HASHED_FILE_BYTES:
            raise ValueError(
                f"task file {path} is {size} bytes, over the {_MAX_HASHED_FILE_BYTES}-byte cap"
            )
        rel = "/".join(rel_parts).encode()
        content = path.read_bytes()
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _pins_to_dict(pins: SuitePins) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if pins.agent_image_digest is not None:
        out["agent_image_digest"] = pins.agent_image_digest
    if pins.verifier_image_digest is not None:
        out["verifier_image_digest"] = pins.verifier_image_digest
    if pins.mcp_tools_digest is not None:
        out["mcp_tools_digest"] = pins.mcp_tools_digest
    if pins.scoring_schema_version is not None:
        out["scoring_schema_version"] = pins.scoring_schema_version
    if pins.toolchain_versions:
        out["toolchain_versions"] = dict(sorted(pins.toolchain_versions.items()))
    if pins.extra:
        out.update(dict(sorted(pins.extra.items())))
    return out


def freeze(suite: Suite, registry_dir: Path | str) -> dict[str, Any]:
    """Build the Suite freeze dict for ``suite`` at ``registry_dir``."""
    root = Path(registry_dir)
    stage_prompts = {
        task.id: _sha256_text(compose_stage(suite, index))
        for index, task in enumerate(suite.tasks)
    }
    task_entries: dict[str, Any] = {}
    for task in suite.tasks:
        tdir = root / task.id
        task_entries[task.id] = {
            "task_dir_sha256": hash_task_dir(tdir),
            "prompt_fragment_sha256": _sha256_text(task.prompt_fragment),
        }
    return {
        "suite_semver": suite.suite_semver,
        "chain_profile": suite.chain_profile,
        "mcp_server_version": suite.mcp_server_version,
        "task_order": [task.id for task in suite.tasks],
        "tasks": task_entries,
        "stage_prompt_sha256": stage_prompts,
        "pointer_prompt_sha256": _sha256_text(pointer_prompt("INSTRUCTIONS.md")),
        "pins": _pins_to_dict(suite.pins),
    }


def write_freeze(freeze_doc: dict[str, Any], dest: Path | str) -> Path:
    """Write ``freeze_doc`` to ``suite.freeze.json`` (or the given path)."""
    path = Path(dest)
    if path.is_dir():
        path = path / "suite.freeze.json"
    path.write_text(json.dumps(freeze_doc, indent=2, sort_keys=True) + "\n")
    return path
