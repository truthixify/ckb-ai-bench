"""Repository-wide test guards.

`build_cell_allowlist()` and `compose_env_for_arm()` write into `containers/proxy` in production.
A test that writes there pollutes the working tree and makes concurrent test processes contend for
the same paths, so the suite proves it creates none.

The guard is deliberately NON-DESTRUCTIVE: absence at fixture setup is not ownership. Another
pytest process legitimately creating a file during this test must survive, so a violation is
reported and the offending path named, never unlinked.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def proxy_dir() -> Path:
    """The watched directory. A seam so the guard's own tests never touch the real one."""
    return Path(__file__).resolve().parent / "containers" / "proxy"


def allowlists(directory: Path | None = None) -> set[Path]:
    return set((directory or proxy_dir()).glob("allowlist.*.built"))


def violation_message(created: set[Path]) -> str:
    return (
        "an allowlist appeared in containers/proxy during this test. If this test wrote it, pass "
        "proxy_dir=/out_dir=tmp_path instead. The file is NOT removed: it may belong to a "
        f"concurrent process. Paths: {sorted(p.name for p in created)}"
    )


@pytest.fixture(autouse=True)
def _no_new_repository_allowlists():
    before = allowlists()
    yield
    created = allowlists() - before
    assert not created, violation_message(created)
