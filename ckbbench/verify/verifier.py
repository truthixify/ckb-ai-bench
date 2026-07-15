"""Top-level per-task Verifier dispatcher (ADR-0003, ADR-0005).

Grades each Task independently: on-chain checks use direct RPC; code Tasks use
the hidden suite runner. One task's failure never crashes another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ckbbench.run.runner import PrepareError
from ckbbench.suite.model import OnchainVerifierSpec, Task
from ckbbench.verify.codetask import RunnerCallable, grade_code_task
from ckbbench.verify.onchain import Verdict, grade_onchain_task
from ckbbench.verify.rpc import RpcCallable


def _read_proof(mount: Path, proof_file: str) -> str | None:
    path = mount / proof_file
    if not path.is_file():
        return None
    return path.read_text().strip()


def verify_task(
    task: Task,
    mount: Path | str,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
    *,
    registry_root: Path | str | None = None,
    runner: RunnerCallable | None = None,
) -> Verdict:
    """Grade one Task. Missing Proof is a fail Verdict, not a crash.

    PrepareError (volume/ownership/stop) propagates for infra_fail scoring.
    """
    mnt = Path(mount)
    try:
        if task.kind == "onchain":
            if not isinstance(task.verifier, OnchainVerifierSpec):
                return Verdict(
                    task_id=task.id,
                    passed=False,
                    reason="onchain task missing OnchainVerifierSpec",
                    proof="",
                )
            proof = _read_proof(mnt, task.proof_file)
            if proof is None:
                return Verdict(
                    task_id=task.id,
                    passed=False,
                    reason="proof file missing",
                    proof="",
                )
            return grade_onchain_task(task.id, proof, task.verifier, verifier_private, rpc)

        if task.kind == "code":
            if runner is None:
                return Verdict(
                    task_id=task.id,
                    passed=False,
                    reason="code task requires a runner seam",
                    proof="",
                )
            if registry_root is None:
                return Verdict(
                    task_id=task.id,
                    passed=False,
                    reason="code task requires registry_root",
                    proof="",
                )
            suite_dir = Path(registry_root) / task.id / str(task.verifier)
            if not suite_dir.is_dir():
                return Verdict(
                    task_id=task.id,
                    passed=False,
                    reason=f"hidden suite not found at {suite_dir}",
                    proof="",
                )
            return grade_code_task(task, mnt, suite_dir, verifier_private, runner)

        return Verdict(
            task_id=task.id,
            passed=False,
            reason=f"unknown task kind {task.kind!r}",
            proof="",
        )
    except PrepareError:
        raise
    except Exception as exc:
        return Verdict(
            task_id=task.id,
            passed=False,
            reason=f"verify error: {type(exc).__name__}: {exc}",
            proof="",
        )


def verify_suite(
    tasks: tuple[Task, ...] | list[Task],
    mount: Path | str,
    verifier_private_by_task: dict[str, dict[str, Any]],
    rpc: RpcCallable,
    *,
    registry_root: Path | str | None = None,
    runner: RunnerCallable | None = None,
) -> list[Verdict]:
    """Grade every Task independently; one failure never affects another."""
    results: list[Verdict] = []
    for task in tasks:
        private = verifier_private_by_task.get(task.id, {})
        results.append(
            verify_task(
                task,
                mount,
                private,
                rpc,
                registry_root=registry_root,
                runner=runner,
            )
        )
    return results