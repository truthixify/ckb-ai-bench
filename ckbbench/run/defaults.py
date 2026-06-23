"""Production run seams: docker runner + proxy violation check (ADR-0006)."""

from __future__ import annotations

import os

from ckbbench.run.proxy_log import make_violation_check
from ckbbench.run.runner import make_docker_runner


def use_docker() -> bool:
    """Return True when CKBBENCH_DOCKER=1 selects the production docker path."""
    return os.getenv("CKBBENCH_DOCKER", "0") == "1"


def production_run_kwargs(*, arm: str, chain: str) -> dict:
    """Return kwargs to pass to run_cell for a production docker run."""
    if not use_docker():
        return {}
    return {
        "runner": make_docker_runner(),
        "violation_check": make_violation_check(arm=arm, chain=chain),
    }