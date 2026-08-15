"""The repository artifact guard must never delete a file it did not create.

Absence at fixture setup is not ownership: a concurrent pytest process legitimately creates files
in `containers/proxy` while this one runs. These tests exercise the guard's logic against a
temporary directory, because writing a probe file into the real production directory would trip a
concurrently running suite's guard -- the very interleaving under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import conftest


def test_a_foreign_file_appearing_during_a_test_survives_the_guard(tmp_path, monkeypatch):
    """Simulates the interleaving: another process creates a file mid-test."""
    monkeypatch.setattr(conftest, "proxy_dir", lambda: tmp_path)
    before = conftest.allowlists()
    foreign = tmp_path / "allowlist.another-process.built"
    foreign.write_text("created by a different process\n")

    created = conftest.allowlists() - before
    assert foreign in created

    with pytest.raises(AssertionError, match="NOT removed"):
        assert not created, conftest.violation_message(created)

    assert foreign.exists(), "the guard deleted a file it did not create"


def test_the_guard_reports_without_deleting():
    """Static: the fixture must name the violation, not unlink it."""
    text = Path(conftest.__file__).read_text()
    assert "NOT removed" in text
    assert ".unlink(" not in text, "the guard removes files it may not own"


def test_the_guard_watches_the_production_proxy_directory():
    """The seam must not silently point the real fixture somewhere harmless."""
    assert conftest.proxy_dir() == Path(conftest.__file__).resolve().parent / "containers" / "proxy"
