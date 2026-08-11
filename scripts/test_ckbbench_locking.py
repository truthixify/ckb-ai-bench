"""Concurrency and fail-closed tests for the shared project lock (scripts/lib/lock.sh, plan §9.1).

`containers/validate.sh` decides that DevNet state is disposable by observing its absence, then
spends minutes building images before tearing that state down. Without the project lock, an ordinary
concurrent `./bench up` can create legitimate operator state inside that window, and the gate would
later remove it believing it had created it. These tests drive the real scripts with a fake `docker`
on PATH, so no image is built, no container starts and nothing is deleted.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LOCK_LIB = REPO / "scripts" / "lib" / "lock.sh"
VALIDATE = REPO / "containers" / "validate.sh"


def _fake_docker(tmp_path: Path, *, ps_rc: int = 0, ps_out: str = "",
                 volume_stderr: str = "Error: No such volume: ckbbench-devnet-data",
                 ps_fails_after: int = 0, build_sleep: float = 0) -> Path:
    """A `docker` that answers the two preflight questions and records every call.

    Anything past the inventory (build, compose, run) is refused loudly: these tests must fail if a
    script reaches Docker mutation, not silently pretend it succeeded.

    `ps_fails_after` makes the Nth and later `ps -a` calls fail, so the preflight inventory can
    succeed while the teardown inventory fails -- the real ordering of that fault.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "docker-calls.log"
    ps_count = tmp_path / "ps-count"
    (bindir / "docker").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> "{calls}"
        case "$1 $2" in
          "ps -a")
            n=$(( $(cat "{ps_count}" 2>/dev/null || echo 0) + 1 ))
            echo "$n" > "{ps_count}"
            if [ "{ps_fails_after}" -gt 0 ] && [ "$n" -ge "{ps_fails_after}" ]; then
              echo "Cannot connect to the Docker daemon" >&2
              exit 1
            fi
            printf '%s' "{ps_out}"
            exit {ps_rc}
            ;;
          "volume inspect")
            printf '%s\\n' "{volume_stderr}" >&2
            exit 1
            ;;
          "build "*|"compose "*|"rmi "*)
            sleep {build_sleep}
            echo "MUTATION ATTEMPTED: $*" >&2
            exit 97
            ;;
        esac
        echo "MUTATION ATTEMPTED: $*" >&2
        exit 97
    """))
    (bindir / "docker").chmod(0o755)
    return bindir


def _run_validate(tmp_path: Path, bindir: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        ["bash", str(VALIDATE)], cwd=REPO, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
             "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), **(env or {})},
    )


def _docker_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker-calls.log"
    return log.read_text().splitlines() if log.exists() else []


@pytest.fixture()
def holder(tmp_path: Path):
    """A live process holding the project lock, as a concurrent `./bench up` would."""
    runtime = tmp_path / "runtime"
    script = textwrap.dedent(f"""\
        set -euo pipefail
        source "{LOCK_LIB}"
        with_lock "fake-concurrent-operation"
        echo READY
        sleep 30
    """)
    proc = subprocess.Popen(
        ["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)},
    )
    assert proc.stdout.readline().strip() == "READY", "lock holder did not start"
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_validation_refuses_before_any_docker_call_while_another_operation_holds_the_lock(
    tmp_path: Path, holder
):
    bindir = _fake_docker(tmp_path)
    res = _run_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "holds the lock" in res.stderr, res.stderr
    assert _docker_calls(tmp_path) == [], "the absence decision was made before the lock was held"


def test_validation_acquires_the_lock_before_reading_the_inventory(tmp_path: Path):
    """Without a competing holder the gate takes the lock itself and reports it."""
    bindir = _fake_docker(tmp_path)
    res = _run_validate(tmp_path, bindir)
    assert "lock: acquired" in res.stdout, res.stdout
    # It proceeds past the gate and stops at the first real Docker mutation (the image build).
    assert res.returncode != 0
    assert any("volume inspect" in c for c in _docker_calls(tmp_path))


def test_copying_the_live_owner_pid_does_not_buy_entry(tmp_path: Path, holder):
    """No environment value is a capability.

    An earlier revision let a caller skip acquisition when an env marker matched the live owner PID
    recorded in `owner.meta` -- which any same-user process can read and copy. That reopened the very
    race the lock closes, so there is no inherited mode at all now. This drives the exact bypass:
    the ACTUAL owner's pid, not an arbitrary one.
    """
    owner_pid = None
    meta = tmp_path / "runtime" / f"ckbbench-{os.getuid()}" / "owner.meta"
    for line in meta.read_text().splitlines():
        if line.startswith("pid="):
            owner_pid = line[4:].strip()
    assert owner_pid == str(holder.pid), (owner_pid, holder.pid)

    bindir = _fake_docker(tmp_path)
    res = _run_validate(tmp_path, bindir, env={"CKBBENCH_LOCK_INHERITED": owner_pid})
    assert res.returncode != 0
    assert "holds the lock" in res.stderr, res.stderr
    assert _docker_calls(tmp_path) == [], "a copied PID reached Docker"


def test_the_cli_does_not_hold_a_lock_across_the_docker_free_test_layers():
    """`./bench test --docker` must not wrap the whole run: the gate owns its own, shorter window,
    and there is no nested lock to hand down."""
    cli = (REPO / "scripts" / "ckbbench").read_text()
    body = cli.split("cmd_test()", 1)[1].split("\npreflight_live()", 1)[0]
    assert "with_lock" not in body, body
    assert "release_lock" not in body


def test_validation_holds_its_own_lock_for_its_whole_run(tmp_path: Path):
    """Self-protection must be durable: while the gate works, another operation must be excluded."""
    bindir = _fake_docker(tmp_path, build_sleep=4)
    proc = subprocess.Popen(
        ["bash", str(VALIDATE)], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
             "XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
    )
    try:
        assert proc.stdout.readline().strip() == "lock: acquired"
        contender = subprocess.run(
            ["bash", "-c", f'source "{LOCK_LIB}"\nwith_lock "concurrent-up"'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
        )
        assert contender.returncode != 0, "another operation entered while validation was running"
        assert "holds the lock" in contender.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_validation_blocks_when_the_container_inventory_cannot_be_read(tmp_path: Path):
    """A daemon failure is not proof that nothing is running."""
    bindir = _fake_docker(tmp_path, ps_rc=1)
    res = _run_validate(tmp_path, bindir)
    assert res.returncode != 0
    assert "cannot inventory containers" in res.stdout, res.stdout


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: No such volume: some-other-volume",
        "Error: No such volume: ckbbench-devnet-data-backup",
        "Error: No such volume: old-ckbbench-devnet-data",
        "Error: No such volume: ckbbench-devnet-data.bak",
    ],
    ids=["unrelated", "suffix", "prefix", "dotted"],
)
def test_validation_blocks_on_absence_text_that_names_another_volume(tmp_path: Path, stderr):
    """`no such volume` about a different -- or merely similar -- object is not proof about ours."""
    bindir = _fake_docker(tmp_path, volume_stderr=stderr)
    res = _run_validate(tmp_path, bindir)
    assert res.returncode != 0
    assert "cannot determine whether ckbbench-devnet-data exists" in res.stdout, res.stdout
    assert not any(c.startswith("build") for c in _docker_calls(tmp_path)), "reached mutation"


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: No such volume: ckbbench-devnet-data",
        "Error response from daemon: get ckbbench-devnet-data: no such volume",
    ],
    ids=["no-such-volume-name", "get-name-no-such-volume"],
)
def test_validation_accepts_both_documented_absence_word_orders(tmp_path: Path, stderr):
    """The exactness fix must not reject a genuine absence in either Docker phrasing."""
    bindir = _fake_docker(tmp_path, volume_stderr=stderr)
    res = _run_validate(tmp_path, bindir)
    assert "lock: acquired" in res.stdout
    assert "cannot determine whether" not in res.stdout, res.stdout


def test_teardown_reports_an_unreadable_inventory_instead_of_claiming_a_clean_stack(tmp_path: Path):
    """Runs the real teardown: the preflight inventory succeeds, the teardown one fails.

    Asserting on the script's source text would stay green if the control flow, exit code or call
    ordering changed, which is exactly what this guard is for.
    """
    bindir = _fake_docker(tmp_path, ps_fails_after=2)
    res = _run_validate(tmp_path, bindir)
    assert res.returncode != 0
    assert "could not inventory containers during teardown" in res.stdout, res.stdout
    assert "RESULT: CONTAINER CHECK FAILURES PRESENT (teardown)" in res.stdout
    ps_calls = [c for c in _docker_calls(tmp_path) if c.startswith("ps -a")]
    assert len(ps_calls) >= 2, "the preflight inventory must have succeeded first"


def test_no_environment_variable_can_stand_in_for_the_lock():
    """Regression guard: the copyable-PID mode must not come back."""
    for path in (LOCK_LIB, VALIDATE, REPO / "scripts" / "ckbbench"):
        assert "CKBBENCH_LOCK_INHERITED" not in path.read_text(), path


def test_the_cli_and_the_gate_share_one_lock_implementation():
    """A private second mechanism would not exclude the first."""
    assert "scripts/lib/lock.sh" in (REPO / "scripts" / "ckbbench").read_text()
    assert "scripts/lib/lock.sh" in VALIDATE.read_text()
    for script in ((REPO / "scripts" / "ckbbench").read_text(), VALIDATE.read_text()):
        assert "fcntl.flock" not in script, "lock logic must live in the shared library"
