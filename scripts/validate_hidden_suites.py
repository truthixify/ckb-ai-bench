#!/usr/bin/env python3
"""Run released code-task references and semantic mutants against hidden suites."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from ckbbench.suite.freeze import hash_task_dir
from ckbbench.suite.registry import load_suite
from ckbbench.verify.codetask import BENCH_PASSWORD_ENV, CODE_CHALLENGE_ENV


ROOT = Path(__file__).resolve().parents[1]
MAX_CANDIDATE_BYTES = 1 << 20
OFFLINE_CHALLENGE = "offline-hidden-suite-challenge"
VERIFIER_TIMEOUT_SECONDS = 300


class HiddenSuiteError(RuntimeError):
    """A hidden-suite gate invariant failed."""


def external_directory(raw: str | Path, label: str) -> Path:
    if not str(raw).strip():
        raise HiddenSuiteError(f"{label} resolved to an empty path")
    path = Path(raw).expanduser().resolve()
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


def validate_suite(suite_root: Path, cargo_root: Path, fixture_root: Path) -> tuple[int, int]:
    suite_root = suite_root.resolve()
    suite = load_suite(suite_root)
    code_tasks = [task for task in suite.tasks if task.kind == "code"]
    if not code_tasks:
        raise HiddenSuiteError("suite has no code tasks")

    cargo_root.mkdir(parents=True, exist_ok=True)
    fixture_root.mkdir(parents=True, exist_ok=True)
    reference_count = 0
    mutant_count = 0

    for task in code_tasks:
        task_dir = suite_root / task.id
        digest_before = hash_task_dir(task_dir)
        hidden_dir = task_dir / str(task.verifier)
        proof_name = Path(task.proof_file).name
        reference = task_dir / "reference" / proof_name
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
        reference_count += 1

        mutant_dir = task_dir / "mutants"
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
            mutant_count += 1

        if hash_task_dir(task_dir) != digest_before:
            raise HiddenSuiteError(f"hidden-suite gate modified {task_dir}")

    return reference_count, mutant_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="suites/ckb-core-v1")
    parser.add_argument("--cargo-target", default="/tmp/ckbbench-rust-target")
    parser.add_argument("--fixture-root", default="/tmp/ckbbench-rust-fixtures")
    args = parser.parse_args()

    try:
        suite_root = (ROOT / args.suite).resolve() if not Path(args.suite).is_absolute() else Path(args.suite).resolve()
        cargo_root = external_directory(args.cargo_target, "cargo target")
        fixture_root = external_directory(args.fixture_root, "fixture root")
        references, mutants = validate_suite(suite_root, cargo_root, fixture_root)
    except (HiddenSuiteError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1

    print(f"hidden suites: {references} references passed, {mutants} mutants rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
