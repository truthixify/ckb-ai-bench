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
from ckbbench.suite.execution_contract import TaskExecutionContract
from ckbbench.suite.model import Suite, SuitePins
from ckbbench.run.retry_policy import RETRY_POLICY


CAMPAIGN_CEILINGS_SCHEMA_VERSION = "ckbbench-campaign-ceilings-v1"


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
    if pins.retry_policy_id is not None:
        out["retry_policy_id"] = pins.retry_policy_id
    if pins.retry_policy_sha256 is not None:
        out["retry_policy_sha256"] = pins.retry_policy_sha256
    if pins.toolchain_versions:
        out["toolchain_versions"] = dict(sorted(pins.toolchain_versions.items()))
    if pins.extra:
        out.update(dict(sorted(pins.extra.items())))
    return out


def execution_ceilings(
    contracts: tuple[TaskExecutionContract, ...],
    *,
    arm_count: int,
    scope: str,
) -> dict[str, Any]:
    if (
        not contracts
        or not all(type(contract) is TaskExecutionContract for contract in contracts)
        or type(arm_count) is not int
        or arm_count <= 0
        or scope not in {"one-trial-per-task-per-arm", "scheduled-campaign"}
    ):
        raise ValueError("execution ceilings need exact planned Task contracts and scope")
    attempts_per_slot = 1 + int(RETRY_POLICY["maximum_retries_per_slot"])
    multiplier = attempts_per_slot
    planned_slots = len(contracts)
    retry_cooldown_seconds = (
        planned_slots
        * int(RETRY_POLICY["maximum_retries_per_slot"])
        * int(RETRY_POLICY["cooldown_seconds"])
    )
    preflight_seconds = multiplier * sum(
        contract.harness_deadlines.preflight_seconds for contract in contracts
    )
    setup_seconds = multiplier * sum(
        contract.harness_deadlines.setup_seconds for contract in contracts
    )
    grading_seconds = multiplier * sum(
        contract.harness_deadlines.grading_seconds for contract in contracts
    )
    teardown_seconds = multiplier * sum(
        contract.harness_deadlines.teardown_seconds for contract in contracts
    )
    agent_wall_seconds = multiplier * sum(
        contract.budget.wall_time_limit_seconds for contract in contracts
    )
    harness_seconds = preflight_seconds + setup_seconds + grading_seconds + teardown_seconds
    output_limits = tuple(contract.budget.output_token_limit for contract in contracts)
    maximum_output_tokens = (
        None
        if any(value is None for value in output_limits)
        else multiplier * sum(int(value) for value in output_limits)
    )
    return {
        "arm_count": arm_count,
        "maximum_agent_wall_seconds": agent_wall_seconds,
        "maximum_attempts": planned_slots * multiplier,
        "maximum_end_to_end_seconds": (
            agent_wall_seconds + harness_seconds + retry_cooldown_seconds
        ),
        "maximum_grading_seconds": grading_seconds,
        "maximum_harness_seconds": harness_seconds,
        "maximum_output_tokens": maximum_output_tokens,
        "maximum_preflight_seconds": preflight_seconds,
        "maximum_provider_calls": multiplier * sum(
            contract.budget.provider_call_limit for contract in contracts
        ),
        "maximum_retry_cooldown_seconds": retry_cooldown_seconds,
        "maximum_setup_seconds": setup_seconds,
        "maximum_steps": multiplier * sum(
            contract.budget.step_limit for contract in contracts
        ),
        "maximum_teardown_seconds": teardown_seconds,
        "planned_slots": planned_slots,
        "schema_version": CAMPAIGN_CEILINGS_SCHEMA_VERSION,
        "scope": scope,
        "whole_task_attempts_per_slot": attempts_per_slot,
    }


def campaign_ceilings(suite: Suite) -> dict[str, Any]:
    tasks = tuple(task for task in suite.tasks if task.scored)
    if not tasks or any(task.execution is None for task in tasks):
        raise ValueError("campaign ceilings need execution contracts for every scored Task")
    contracts = tuple(
        task.execution
        for task in tasks
        for _arm in ("B", "C")
        if task.execution is not None
    )
    return execution_ceilings(
        contracts,
        arm_count=2,
        scope="one-trial-per-task-per-arm",
    )


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
        if task.execution is not None:
            task_entries[task.id]["execution_contract_sha256"] = task.execution.sha256
    document = {
        "suite_semver": suite.suite_semver,
        "chain_profile": suite.chain_profile,
        "mcp_server_version": suite.mcp_server_version,
        "task_order": [task.id for task in suite.tasks],
        "tasks": task_entries,
        "stage_prompt_sha256": stage_prompts,
        "pointer_prompt_sha256": _sha256_text(pointer_prompt("INSTRUCTIONS.md")),
        "pins": _pins_to_dict(suite.pins),
    }
    if suite.task_execution_schema_version is not None:
        document["task_execution_schema_version"] = suite.task_execution_schema_version
        document["campaign_ceilings"] = campaign_ceilings(suite)
    return document


def freeze_sha256(freeze_doc: dict[str, Any]) -> str:
    """Digest the canonical suite-freeze document used by result and campaign identities."""
    canonical = json.dumps(
        freeze_doc,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return _sha256_bytes(canonical)


def write_freeze(freeze_doc: dict[str, Any], dest: Path | str) -> Path:
    """Write ``freeze_doc`` to ``suite.freeze.json`` (or the given path)."""
    path = Path(dest)
    if path.is_dir():
        path = path / "suite.freeze.json"
    path.write_text(json.dumps(freeze_doc, indent=2, sort_keys=True) + "\n")
    return path
