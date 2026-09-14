import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.validate_hidden_suites import (
    MAX_CANDIDATE_BYTES,
    HiddenSuiteError,
    compile_hidden_suites,
    _diagnostics,
    external_directory,
    validate_candidate,
    validate_suite,
)


def test_direct_cli_can_import_its_sibling_modules():
    completed = subprocess.run(
        (sys.executable, "scripts/validate_hidden_suites.py", "--help"),
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_current_hidden_verifiers_are_all_compiled_offline(tmp_path: Path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    count = compile_hidden_suites(
        Path("suites/ckb-core-v3"),
        tmp_path / "cargo",
        run=run,
    )
    assert count == 10
    assert len(calls) == 10
    for argv, kwargs in calls:
        assert argv[-3:] == ("--offline", "--no-run", "--quiet")
        assert kwargs["env"]["CARGO_NET_OFFLINE"] == "true"
        assert Path(kwargs["env"]["CARGO_TARGET_DIR"]).is_relative_to(tmp_path)


def test_external_directory_rejects_repository_root_ancestors_and_descendants(tmp_path: Path):
    from scripts.validate_hidden_suites import ROOT

    for unsafe in ("/", ROOT, ROOT.parent, ROOT / "generated"):
        with pytest.raises(HiddenSuiteError):
            external_directory(unsafe, "test path")
    assert external_directory(tmp_path, "test path") == tmp_path.resolve()


def test_candidate_must_be_a_bounded_regular_file(tmp_path: Path):
    good = tmp_path / "good"
    good.write_bytes(b"binary")
    validate_candidate(good, "candidate")

    empty = tmp_path / "empty"
    empty.touch()
    with pytest.raises(HiddenSuiteError):
        validate_candidate(empty, "candidate")

    large = tmp_path / "large"
    large.write_bytes(b"x" * (MAX_CANDIDATE_BYTES + 1))
    with pytest.raises(HiddenSuiteError):
        validate_candidate(large, "candidate")

    link = tmp_path / "link"
    link.symlink_to(good)
    with pytest.raises(HiddenSuiteError):
        validate_candidate(link, "candidate")


def test_hidden_suite_gate_requires_structured_diagnostic_counts():
    completed = subprocess.CompletedProcess(
        args=("cargo", "test"),
        returncode=101,
        stdout=(
            "test result: FAILED. 3 passed; 2 failed; 0 ignored; "
            "0 measured; 0 filtered out; finished in 0.00s\n"
        ),
        stderr="",
    )
    diagnostic = _diagnostics(completed, "mutant")
    assert diagnostic.criteria_passed == 3
    assert diagnostic.criteria_failed == 2

    completed.stdout = "test result: FAILED. malformed\n"
    with pytest.raises(HiddenSuiteError, match="diagnostic counts"):
        _diagnostics(completed, "mutant")


def test_clean_release_without_a_candidate_bundle_is_compile_only(tmp_path: Path, monkeypatch):
    suite_root = tmp_path / "suite"
    suite_root.mkdir()
    cargo_root = tmp_path / "cargo"
    fixture_root = tmp_path / "fixtures"
    task = SimpleNamespace(
        id="task-code",
        kind="code",
        proof_file="build/release/proof",
        verifier="hidden",
    )
    monkeypatch.setattr(
        "scripts.validate_hidden_suites.load_suite",
        lambda _root: SimpleNamespace(tasks=[task], pins=SimpleNamespace()),
    )

    assert validate_suite(suite_root, cargo_root, fixture_root) == (0, 0)
    assert not cargo_root.exists()
    assert not fixture_root.exists()


def test_compile_gate_rejects_a_declared_hidden_verifier_without_manifest(
    tmp_path: Path, monkeypatch
):
    suite_root = tmp_path / "suite"
    hidden = suite_root / "task-code" / "hidden"
    (hidden / "src").mkdir(parents=True)
    task = SimpleNamespace(
        id="task-code",
        kind="code",
        verifier="hidden",
    )
    monkeypatch.setattr(
        "scripts.validate_hidden_suites.load_suite",
        lambda _root: SimpleNamespace(tasks=[task]),
    )

    with pytest.raises(HiddenSuiteError, match="missing Cargo.toml"):
        compile_hidden_suites(suite_root, tmp_path / "cargo")
