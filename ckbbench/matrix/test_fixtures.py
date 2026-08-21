"""Shared SYNTHETIC run fixtures for matrix tests (NOT real benchmark data)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ckbbench.run.mcp_surface import profile_for_arm
from ckbbench.run.metrics import RunMetrics
from ckbbench.run.result import RESULT_SCHEMA_VERSION, RunResult, write_result

# The synthetic model path these fixtures describe. The matrix conftest injects a reviewed profile
# with exactly these values, so tests never depend on the real tracked profile.
SYNTHETIC_MODEL = "Opus"
SYNTHETIC_RESPONSE_MODEL = "synthetic-gpt"
SYNTHETIC_PROFILE_SHA256 = "1" * 64

_DEFAULT_SYNTHETIC_LIMITS: dict[str, Any] = {
    "step_limit": 80,
    "cost_limit": 0.0,
    "wall_time_limit_seconds": 900,
}


def synthetic_run_dict(
    *,
    suite_semver: str = "1.0.0-synthetic",
    chain: str = "devnet",
    arm: str = "B",
    model: str = SYNTHETIC_MODEL,
    seed: int = 1,
    run_id: str | None = None,
    outcome: str = "pass",
    suite_freeze_hash: str = "synthetic-freeze-abc",
    mcp_server_version: str = "1.6.12",
    agent_limits: dict[str, Any] | None = None,
    mcp_surface_profile: str | None = None,
    model_profile_id: str = "phase1-gpt-v6",
    model_profile_sha256: str = SYNTHETIC_PROFILE_SHA256,
    model_response_id: str | None = SYNTHETIC_RESPONSE_MODEL,
    metrics: RunMetrics | None = None,
) -> dict[str, Any]:
    """One SYNTHETIC run row matching the Phase 4 JSON schema."""
    rid = run_id or f"synthetic-{chain}-{arm}-{model}-s{seed}"
    return RunResult(
        schema_version=RESULT_SCHEMA_VERSION,
        suite_semver=suite_semver,
        chain=chain,
        arm=arm,
        model=model,
        seed=seed,
        run_id=rid,
        suite_freeze_hash=suite_freeze_hash,
        mcp_server_version=mcp_server_version,
        # Defaults to the arm's fixed profile so a row is valid unless a test deliberately
        # mismatches it; `""` is preserved so a blank-provenance test can reach the validator.
        mcp_surface_profile=(
            profile_for_arm(arm) if mcp_surface_profile is None else mcp_surface_profile
        ),
        outcome=outcome,  # type: ignore[arg-type]
        total_score=10 if outcome == "pass" else 0,
        max_score=10,
        tasks=(),
        model_profile_id=model_profile_id,
        model_profile_sha256=model_profile_sha256,
        model_response_id=model_response_id,
        metrics=metrics if metrics is not None else RunMetrics(
            total_wall_seconds=1.0,
            prompt_tokens=70,
            completion_tokens=30,
            total_tokens=100,
            model_calls=2,
            provider_attempts=2,
            provider_responses=2,
            token_usage_status="complete",
        ),
        # `is None`, not truthiness: an explicitly empty mapping is a caller's malformed fixture and
        # must reach the validator, not be replaced by a valid default.
        agent_limits=_DEFAULT_SYNTHETIC_LIMITS.copy() if agent_limits is None else agent_limits,
    ).to_dict()


def write_synthetic_results(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    """Persist SYNTHETIC JSON files under a temp results directory."""
    dest = tmp_path / "results"
    dest.mkdir(parents=True, exist_ok=True)
    for row in rows:
        result = RunResult.from_dict(row)
        write_result(result, dest)
    return dest


def load_rows_from_dir(results_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("*.json")):
        out.append(json.loads(path.read_text()))
    return out
