"""Frozen run result schema: one JSON artifact per cell (RECOMMENDATION §4/§5).

Flat JSON files are the source-of-truth artifact; no database. Outcome classification
at run level: pass, agent_fail, infra_fail, or protocol_violation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ckbbench.run.metrics import RunMetrics
from ckbbench.verify.onchain import Verdict

RESULT_SCHEMA_VERSION = "1.0.0"

RunOutcome = Literal["pass", "agent_fail", "infra_fail", "protocol_violation"]


@dataclass(frozen=True)
class TaskOutcome:
    """Per-task grade embedded in the run artifact.

    ``scored`` is False for PLACEHOLDER scaffolds: they run and report a verdict but award 0 and do
    not count toward the run's total/max (they must not inflate the headline)."""

    task_id: str
    passed: bool
    score: int
    score_awarded: int
    reason: str
    proof: str
    scored: bool = True


@dataclass(frozen=True)
class RunResult:
    """One scored run: the matrix cell plus grades and v1 metrics."""

    schema_version: str
    suite_semver: str
    chain: str
    arm: str
    model: str
    seed: int
    run_id: str
    suite_freeze_hash: str
    mcp_server_version: str
    outcome: RunOutcome
    total_score: int
    max_score: int
    tasks: tuple[TaskOutcome, ...]
    metrics: RunMetrics
    agent_exit_status: str | None = None
    preflight_server_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Stable, versioned JSON shape for persistence."""
        return {
            "schema_version": self.schema_version,
            "suite_semver": self.suite_semver,
            "chain": self.chain,
            "arm": self.arm,
            "model": self.model,
            "seed": self.seed,
            "run_id": self.run_id,
            "suite_freeze_hash": self.suite_freeze_hash,
            "mcp_server_version": self.mcp_server_version,
            "outcome": self.outcome,
            "agent_exit_status": self.agent_exit_status,
            "preflight_server_version": self.preflight_server_version,
            "total_score": self.total_score,
            "max_score": self.max_score,
            "tasks": [
                {
                    "task_id": t.task_id,
                    "passed": t.passed,
                    "score": t.score,
                    "score_awarded": t.score_awarded,
                    "reason": t.reason,
                    "proof": t.proof,
                    "scored": t.scored,
                }
                for t in self.tasks
            ],
            "metrics": {
                "total_wall_seconds": self.metrics.total_wall_seconds,
                "total_tokens": self.metrics.total_tokens,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunResult:
        """Round-trip helper for tests and downstream consumers."""
        metrics_raw = data.get("metrics", {})
        tasks_raw = data.get("tasks", [])
        return cls(
            schema_version=str(data["schema_version"]),
            suite_semver=str(data["suite_semver"]),
            chain=str(data["chain"]),
            arm=str(data["arm"]),
            model=str(data["model"]),
            seed=int(data["seed"]),
            run_id=str(data["run_id"]),
            suite_freeze_hash=str(data["suite_freeze_hash"]),
            mcp_server_version=str(data["mcp_server_version"]),
            outcome=data["outcome"],  # type: ignore[arg-type]
            total_score=int(data["total_score"]),
            max_score=int(data["max_score"]),
            tasks=tuple(
                TaskOutcome(
                    task_id=str(t["task_id"]),
                    passed=bool(t["passed"]),
                    score=int(t["score"]),
                    score_awarded=int(t["score_awarded"]),
                    reason=str(t["reason"]),
                    proof=str(t["proof"]),
                    scored=bool(t.get("scored", True)),
                )
                for t in tasks_raw
            ),
            metrics=RunMetrics(
                total_wall_seconds=float(metrics_raw["total_wall_seconds"]),
                total_tokens=(
                    None
                    if metrics_raw.get("total_tokens") is None
                    else int(metrics_raw["total_tokens"])
                ),
            ),
            agent_exit_status=data.get("agent_exit_status"),
            preflight_server_version=data.get("preflight_server_version"),
        )


def task_outcomes_from_verdicts(
    tasks: tuple[Any, ...],
    verdicts: list[Verdict],
) -> tuple[TaskOutcome, ...]:
    """Map verifier Verdicts to scored TaskOutcome rows."""
    by_id = {t.id: t for t in tasks}
    out: list[TaskOutcome] = []
    for v in verdicts:
        task = by_id.get(v.task_id)
        score = task.score if task is not None else 0
        scored = task.scored if task is not None else True
        # Unscored scaffolds award 0 and contribute nothing to the headline, even on a pass.
        awarded = score if (v.passed and scored) else 0
        out.append(
            TaskOutcome(
                task_id=v.task_id,
                passed=v.passed,
                score=score if scored else 0,
                score_awarded=awarded,
                reason=v.reason,
                proof=v.proof,
                scored=scored,
            )
        )
    return tuple(out)


def write_result(result: RunResult, directory: Path | str) -> Path:
    """Write one flat JSON file per run; return the path."""
    dest = Path(directory)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{result.run_id}.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n")
    return path