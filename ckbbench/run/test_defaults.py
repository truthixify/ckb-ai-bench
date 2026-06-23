"""Tests for production docker run kwargs (CKBBENCH_DOCKER seam)."""

from __future__ import annotations

from pathlib import Path

from ckbbench.run.defaults import production_run_kwargs, use_docker


def test_use_docker_false_by_default(monkeypatch):
    monkeypatch.delenv("CKBBENCH_DOCKER", raising=False)
    assert use_docker() is False


def test_use_docker_true_when_set(monkeypatch):
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    assert use_docker() is True


def test_production_run_kwargs_empty_without_docker(monkeypatch):
    monkeypatch.delenv("CKBBENCH_DOCKER", raising=False)
    assert production_run_kwargs(arm="A", chain="devnet") == {}


def test_production_run_kwargs_includes_runner_and_violation_check(tmp_path: Path, monkeypatch):
    allowlist = tmp_path / "allowlist.A.devnet.built"
    allowlist.write_text("^192\\.168\\.0\\.73$\n", encoding="utf-8")
    monkeypatch.setenv("CKBBENCH_ALLOWLIST_FILE", str(allowlist))
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    kwargs = production_run_kwargs(arm="A", chain="devnet")
    assert set(kwargs) == {"runner", "violation_check"}
    assert callable(kwargs["runner"])
    assert callable(kwargs["violation_check"])