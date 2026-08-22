"""Suite composer: registry storage to staged prompt delivery (ADR-0008).

Assembles deterministic review and single-task stage prompts, plus the thin pointer injected
into the agent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ckbbench.suite.model import Suite

PREAMBLE = """You are a CKB engineering agent completing one benchmark suite in a single
session. The harness releases independent tasks one at a time in the suite's fixed order.
Work only on the task shown below. Do not create or prepare files for an unreleased task.
"""

REVIEW_PREAMBLE = """Canonical review view of the independent benchmark tasks in manifest order.
At runtime the harness releases these tasks one at a time in the same agent session.
"""

POSTAMBLE = """When ALL tasks above are done and every Proof file has been written, submit.
Do not stop until every Proof file exists.
"""

STAGE_POSTAMBLE = """Do not submit yet. Finish this task and write its Proof file. After the
command that creates the Proof returns, the harness will announce the next task and replace
INSTRUCTIONS.md. Read the replaced file before continuing.
"""

FINAL_STAGE_POSTAMBLE = """This is the final task. After its Proof file exists, submit with the
exact completion command from the system instructions.
"""

CHAIN_CONTEXT = """This run targets the CKB {chain} chain. Its JSON-RPC endpoint is available to
your shell in the environment variable CKB_RPC_URL, and the chain profile is in
CKBBENCH_CHAIN_PROFILE. Every chain-dependent task below refers to that chain. If a task requires
signing, look for a sender key in {signer_env}; if it requires transaction tooling, a pinned CKB
JavaScript SDK is installed inside the benchmark container at the path in CKB_SDK_HOME. Read the
environment to see what this run actually provides rather than assuming a variable is set."""

# The signer names a given chain's cell can carry. DevNet injects its public fixture under one
# name; TestNet keeps BOTH of the operator's supported names, because only the ones actually
# exported reach the container -- naming just the preferred one would point a legacy-only operator's
# agent at a variable that is never set.
_SIGNER_ENV_BY_CHAIN = {
    "devnet": ("CKB_SENDER_PRIVKEY",),
    "testnet": ("CKBBENCH_TESTNET_SENDER_PRIVKEY", "BENCH_TESTNET_SENDER_PRIVKEY"),
}


def chain_context_text(chain: str) -> str:
    """The chain facts every arm receives for one cell (plan §8.1).

    Run-time context, deliberately not baked into the frozen task fragments: the same suite runs
    against different chains, and the fragments are hashed into the suite freeze. Endpoint, signer,
    and SDK are NAMED, never rendered: the prompt and the agent environment cannot drift apart, and
    no key value can reach a prompt, a transcript, or a result artifact.

    The signer name is chain-specific and the wording is conditional, because the prompt must not
    assert that a variable exists in a runtime combination that does not define it: a TestNet cell
    has no ``CKB_SENDER_PRIVKEY``, and a local (non-container) cell has no ``CKB_SDK_HOME``.
    """
    names = _SIGNER_ENV_BY_CHAIN.get(chain)
    if names is None:
        raise ValueError(
            f"unknown chain profile {chain!r}; expected one of {sorted(_SIGNER_ENV_BY_CHAIN)}"
        )
    return CHAIN_CONTEXT.format(chain=chain, signer_env=" or ".join(names))


def compose(suite: Suite, *, extra_preamble: str = "", chain_context: str = "") -> str:
    """Assemble a deterministic review view of the Suite's ordered Task list.

    Deterministic: base preamble (+ optional ``chain_context``, then optional arm-specific
    ``extra_preamble``, both placed structurally right after it, before the task list) + fragments
    in manifest order + postamble, so it can be hashed. The arm preamble is a first-class slot
    here, NOT a fragile string-splice by the caller: A/D get the no-web instruction and C/D the MCP
    steering exactly between the base rules and the tasks, where the agent reads them before any
    task. ``chain_context`` sits above the arm slot because it is identical for all four arms.
    """
    parts = [REVIEW_PREAMBLE.strip(), ""]
    if chain_context.strip():
        parts.append(chain_context.strip())
        parts.append("")
    if extra_preamble.strip():
        parts.append(extra_preamble.strip())
        parts.append("")
    for idx, task in enumerate(suite.tasks, start=1):
        parts.append(f"{idx}. {task.prompt_fragment.strip()}")
        parts.append("")
    parts.append(POSTAMBLE.strip())
    return "\n".join(parts).strip() + "\n"


def compose_stage(
    suite: Suite,
    stage_index: int,
    *,
    extra_preamble: str = "",
    chain_context: str = "",
) -> str:
    """Assemble the single task released at ``stage_index`` in one agent session."""
    if isinstance(stage_index, bool) or not isinstance(stage_index, int):
        raise TypeError("stage_index must be an integer")
    if stage_index < 0 or stage_index >= len(suite.tasks):
        raise IndexError("stage_index is outside the suite task order")

    task = suite.tasks[stage_index]
    parts = [PREAMBLE.strip(), ""]
    if chain_context.strip():
        parts.extend((chain_context.strip(), ""))
    if extra_preamble.strip():
        parts.extend((extra_preamble.strip(), ""))
    parts.extend(
        (
            f"Task {stage_index + 1} of {len(suite.tasks)}: {task.id}",
            "",
            task.prompt_fragment.strip(),
            "",
            (
                FINAL_STAGE_POSTAMBLE.strip()
                if stage_index == len(suite.tasks) - 1
                else STAGE_POSTAMBLE.strip()
            ),
        )
    )
    return "\n".join(parts).strip() + "\n"


def write_instructions(composed: str, mount_dir: Path | str) -> tuple[Path, str]:
    """Write instruction text to the mount and return its path and SHA-256."""
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
        f"Read {name} in the current directory. It contains the first task released by the "
        f"benchmark harness. Complete only the released task. The harness will replace the file "
        f"and announce each next task in this same session. Submit only after the final task."
    )
