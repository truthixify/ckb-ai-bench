"""Run result schema tests: stable JSON + outcome classification (RECOMMENDATION §4)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from ckbbench.run.metrics import RunMetrics
from ckbbench.run.result import (
    RESULT_SCHEMA_VERSION,
    RunResult,
    TaskOutcome,
    task_outcomes_from_verdicts,
    write_result,
)
import pytest

from ckbbench.suite.model import OnchainVerifierSpec, Task
from ckbbench.verify.onchain import Verdict


def _sample_result() -> RunResult:
    return RunResult(
        schema_version=RESULT_SCHEMA_VERSION,
        suite_semver="1.0.0",
        chain="devnet",
        arm="C",
        model="openai/grok",
        seed=42,
        run_id="run-abc",
        suite_freeze_hash="deadbeef",
        mcp_server_version="1.6.12",
        outcome="pass",
        total_score=15,
        max_score=15,
        tasks=(
            TaskOutcome(
                task_id="task-a",
                passed=True,
                score=10,
                score_awarded=10,
                reason="ok",
                proof="0x1",
            ),
        ),
        metrics=RunMetrics(total_wall_seconds=3.5, total_tokens=1200),
        agent_limits={
            "step_limit": 80,
            "cost_limit": 0.0,
            "wall_time_limit_seconds": 900,
        },
        agent_exit_status="Submitted",
        preflight_server_version="1.6.12",
    )


def _result(*, arm: str, outcome: str = "pass", mcp_surface_profile: str | None = None,
            agent_limits: dict | None = None) -> RunResult:
    """A sample row for one arm, defaulting to the empty limits of a pre-agent failure."""
    base = _sample_result()
    empty = {"step_limit": None, "cost_limit": None, "wall_time_limit_seconds": None}
    return RunResult(
        **{
            **{f.name: getattr(base, f.name) for f in dataclasses.fields(base)},
            "arm": arm,
            "outcome": outcome,
            "mcp_surface_profile": mcp_surface_profile,
            "agent_limits": (
                agent_limits
                if agent_limits is not None
                else (empty if outcome == "infra_fail" else base.agent_limits)
            ),
            "tasks": () if outcome == "infra_fail" else base.tasks,
        }
    )


def test_to_dict_has_schema_version_and_cell_keys():
    d = _sample_result().to_dict()
    assert d["schema_version"] == RESULT_SCHEMA_VERSION
    for key in ("suite_semver", "chain", "arm", "model", "seed", "run_id"):
        assert d[key] is not None
    assert d["outcome"] == "pass"
    assert d["suite_freeze_hash"] == "deadbeef"
    assert d["mcp_server_version"] == "1.6.12"
    assert d["agent_limits"]["step_limit"] == 80
    assert d["agent_limits"]["wall_time_limit_seconds"] == 900
    assert d["metrics"]["total_wall_seconds"] == 3.5
    assert d["metrics"]["total_tokens"] == 1200


def test_round_trip_from_dict():
    original = _sample_result()
    restored = RunResult.from_dict(original.to_dict())
    assert restored == original


def test_round_trip_none_tokens():
    original = _sample_result()
    data = original.to_dict()
    data["metrics"]["total_tokens"] = None
    restored = RunResult.from_dict(data)
    assert restored.metrics.total_tokens is None


def test_from_dict_normalizes_missing_agent_limits_for_old_raw_rows():
    """Raw store validation rejects missing limits; from_dict only keeps legacy helpers loadable."""
    data = _sample_result().to_dict()
    del data["agent_limits"]
    restored = RunResult.from_dict(data)
    assert restored.agent_limits == {
        "step_limit": None,
        "cost_limit": None,
        "wall_time_limit_seconds": None,
    }


def test_write_result_writes_stable_json(tmp_path: Path):
    result = _sample_result()
    path = write_result(result, tmp_path)
    assert path == tmp_path / "run-abc.json"
    loaded = json.loads(path.read_text())
    assert loaded["schema_version"] == RESULT_SCHEMA_VERSION
    assert loaded["run_id"] == "run-abc"


def test_task_outcomes_from_verdicts():
    tasks = (
        Task(
            id="t1",
            prompt_fragment="x",
            score=5,
            proof_file="p.txt",
            kind="onchain",
            verifier=OnchainVerifierSpec(check="constant_hex", rpc_method="constant"),
        ),
    )
    verdicts = [Verdict(task_id="t1", passed=False, reason="bad", proof="0x0")]
    rows = task_outcomes_from_verdicts(tasks, verdicts)
    assert len(rows) == 1
    assert rows[0].score_awarded == 0
    assert not rows[0].passed


# --- mcp_surface_profile provenance (schema 1.2.0, ADR-0013) -------------------------------------

def test_schema_version_is_the_bumped_one():
    """The serialized shape changed, so the version must say so rather than reuse 1.1.0."""
    assert RESULT_SCHEMA_VERSION == "1.2.0"


@pytest.mark.parametrize("arm,profile", [
    ("A", "off"), ("B", "off"), ("C", "docs-only-v1"), ("D", "docs-only-v1"),
])
def test_surface_profile_round_trips_deterministically(arm, profile):
    result = _result(arm=arm, mcp_surface_profile=profile)
    data = result.to_dict()
    assert data["mcp_surface_profile"] == profile
    assert RunResult.from_dict(data).mcp_surface_profile == profile
    assert RunResult.from_dict(data).to_dict() == data


@pytest.mark.parametrize("arm,profile", [
    ("A", "off"), ("B", "off"), ("C", "docs-only-v1"), ("D", "docs-only-v1"),
])
def test_a_pre_agent_infra_row_still_records_the_configured_profile(arm, profile):
    """Unlike agent_limits, the surface is a methodology choice known before the agent exists."""
    row = _result(arm=arm, outcome="infra_fail", mcp_surface_profile=profile).to_dict()
    assert row["mcp_surface_profile"] == profile
    assert row["agent_limits"] == {
        "step_limit": None, "cost_limit": None, "wall_time_limit_seconds": None,
    }


def test_a_legacy_row_parses_with_no_profile_rather_than_an_inferred_one():
    """from_dict stays explicit; whether such a row may build a report is the validator's call."""
    legacy = _result(arm="C", mcp_surface_profile="docs-only-v1").to_dict()
    legacy["schema_version"] = "1.1.0"
    del legacy["mcp_surface_profile"]
    assert RunResult.from_dict(legacy).mcp_surface_profile is None


def test_the_profile_field_carries_no_endpoint_or_secret():
    serialized = json.dumps(_result(arm="C", mcp_surface_profile="docs-only-v1").to_dict())
    for leak in ("http://", "https://", "api_key", "Authorization", "mcp.ckbdev"):
        assert leak not in serialized
