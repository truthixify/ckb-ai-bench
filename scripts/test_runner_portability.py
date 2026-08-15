"""`scripts/test.sh` must run on a stock host shell.

The runner is the project's documented local and CI entry point and declares `#!/usr/bin/env bash`,
so it has to work where the only bash is the system one. Expanding `"${arr[@]}"` on an EMPTY array
under `set -u` is an error before bash 4.4, and macOS ships 3.2, so `--no-cov` aborted the runner
before a single test ran. These tests execute the real script under `/bin/bash` with a stub
interpreter, so removing the safe forwarding fails them on such a host rather than only in review.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts" / "test.sh"
SYSTEM_BASH = Path("/bin/bash")
COV_FLAGS = ["--cov=ckbbench", "--cov=containers", "--cov-report=term-missing"]
UNSAFE_FORWARD = '"${cov[@]}"'
SAFE_FORWARD = '"${cov[@]+"${cov[@]}"}"'
# The version that made an empty array expansion legal under `set -u`.
SAFE_BASH = (4, 4)


def _bash_version(bash: Path) -> tuple[int, int]:
    out = subprocess.run([str(bash), "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'],
                         capture_output=True, text=True, check=True)
    major, minor = out.stdout.split()
    return int(major), int(minor)


def _pinned_python() -> str:
    for line in (REPO / ".tool-versions").read_text().splitlines():
        if line.startswith("python "):
            return line.split()[1]
    raise AssertionError("python is not pinned in .tool-versions")


def _stub_interpreter(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for `$PY` that satisfies the runner's preflight and records its pytest argv.

    Nothing real is executed: the point is to reach the pytest line under the shell being tested,
    not to run the suite again from inside itself.
    """
    argv = tmp_path / "pytest-argv.txt"
    stub = tmp_path / "python-stub"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        '  case "$2" in\n'
        f"    *python_version*) echo '{_pinned_python()}' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        f'printf "%s\\n" "$@" > "{argv}"\n'
        "exit 0\n")
    stub.chmod(0o755)
    return stub, argv


def _run(bash: Path, script: Path, args: list[str], tmp_path: Path) -> tuple[
        subprocess.CompletedProcess, list[str]]:
    stub, argv = _stub_interpreter(tmp_path)
    # A minimal PATH keeps cargo out of reach, so the rust layer skips and this stays a shell test.
    proc = subprocess.run(
        [str(bash), str(script), *args], cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path),
             "CKBBENCH_PYTHON": str(stub)})
    recorded = argv.read_text().splitlines() if argv.is_file() else []
    return proc, recorded


def test_the_system_shell_runs_the_runner_with_no_coverage(tmp_path: Path):
    """The exact case that aborted the release gate: `--no-cov` empties the array."""
    proc, recorded = _run(SYSTEM_BASH, RUNNER, ["--no-cov"], tmp_path)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "unbound variable" not in proc.stderr
    assert recorded == ["-m", "pytest"], recorded
    assert "ALL WIRED TEST LAYERS PASSED" in proc.stdout


def test_the_system_shell_still_forwards_the_coverage_flags(tmp_path: Path):
    """The safe form must forward a populated array unchanged, not silently drop coverage."""
    proc, recorded = _run(SYSTEM_BASH, RUNNER, [], tmp_path)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert recorded == ["-m", "pytest", *COV_FLAGS], recorded


def test_the_unsafe_forwarding_would_fail_on_this_shell(tmp_path: Path):
    """Proof the test above has teeth: the previous form aborts on a pre-4.4 shell.

    Run against a copy. The real runner is never mutated.
    """
    version = _bash_version(SYSTEM_BASH)
    if version >= SAFE_BASH:
        pytest.skip(f"/bin/bash is {version[0]}.{version[1]}; the unsafe form is legal here")
    # The runner resolves its own repository root, so the copy gets a minimal one of its own. Only
    # `.tool-versions` is read before the line under test, and it is linked read-only.
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".tool-versions").symlink_to(REPO / ".tool-versions")
    broken = tmp_path / "scripts" / "test.sh"
    shutil.copy2(RUNNER, broken)
    text = broken.read_text()
    assert text.count(SAFE_FORWARD) == 1
    broken.write_text(text.replace(SAFE_FORWARD, UNSAFE_FORWARD))
    proc, recorded = _run(SYSTEM_BASH, broken, ["--no-cov"], tmp_path)
    assert proc.returncode != 0
    assert "cov[@]: unbound variable" in proc.stderr
    assert recorded == []
    assert RUNNER.read_text().count(SAFE_FORWARD) == 1


def test_the_runner_uses_the_repository_safe_forwarding_pattern():
    text = RUNNER.read_text()
    assert SAFE_FORWARD in text
    assert not re.search(r'\$\{cov\[@\]\}(?!")', text.replace(SAFE_FORWARD, "")), (
        "scripts/test.sh still expands cov[@] without the empty-array guard")
    # The same pattern the operator CLI already uses to forward its own optional array.
    assert '"${extra[@]+"${extra[@]}"}"' in (REPO / "scripts" / "ckbbench").read_text()
