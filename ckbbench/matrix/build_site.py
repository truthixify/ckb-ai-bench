"""Deterministic reporting build step (ADR-0012).

Load flat JSON results, validate invariants, aggregate Pass@1, render static HTML to ``site/``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ckbbench.matrix.metrics import build_dataset
from ckbbench.matrix.render import write_site
from ckbbench.matrix.store import load_results, validate_results


_CANONICAL_RUN_TIMESTAMP = re.compile(r"-(\d{10})$")


def results_through_utc(results: list[dict[str, object]]) -> str:
    """Return a deterministic UTC data timestamp from canonical production run IDs.

    Production rows end their run ID with the Unix time captured when the cell starts. Using the
    newest such value gives the report a meaningful data vintage without making identical rebuilds
    differ by wall clock. Synthetic or legacy rows without that suffix remain explicit.
    """
    timestamps: list[int] = []
    for row in results:
        match = _CANONICAL_RUN_TIMESTAMP.search(str(row.get("run_id", "")))
        if match is None:
            continue
        value = int(match.group(1))
        try:
            datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        timestamps.append(value)
    if not timestamps:
        return "timestamp unavailable"
    return (
        datetime.fromtimestamp(max(timestamps), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_site_from_results_dir(
    results_dir: Path | str,
    site_dir: Path | str,
    *,
    synthetic: bool = False,
    generated_at: str | None = None,
) -> Path:
    """Load, validate, aggregate, and render the ladder chart site."""
    results = load_results(results_dir)
    validate_results(results)
    dataset = build_dataset(
        results,
        synthetic=synthetic,
        generated_at=(
            results_through_utc(results) if generated_at is None else generated_at
        ),
    )
    return write_site(site_dir, dataset)


def build_site(
    results_dir: Path | str,
    site_dir: Path | str,
    *,
    synthetic: bool = False,
    generated_at: str | None = None,
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
