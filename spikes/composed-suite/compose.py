"""Spike (NOT production): the Suite composer (ADR-0008).

Storage shape and delivery shape are deliberately different:
- STORAGE = a registry of Task directories (registry/<id>/{prompt.txt, meta.json}),
  indexed by registry/manifest.json with the ordered task list + suite pins.
- DELIVERY = ONE composed prompt (preamble + ordered fragments + postamble) written
  as an instructions file into the MOUNTED folder. The prompt actually injected into
  the agent is a thin POINTER telling it to read that file.

The composed prompt is hashed (sha256) so "what the agent saw" is reproducible per arm
(the Suite freeze). Task order is load-bearing for assembly even though scoring is
order-independent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PREAMBLE = """You are a CKB engineering agent. Below is a numbered list of INDEPENDENT
tasks. Work through ALL of them in this one session. Each task is self-contained: no
task depends on the output of another, and you may do them in any order. Each task tells
you exactly which file to write its result (its Proof) into. Write each Proof file in the
current working directory.
"""

POSTAMBLE = """When ALL tasks above are done and every Proof file has been written, submit.
Do not stop until every Proof file exists.
"""


def load_manifest(registry: Path) -> dict:
    return json.loads((registry / "manifest.json").read_text())


def compose(registry: Path) -> tuple[str, list[dict]]:
    """Assemble the composed prompt from the manifest's ordered task list.

    Returns (composed_text, task_metas). Deterministic: preamble + fragments in
    manifest order + postamble, so it can be reviewed and hashed.
    """
    manifest = load_manifest(registry)
    parts = [PREAMBLE.strip(), ""]
    metas: list[dict] = []
    for idx, task_id in enumerate(manifest["tasks"], start=1):
        tdir = registry / task_id
        fragment = (tdir / "prompt.txt").read_text().strip()
        meta = json.loads((tdir / "meta.json").read_text())
        metas.append(meta)
        parts.append(f"{idx}. {fragment}")
        parts.append("")
    parts.append(POSTAMBLE.strip())
    composed = "\n".join(parts).strip() + "\n"
    return composed, metas


def write_instructions(composed: str, mount: Path) -> tuple[Path, str]:
    """Write the composed prompt as the on-mount instructions file; return (path, sha256).

    The agent-readable area of the mount holds this file during the run; the pointer
    injected into the agent references it (ADR-0008 timing partition).
    """
    mount.mkdir(parents=True, exist_ok=True)
    inst = mount / "INSTRUCTIONS.md"
    inst.write_text(composed)
    digest = hashlib.sha256(composed.encode()).hexdigest()
    return inst, digest


def pointer_prompt(instructions_path: Path) -> str:
    """The thin pointer actually injected into the agent (not the wall of text)."""
    return (
        f"Read the file {instructions_path.name} in the current directory. It contains a "
        f"numbered list of independent tasks. Do every task it lists, writing each Proof "
        f"file as instructed, then submit."
    )
