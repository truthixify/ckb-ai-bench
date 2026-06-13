"""Deterministic reporting build step (ADR-0012).

Load flat JSON results, validate invariants, aggregate Pass@1, render static HTML to ``site/``.
"""

from __future__ import annotations

from pathlib import Path

from ckbbench.matrix.metrics import build_dataset
from ckbbench.matrix.render import write_site
from ckbbench.matrix.store import load_results, validate_results


def build_site_from_results_dir(
    results_dir: Path | str,
    site_dir: Path | str,
    *,
    synthetic: bool = False,
    generated_at: str = "deterministic",
) -> Path:
    """Load, validate, aggregate, and render the ladder chart site."""
    results = load_results(results_dir)
    validate_results(results)
    dataset = build_dataset(
        results,
        synthetic=synthetic,
        generated_at=generated_at,
    )
    return write_site(site_dir, dataset)


def build_site(
    results_dir: Path | str,
    site_dir: Path | str,
    *,
    synthetic: bool = False,
    generated_at: str = "deterministic",
) -> Path:
    """Public alias for the reporting build entry point."""
    return build_site_from_results_dir(
        results_dir,
        site_dir,
        synthetic=synthetic,
        generated_at=generated_at,
    )


def main() -> None:
    """CLI entry: ``python -m ckbbench.matrix.build_site <results_dir> <site_dir>``."""
    import sys

    if len(sys.argv) != 3:
        print(
            "usage: python -m ckbbench.matrix.build_site <results_dir> <site_dir>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    path = build_site(sys.argv[1], sys.argv[2])
    print(f"wrote {path}")


if __name__ == "__main__":
    main()