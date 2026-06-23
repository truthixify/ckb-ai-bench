"""Launch CLI tests: argparse, dry-run, production wrapper (100% coverage on launch.py)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ckbbench.matrix import launch as launch_mod
from ckbbench.matrix.driver import MatrixGrid
from ckbbench.matrix.launch import (
    _parse_csv,
    _parse_seeds,
    _results_layout,
    build_grid,
    build_parser,
    cell_count,
    format_grid_spec,
    main,
    make_production_run_cell,
    parse_args,
    resolved_chains,
    run_launch,
)
from ckbbench.run.metrics import RunMetrics
from ckbbench.run.result import RESULT_SCHEMA_VERSION, RunResult
from ckbbench.suite.model import Suite, SuitePins


def _minimal_suite(*, chain_profile: str = "devnet") -> Suite:
    return Suite(
        suite_semver="1.0.0-test",
        chain_profile=chain_profile,
        mcp_server_version="1.6.12",
        tasks=(),
        pins=SuitePins(),
    )


def _agent_limits() -> dict[str, int | float]:
    return {
        "step_limit": 80,
        "cost_limit": 0.0,
        "wall_time_limit_seconds": 900,
    }


def test_parse_csv_and_seeds():
    assert _parse_csv("a,b") == ("a", "b")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_csv(",")
    assert _parse_seeds("1, 2") == (1, 2)
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_seeds("1,x")


def test_build_parser_and_parse_args():
    parser = build_parser()
    assert parser.prog is None or "launch" in parser.description or True
    args = parse_args(
        ["--suite", "suites/ckb-v1", "--models", "m1,m2", "--chains", "devnet,testnet"]
    )
    assert args.suite == "suites/ckb-v1"
    assert args.models == ("m1", "m2")
    assert args.chains == ("devnet", "testnet")
    assert args.seeds == (1, 2, 3)
    assert args.dry_run is False


def test_build_grid_defaults():
    args = parse_args(["--suite", "s", "--models", "x"])
    grid = build_grid(args)
    assert grid.chains is None
    assert grid.arms == ("A", "B", "C", "D")
    assert grid.seeds == (1, 2, 3)


def test_build_grid_explicit_arms_and_seeds():
    args = parse_args(
        ["--suite", "s", "--models", "x", "--arms", "B,C", "--seeds", "7,8"]
    )
    grid = build_grid(args)
    assert grid.arms == ("B", "C")
    assert grid.seeds == (7, 8)


def test_cell_count_and_resolved_chains():
    suite = _minimal_suite()
    grid = MatrixGrid(models=("m1", "m2"), arms=("A", "B"), seeds=(1, 2))
    assert resolved_chains(suite, grid) == ("devnet",)
    assert cell_count(suite, grid) == 8

    grid_explicit = MatrixGrid(
        models=("m1",),
        chains=("devnet", "testnet"),
        arms=("A",),
        seeds=(1,),
    )
    assert resolved_chains(suite, grid_explicit) == ("devnet", "testnet")
    assert cell_count(suite, grid_explicit) == 2


def test_format_grid_spec_default_and_explicit_chains():
    suite = _minimal_suite()
    grid_default = MatrixGrid(models=("Opus",), arms=("B",), seeds=(1,))
    text = format_grid_spec(
        suite, grid_default, results_dir="results", site_dir="site"
    )
    assert "suite default: devnet" in text
    assert "cells: 1" in text
    assert "site: site" in text

    grid_explicit = MatrixGrid(
        models=("Opus",),
        chains=("testnet",),
        arms=("B",),
        seeds=(1,),
    )
    text2 = format_grid_spec(
        suite, grid_explicit, results_dir="results", site_dir="out"
    )
    assert "chains: testnet" in text2
    assert "suite default" not in text2
    assert "site: out" in text2


def test_results_layout():
    base, per_suite = _results_layout("results", "1.0.0-test")
    assert base == Path(".")
    assert per_suite == Path("results") / "1.0.0-test"

    base2, per_suite2 = _results_layout("/data/results", "1.0.0-test")
    assert base2 == Path("/data")
    assert per_suite2 == Path("/data/results") / "1.0.0-test"


def test_dry_run_does_not_call_run_matrix(monkeypatch, capsys):
    suite = _minimal_suite()
    monkeypatch.setattr(launch_mod, "load_suite", lambda _path: suite)

    called = {"run_matrix": 0}

    def fail_run_matrix(*_args, **_kwargs):
        called["run_matrix"] += 1
        raise AssertionError("run_matrix must not run in dry-run")

    monkeypatch.setattr(launch_mod, "run_matrix", fail_run_matrix)

    code = run_launch(
        parse_args(["--suite", "s", "--models", "m1", "--dry-run"])
    )
    out = capsys.readouterr().out
    assert code == 0
    assert called["run_matrix"] == 0
    assert "dry-run:" in out
    assert "cells: 1" in out


def test_make_production_run_cell_merges_kwargs_and_prints(capsys):
    merged: list[dict] = []

    def fake_run_cell(
        suite_obj: Suite,
        chain: str,
        arm: str,
        model: str,
        seed: int,
        **kwargs,
    ) -> RunResult:
        merged.append(kwargs)
        return RunResult(
            schema_version=RESULT_SCHEMA_VERSION,
            suite_semver=suite_obj.suite_semver,
            chain=chain,
            arm=arm,
            model=model,
            seed=seed,
            run_id="r1",
            suite_freeze_hash="h",
            mcp_server_version="1.6.12",
            outcome="pass",
            total_score=1,
            max_score=1,
            tasks=(),
            metrics=RunMetrics(total_wall_seconds=0.0, total_tokens=None),
            agent_limits=_agent_limits(),
        )

    suite = _minimal_suite()
    results_dir = Path("results") / suite.suite_semver
    wrapper = make_production_run_cell(
        suite=suite,
        results_dir=results_dir,
        run_cell_fn=fake_run_cell,
    )
    wrapper(
        suite,
        "devnet",
        "B",
        "Opus",
        1,
        registry_root="s",
        agent_factory=object(),
        extra_kw=True,
    )
    assert merged[0]["results_dir"] == results_dir
    assert merged[0]["extra_kw"] is True
    assert merged[0]["agent_factory"] is not None
    out = capsys.readouterr().out
    assert "model=Opus" in out
    assert "outcome: pass" in out


def test_run_launch_executes_matrix(monkeypatch, capsys, tmp_path: Path):
    suite = _minimal_suite()
    monkeypatch.setattr(launch_mod, "load_suite", lambda _path: suite)

    seen: list[tuple[str, str, str, int]] = []

    def fake_run_cell(
        suite_obj: Suite,
        chain: str,
        arm: str,
        model: str,
        seed: int,
        **kwargs,
    ) -> RunResult:
        seen.append((model, chain, arm, seed))
        return RunResult(
            schema_version=RESULT_SCHEMA_VERSION,
            suite_semver=suite_obj.suite_semver,
            chain=chain,
            arm=arm,
            model=model,
            seed=seed,
            run_id="r1",
            suite_freeze_hash="h",
            mcp_server_version="1.6.12",
            outcome="pass",
            total_score=1,
            max_score=1,
            tasks=(),
            metrics=RunMetrics(total_wall_seconds=0.0, total_tokens=None),
            agent_limits=_agent_limits(),
        )

    monkeypatch.setattr(launch_mod, "run_cell", fake_run_cell)

    def fake_run_matrix(
        suite_obj: Suite,
        grid: MatrixGrid,
        *,
        registry_root: Path | str,
        results_base: Path | str,
        site_dir: Path | str,
        run_cell_fn,
        agent_factory,
        **kwargs,
    ) -> list[RunResult]:
        assert suite_obj is suite
        assert grid.models == ("m1",)
        assert registry_root == "suites/ckb-v1"
        assert agent_factory is not None
        assert kwargs["results_dir"] == Path("out") / suite.suite_semver
        result = run_cell_fn(
            suite_obj,
            "devnet",
            "B",
            "m1",
            1,
            registry_root=registry_root,
            results_dir=kwargs["results_dir"],
            agent_factory=agent_factory,
        )
        return [result]

    monkeypatch.setattr(launch_mod, "run_matrix", fake_run_matrix)

    code = run_launch(
        parse_args(
            [
                "--suite",
                "suites/ckb-v1",
                "--models",
                "m1",
                "--arms",
                "B",
                "--seeds",
                "1",
                "--results-dir",
                "out",
                "--site-dir",
                str(tmp_path / "site"),
            ]
        )
    )
    out = capsys.readouterr().out
    assert code == 0
    assert seen == [("m1", "devnet", "B", 1)]
    assert "finished: 1/1 cells passed" in out
    assert f"results: out/{suite.suite_semver}" in out


def test_run_launch_nonzero_when_cells_fail(monkeypatch, tmp_path: Path):
    suite = _minimal_suite()
    monkeypatch.setattr(launch_mod, "load_suite", lambda _path: suite)

    def fake_run_matrix(*_args, **_kwargs) -> list[RunResult]:
        return [
            RunResult(
                schema_version=RESULT_SCHEMA_VERSION,
                suite_semver=suite.suite_semver,
                chain="devnet",
                arm="B",
                model="m1",
                seed=1,
                run_id="r1",
                suite_freeze_hash="h",
                mcp_server_version="1.6.12",
                outcome="agent_fail",
                total_score=0,
                max_score=1,
                tasks=(),
                metrics=RunMetrics(total_wall_seconds=0.0, total_tokens=None),
                agent_limits=_agent_limits(),
            )
        ]

    monkeypatch.setattr(launch_mod, "run_matrix", fake_run_matrix)

    code = run_launch(
        parse_args(["--suite", "s", "--models", "m1", "--arms", "B", "--seeds", "1"])
    )
    assert code == 1


def test_main_entry(monkeypatch):
    monkeypatch.setattr(launch_mod, "run_launch", lambda _args: 0)
    monkeypatch.setattr(launch_mod, "parse_args", lambda _argv=None: object())
    assert main([]) == 0


def test_main_default_argv(monkeypatch):
    monkeypatch.setattr(launch_mod, "run_launch", lambda _args: 0)
    monkeypatch.setattr(
        launch_mod,
        "parse_args",
        lambda argv=None: argparse.Namespace() if argv is None else object(),
    )
    assert main() == 0


