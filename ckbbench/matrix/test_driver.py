"""Driver tests: fake run_cell, paired seeds, append-run re-aggregation (RECOMMENDATION §7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ckbbench.matrix.build_site import build_site
from ckbbench.matrix.driver import MatrixGrid, paired_seeds_for_cell, rebuild_site, run_matrix
from ckbbench.matrix.metrics import build_dataset, line_series_for_chain
from ckbbench.matrix.store import load_results, suite_results_dir
from ckbbench.matrix.test_fixtures import synthetic_run_dict
from ckbbench.run.metrics import RunMetrics
from ckbbench.run.result import RESULT_SCHEMA_VERSION, RunResult, write_result
from ckbbench.suite.model import Suite, SuitePins
from ckbbench.suite.test_registry import build_registry
from ckbbench.suite.registry import load_suite


def _minimal_suite() -> Suite:
    return Suite(
        suite_semver="1.0.0-synthetic",
        chain_profile="devnet",
        mcp_server_version="1.6.12",
        tasks=(),
        pins=SuitePins(),
    )


def test_paired_seeds_same_across_arms():
    # The seed list is identical for every arm so C-B deltas are paired (RECOMMENDATION 7).
    assert paired_seeds_for_cell((10, 20)) == [10, 20]


def test_run_matrix_fake_run_cell_writes_and_renders(tmp_path: Path):
    registry = build_registry(tmp_path / "registry")
    suite = load_suite(registry)
    suite = Suite(
        suite_semver=suite.suite_semver,
        chain_profile=suite.chain_profile,
        mcp_server_version=suite.mcp_server_version,
        tasks=suite.tasks,
        pins=suite.pins,
    )

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
            model=model,
            seed=seed,
            run_id=f"fake-{chain}-{arm}-{model}-s{seed}",
            suite_freeze_hash="fake-freeze",
            mcp_server_version=suite_obj.mcp_server_version,
            outcome=outcome,
            total_score=10 if outcome == "pass" else 0,
            max_score=10,
            tasks=(),
            metrics=RunMetrics(total_wall_seconds=0.1, total_tokens=10),
        )
        write_result(result, results_dir)
        return result

    grid = MatrixGrid(
        models=("Opus",),
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
    assert seeds_seen[("Opus", "devnet")] == [42, 42]

    site = tmp_path / "site" / "index.html"
    assert site.is_file()
    html = site.read_text(encoding="utf-8")
    assert "bc-segment" in html


def test_run_matrix_passes_agent_factory_when_provided(tmp_path: Path):
    """agent_factory is forwarded in kwargs even with a fake run_cell seam."""
    received: list[bool] = []

    def fake_with_factory(**kwargs):
        received.append("agent_factory" in kwargs)
        result = RunResult(
            schema_version=RESULT_SCHEMA_VERSION,
            suite_semver="1.0.0-synthetic",
            chain="devnet",
            arm="B",
            model="Opus",
            seed=1,
            run_id="af-test",
            suite_freeze_hash="h",
            mcp_server_version="1.6.12",
            outcome="pass",
            total_score=10,
            max_score=10,
            tasks=(),
            metrics=RunMetrics(total_wall_seconds=0.0, total_tokens=None),
        )
        # The real run_cell persists its own result; the fake must too (the driver no longer
        # double-writes), so rebuild_site finds the artifact.
        write_result(result, kwargs["results_dir"])
        return result

    def fake_run_cell(*args, **kwargs):
        return fake_with_factory(**kwargs)

    suite = _minimal_suite()
    grid = MatrixGrid(models=("Opus",), chains=("devnet",), arms=("B",), seeds=(1,))
    run_matrix(
        suite,
        grid,
        registry_root=tmp_path,
        results_base=tmp_path,
        site_dir=tmp_path / "site",
        run_cell_fn=fake_run_cell,
        agent_factory=object(),
    )
    assert received == [True]


def test_run_matrix_requires_agent_factory_for_production_run_cell():
    suite = _minimal_suite()
    grid = MatrixGrid(models=("Opus",), chains=("devnet",), arms=("B",), seeds=(1,))
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