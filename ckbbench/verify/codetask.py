"""Code Task grading via hidden suite in a hermetic container (ADR-0005, FINDINGS).

Orchestration policy (Phase 2): rebuild the agent binary from submitted sources
before grading (never trust a stale ``build/release/``), withhold the hidden suite
and ``BENCH_PASSWORD`` from the build stage, inject them only at verify time.
Container wiring lands in Phase 3; this module exposes an injectable runner seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from ckbbench.suite.model import Task
from ckbbench.verify.onchain import Verdict

RunnerStage = Literal["build", "verify"]


@dataclass(frozen=True)
class RunnerInvocation:
    """One container (or fake) invocation: mounts, env, command, and stage label."""

    stage: RunnerStage
    mounts: dict[str, str]
    env: dict[str, str]
    command: tuple[str, ...]


RunnerCallable = Callable[[RunnerInvocation], int]

DEFAULT_BUILD_COMMAND: tuple[str, ...] = ("make", "build")
DEFAULT_VERIFY_COMMAND: tuple[str, ...] = ("cargo", "test", "--release")
BENCH_PASSWORD_ENV = "BENCH_PASSWORD"


def _artifact_dir(mount: Path, artifact_dir: Path | None) -> Path:
    return artifact_dir if artifact_dir is not None else mount / ".ckbbench-artifact"


def _binary_relpath(task: Task) -> str:
    return task.proof_file


def _assert_build_policy(inv: RunnerInvocation, hidden_suite_dir: Path) -> str | None:
    """Build stage must not see the hidden suite or the per-run password."""
    hidden = str(hidden_suite_dir.resolve())
    if hidden in inv.mounts:
        return "build stage must not mount the hidden suite (ADR-0005)"
    if BENCH_PASSWORD_ENV in inv.env:
        return f"build stage must not set {BENCH_PASSWORD_ENV} (ADR-0009)"
    return None


def _assert_verify_policy(
    inv: RunnerInvocation,
    hidden_suite_dir: Path,
    artifact_host: str,
) -> str | None:
    """Verify stage must mount the suite, read-only artifact, and inject the password."""
    hidden = str(hidden_suite_dir.resolve())
    if hidden not in inv.mounts:
        return "verify stage must mount the hidden suite"
    artifact_spec = inv.mounts.get(artifact_host)
    if artifact_spec is None:
        return "verify stage must mount the agent artifact"
    if not artifact_spec.endswith(":ro"):
        return "verify stage must mount the agent artifact read-only"
    if BENCH_PASSWORD_ENV not in inv.env or not inv.env[BENCH_PASSWORD_ENV]:
        return f"verify stage must inject non-empty {BENCH_PASSWORD_ENV}"
    return None


def grade_code_task(
    task: Task,
    mount: Path,
    hidden_suite_dir: Path,
    verifier_private: dict[str, Any],
    runner: RunnerCallable,
    *,
    artifact_dir: Path | None = None,
    build_command: tuple[str, ...] = DEFAULT_BUILD_COMMAND,
    verify_command: tuple[str, ...] = DEFAULT_VERIFY_COMMAND,
) -> Verdict:
    """Rebuild from agent sources, then grade via hidden suite exit code (0 = pass)."""
    out = _artifact_dir(mount, artifact_dir)
    binary_rel = _binary_relpath(task)
    password = verifier_private.get(BENCH_PASSWORD_ENV) or verifier_private.get("bench_password")
    if not password:
        return Verdict(
            task_id=task.id,
            passed=False,
            reason=f"verifier-private missing {BENCH_PASSWORD_ENV}",
            proof="",
        )

    build_inv = RunnerInvocation(
        stage="build",
        mounts={
            str(mount.resolve()): "/sources:ro",
            str(out.resolve()): "/artifact",
        },
        env={},
        command=build_command,
    )
    policy_err = _assert_build_policy(build_inv, hidden_suite_dir)
    if policy_err:
        return Verdict(task_id=task.id, passed=False, reason=policy_err, proof="")

    build_exit = runner(build_inv)
    if build_exit != 0:
        return Verdict(
            task_id=task.id,
            passed=False,
            reason=f"rebuild from sources failed (exit {build_exit})",
            proof="",
        )

    verify_inv = RunnerInvocation(
        stage="verify",
        mounts={
            str(hidden_suite_dir.resolve()): "/suite",
            str(out.resolve()): "/artifact:ro",
        },
        env={
            BENCH_PASSWORD_ENV: str(password),
            "TOP": "/artifact",
            "MODE": "release",
        },
        command=verify_command,
    )
    policy_err = _assert_verify_policy(verify_inv, hidden_suite_dir, str(out.resolve()))
    if policy_err:
        return Verdict(task_id=task.id, passed=False, reason=policy_err, proof="")

    verify_exit = runner(verify_inv)
    proof_path = out / binary_rel
    proof = str(proof_path) if proof_path.exists() else binary_rel
    if verify_exit == 0:
        return Verdict(
            task_id=task.id,
            passed=True,
            reason="hidden suite passed (exit 0)",
            proof=proof,
        )
    return Verdict(
        task_id=task.id,
        passed=False,
        reason=f"hidden suite failed (exit {verify_exit})",
        proof=proof,
    )