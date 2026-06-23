"""Production matrix launch CLI (ADR-0012).

Operators run the full benchmark grid without writing Python::

    python -m ckbbench.matrix.launch --suite suites/ckb-v1 --models m1,m2
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ckbbench.config import ARMS
from ckbbench.matrix.driver import MatrixGrid, run_matrix
from ckbbench.run.agent_factory import make_agent_factory
from ckbbench.run.defaults import production_run_kwargs
from ckbbench.run.orchestrate import run_cell
from ckbbench.run.result import RunResult
from ckbbench.suite.model import Suite
from ckbbench.suite.registry import load_suite


def _parse_csv(value: str) -> tuple[str, ...]:
    parts = tuple(item.strip() for item in value.split(",") if item.strip())
    if not parts:
        raise argparse.ArgumentTypeError("expected at least one comma-separated value")
    return parts


def _parse_seeds(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid seed list {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Construct the launch argument parser."""
    parser = argparse.ArgumentParser(
        description="Run the CKB AI Bench matrix (model x chain x arm x seed).",
    )
    parser.add_argument(
        "--suite",
        required=True,
        help="Suite registry root (e.g. suites/ckb-v1)",
    )
    parser.add_argument(
        "--models",
        required=True,
        type=_parse_csv,
        help="Comma-separated model names",
    )
    parser.add_argument(
        "--chains",
        default=None,
        type=_parse_csv,
        help="Comma-separated chain profiles (default: suite chain_profile)",
    )
    parser.add_argument(
        "--arms",
        default=None,
        type=_parse_csv,
        help=f"Comma-separated arms (default: {','.join(ARMS)})",
    )
    parser.add_argument(
        "--seeds",
        default="1,2,3",
        type=_parse_seeds,
        help="Comma-separated integer seeds (default: 1,2,3)",
    )
    parser.add_argument(
        "--results-dir",
        default="results",
        help="Results root directory containing per-suite semver subdirs (default: results)",
    )
    parser.add_argument(
        "--site-dir",
        default="site",
        help="Static report output directory (default: site)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cell count and grid spec without running",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    return build_parser().parse_args(list(argv) if argv is not None else None)


def build_grid(args: argparse.Namespace) -> MatrixGrid:
    """Build a MatrixGrid from parsed CLI arguments."""
    arms = tuple(args.arms) if args.arms is not None else ARMS
    return MatrixGrid(
        models=tuple(args.models),
        chains=tuple(args.chains) if args.chains is not None else None,
        arms=arms,
        seeds=tuple(args.seeds),
    )


def resolved_chains(suite: Suite, grid: MatrixGrid) -> tuple[str, ...]:
    """Return the chain list that will be executed for this grid."""
    if grid.chains is not None:
        return grid.chains
    return (suite.chain_profile,)


def cell_count(suite: Suite, grid: MatrixGrid) -> int:
    """Number of matrix cells in the launch grid."""
    chains = resolved_chains(suite, grid)
    return len(grid.models) * len(chains) * len(grid.arms) * len(grid.seeds)


def format_grid_spec(
    suite: Suite,
    grid: MatrixGrid,
    *,
    results_dir: str,
    site_dir: str,
) -> str:
    """Human-readable grid summary for dry-run and logging."""
    chains = resolved_chains(suite, grid)
    if grid.chains is None:
        chains_line = f"(suite default: {suite.chain_profile})"
    else:
        chains_line = ", ".join(chains)
    lines = [
        f"cells: {cell_count(suite, grid)}",
        f"suite: {suite.suite_semver} (chain_profile={suite.chain_profile})",
        f"models: {', '.join(grid.models)}",
        f"chains: {chains_line}",
        f"arms: {', '.join(grid.arms)}",
        f"seeds: {', '.join(str(s) for s in grid.seeds)}",
        f"results: {results_dir}/{suite.suite_semver}",
        f"site: {site_dir}",
    ]
    return "\n".join(lines)


def resolve_results_dir(results_dir: str, suite_semver: str) -> Path:
    """Map ``--results-dir`` to the per-suite artifact directory.

    ``--results-dir`` is the parent that holds semver subdirs (e.g. ``out`` -> ``out/1.0.0/``).
    """
    return Path(results_dir) / suite_semver


def make_production_run_cell(
    *,
    suite: Suite,
    results_dir: Path,
    run_cell_fn: Any | None = None,
) -> Any:
    """Return a run_cell wrapper that merges production kwargs and prints progress."""
    cell_runner = run_cell if run_cell_fn is None else run_cell_fn

    def production_run_cell(
        suite_obj: Suite,
        chain: str,
        arm: str,
        model: str,
        seed: int,
        **kwargs: Any,
    ) -> RunResult:
        t0 = time.time()
        merged = {
            **production_run_kwargs(
                arm=arm, chain=chain, suite=suite, log_since=t0
            ),
            **kwargs,
        }
        merged["results_dir"] = results_dir
        print(f"== cell == model={model} chain={chain} arm={arm} seed={seed}")
        result = cell_runner(suite_obj, chain, arm, model, seed, **merged)
        print(
            f"   outcome: {result.outcome} "
            f"score={result.total_score}/{result.max_score} "
            f"run_id={result.run_id}"
        )
        return result

    return production_run_cell


def run_launch(args: argparse.Namespace) -> int:
    """Execute the matrix launch (or dry-run) and return a process exit code."""
    suite = load_suite(args.suite)
    grid = build_grid(args)
    per_suite_results = resolve_results_dir(args.results_dir, suite.suite_semver)
    site_dir = Path(args.site_dir)

    spec = format_grid_spec(
        suite,
        grid,
        results_dir=args.results_dir,
        site_dir=str(site_dir),
    )

    if args.dry_run:
        print("dry-run:")
        print(spec)
        return 0

    print(spec)
    print()

    agent_factory = make_agent_factory()
    production_run_cell = make_production_run_cell(
        suite=suite,
        results_dir=per_suite_results,
    )

    results = run_matrix(
        suite,
        grid,
        registry_root=args.suite,
        results_base=Path("."),
        site_dir=site_dir,
        agent_factory=agent_factory,
        run_cell_fn=production_run_cell,
        results_dir=per_suite_results,
    )

    outcomes = [r.outcome for r in results]
    passed = sum(1 for o in outcomes if o == "pass")
    print()
    print(f"finished: {passed}/{len(results)} cells passed")
    print(f"results: {per_suite_results}")
    print(f"site: {site_dir / 'index.html'}")

    return 0 if passed == len(results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: ``python -m ckbbench.matrix.launch``."""
    args = parse_args(argv)
    return run_launch(args)


if __name__ == "__main__":
    raise SystemExit(main())