"""Suite freeze: reproducible hashes of what the agent saw (ADR-0008).

Hashes each Task directory, each prompt fragment, the Composed prompt, and records
suite-level pins so a run can be tied to an immutable Suite snapshot.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ckbbench.suite.compose import compose
from ckbbench.suite.model import Suite, SuitePins


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode())


def hash_task_dir(task_dir: Path) -> str:
    """Deterministic sha256 over all files in a Task directory (relative paths sorted)."""
    if not task_dir.is_dir():
        raise FileNotFoundError(task_dir)
    digest = hashlib.sha256()
    for path in sorted(task_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(task_dir).as_posix().encode()
        digest.update(rel)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _pins_to_dict(pins: SuitePins) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if pins.docker_image_digest is not None:
        out["docker_image_digest"] = pins.docker_image_digest
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
    composed = compose(suite)
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
        "tasks": task_entries,
        "composed_prompt_sha256": _sha256_text(composed),
        "pins": _pins_to_dict(suite.pins),
    }


def write_freeze(freeze_doc: dict[str, Any], dest: Path | str) -> Path:
    """Write ``freeze_doc`` to ``suite.freeze.json`` (or the given path)."""
    path = Path(dest)
    if path.is_dir():
        path = path / "suite.freeze.json"
    path.write_text(json.dumps(freeze_doc, indent=2, sort_keys=True) + "\n")
    return path