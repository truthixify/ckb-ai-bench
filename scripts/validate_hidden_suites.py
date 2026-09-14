#!/usr/bin/env python3
"""Run binary code candidates against hidden suites and compile released verifiers."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from ckbbench.suite.freeze import hash_task_dir
from ckbbench.suite.model import ProjectVerifierSpec
from ckbbench.suite.registry import load_suite
from ckbbench.verify.codetask import (
    BENCH_PASSWORD_ENV,
    CODE_CHALLENGE_ENV,
    parse_libtest_diagnostics,
)
from ckbbench.verify.diagnostics import VerificationDiagnostics

if __package__:
    from scripts.validate_suite_candidates import qualification_bundle_sha256
else:
    from validate_suite_candidates import qualification_bundle_sha256


ROOT = Path(__file__).resolve().parents[1]
MAX_CANDIDATE_BYTES = 1 << 20
OFFLINE_CHALLENGE = "offline-hidden-suite-challenge"
VERIFIER_TIMEOUT_SECONDS = 300
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class HiddenSuiteError(RuntimeError):
    """A hidden-suite gate invariant failed."""


def external_directory(raw: str | Path, label: str) -> Path:
    if not str(raw).strip():
        raise HiddenSuiteError(f"{label} resolved to an empty path")
    unresolved = Path(raw).expanduser()
    if unresolved.is_symlink():
        raise HiddenSuiteError(f"{label} must not be a symlink")
    path = unresolved.resolve()
    root = ROOT.resolve()
    if path == Path(path.anchor):
        raise HiddenSuiteError(f"{label} must not be the filesystem root")
    if path == root or path in root.parents or root in path.parents:
        raise HiddenSuiteError(f"{label} must be outside the repository: {path}")
    return path


def validate_candidate(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise HiddenSuiteError(f"{label} is not a regular file: {path}")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise HiddenSuiteError(f"{label} is not a regular file: {path}")
    size = path.stat().st_size
    if size == 0 or size > MAX_CANDIDATE_BYTES:
        raise HiddenSuiteError(
            f"{label} is {size} bytes; expected 1..{MAX_CANDIDATE_BYTES}"
        )


def _run_candidate(
    *,
    candidate: Path,
    proof_name: str,
    hidden_dir: Path,
    cargo_target: Path,
    fixture_root: Path,
) -> subprocess.CompletedProcess[str]:
    fixture = Path(tempfile.mkdtemp(prefix="hidden-suite-", dir=fixture_root))
    try:
        destination = fixture / "build" / "release" / proof_name
        destination.parent.mkdir(parents=True)
        shutil.copyfile(candidate, destination)
        destination.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                BENCH_PASSWORD_ENV: OFFLINE_CHALLENGE,
                CODE_CHALLENGE_ENV: OFFLINE_CHALLENGE,
                "CARGO_NET_OFFLINE": "true",
                "CARGO_TARGET_DIR": str(cargo_target),
                "MODE": "release",
                "TOP": str(fixture),
            }
        )
        try:
            return subprocess.run(
                ("cargo", "test", "--release", "--locked", "--offline", "--quiet"),
                cwd=hidden_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=VERIFIER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise HiddenSuiteError(
                f"hidden verifier exceeded {VERIFIER_TIMEOUT_SECONDS} seconds"
            ) from exc
    finally:
        shutil.rmtree(fixture)


def _failure_tail(completed: subprocess.CompletedProcess[str]) -> str:
    lines = (completed.stdout + completed.stderr).splitlines()
    return "\n".join(lines[-20:])


def _diagnostics(
    completed: subprocess.CompletedProcess[str],
    label: str,
) -> VerificationDiagnostics:
    diagnostic = parse_libtest_diagnostics(
        completed.stdout + completed.stderr,
        completed.returncode,
    )
    if diagnostic.status != "complete":
        raise HiddenSuiteError(f"{label} did not produce trustworthy diagnostic counts")
    return diagnostic


def _release_has_candidates(suite_root: Path, code_tasks: list) -> bool:
    return any(
        (suite_root / task.id / "reference").exists()
        or (suite_root / task.id / "mutants").exists()
        for task in code_tasks
    )


def _candidate_bundle_for(
    suite_root: Path,
    code_tasks: list,
    candidate_root: Path | None,
) -> Path | None:
    """Choose legacy in-tree candidates or the separately pinned qualification bundle."""
    if candidate_root is None:
        default = ROOT / "benchmark-output" / "suite-qualification" / suite_root.name
        if default.is_dir():
            candidate_root = default
        elif _release_has_candidates(suite_root, code_tasks):
            candidate_root = suite_root
        else:
            return None
    raw = Path(candidate_root).expanduser()
    if raw.is_symlink() or not raw.is_dir():
        raise HiddenSuiteError(f"qualification candidate root is not a regular directory: {raw}")
    resolved = raw.resolve()
    if resolved == suite_root:
        if not _release_has_candidates(suite_root, code_tasks):
            raise HiddenSuiteError("released suite has no qualification candidates")
        return resolved
    if resolved in suite_root.parents or suite_root in resolved.parents:
        raise HiddenSuiteError("qualification candidate root must be separate from the suite")
    expected = load_suite(suite_root).pins.qualification_bundle_sha256
    if expected is None:
        raise HiddenSuiteError("suite does not pin its qualification bundle")
    if qualification_bundle_sha256(resolved) != expected:
        raise HiddenSuiteError("qualification bundle differs from the suite pin")
    return resolved


def validate_suite(
    suite_root: Path,
    cargo_root: Path,
    fixture_root: Path,
    *,
    candidate_root: Path | None = None,
) -> tuple[int, int]:
    suite_root = suite_root.resolve()
    suite = load_suite(suite_root)
    code_tasks = [task for task in suite.tasks if task.kind == "code"]
    if not code_tasks:
        raise HiddenSuiteError("suite has no code tasks")

    candidate_root = _candidate_bundle_for(suite_root, code_tasks, candidate_root)
    if candidate_root is None:
        return 0, 0

    cargo_root.mkdir(parents=True, exist_ok=True)
    fixture_root.mkdir(parents=True, exist_ok=True)
    reference_count = 0
    mutant_count = 0

    for task in code_tasks:
        task_dir = suite_root / task.id
        digest_before = hash_task_dir(task_dir)
        hidden_dir = task_dir / str(task.verifier)
        proof_name = Path(task.proof_file).name
        reference_root = candidate_root / task.id / "reference"
        # Source-tree candidates are exercised by validate_suite_candidates.py in the isolated
        # role images. This host gate deliberately handles only the legacy binary contract.
        if reference_root.is_dir() and (reference_root / "Makefile").is_file():
            continue
        reference = reference_root / proof_name
        validate_candidate(reference, f"{task.id} reference")

        cargo_target = cargo_root / task.id
        completed = _run_candidate(
            candidate=reference,
            proof_name=proof_name,
            hidden_dir=hidden_dir,
            cargo_target=cargo_target,
            fixture_root=fixture_root,
        )
        if completed.returncode != 0:
            raise HiddenSuiteError(
                f"{task.id} reference failed its hidden suite:\n{_failure_tail(completed)}"
            )
        combined = completed.stdout + completed.stderr
        if re.search(r"running [1-9][0-9]* tests", combined) is None:
            raise HiddenSuiteError(f"{task.id} reference ran no verifier tests")
        diagnostic = _diagnostics(completed, f"{task.id} reference")
        if diagnostic.criteria_failed or diagnostic.criteria_passed != diagnostic.criteria_total:
            raise HiddenSuiteError(f"{task.id} reference diagnostic contradicts its pass")
        reference_count += 1

        mutant_dir = candidate_root / task.id / "mutants"
        mutants = sorted(mutant_dir.iterdir()) if mutant_dir.is_dir() else []
        for mutant in mutants:
            validate_candidate(mutant, f"{task.id} mutant")
            completed = _run_candidate(
                candidate=mutant,
                proof_name=proof_name,
                hidden_dir=hidden_dir,
                cargo_target=cargo_target,
                fixture_root=fixture_root,
            )
            combined = completed.stdout + completed.stderr
            if completed.returncode == 0:
                raise HiddenSuiteError(f"{task.id} accepted mutant {mutant.name}")
            if "test result: FAILED" not in combined or "could not compile" in combined:
                raise HiddenSuiteError(
                    f"{task.id} mutant {mutant.name} did not reach verifier assertions:\n"
                    f"{_failure_tail(completed)}"
                )
            diagnostic = _diagnostics(completed, f"{task.id} mutant {mutant.name}")
            if diagnostic.criteria_failed == 0:
                raise HiddenSuiteError(
                    f"{task.id} mutant {mutant.name} diagnostic contradicts its failure"
                )
            mutant_count += 1

        if hash_task_dir(task_dir) != digest_before:
            raise HiddenSuiteError(f"hidden-suite gate modified {task_dir}")

    return reference_count, mutant_count


def compile_hidden_suites(
    suite_root: Path,
    cargo_root: Path,
    *,
    run: CommandRunner = subprocess.run,
) -> int:
    """Compile every Rust-backed hidden verifier without executing candidate-dependent tests."""
    suite_root = suite_root.resolve()
    suite = load_suite(suite_root)
    cargo_root.mkdir(parents=True, exist_ok=True)
    count = 0
    for task in suite.tasks:
        verifier_dir: str | None = None
        if task.kind == "code":
            verifier_dir = str(task.verifier)
        elif task.kind == "project" and isinstance(task.verifier, ProjectVerifierSpec):
            verifier_dir = task.verifier.verifier_dir
        if verifier_dir is None:
            continue
        hidden = suite_root / task.id / verifier_dir
        manifest = hidden / "Cargo.toml"
        if hidden.is_symlink() or not hidden.is_dir():
            raise HiddenSuiteError(f"{task.id} hidden verifier directory is missing")
        if manifest.is_symlink() or not manifest.is_file():
            raise HiddenSuiteError(f"{task.id} hidden verifier is missing Cargo.toml")
        before = hash_task_dir(suite_root / task.id)
        env = os.environ.copy()
        env.update({
            "CARGO_NET_OFFLINE": "true",
            "CARGO_TARGET_DIR": str(cargo_root / task.id),
        })
        try:
            completed = run(
                ("cargo", "test", "--release", "--locked", "--offline", "--no-run", "--quiet"),
                cwd=hidden,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=VERIFIER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise HiddenSuiteError(
                f"{task.id} hidden verifier compile exceeded {VERIFIER_TIMEOUT_SECONDS} seconds"
            ) from exc
        if completed.returncode != 0:
            raise HiddenSuiteError(
                f"{task.id} hidden verifier did not compile:\n{_failure_tail(completed)}"
            )
        if hash_task_dir(suite_root / task.id) != before:
            raise HiddenSuiteError(f"hidden verifier compile modified {task.id}")
        count += 1
    if count == 0:
        raise HiddenSuiteError("suite has no Rust-backed hidden verifiers")
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="suites/ckb-core-v2")
    parser.add_argument("--compile-suite", action="append", default=[])
    parser.add_argument("--cargo-target", default="/tmp/ckbbench-rust-target")
    parser.add_argument("--fixture-root", default="/tmp/ckbbench-rust-fixtures")
    parser.add_argument(
        "--candidate-root",
        help="private qualification bundle; omitted candidates are skipped for a clean release",
    )
    args = parser.parse_args()

    try:
        suite_root = (ROOT / args.suite).resolve() if not Path(args.suite).is_absolute() else Path(args.suite).resolve()
        cargo_root = external_directory(args.cargo_target, "cargo target")
        fixture_root = external_directory(args.fixture_root, "fixture root")
        references, mutants = validate_suite(
            suite_root,
            cargo_root,
            fixture_root,
            candidate_root=Path(args.candidate_root).expanduser()
            if args.candidate_root
            else None,
        )
        compiled = 0
        for raw_suite in args.compile_suite:
            compile_root = (
                (ROOT / raw_suite).resolve()
                if not Path(raw_suite).is_absolute()
                else Path(raw_suite).resolve()
            )
            compiled += compile_hidden_suites(
                compile_root,
                cargo_root / f"compile-{compiled}",
            )
    except (HiddenSuiteError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1

    suffix = f", {compiled} additional hidden verifiers compiled" if compiled else ""
    qualification = (
        f"{references} references passed, {mutants} mutants rejected"
        if references or mutants
        else "qualification candidates not present"
    )
    print(f"hidden suites: {qualification}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
