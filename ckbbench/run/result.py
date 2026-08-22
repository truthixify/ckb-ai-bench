"""Frozen run result schema: one JSON artifact per cell (RECOMMENDATION §4/§5).

Flat JSON files are the source-of-truth artifact; no database. Outcome classification
at run level: pass, agent_fail, infra_fail, or protocol_violation.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ckbbench.run.devnet import DevnetState
from ckbbench.run.metrics import NOT_STARTED, RunMetrics
from ckbbench.suite.runparams import RUN_PARAMS_DERIVATION_VERSION
from ckbbench.verify.onchain import Verdict

# 1.1.0 adds devnet_state (managed per-cell chain provenance). 1.2.0 adds mcp_surface_profile, the
# configured model-visible MCP surface (ADR-0013). 1.3.0 adds the model profile, the returned model
# identity and provider token provenance (ADR-0014). from_dict still reads older rows, where the
# fields are simply absent; the raw-result validator refuses them for a current report.
# 1.4.0 adds `metrics.provider_failure_category`. 1.5.0 permits a correctness-scored row after one
# counted provider recovery attempt while keeping its token usage explicitly incomplete. 1.6.0
# records bounded-retry count, scheduled delay and sanitized failure counts. 1.7.0 records local
# history-compaction evidence so context reduction cannot be hidden behind an ordinary score. 1.8.0
# binds each row to deterministic seed-derived task material.
RESULT_SCHEMA_VERSION = "1.8.0"

RunOutcome = Literal["pass", "agent_fail", "infra_fail", "protocol_violation"]
AgentLimits = dict[str, int | float | None]


def _empty_agent_limits() -> AgentLimits:
    return {
        "step_limit": None,
        "cost_limit": None,
        "wall_time_limit_seconds": None,
    }


def _agent_limits_dict(raw: Any) -> AgentLimits:
    if not isinstance(raw, dict):
        return _empty_agent_limits()
    out = _empty_agent_limits()
    for key in out:
        out[key] = raw.get(key)
    return out


def _optional_int(raw: Any) -> int | None:
    return None if raw is None else int(raw)


def _metrics_from_dict(raw: Any) -> RunMetrics:
    """Parse one serialized metrics block. Absent newer fields stay explicit for legacy rows.

    A pre-1.4.0 row parses with `provider_failure_category=None` for direct inspection. Whether such
    a row may enter a report is the store validator's decision, not this reader's.
    """
    if not isinstance(raw, dict):
        raw = {}
    return RunMetrics(
        total_wall_seconds=float(raw["total_wall_seconds"]),
        total_tokens=_optional_int(raw.get("total_tokens")),
        prompt_tokens=_optional_int(raw.get("prompt_tokens")),
        completion_tokens=_optional_int(raw.get("completion_tokens")),
        model_calls=int(raw.get("model_calls", 0)),
        provider_attempts=int(raw.get("provider_attempts", 0)),
        provider_responses=int(raw.get("provider_responses", 0)),
        provider_retry_count=int(raw.get("provider_retry_count", 0)),
        provider_retry_delay_seconds=int(raw.get("provider_retry_delay_seconds", 0)),
        history_compaction_count=int(raw.get("history_compaction_count", 0)),
        history_dropped_groups=int(raw.get("history_dropped_groups", 0)),
        history_dropped_items=int(raw.get("history_dropped_items", 0)),
        history_max_prepared_bytes=int(raw.get("history_max_prepared_bytes", 0)),
        token_usage_status=raw.get("token_usage_status", NOT_STARTED),
        provider_failure_category=raw.get("provider_failure_category"),
        provider_failure_counts=(
            dict(raw.get("provider_failure_counts", {}))
            if isinstance(raw.get("provider_failure_counts", {}), dict) else {}
        ),
    )


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
    agent_limits: AgentLimits = field(default_factory=_empty_agent_limits)
    run_params_derivation: str = RUN_PARAMS_DERIVATION_VERSION
    # The arm's configured MCP surface. Known before the agent starts, so unlike agent_limits it is
    # recorded even on a pre-agent infra_fail. None only when parsing a pre-1.2.0 row.
    mcp_surface_profile: str | None = None
    # The reviewed model profile this cell ran under. Also known before the agent starts.
    model_profile_id: str | None = None
    model_profile_sha256: str | None = None
    # The one model identity every provider response reported. None when no response exists or the
    # identity was missing or drifted -- never guessed from the requested model.
    model_response_id: str | None = None
    agent_exit_status: str | None = None
    preflight_server_version: str | None = None
    # Present only for a managed Docker DevNet cell; None for TestNet, local runs and old rows.
    devnet_state: DevnetState | None = None

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
            "mcp_surface_profile": self.mcp_surface_profile,
            "model_profile_id": self.model_profile_id,
            "model_profile_sha256": self.model_profile_sha256,
            "model_response_id": self.model_response_id,
            "outcome": self.outcome,
            "agent_exit_status": self.agent_exit_status,
            "run_params_derivation": self.run_params_derivation,
            "preflight_server_version": self.preflight_server_version,
            "devnet_state": None if self.devnet_state is None else self.devnet_state.to_dict(),
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
            "agent_limits": _agent_limits_dict(self.agent_limits),
            "metrics": {
                "total_wall_seconds": self.metrics.total_wall_seconds,
                "model_calls": self.metrics.model_calls,
                "provider_attempts": self.metrics.provider_attempts,
                "provider_responses": self.metrics.provider_responses,
                "provider_retry_count": self.metrics.provider_retry_count,
                "provider_retry_delay_seconds": self.metrics.provider_retry_delay_seconds,
                "history_compaction_count": self.metrics.history_compaction_count,
                "history_dropped_groups": self.metrics.history_dropped_groups,
                "history_dropped_items": self.metrics.history_dropped_items,
                "history_max_prepared_bytes": self.metrics.history_max_prepared_bytes,
                "prompt_tokens": self.metrics.prompt_tokens,
                "completion_tokens": self.metrics.completion_tokens,
                "total_tokens": self.metrics.total_tokens,
                "token_usage_status": self.metrics.token_usage_status,
                "provider_failure_category": self.metrics.provider_failure_category,
                "provider_failure_counts": dict(sorted(self.metrics.provider_failure_counts.items())),
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
            metrics=_metrics_from_dict(metrics_raw),
            agent_limits=_agent_limits_dict(data.get("agent_limits")),
            run_params_derivation=data.get("run_params_derivation", ""),
            # Legacy rows carry no profile. Normalizing to None keeps direct parsing explicit; the
            # store validator, not this reader, decides whether such a row may enter a report.
            mcp_surface_profile=data.get("mcp_surface_profile"),
            model_profile_id=data.get("model_profile_id"),
            model_profile_sha256=data.get("model_profile_sha256"),
            model_response_id=data.get("model_response_id"),
            agent_exit_status=data.get("agent_exit_status"),
            preflight_server_version=data.get("preflight_server_version"),
            devnet_state=(
                None
                if data.get("devnet_state") is None
                else DevnetState.from_dict(data["devnet_state"])
            ),
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


class ResultPersistenceError(RuntimeError):
    """A run artifact could not be appended without replacing existing evidence."""


def write_result(result: RunResult, directory: Path | str) -> Path:
    """Atomically append one complete run artifact without replacing an existing file."""
    dest = Path(directory)
    dest.mkdir(parents=True, exist_ok=True)
    if not result.run_id or Path(result.run_id).name != result.run_id:
        raise ResultPersistenceError("run_id must be a non-empty filename-safe value")
    path = dest / f"{result.run_id}.json"
    payload = json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        dir=dest, prefix=f".{result.run_id}.", suffix=".tmp", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ResultPersistenceError(
                f"result artifact already exists for run_id {result.run_id!r}"
            ) from None
        directory_fd = os.open(dest, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return path
