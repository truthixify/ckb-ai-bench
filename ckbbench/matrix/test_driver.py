"""Driver tests: fake run_cell, paired seeds, append-run re-aggregation (RECOMMENDATION §7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ckbbench.matrix.build_site import build_site
from ckbbench.matrix.driver import (
    MatrixGrid,
    paired_seeds_for_cell,
    rebuild_site,
    run_matrix,
    scheduled_cells,
)
from ckbbench.matrix.metrics import build_dataset, line_series_for_chain
from ckbbench.matrix.store import load_results, result_suite_contract, suite_results_dir
from ckbbench.matrix.test_fixtures import (
    SYNTHETIC_MODEL,
    SYNTHETIC_RESPONSE_MODEL,
    SYNTHETIC_SUITE_FREEZE,
    SYNTHETIC_SUITE_SEMVER,
    SYNTHETIC_TASK_ID,
    synthetic_run_dict,
)
from ckbbench.run.mcp_surface import profile_for_arm
from ckbbench.run.metrics import RunMetrics
from ckbbench.run.result import RESULT_SCHEMA_VERSION, RunResult, TaskOutcome, write_result
from ckbbench.suite.model import OnchainVerifierSpec, Suite, SuitePins, Task


def _phase_one_provenance(arm: str) -> dict:
    """The model provenance a production cell records, for a stand-in run_cell (ADR-0014)."""
    return {
        "mcp_surface_profile": profile_for_arm(arm),
        "model_profile_id": "model-profile-synthetic-v1",
        "model_profile_sha256": "1" * 64,
        "model_response_id": SYNTHETIC_RESPONSE_MODEL,
    }


def _complete_metrics(wall: float = 1.0) -> RunMetrics:
    return RunMetrics(
        total_wall_seconds=wall, prompt_tokens=70, completion_tokens=30, total_tokens=100,
        model_calls=2, provider_attempts=2, provider_responses=2, token_usage_status="complete",
    )



def _minimal_suite() -> Suite:
    return Suite(
        suite_semver=SYNTHETIC_SUITE_SEMVER,
        chain_profile="devnet",
        mcp_server_version="1.6.12",
        tasks=(
            Task(
                id=SYNTHETIC_TASK_ID,
                prompt_fragment="Synthetic task.",
                score=10,
                proof_file="synthetic.txt",
                kind="onchain",
                verifier=OnchainVerifierSpec(check="constant_hex", rpc_method="constant"),
            ),
        ),
        pins=SuitePins(),
    )


def _suite_fixture(tmp_path: Path) -> tuple[Suite, Path, str]:
    suite = _minimal_suite()
    registry = tmp_path / "registry"
    for task in suite.tasks:
        task_dir = registry / task.id
        task_dir.mkdir(parents=True)
        (task_dir / "fixture.txt").write_text("synthetic task source\n", encoding="utf-8")
    contract = result_suite_contract(suite, registry)
    return suite, registry, contract.suite_freeze_hash


def _task_rows(passed: bool) -> tuple[TaskOutcome, ...]:
    return (
        TaskOutcome(
            task_id=SYNTHETIC_TASK_ID,
            passed=passed,
            score=10,
            score_awarded=10 if passed else 0,
            reason="synthetic verdict",
            proof="synthetic proof",
        ),
    )


def _agent_limits() -> dict[str, int | float]:
    return {
        "step_limit": 80,
        "cost_limit": 0.0,
        "wall_time_limit_seconds": 900,
    }


def test_paired_seeds_same_across_arms():
    # The seed list is identical for every arm so C-B deltas are paired (RECOMMENDATION 7).
    assert paired_seeds_for_cell((10, 20)) == [10, 20]


def test_cells_are_seed_blocked_and_treatment_order_is_counterbalanced():
    assert scheduled_cells(("B", "C"), (1, 2, 3)) == [
        ("B", 1), ("C", 1), ("C", 2), ("B", 2), ("B", 3), ("C", 3),
    ]


def test_run_matrix_defaults_to_suite_chain_profile(tmp_path: Path):
    """A devnet-authored suite must not silently generate testnet-labeled cells."""
    suite, registry, suite_freeze = _suite_fixture(tmp_path)
    seen_chains: list[str] = []

    def fake_run_cell(
        suite_obj: Suite,
        chain: str,
        arm: str,
        model: str,
        seed: int,
        *,
        results_dir: Path,
        **kwargs,
    ) -> RunResult:
        seen_chains.append(chain)
        result = RunResult(
            schema_version=RESULT_SCHEMA_VERSION,
            suite_semver=suite_obj.suite_semver,
            chain=chain,
            arm=arm,
            **_phase_one_provenance(arm),
            model=model,
            seed=seed,
            run_id=f"default-chain-{chain}-{arm}",
            suite_freeze_hash=suite_freeze,
            mcp_server_version=suite_obj.mcp_server_version,
            outcome="pass",
            total_score=10,
            max_score=10,
            tasks=_task_rows(True),
            metrics=_complete_metrics(0.0),
            agent_limits=_agent_limits(),
            agent_exit_status="Submitted",
        )
        write_result(result, results_dir)
        return result

    run_matrix(
        suite,
        MatrixGrid(models=(SYNTHETIC_MODEL,), arms=("B",), seeds=(1,)),
        registry_root=registry,
        results_base=tmp_path,
        site_dir=tmp_path / "site",
        run_cell_fn=fake_run_cell,
    )

    assert seen_chains == [suite.chain_profile]


def test_run_matrix_chain_override_reaches_run_cell(tmp_path: Path):
    """--chains overrides the suite default, and that concrete value is what run_cell (and through
    it the agent factory) must receive: falling back to suite.chain_profile downstream would point
    the agent at a different chain than the cell is labelled with (plan §8.1)."""
    suite, registry, suite_freeze = _suite_fixture(tmp_path)
    assert suite.chain_profile == "devnet"
    seen_chains: list[str] = []

    def fake_run_cell(
        suite_obj: Suite,
        chain: str,
        arm: str,
        model: str,
        seed: int,
        *,
        results_dir: Path,
        **kwargs,
    ) -> RunResult:
        seen_chains.append(chain)
        result = RunResult(
            schema_version=RESULT_SCHEMA_VERSION,
            suite_semver=suite_obj.suite_semver,
            chain=chain,
            arm=arm,
            **_phase_one_provenance(arm),
            model=model,
            seed=seed,
            run_id=f"override-chain-{chain}-{arm}",
            suite_freeze_hash=suite_freeze,
            mcp_server_version=suite_obj.mcp_server_version,
            outcome="pass",
            total_score=10,
            max_score=10,
            tasks=_task_rows(True),
            metrics=_complete_metrics(0.0),
            agent_limits=_agent_limits(),
            agent_exit_status="Submitted",
        )
        write_result(result, results_dir)
        return result

    run_matrix(
        suite,
        MatrixGrid(models=(SYNTHETIC_MODEL,), chains=("testnet",), arms=("B",), seeds=(1,)),
        registry_root=registry,
        results_base=tmp_path,
        site_dir=tmp_path / "site",
        run_cell_fn=fake_run_cell,
    )

    assert seen_chains == ["testnet"]


def test_run_matrix_fake_run_cell_writes_and_renders(tmp_path: Path):
    suite, registry, suite_freeze = _suite_fixture(tmp_path)

    seeds_seen: dict[tuple[str, str], list[int]] = {}

    def fake_run_cell(
        suite_obj: Suite,
        chain: str,
        arm: str,
        model: str,
        seed: int,
        *,
        registry_root: Path,
        results_dir: Path,
        **kwargs,
    ) -> RunResult:
        key = (model, chain)
        seeds_seen.setdefault(key, []).append(seed)
        outcome = "pass" if arm in ("B", "C") else "agent_fail"
        result = RunResult(
            schema_version=RESULT_SCHEMA_VERSION,
            suite_semver=suite_obj.suite_semver,
            chain=chain,
            arm=arm,
            **_phase_one_provenance(arm),
            model=model,
            seed=seed,
            run_id=f"fake-{chain}-{arm}-{model.replace('/', '-')}-s{seed}",
            suite_freeze_hash=suite_freeze,
            mcp_server_version=suite_obj.mcp_server_version,
            outcome=outcome,
            total_score=10 if outcome == "pass" else 0,
            max_score=10,
            tasks=_task_rows(outcome == "pass"),
            metrics=_complete_metrics(0.1),
            agent_limits=_agent_limits(),
            agent_exit_status="Submitted",
        )
        write_result(result, results_dir)
        return result

    grid = MatrixGrid(
        models=(SYNTHETIC_MODEL,),
        chains=("devnet",),
        arms=("B", "C"),
        seeds=(42,),
    )
    results = run_matrix(
        suite,
        grid,
        registry_root=registry,
        results_base=tmp_path,
        site_dir=tmp_path / "site",
        run_cell_fn=fake_run_cell,
    )
    assert len(results) == 2
    assert seeds_seen[(SYNTHETIC_MODEL, "devnet")] == [42, 42]

    site = tmp_path / "site" / "index.html"
    assert site.is_file()
    html = site.read_text(encoding="utf-8")
    assert "C − B" in html
    assert "Inconclusive" in html
    # A sparse cohort publishes its arithmetic but is never promoted to a headline.
    assert "Correctness provisional" in html
    assert "Observed positive difference" not in html


def test_run_matrix_passes_agent_factory_when_provided(tmp_path: Path):
    """agent_factory is forwarded in kwargs even with a fake run_cell seam."""
    suite, registry, suite_freeze = _suite_fixture(tmp_path)
    received: list[bool] = []

    def fake_with_factory(**kwargs):
        received.append("agent_factory" in kwargs)
        result = RunResult(
            schema_version=RESULT_SCHEMA_VERSION,
            suite_semver=SYNTHETIC_SUITE_SEMVER,
            chain="devnet",
            arm="B",
            **_phase_one_provenance("B"),
            model=SYNTHETIC_MODEL,
            seed=1,
            run_id="af-test",
            suite_freeze_hash=suite_freeze,
            mcp_server_version="1.6.12",
            outcome="pass",
            total_score=10,
            max_score=10,
            tasks=_task_rows(True),
            metrics=_complete_metrics(0.0),
            agent_limits=_agent_limits(),
            agent_exit_status="Submitted",
        )
        # The real run_cell persists its own result; the fake must too (the driver no longer
        # double-writes), so rebuild_site finds the artifact.
        write_result(result, kwargs["results_dir"])
        return result

    def fake_run_cell(*args, **kwargs):
        return fake_with_factory(**kwargs)

    grid = MatrixGrid(models=(SYNTHETIC_MODEL,), chains=("devnet",), arms=("B",), seeds=(1,))
    run_matrix(
        suite,
        grid,
        registry_root=registry,
        results_base=tmp_path,
        site_dir=tmp_path / "site",
        run_cell_fn=fake_run_cell,
        agent_factory=object(),
    )
    assert received == [True]


def test_run_matrix_requires_agent_factory_for_production_run_cell():
    suite = _minimal_suite()
    grid = MatrixGrid(models=(SYNTHETIC_MODEL,), chains=("devnet",), arms=("B",), seeds=(1,))
    with pytest.raises(ValueError, match="agent_factory"):
        run_matrix(
            suite,
            grid,
            registry_root=Path("/tmp/unused"),
            results_base=Path("/tmp/unused"),
            site_dir=Path("/tmp/unused"),
        )


def test_append_run_reaggregates_without_error(tmp_path: Path):
    rows = [
        synthetic_run_dict(arm="B", outcome="pass", run_id="b1"),
        synthetic_run_dict(arm="C", outcome="agent_fail", run_id="c1"),
    ]
    results_dir = tmp_path / "results" / "1.0.0-synthetic"
    results_dir.mkdir(parents=True)
    for row in rows:
        write_result(RunResult.from_dict(row), results_dir)

    rebuild_site(tmp_path, "1.0.0-synthetic", tmp_path / "site")
    ds1 = build_dataset(load_results(results_dir), synthetic=True)
    mean_c1 = line_series_for_chain(ds1, "devnet")[0]["points"]["C"]["mean"]

    extra = synthetic_run_dict(arm="C", outcome="pass", run_id="c2", seed=2)
    write_result(RunResult.from_dict(extra), results_dir)

    rebuild_site(tmp_path, "1.0.0-synthetic", tmp_path / "site2")
    ds2 = build_dataset(load_results(results_dir), synthetic=True)
    mean_c2 = line_series_for_chain(ds2, "devnet")[0]["points"]["C"]["mean"]
    assert mean_c2 > mean_c1


def test_build_site_end_to_end(tmp_path: Path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    row = synthetic_run_dict(arm="B")
    write_result(RunResult.from_dict(row), results_dir)
    path = build_site(results_dir, tmp_path / "site")
    assert path.exists()
