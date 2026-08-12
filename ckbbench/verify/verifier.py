"""Top-level per-task Verifier dispatcher (ADR-0003, ADR-0005).

Grades each Task independently: on-chain checks use direct RPC; code Tasks use
the hidden suite runner. One task's failure never crashes another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ckbbench.run.runner import PrepareError
from ckbbench.suite.model import OnchainVerifierSpec, Task
from ckbbench.verify.codetask import RunnerCallable, grade_code_task
from ckbbench.verify.onchain import (
    VerificationInfrastructureError,
    Verdict,
    grade_onchain_task,
)
from ckbbench.verify.rpc import RpcCallable


def _read_proof(mount: Path, proof_file: str) -> str | None:
    """Return the proof file's exact text; each checker owns its own normalization.

    newline="" disables universal-newline translation: without it CRLF and bare CR become LF, so
    Verdict.proof and the result artifact would not be the bytes the agent actually wrote.
    """
    path = mount / proof_file
    if not path.is_file():
        return None
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def verify_task(
    task: Task,
    mount: Path | str,
    verifier_private: dict[str, Any],
    rpc: RpcCallable,
    *,
    registry_root: Path | str | None = None,
    runner: RunnerCallable | None = None,
    monotonic_fn: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], Any] | None = None,
) -> Verdict:
    """Grade one Task. An ordinary task failure is a fail Verdict, not a crash.

    Two exceptions are not task results and propagate for infra_fail scoring: PrepareError
    (volume/ownership/stop) and VerificationInfrastructureError (the verification channel itself
    could not produce a trustworthy observation). Every other exception stays isolated as a failed
    Verdict for that one task.
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
            return grade_onchain_task(
                task.id,
                proof,
                task.verifier,
                verifier_private,
                rpc,
                monotonic_fn=monotonic_fn,
                sleep_fn=sleep_fn,
            )

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
    except (PrepareError, VerificationInfrastructureError):
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
    monotonic_fn: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], Any] | None = None,
) -> list[Verdict]:
    """Grade every Task independently; one task's failed Verdict never affects another.

    A verifier-infrastructure failure is not a task result, so it aborts the whole suite rather
    than returning partial verdicts that would be scored as if grading had completed.
    """
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
                monotonic_fn=monotonic_fn,
                sleep_fn=sleep_fn,
            )
        )
    return results
