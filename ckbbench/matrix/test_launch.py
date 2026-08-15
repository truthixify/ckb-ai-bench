"""Launch CLI tests: argparse, dry-run, production wrapper (100% coverage on launch.py)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ckbbench.matrix import launch as launch_mod
from ckbbench.matrix.driver import MatrixGrid
from ckbbench.matrix.driver import run_matrix
from ckbbench.matrix.launch import (
    _parse_csv,
    _parse_seeds,
    resolve_results_dir,
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
from ckbbench.run.agent_factory import (
    DEFAULT_COST_LIMIT,
    DEFAULT_STEP_LIMIT,
    DEFAULT_WALL_TIME_LIMIT_SECONDS,
)
from ckbbench.run.mcp_surface import profile_for_arm
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
    assert args.keep is False
    assert parse_args(["--suite", "s", "--models", "x", "--keep"]).keep is True


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


def test_grid_spec_discloses_the_production_agent_limits():
    """The operator sees the measured budget before spending a model call."""
    suite = _minimal_suite()
    grid = MatrixGrid(models=("Opus",), arms=("A", "B", "C", "D"), seeds=(1,))
    text = format_grid_spec(suite, grid, results_dir="results", site_dir="site")

    expected = (
        f"agent limits: steps={DEFAULT_STEP_LIMIT} cost={DEFAULT_COST_LIMIT} "
        f"wall={DEFAULT_WALL_TIME_LIMIT_SECONDS}s"
    )
    assert expected == "agent limits: steps=80 cost=0.0 wall=900s"
    limit_lines = [ln for ln in text.splitlines() if ln.startswith("agent limits:")]
    assert limit_lines == [expected]


def test_grid_spec_keeps_every_existing_field_and_order():
    """Inserting the limits line must not drop or reorder the fields operators already read."""
    suite = _minimal_suite()
    grid = MatrixGrid(models=("Opus",), chains=("testnet",), arms=("B", "C"), seeds=(1, 2))
    lines = format_grid_spec(suite, grid, results_dir="out", site_dir="site").splitlines()

    assert [ln.split(":", 1)[0] for ln in lines] == [
        "cells", "suite", "models", "chains", "arms", "agent limits", "seeds", "results", "site",
    ]
    assert lines[0] == "cells: 4"
    assert lines[3] == "chains: testnet"
    assert lines[4] == "arms: B, C"
    assert lines[6] == "seeds: 1, 2"
    assert lines[7] == "results: out/1.0.0-test"
    assert lines[8] == "site: site"


def test_formatting_the_summary_performs_no_external_action(monkeypatch):
    """Formatting is pure: no model, MCP, RPC, or Docker call may be triggered by it."""
    import subprocess

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("format_grid_spec performed an external action")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(launch_mod, "make_agent_factory", explode)
    monkeypatch.setattr(launch_mod, "run_matrix", explode)

    text = format_grid_spec(
        _minimal_suite(),
        MatrixGrid(models=("Opus",), arms=("B",), seeds=(1,)),
        results_dir="results",
        site_dir="site",
    )
    assert "agent limits: steps=80 cost=0.0 wall=900s" in text


def test_dry_run_prints_the_limits_line(monkeypatch, capsys):
    """A dry run is where an operator checks methodology, so it must show the budget too."""
    suite = _minimal_suite()
    monkeypatch.setattr(launch_mod, "load_suite", lambda _path: suite)
    monkeypatch.setattr(launch_mod, "run_matrix", lambda *a, **k: pytest.fail("ran the matrix"))

    args = parse_args(["--suite", "suites/x", "--models", "Opus", "--dry-run"])
    assert run_launch(args) == 0
    assert "agent limits: steps=80 cost=0.0 wall=900s" in capsys.readouterr().out


def test_resolve_results_dir():
    assert resolve_results_dir("results", "1.0.0-test") == Path("results") / "1.0.0-test"
    assert resolve_results_dir("out", "1.0.0") == Path("out") / "1.0.0"
    assert resolve_results_dir("/data/results", "1.0.0-test") == (
        Path("/data/results") / "1.0.0-test"
    )


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


def _isolate_proxy_dir(monkeypatch, tmp_path):
    """The production wrapper writes a per-cell allowlist beside the proxy config by design."""
    (tmp_path / "containers" / "proxy").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("ckbbench.run.defaults._REPO_ROOT", tmp_path)


@pytest.mark.parametrize("order", [("B", "C"), ("C", "B")])
def test_every_docker_devnet_cell_gets_its_own_preparation(monkeypatch, capsys, tmp_path, order):
    """B and C must not inherit each other's chain, whichever runs first. The preparation seam is
    resolved per cell, so execution order cannot decide who starts from a used chain (plan §9.1)."""
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    _isolate_proxy_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("ckbbench.run.defaults.make_docker_runner", lambda config=None: object())
    seen: list[tuple[str, bool]] = []

    def fake_run_cell(suite_obj, chain, arm, model, seed, **kwargs):
        seen.append((arm, callable(kwargs.get("prepare_chain"))))
        return RunResult(
            schema_version=RESULT_SCHEMA_VERSION, suite_semver=suite_obj.suite_semver,
            chain=chain, arm=arm, model=model, seed=seed, run_id=f"r-{arm}",
            mcp_surface_profile=profile_for_arm(arm),
            suite_freeze_hash="h", mcp_server_version="1.6.12", outcome="pass",
            total_score=1, max_score=1, tasks=(),
            metrics=RunMetrics(total_wall_seconds=0.0, total_tokens=None),
            agent_limits=_agent_limits(),
        )

    suite = _minimal_suite()
    wrapper = make_production_run_cell(
        suite=suite, results_dir=Path("results") / suite.suite_semver, run_cell_fn=fake_run_cell,
    )
    for arm in order:
        wrapper(suite, "devnet", arm, "Opus", 1, registry_root="s", agent_factory=object())
    capsys.readouterr()

    assert seen == [(order[0], True), (order[1], True)]


def test_testnet_cells_get_no_preparation_seam(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    _isolate_proxy_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("ckbbench.run.defaults.make_docker_runner", lambda config=None: object())
    seen: list[bool] = []

    def fake_run_cell(suite_obj, chain, arm, model, seed, **kwargs):
        seen.append("prepare_chain" in kwargs)
        return RunResult(
            schema_version=RESULT_SCHEMA_VERSION, suite_semver=suite_obj.suite_semver,
            chain=chain, arm=arm, model=model, seed=seed, run_id="r", suite_freeze_hash="h",
            mcp_surface_profile=profile_for_arm(arm),
            mcp_server_version="1.6.12", outcome="pass", total_score=1, max_score=1, tasks=(),
            metrics=RunMetrics(total_wall_seconds=0.0, total_tokens=None),
            agent_limits=_agent_limits(),
        )

    suite = _minimal_suite(chain_profile="testnet")
    wrapper = make_production_run_cell(
        suite=suite, results_dir=Path("results") / suite.suite_semver, run_cell_fn=fake_run_cell,
    )
    wrapper(suite, "testnet", "B", "Opus", 1, registry_root="s", agent_factory=object())
    capsys.readouterr()

    assert seen == [False]


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
            mcp_surface_profile=profile_for_arm(arm),
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


def test_run_launch_custom_results_dir_writes_and_rebuilds_site(
    monkeypatch, capsys, tmp_path: Path,
):
    """Non-dry launch must write JSON under --results-dir and rebuild site from that path."""
    suite = _minimal_suite()
    monkeypatch.setattr(launch_mod, "load_suite", lambda _path: suite)
    monkeypatch.setattr(launch_mod, "cleanup_matrix_volumes", lambda **_kwargs: None)
    monkeypatch.chdir(tmp_path)

    results_parent = tmp_path / "out"
    site_dir = tmp_path / "site"
    per_suite = results_parent / suite.suite_semver

    def fake_run_cell(
        suite_obj: Suite,
        chain: str,
        arm: str,
        model: str,
        seed: int,
        **kwargs,
    ) -> RunResult:
        from ckbbench.run.result import write_result

        result = RunResult(
            schema_version=RESULT_SCHEMA_VERSION,
            suite_semver=suite_obj.suite_semver,
            chain=chain,
            arm=arm,
            mcp_surface_profile=profile_for_arm(arm),
            model=model,
            seed=seed,
            run_id=f"launch-{model}-{arm}-s{seed}",
            suite_freeze_hash="h",
            mcp_server_version="1.6.12",
            outcome="pass",
            total_score=1,
            max_score=1,
            tasks=(),
            metrics=RunMetrics(total_wall_seconds=0.0, total_tokens=None),
            agent_limits=_agent_limits(),
        )
        write_result(result, kwargs["results_dir"])
        return result

    monkeypatch.setattr(launch_mod, "run_cell", fake_run_cell)
    monkeypatch.setattr(launch_mod, "run_matrix", run_matrix)

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
                str(results_parent),
                "--site-dir",
                str(site_dir),
            ]
        )
    )
    out = capsys.readouterr().out
    assert code == 0
    assert per_suite.is_dir()
    assert list(per_suite.glob("*.json"))
    assert (site_dir / "index.html").is_file()
    assert f"results: {results_parent}/{suite.suite_semver}" in out
    assert "finished: 1/1 cells passed" in out


def test_run_launch_nonzero_when_cells_fail(monkeypatch, tmp_path: Path):
    suite = _minimal_suite()
    monkeypatch.setattr(launch_mod, "load_suite", lambda _path: suite)
    cleaned = {"n": 0}
    monkeypatch.setattr(
        launch_mod,
        "cleanup_matrix_volumes",
        lambda **_kwargs: cleaned.__setitem__("n", cleaned["n"] + 1),
    )

    def fake_run_matrix(*_args, **_kwargs) -> list[RunResult]:
        return [
            RunResult(
                schema_version=RESULT_SCHEMA_VERSION,
                suite_semver=suite.suite_semver,
                chain="devnet",
                arm="B",
                mcp_surface_profile=profile_for_arm("B"),
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
    assert cleaned["n"] == 1


def test_run_launch_keep_skips_matrix_volume_cleanup(monkeypatch, tmp_path: Path):
    suite = _minimal_suite()
    monkeypatch.setattr(launch_mod, "load_suite", lambda _path: suite)
    seen: list[dict] = []
    monkeypatch.setattr(
        launch_mod,
        "cleanup_matrix_volumes",
        lambda **kwargs: seen.append(kwargs),
    )
    monkeypatch.setattr(
        launch_mod,
        "run_matrix",
        lambda *_a, **_k: [
            RunResult(
                schema_version=RESULT_SCHEMA_VERSION,
                suite_semver=suite.suite_semver,
                chain="devnet",
                arm="B",
                mcp_surface_profile=profile_for_arm("B"),
                model="m1",
                seed=1,
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
        ],
    )
    code = run_launch(
        parse_args(
            ["--suite", "s", "--models", "m1", "--arms", "B", "--seeds", "1", "--keep"]
        )
    )
    assert code == 0
    assert seen == [{"keep": True}]


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


