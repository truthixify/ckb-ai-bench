"""Suite composer: registry storage to Composed prompt delivery (ADR-0008).

Assembles preamble + ordered Task fragments + postamble, writes the instructions file
to the mount, and produces the thin pointer injected into the agent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ckbbench.suite.model import Suite

PREAMBLE = """You are a CKB engineering agent. Below is a numbered list of INDEPENDENT
tasks. Work through ALL of them in this one session. Each task is self-contained: no
task depends on the output of another, and you may do them in any order. Each task tells
you exactly which file to write its result (its Proof) into. Write each Proof file in the
current working directory.
"""

POSTAMBLE = """When ALL tasks above are done and every Proof file has been written, submit.
Do not stop until every Proof file exists.
"""


def compose(suite: Suite, *, extra_preamble: str = "") -> str:
    """Assemble the Composed prompt from the Suite's ordered Task list.

    Deterministic: base preamble (+ optional arm-specific ``extra_preamble`` placed structurally
    right after it, before the task list) + fragments in manifest order + postamble, so it can be
    hashed. The arm preamble is a first-class slot here, NOT a fragile string-splice by the caller:
    A/D get the no-web instruction and C/D the MCP steering exactly between the base rules and the
    tasks, where the agent reads them before any task.
    """
    parts = [PREAMBLE.strip(), ""]
    if extra_preamble.strip():
        parts.append(extra_preamble.strip())
        parts.append("")
    for idx, task in enumerate(suite.tasks, start=1):
        parts.append(f"{idx}. {task.prompt_fragment.strip()}")
        parts.append("")
    parts.append(POSTAMBLE.strip())
    return "\n".join(parts).strip() + "\n"


def write_instructions(composed: str, mount_dir: Path | str) -> tuple[Path, str]:
    """Write the Composed prompt as the on-mount instructions file; return (path, sha256)."""
    mount = Path(mount_dir)
    mount.mkdir(parents=True, exist_ok=True)
    inst = mount / "INSTRUCTIONS.md"
    inst.write_text(composed)
    digest = hashlib.sha256(composed.encode()).hexdigest()
    return inst, digest


def pointer_prompt(instructions_path: Path | str) -> str:
    """The thin pointer actually injected into the agent (not the wall of text)."""
    name = Path(instructions_path).name
    return (
        f"Read the file {name} in the current directory. It contains a "
        f"numbered list of independent tasks. Do every task it lists, writing each Proof "
        f"file as instructed, then submit."
    )