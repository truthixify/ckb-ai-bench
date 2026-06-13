"""Build-site entry tests and CLI guard."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ckbbench.matrix import build_site as build_site_mod
from ckbbench.matrix.build_site import build_site, build_site_from_results_dir
from ckbbench.matrix.test_fixtures import synthetic_run_dict, write_synthetic_results
from ckbbench.run.result import RunResult, write_result


def test_build_site_from_results_dir(tmp_path: Path):
    rows = [
        synthetic_run_dict(arm="B"),
        synthetic_run_dict(arm="C", run_id="c-run"),
    ]
    dest = write_synthetic_results(tmp_path, rows)
    path = build_site_from_results_dir(dest, tmp_path / "site", synthetic=True)
    html = path.read_text(encoding="utf-8")
    assert "SYNTHETIC" in html


def test_build_site_alias(tmp_path: Path):
    dest = tmp_path / "results"
    dest.mkdir()
    write_result(RunResult.from_dict(synthetic_run_dict()), dest)
    path = build_site(dest, tmp_path / "site2")
    assert path.name == "index.html"


def test_main_cli_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        build_site_mod.main()
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err


def test_main_cli_builds(tmp_path: Path, monkeypatch):
    dest = tmp_path / "results"
    dest.mkdir()
    write_result(RunResult.from_dict(synthetic_run_dict()), dest)
    site = tmp_path / "site"
    monkeypatch.setattr(sys, "argv", ["build_site", str(dest), str(site)])
    build_site_mod.main()
    assert (site / "index.html").is_file()