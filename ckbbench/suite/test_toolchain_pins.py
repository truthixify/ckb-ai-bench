"""The frozen toolchain pins must describe what the release images actually run.

`.tool-versions` is the single source of truth. These offline checks catch a
pin being replaced by a mutable major stream, an unversioned package install, or the removal of a
build-time assertion, without needing Docker.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL_VERSIONS = ROOT / ".tool-versions"
DOCKERFILES = (
    ROOT / "containers" / "agent.Dockerfile",
    ROOT / "containers" / "verifier.Dockerfile",
)


def _pinned(tool: str) -> str:
    for line in TOOL_VERSIONS.read_text().splitlines():
        if line.startswith(f"{tool} "):
            return line.split()[1]
    raise AssertionError(f"{tool} is not pinned in .tool-versions")


def test_tool_versions_declares_the_three_pins():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _pinned("nodejs"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", _pinned("rust"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", _pinned("python"))


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_node_arg_equals_the_single_source_of_truth(dockerfile: Path):
    text = dockerfile.read_text()
    declared = re.search(r"^ARG NODE_VERSION=(\S+)$", text, re.M)
    assert declared, f"{dockerfile.name} must declare an exact ARG NODE_VERSION"
    assert declared.group(1) == _pinned("nodejs"), (
        f"{dockerfile.name} pins Node {declared.group(1)}, .tool-versions says {_pinned('nodejs')}"
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_node_pin_is_exact_not_a_major_stream(dockerfile: Path):
    """A `setup_22.x` install resolves to whatever 22.x is current and cannot back a frozen pin."""
    text = dockerfile.read_text()
    assert "NODE_MAJOR" not in text, f"{dockerfile.name} still carries a major-only Node pin"
    assert "deb.nodesource.com" not in text, (
        f"{dockerfile.name} installs Node from the mutable NodeSource stream"
    )
    assert re.search(r"^\s*ARG NODE_VERSION=\d+\.\d+\.\d+$", text, re.M), (
        f"{dockerfile.name} must pin an exact three-part Node version"
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_asserts_the_node_version_at_build_time(dockerfile: Path):
    text = dockerfile.read_text()
    assert 'test "$(node --version)" = "v${NODE_VERSION}"' in text, (
        f"{dockerfile.name} must fail the build if the installed Node is not the pin"
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_installs_no_unversioned_nodejs_package(dockerfile: Path):
    text = dockerfile.read_text()
    assert not re.search(r"apt-get install[^\n]*\bnodejs\b", text), (
        f"{dockerfile.name} installs an unversioned nodejs package"
    )


def test_rust_base_image_matches_the_pinned_rust_major_minor():
    want = _pinned("rust")
    major_minor = ".".join(want.split(".")[:2])
    for dockerfile in DOCKERFILES:
        text = dockerfile.read_text()
        assert f"FROM rust:{major_minor}-slim" in text, (
            f"{dockerfile.name} does not build on the pinned rust {major_minor} base"
        )


def test_no_dockerfile_claims_a_pinned_python_runtime():
    for dockerfile in DOCKERFILES:
        text = dockerfile.read_text()
        assert not re.search(r"ARG PYTHON_VERSION", text), (
            f"{dockerfile.name} declares a Python pin that nothing asserts"
        )


REPO_ROOT = ROOT
BOOTSTRAP = REPO_ROOT / "scripts" / "ckbbench"
TEST_RUNNER = REPO_ROOT / "scripts" / "test.sh"
MATRIX_RUNNER = REPO_ROOT / "scripts" / "run-matrix.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"


def test_bootstrap_selects_the_exact_pinned_interpreter():
    """A 3.12.x that merely satisfies major.minor would make the frozen provenance false."""
    text = BOOTSTRAP.read_text()
    assert 'awk \'$1=="python"{print $2}\' "$REPO/.tool-versions"' in text, (
        "setup must read the python pin from the single source of truth"
    )
    assert "uv venv --python 3.12 .venv" not in text, "setup still accepts any 3.12"
    assert 'uv venv --python "$pinned_python" .venv' in text


def test_bootstrap_recreates_a_venv_that_is_not_the_pin():
    text = BOOTSTRAP.read_text()
    assert 'uv venv --clear --python "$pinned_python" .venv' in text, (
        "setup must repair an existing venv whose interpreter is not the pin"
    )


def test_release_test_runner_fails_when_its_runtime_is_not_the_pin():
    text = TEST_RUNNER.read_text()
    assert 'PINNED_PYTHON="$(awk \'$1=="python"{print $2}\' .tool-versions)"' in text
    assert '[ "$RUNTIME_PYTHON" != "$PINNED_PYTHON" ]' in text, (
        "the release gate must fail closed on a runtime that is not the pinned interpreter"
    )
    assert "not the pinned $PINNED_PYTHON" in text


def test_direct_matrix_runner_also_fails_when_its_runtime_is_not_the_pin():
    text = MATRIX_RUNNER.read_text()
    assert 'PINNED_PYTHON="$(awk \'$1=="python"{print $2}\' .tool-versions)"' in text
    assert '"$RUNTIME_PYTHON" != "$PINNED_PYTHON"' in text
    assert "is not the pinned" in text


def test_direct_matrix_runner_refuses_the_wrong_runtime_before_launch(tmp_path: Path):
    launched = tmp_path / "launched"
    stub = tmp_path / "python"
    stub.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then echo 3.12.13; exit 0; fi\n"
        f'touch "{launched}"\n'
    )
    stub.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(MATRIX_RUNNER), "--dry-run"],
        cwd=ROOT,
        env={**os.environ, "CKBBENCH_PYTHON": str(stub)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 2
    assert f"Python 3.12.13 is not the pinned {_pinned('python')}" in proc.stderr
    assert not launched.exists()


def test_ci_pins_the_exact_interpreter():
    text = CI_WORKFLOW.read_text()
    assert f'python-version: "{_pinned("python")}"' in text, (
        "CI must install the exact pinned interpreter, not the 3.12 series"
    )
    assert 'python-version: "3.12"' not in text


def test_ci_creates_the_venv_with_the_pinned_interpreter():
    """setup-python only makes it available; `uv venv` must actually select it."""
    text = CI_WORKFLOW.read_text()
    assert "uv venv .venv" not in text, "CI creates the venv without naming an interpreter"
    assert 'uv venv --python "$PINNED_PYTHON" .venv' in text
    assert 'PINNED_PYTHON="$(awk \'$1=="python"{print $2}\' .tool-versions)"' in text


def test_tool_versions_python_scope_is_timeless():
    """Durable provenance must not embed one machine's temporary state."""
    text = TOOL_VERSIONS.read_text()
    assert "EXACT harness-side runtime" in text
    assert "NOT an" in text and "image-runtime claim" in text
    for transient in ("3.12.13", "transient review note", "is not asserted equal"):
        assert transient not in text, f"provenance embeds transient state: {transient}"


def test_readme_bootstrap_matches_the_pin():
    text = (REPO_ROOT / "README.md").read_text()
    assert f"uv venv --python {_pinned('python')} .venv" in text
    assert "uv venv --python 3.12 .venv" not in text
