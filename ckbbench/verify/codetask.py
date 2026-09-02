"""Code Task grading via hidden suite in a hermetic container (ADR-0005, FINDINGS).

Orchestration policy: rebuild the agent binary from submitted sources
before grading (never trust a stale ``build/release/``), withhold the hidden suite
and verifier challenge from the build stage, inject them only at verify time.
Container wiring is supplied through an injectable runner seam.
"""

from __future__ import annotations

import shutil
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
DEFAULT_VERIFY_COMMAND: tuple[str, ...] = (
    "cargo",
    "test",
    "--release",
    "--locked",
    "--offline",
)
BENCH_PASSWORD_ENV = "BENCH_PASSWORD"
CODE_CHALLENGE_ENV = "CKBBENCH_CHALLENGE"
PRIVATE_CHALLENGE_ENVS = (CODE_CHALLENGE_ENV, BENCH_PASSWORD_ENV)


def _artifact_dir(mount: Path, artifact_dir: Path | None) -> Path:
    # Prefer outside the agent mount so root-owned agent writes cannot block host clear.
    return artifact_dir if artifact_dir is not None else mount.parent / ".ckbbench-artifact"


def prepare_artifact_dir(path: Path) -> None:
    """Create artifact dir owned by the runner process; clear any prior contents.

    Permission failures raise PrepareError (infra), not a silent agent_fail.
    """
    # Local import avoids import cycle at module load (runner imports codetask).
    from ckbbench.run.runner import PrepareError

    try:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PrepareError(f"artifact dir prepare failed for {path}: {exc}") from exc


def _binary_relpath(task: Task) -> str:
    return task.proof_file


def _assert_build_policy(inv: RunnerInvocation, hidden_suite_dir: Path) -> str | None:
    """Build stage must not see the hidden suite or the per-run challenge."""
    hidden = str(hidden_suite_dir.resolve())
    if hidden in inv.mounts:
        return "build stage must not mount the hidden suite (ADR-0005)"
    for name in PRIVATE_CHALLENGE_ENVS:
        if name in inv.env:
            return f"build stage must not set {name} (ADR-0009)"
    return None


def _assert_verify_policy(
    inv: RunnerInvocation,
    hidden_suite_dir: Path,
    artifact_host: str,
) -> str | None:
    """Verify stage must mount the suite, read-only artifact, and inject the challenge."""
    hidden = str(hidden_suite_dir.resolve())
    suite_spec = inv.mounts.get(hidden)
    if suite_spec is None:
        return "verify stage must mount the hidden suite"
    if not suite_spec.endswith(":ro"):
        return "verify stage must mount the hidden suite read-only"
    artifact_spec = inv.mounts.get(artifact_host)
    if artifact_spec is None:
        return "verify stage must mount the agent artifact"
    if not artifact_spec.endswith(":ro"):
        return "verify stage must mount the agent artifact read-only"
    values = [inv.env[name] for name in PRIVATE_CHALLENGE_ENVS if inv.env.get(name)]
    if not values:
        return f"verify stage must inject non-empty {CODE_CHALLENGE_ENV} or {BENCH_PASSWORD_ENV}"
    if len(set(values)) != 1:
        return "verify stage challenge aliases must match"
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
    """Rebuild from agent sources, then grade via hidden suite exit code (0 = pass).

    PrepareError from the runner (volume/ownership) propagates to the orchestrator for
    infra_fail scoring and is not converted into an agent_fail Verdict.
    """
    out = _artifact_dir(mount, artifact_dir)
    prepare_artifact_dir(out)
    binary_rel = _binary_relpath(task)
    generic = verifier_private.get(CODE_CHALLENGE_ENV)
    legacy = verifier_private.get(BENCH_PASSWORD_ENV) or verifier_private.get("bench_password")
    if generic and legacy and str(generic) != str(legacy):
        return Verdict(
            task_id=task.id,
            passed=False,
            reason="verifier-private challenge aliases do not match",
            proof="",
        )
    challenge = generic or legacy
    if not challenge:
        return Verdict(
            task_id=task.id,
            passed=False,
            reason=f"verifier-private missing {CODE_CHALLENGE_ENV} or {BENCH_PASSWORD_ENV}",
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
            str(hidden_suite_dir.resolve()): "/suite:ro",
            str(out.resolve()): "/artifact:ro",
        },
        env={
            CODE_CHALLENGE_ENV: str(challenge),
            BENCH_PASSWORD_ENV: str(challenge),
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
