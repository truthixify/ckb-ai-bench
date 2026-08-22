"""Matrix driver: model x chain x arm grid with paired seeds (ADR-0012, RECOMMENDATION §7).

Calls ``run_cell`` per cell, persists flat JSON, then validate + aggregate + render.
``run_cell`` is injectable so the driver is unit-testable without live LLM/docker/MCP.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ckbbench.config import ARMS
from ckbbench.matrix.build_site import build_site_from_results_dir
from ckbbench.matrix.store import (
    ResultSuiteContract,
    result_suite_contract,
    suite_results_dir,
)
from ckbbench.run.orchestrate import run_cell
from ckbbench.run.result import RunResult
from ckbbench.suite.model import Suite

RunCellFn = Callable[..., RunResult]


@dataclass(frozen=True)
class MatrixGrid:
    """Full matrix specification for one suite launch (RECOMMENDATION §7)."""

    models: tuple[str, ...]
    # None means "use the Suite's declared chain_profile". A cross-chain run must be explicit.
    chains: tuple[str, ...] | None = None
    arms: tuple[str, ...] = ARMS
    seeds: tuple[int, ...] = (1, 2, 3)


def paired_seeds_for_cell(seeds: Sequence[int]) -> list[int]:
    """The seed list is the SAME for every arm within a (model, chain) slice, so the C-B deltas
    are paired across conditions (RECOMMENDATION 7). Seeds do not vary by arm/model/chain."""
    return list(seeds)


def scheduled_cells(arms: Sequence[str], seeds: Sequence[int]) -> list[tuple[str, int]]:
    """Keep each seed block adjacent and alternate treatment order between blocks."""
    arm_order = tuple(arms)
    cells: list[tuple[str, int]] = []
    for index, seed in enumerate(paired_seeds_for_cell(seeds)):
        ordered_arms = arm_order if index % 2 == 0 else tuple(reversed(arm_order))
        cells.extend((arm, seed) for arm in ordered_arms)
    return cells


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

    suite_contract = result_suite_contract(suite, registry_root)
    chains = grid.chains if grid.chains is not None else (suite.chain_profile,)

    explicit_results = run_cell_kwargs.get("results_dir")
    artifact_dir = (
        Path(explicit_results)
        if explicit_results is not None
        else suite_results_dir(results_base, suite.suite_semver)
    )

    results: list[RunResult] = []
    for model in grid.models:
        for chain in chains:
            for arm, seed in scheduled_cells(grid.arms, grid.seeds):
                kwargs = dict(run_cell_kwargs)
                if agent_factory is not None:
                    kwargs["agent_factory"] = agent_factory
                # run_cell persists its own RunResult to results_dir, so the driver does NOT
                # double-write (grok-build): it just points run_cell at the suite's dir.
                kwargs.setdefault("results_dir", artifact_dir)
                result = run_cell_fn(
                    suite,
                    chain,
                    arm,
                    model,
                    seed,
                    registry_root=registry_root,
                    **kwargs,
                )
                results.append(result)

    rebuild_site(
        results_base,
        suite.suite_semver,
        site_dir,
        results_dir=artifact_dir,
        suite_contracts=(suite_contract,),
    )
    return results


def rebuild_site(
    results_base: Path | str,
    suite_semver: str,
    site_dir: Path | str,
    *,
    results_dir: Path | str | None = None,
    suite_contracts: tuple[ResultSuiteContract, ...] | None = None,
) -> Path:
    """Validate stored results for one suite and rebuild the static site."""
    resolved = (
        Path(results_dir)
        if results_dir is not None
        else suite_results_dir(results_base, suite_semver)
    )
    return build_site_from_results_dir(
        resolved,
        site_dir,
        suite_contracts=suite_contracts,
    )
