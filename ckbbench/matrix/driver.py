"""Matrix driver: model x chain x arm grid with paired seeds (ADR-0012, RECOMMENDATION §7).

Calls ``run_cell`` per cell, persists flat JSON, then validate + aggregate + render.
``run_cell`` is injectable so the driver is unit-testable without live LLM/docker/MCP.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ckbbench.config import ARMS, CHAIN_PROFILES
from ckbbench.matrix.build_site import build_site_from_results_dir
from ckbbench.matrix.store import load_results, persist_result, suite_results_dir, validate_results
from ckbbench.run.orchestrate import run_cell
from ckbbench.run.result import RunResult
from ckbbench.suite.model import Suite

RunCellFn = Callable[..., RunResult]


@dataclass(frozen=True)
class MatrixGrid:
    """Full matrix specification for one suite launch (RECOMMENDATION §7)."""

    models: tuple[str, ...]
    chains: tuple[str, ...] = CHAIN_PROFILES
    arms: tuple[str, ...] = ARMS
    seeds: tuple[int, ...] = (1, 2, 3)


def paired_seeds_for_cell(*, seeds: Sequence[int], arm: str, model: str, chain: str) -> list[int]:
    """Return the same seed list for every arm so C-B deltas are paired across conditions."""
    _ = (arm, model, chain)  # explicit: seeds do not vary by arm within a (model, chain) slice
    return list(seeds)


def run_matrix(
    suite: Suite,
    grid: MatrixGrid,
    *,
    registry_root: Path | str,
    results_base: Path | str,
    site_dir: Path | str,
    run_cell_fn: RunCellFn = run_cell,
    agent_factory: Any = None,
    **run_cell_kwargs: Any,
) -> list[RunResult]:
    """Execute the full matrix, persist JSON, validate, aggregate, and render the site."""
    if agent_factory is None and run_cell_fn is run_cell:
        raise ValueError("agent_factory is required when using the production run_cell")

    results: list[RunResult] = []
    for model in grid.models:
        for chain in grid.chains:
            for arm in grid.arms:
                for seed in paired_seeds_for_cell(
                    seeds=grid.seeds, arm=arm, model=model, chain=chain
                ):
                    kwargs = dict(run_cell_kwargs)
                    if agent_factory is not None:
                        kwargs["agent_factory"] = agent_factory
                    kwargs.setdefault(
                        "results_dir",
                        suite_results_dir(results_base, suite.suite_semver),
                    )
                    result = run_cell_fn(
                        suite,
                        chain,
                        arm,
                        model,
                        seed,
                        registry_root=registry_root,
                        **kwargs,
                    )
                    persist_result(result, results_base)
                    results.append(result)

    rebuild_site(results_base, suite.suite_semver, site_dir)
    return results


def rebuild_site(
    results_base: Path | str,
    suite_semver: str,
    site_dir: Path | str,
) -> Path:
    """Validate stored results for one suite and rebuild the static site."""
    results_dir = suite_results_dir(results_base, suite_semver)
    raw = load_results(results_dir)
    validate_results(raw)
    return build_site_from_results_dir(results_dir, site_dir)