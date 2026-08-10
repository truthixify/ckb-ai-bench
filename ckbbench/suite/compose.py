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
    """Assemble the Composed prompt from the Suite's ordered Task list.

    Deterministic: base preamble (+ optional ``chain_context``, then optional arm-specific
    ``extra_preamble``, both placed structurally right after it, before the task list) + fragments
    in manifest order + postamble, so it can be hashed. The arm preamble is a first-class slot
    here, NOT a fragile string-splice by the caller: A/D get the no-web instruction and C/D the MCP
    steering exactly between the base rules and the tasks, where the agent reads them before any
    task. ``chain_context`` sits above the arm slot because it is identical for all four arms.
    """
    parts = [PREAMBLE.strip(), ""]
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