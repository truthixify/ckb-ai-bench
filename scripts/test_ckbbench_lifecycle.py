"""Operator-CLI tests for the DevNet chain-state lifecycle (scripts/ckbbench, plan §9.1).

`./bench reset` is the command an operator trusts to leave no benchmark chain state. The risk is
not that it fails to delete, but that it deletes the wrong thing or quietly deletes nothing: a raw
`docker volume rm` in the shell would skip the ownership labels, and a `down -v` would take any
volume Compose happens to know about. These tests drive the real script with a recording stub in
place of the python interpreter, so they prove the wiring without Docker and without deleting
anything.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "scripts" / "ckbbench"
CLI_TEXT = CLI.read_text()


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], cwd=REPO, env={**os.environ, **(env or {})},
        capture_output=True, text=True, timeout=60,
    )


def _recording_python(tmp_path: Path) -> Path:
    """A stand-in for the venv python that records its argv instead of running anything."""
    stub = tmp_path / "recording-python"
    log = tmp_path / "argv.log"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
    stub.chmod(0o755)
    return stub


def test_reset_removes_state_through_the_lifecycle_controller(tmp_path: Path):
    """The CLI must delegate removal to the labelled, inspected python path -- not delete a volume
    itself -- so the ownership checks cannot be bypassed from the shell."""
    stub = _recording_python(tmp_path)
    res = _bash(
        f'source "{CLI}"\ndevnet_remove_data_volume',
        env={"CKBBENCH_PYTHON": str(stub)},
    )
    assert res.returncode == 0, res.stderr
    logged = (tmp_path / "argv.log").read_text()
    assert "-m ckbbench.run.devnet" in logged
    assert "--remove-data-volume" in logged


def test_reset_calls_the_state_removal_step():
    """A reset that only stopped containers left every prior cell's transactions behind."""
    reset_body = CLI_TEXT.split("cmd_reset()", 1)[1].split("\ncmd_status()", 1)[0]
    assert "devnet_remove_data_volume" in reset_body
    assert "down_impl" in reset_body


def test_down_does_not_remove_chain_state():
    """`down` is a stop, not a reset: an operator inspecting a finished cell must keep the state."""
    down_body = CLI_TEXT.split("down_impl()", 1)[1].split("\nclean_impl()", 1)[0]
    assert "devnet_remove_data_volume" not in down_body
    assert "-v" not in down_body.split("down --remove-orphans")[0].split("compose")[-1]


def test_cli_never_uses_broad_or_unlabelled_docker_deletion():
    """Regression guard for the destructive-safety boundary: no prune, no `down -v`, and no direct
    removal of the DevNet state volume from the shell."""
    forbidden = (
        r"volume\s+prune",
        r"system\s+prune",
        r"down\s+(--[a-z-]+\s+)*-v\b",
        r"docker\s+volume\s+rm\s+.*devnet-data",
    )
    for pattern in forbidden:
        assert not re.search(pattern, CLI_TEXT), f"CLI contains forbidden deletion: {pattern}"


def test_status_checks_chain_identity_and_miner_progress():
    """A node answering RPC is not a ready DevNet: the wrong chain or a stalled miner must fail."""
    status_body = CLI_TEXT.split("cmd_status()", 1)[1].split("\ncmd_test()", 1)[0]
    assert "devnet_is_ckb_dev" in status_body
    assert "devnet_miner_advancing" in status_body


def test_help_distinguishes_down_from_reset():
    res = _bash(f'bash "{CLI}" help')
    assert res.returncode == 0, res.stderr
    assert "chain state is retained" in res.stdout
    assert "remove the benchmark-owned DevNet chain state" in res.stdout
