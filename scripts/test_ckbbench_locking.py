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
import re
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LOCK_LIB = REPO / "scripts" / "lib" / "lock.sh"
VALIDATE = REPO / "containers" / "validate.sh"


def _fake_docker(tmp_path: Path, *, ps_rc: int = 0, ps_out: str = "",
                 volume_stderr: str = ("Error response from daemon: "
                                      "get ckbbench-devnet-data: no such volume"),
                 ps_fails_after: int = 0, build_sleep: float = 0,
                 present_networks: tuple = (), present_images: tuple = (),
                 network_stderr: str = "", image_stderr: str = "",
                 validate_tag_exists: bool = False) -> Path:
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
    present_networks_sh = " ".join(f'"{n}"' for n in present_networks) or '""'
    present_images_sh = " ".join(f'"{i}"' for i in present_images) or '""'
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
            # Follow the name actually requested: validation now uses an invocation-scoped volume,
            # while the message's WORD ORDER stays under test.
            printf '%s\\n' "{volume_stderr}" | sed "s/ckbbench-devnet-data/$3/" >&2
            exit 1
            ;;
          "network inspect")
            if printf '%s' "$3" | grep -q '^netid-'; then
              for f in "$S"/n-*; do
                [ -e "$f" ] || continue
                n="$(basename "$f" | sed 's/^n-//')"
                if [ "netid-$n-$(cat "$f")" = "$3" ]; then
                  case "$5" in
                    *compose.network*)
                      logical="$(printf '%s' "$n" | sed 's/^ckbbench-//')"
                      printf 'netid-%s-%s|%s|ckbbench|%s' "$n" "$(cat "$f")" "$(cat "$f")" "$logical" ;;
                    *) printf 'netid-%s-%s' "$n" "$(cat "$f")" ;;
                  esac
                  exit 0
                fi
              done
              printf 'Error response from daemon: network %s not found\\n' "$3" >&2; exit 1
            fi
            if [ -n "{network_stderr}" ]; then
              printf '%s\\n' "{network_stderr}" | sed "s/{{name}}/$3/g" >&2; exit 1
            fi
            # Physical names are run-scoped, so a fixture names the logical prefix.
            for present in {present_networks_sh}; do
              case "$3" in "$present"|"$present"-*) exit 0 ;; esac
            done
            printf 'Error response from daemon: network %s not found\\n' "$3" >&2
            exit 1
            ;;
          "image inspect")
            if printf '%s' "$3" | grep -q '^sha256:'; then
              for f in "$S"/i-*; do
                [ -e "$f" ] && [ "$(cat "$f")" = "$3" ] && {{ printf '%s' "$3"; exit 0; }}
              done
              printf 'Error response from daemon: No such image: %s\\n' "$3" >&2; exit 1
            fi
            if [ -n "{image_stderr}" ]; then printf '%s\\n' "{image_stderr}" >&2; exit 1; fi
            if [ "{1 if validate_tag_exists else 0}" = "1" ] && printf '%s' "$3" | grep -q ':validate-'; then
              exit 0
            fi
            for present in {present_images_sh}; do
              [ "$3" = "$present" ] && exit 0
            done
            printf 'Error response from daemon: No such image: %s\\n' "$3" >&2
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


def _run_validate(tmp_path: Path, bindir: Path, env: dict[str, str] | None = None,
                  script: Path | None = None):
    # TMPDIR is always redirected into tmp_path: the gate creates its log directory there, and a
    # test that let it land in the real temp directory would pollute the developer's machine.
    # `script` selects a scratch copy for a mutation proof; the working file is never edited.
    default_tmp = tmp_path / "tmpdir"
    default_tmp.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(script or VALIDATE)], cwd=REPO, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
             "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "TMPDIR": str(default_tmp),
             **(env or {})},
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
    calls = _docker_calls(tmp_path)
    assert any(c.startswith("network inspect") for c in calls), calls
    assert calls.index(next(c for c in calls if c.startswith("network inspect"))) < len(calls)


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
    # TMPDIR is redirected here too: this test SIGKILLs the gate, so its EXIT trap cannot run and
    # its log directory necessarily survives. Scoping it to tmp_path keeps that unavoidable
    # remnant out of the developer's temp directory.
    killed_tmp = tmp_path / "killed-tmp"
    killed_tmp.mkdir()
    proc = subprocess.Popen(
        ["bash", str(VALIDATE)], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
             "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "TMPDIR": str(killed_tmp)},
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
    "template",
    [
        "Error response from daemon: network some-other-net not found",
        "Error response from daemon: network {name}-backup not found",
        "Error response from daemon: network old-{name} not found",
        "Error response from daemon: network {name}.bak not found",
    ],
    ids=["unrelated", "suffix", "prefix", "dotted"],
)
def test_validation_blocks_on_absence_text_that_names_another_network(tmp_path: Path, template):
    """Absence text about a different -- or merely similar -- object is not proof about ours.

    The DevNet data mount is anonymous now, so networks are where a reusable fixed name still has
    to be proved absent before this gate will create and later remove one.
    """
    bindir = _fake_docker(tmp_path, network_stderr=template)
    res = _run_validate(tmp_path, bindir)
    assert res.returncode != 0
    assert "cannot determine whether network ckbbench-net-" in res.stdout, res.stdout
    assert not any(c.startswith("build") for c in _docker_calls(tmp_path)), "reached mutation"


def test_validation_accepts_a_genuine_network_absence(tmp_path: Path):
    """The exactness fix must not reject a real absence."""
    bindir = _fake_docker(tmp_path, network_stderr="Error response from daemon: network {name} not found")
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
    # Teardown now completes every owned cleanup step before reporting, hence "complete".
    assert "RESULT: CONTAINER CHECK FAILURES PRESENT (teardown complete)" in res.stdout
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


def test_a_preexisting_network_blocks_before_any_mutation(tmp_path: Path):
    """Naming a network does not make a validation run its owner."""
    bindir = _fake_docker(tmp_path, present_networks=("ckbbench-net-internal",))
    res = _run_validate(tmp_path, bindir)
    assert res.returncode == 1, res.stdout
    assert "ckbbench-net-internal-" in res.stdout and "already exists" in res.stdout, res.stdout
    assert not [c for c in _docker_calls(tmp_path) if c.startswith(("build", "compose", "rmi"))]


def test_each_compose_network_is_probed_before_mutation(tmp_path: Path):
    bindir = _fake_docker(tmp_path)
    _run_validate(tmp_path, bindir)
    probed = {c for c in _docker_calls(tmp_path) if c.startswith("network inspect")}
    for net in ("ckbbench-net-internal", "ckbbench-net-rpc", "ckbbench-net-egress"):
        assert any(c.startswith(f"network inspect {net}-") for c in probed), (
            f"{net} was never probed: {probed}"
        )


def test_a_preexisting_validation_image_tag_blocks_before_any_mutation(tmp_path: Path):
    """Run-scoped tags make collision near-impossible; the guard must still fail closed on one.

    The identity is generated inside the script, so the fake reacts to the tag shape rather than
    predicting the value.
    """
    bindir = _fake_docker(tmp_path, validate_tag_exists=True)
    res = _run_validate(tmp_path, bindir)
    assert res.returncode == 1, res.stdout
    assert "already exists" in res.stdout and ":validate-" in res.stdout, res.stdout
    assert not [c for c in _docker_calls(tmp_path) if c.startswith(("build", "compose", "rmi"))]


def test_each_validation_image_tag_is_probed_before_mutation(tmp_path: Path):
    bindir = _fake_docker(tmp_path)
    _run_validate(tmp_path, bindir)
    probed = [c for c in _docker_calls(tmp_path) if c.startswith("image inspect")]
    for role in ("agent", "verifier", "proxy"):
        assert any(f"ckbbench-{role}:validate-" in c for c in probed), (
            f"the {role} validation tag was never probed: {probed}"
        )


def test_validation_image_tags_are_run_scoped():
    """A fixed `:validate` tag can be created or retargeted by another client after preflight."""
    text = VALIDATE.read_text()
    assert 'AGENT_IMAGE="ckbbench-agent:validate-$RUN_ID"' in text
    assert 'VERIFIER_IMAGE="ckbbench-verifier:validate-$RUN_ID"' in text
    assert 'PROXY_IMAGE="ckbbench-proxy:validate-$RUN_ID"' in text


def test_the_run_identity_is_generated_internally_and_never_read_from_the_caller():
    """A caller who can choose this value can choose what the gate is willing to delete."""
    text = VALIDATE.read_text()
    assert 'RUN_ID="$(head -c 16 /dev/urandom' in text
    for caller_input in ('RUN_ID="${CKBBENCH_VALIDATE_RUN_SEED:-}"',
                         'RUN_ID="${CKBBENCH_VALIDATE_RUN_ID:-}"'):
        assert caller_input not in text, f"the run identity is caller-settable via {caller_input}"


def test_a_network_absence_probe_error_fails_closed(tmp_path: Path):
    """A daemon or permission failure is not permission to create and later delete state."""
    bindir = _fake_docker(tmp_path, network_stderr="Cannot connect to the Docker daemon")
    res = _run_validate(tmp_path, bindir)
    assert res.returncode == 1, res.stdout
    assert "cannot determine whether" in res.stdout, res.stdout
    assert not [c for c in _docker_calls(tmp_path) if c.startswith(("build", "compose", "rmi"))]


def test_an_image_absence_probe_error_fails_closed(tmp_path: Path):
    bindir = _fake_docker(tmp_path, image_stderr="permission denied")
    res = _run_validate(tmp_path, bindir)
    assert res.returncode == 1, res.stdout
    assert "cannot determine whether" in res.stdout, res.stdout
    assert not [c for c in _docker_calls(tmp_path) if c.startswith(("build", "compose", "rmi"))]


def test_the_allowlist_is_written_into_the_invocations_own_directory(tmp_path: Path):
    """A shared repository path collides between concurrent runs and invites foreign deletion."""
    text = VALIDATE.read_text()
    assert 'ALLOWLIST_ARTIFACT="$LOG_DIR/allowlist.validate.built"' in text
    assert '"$ROOT/containers/proxy/allowlist.validate.built"' not in text, (
        "the gate still writes the shared repository allowlist path"
    )


def test_validation_never_touches_the_shared_repository_allowlist(tmp_path: Path):
    shared = REPO / "containers" / "proxy" / "allowlist.validate.built"
    assert not shared.exists(), "a previous run left the shared artifact behind"
    bindir = _state_docker(tmp_path)
    _run_validate(tmp_path, bindir)
    assert not shared.exists(), "the gate created a file in the shared repository path"


def test_validation_binds_compose_to_the_tags_it_builds():
    """Static: the topology checks must exercise the images validation just built."""
    text = VALIDATE.read_text()
    assert 'export CKBBENCH_AGENT_COMPOSE_IMAGE="$AGENT_IMAGE"' in text
    assert 'export CKBBENCH_PROXY_IMAGE="$PROXY_IMAGE"' in text
    compose = (REPO / "containers" / "compose.yml").read_text()
    assert "${CKBBENCH_AGENT_COMPOSE_IMAGE:-ckbbench-agent:latest}" in compose
    assert "${CKBBENCH_PROXY_IMAGE:-ckbbench-proxy:latest}" in compose


def test_validation_uses_an_owned_log_directory_not_fixed_tmp_paths():
    text = VALIDATE.read_text()
    assert "/tmp/ckbbench-validate-agent.log" not in text
    assert "/tmp/ckbbench-validate-verifier.log" not in text
    assert 'LOG_DIR="$(mktemp -d' in text
    assert 'rm -rf "$LOG_DIR"' in text


def test_teardown_cannot_report_success_with_an_owned_survivor():
    """Static: teardown verifies each owned object is gone instead of ignoring rmi failure."""
    text = VALIDATE.read_text()
    assert 'docker rmi "$AGENT_IMAGE" "$VERIFIER_IMAGE" "$PROXY_IMAGE" >/dev/null 2>&1 || true' \
        not in text, "teardown still discards image-removal failures"
    assert "remains after teardown" in text
    assert "RESULT: CONTAINER CHECK FAILURES PRESENT (teardown complete)" in text


def test_validation_compares_exact_pins_not_prefixes():
    """A `rustc 1.95` prefix accepts 1.95.9; a major-only Node check accepts any 22.x."""
    text = VALIDATE.read_text()
    assert 'grep -q "rustc 1.95"' not in text, "validation still prefix-matches rustc"
    assert 'PINNED_NODE="$(awk' in text and 'PINNED_RUST="$(awk' in text, (
        "validation must read the pins from .tool-versions"
    )
    assert '[ "$node_v" != "v${PINNED_NODE}" ]' in text
    assert '[ "$rustc_v" != "$PINNED_RUST" ]' in text


def test_a_blocked_run_leaves_no_log_directory(tmp_path: Path):
    """A directory created before its remover exists leaks on every blocked exit."""
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    bindir = _fake_docker(tmp_path, present_networks=("ckbbench-net-internal",))
    res = _run_validate(tmp_path, bindir, env={"TMPDIR": str(tmpdir)})
    assert res.returncode == 1, res.stdout
    leaked = list(tmpdir.glob("ckbbench-validate-*"))
    assert leaked == [], f"a blocked run leaked {len(leaked)} log directories"


def test_log_directory_is_created_only_after_its_teardown_trap():
    text = VALIDATE.read_text()
    trap_at = text.index("trap teardown EXIT")
    mktemp_at = text.index('LOG_DIR="$(mktemp -d')
    assert mktemp_at > trap_at, (
        "LOG_DIR is created before the trap that removes it; blocked exits will leak it"
    )
    assert 'LOG_DIR=""' in text, "LOG_DIR must be defined before teardown can reference it"






def _state_docker(tmp_path: Path, *, rmi_fails: bool = False, volume_rm_fails: bool = False,
                  agent_build_fails: bool = False, verifier_build_fails: bool = False,
                  inspect_error: str = "",
                  replace_at_teardown: str = "", wrong_service_at_teardown: bool = False,
                  proxy_up_fails: bool = False, agent_up_fails: bool = False,
                  node_version: str = "", rust_version: str = "",
                  record_swap: str = "", build_swap: str = "",
                  devnet_ready_fails: bool = False) -> Path:
    """A `docker` faithful enough that a clean run reaches 14/14 and exits zero.

    Models what the real gate does: proxy-only first `up`, then the devnet services, then the agent;
    per-service ids/labels/mounts; run-scoped image tags; networks and the named volume created by
    Compose. Each object is stamped with the project that call resolved, so an unpinned Compose
    command really does produce the foreign-labelled topology the ledger has to refuse. A single
    injected fault is then the only reason a run fails.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "docker-calls.log"
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    node_v = node_version or _pinned_tool("nodejs")
    rust_v = rust_version or _pinned_tool("rust")
    compose_default_project = _compose_file_project()
    (bindir / "docker").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> "{calls}"
        S="{state}"
        rid="${{CKBBENCH_VALIDATE_RUN_ID:-}}"
        # The project every object created by a compose call is stamped with. Set from that call's
        # own argv and environment, never assumed.
        PROJ=""
        compose_project () {{  # compose argv -> the project Compose would use for this command
          # Real precedence: an explicit -p/--project-name wins, then an inherited
          # COMPOSE_PROJECT_NAME, then the compose file's top-level `name:`.
          local prev="" a
          for a in "$@"; do
            case "$prev" in -p|--project-name) printf '%s' "$a"; return 0 ;; esac
            case "$a" in --project-name=*) printf '%s' "${{a#*=}}"; return 0 ;; esac
            prev="$a"
          done
          printf '%s' "${{COMPOSE_PROJECT_NAME:-{compose_default_project}}}"
        }}
        logical_of () {{  # physical network name -> the logical name compose stamps
          case "$1" in
            "${{CKBBENCH_NET_INTERNAL:-ckbbench-net-internal}}") printf 'net-internal' ;;
            "${{CKBBENCH_NET_RPC:-ckbbench-net-rpc}}")           printf 'net-rpc' ;;
            "${{CKBBENCH_NET_EGRESS:-ckbbench-net-egress}}")     printf 'net-egress' ;;
            *) printf '%s' "$1" | sed 's/^ckbbench-//' ;;
          esac
        }}
        # A network record is `run-label|compose-project`, parsed by field everywhere it is read.
        n_label () {{ cut -d'|' -f1 < "$S/n-$1"; }}
        n_project () {{ cut -d'|' -f2 < "$S/n-$1"; }}
        rswap () {{  # kind name
          # Swap the object under its fixed name at the record boundary: after Compose created it
          # and before the gate captures its ID. Compose labels stay correct; only the immutable id
          # and this run's label change, the hardest case for a name-based recorder.
          local want wk wn svc anon proj
          want="{record_swap}"
          [ -n "$want" ] || return 0
          wk="${{want%%:*}}"; wn="${{want#*:}}"
          [ "$wk" = "$1" ] || return 0
          if [ "$want" != "$wk" ]; then
            case "$2" in "$wn"|"$wn"-*) : ;; *) return 0 ;; esac
          fi
          [ -f "$S/rswapped" ] && return 0
          touch "$S/rswapped"
          case "$1" in
            container)
              svc="$(cut -d'|' -f4 < "$S/c-$2")"; anon="$(cut -d'|' -f5 < "$S/c-$2")"
              proj="$(cut -d'|' -f3 < "$S/c-$2")"
              printf 'OTHERID-%s|FOREIGN|%s|%s|%s' "$2" "$proj" "$svc" "$anon" > "$S/c-$2" ;;
            network)
              proj="$(n_project "$2")"
              printf 'FOREIGN|%s' "$proj" > "$S/n-$2" ;;
            volume)  printf 'FOREIGN' > "$S/v-$2" ;;
          esac
        }}
        mk_container () {{  # name service anon_mount
          [ -f "$S/c-$1" ] || printf '%s|%s|%s|%s|%s,' "id-$1" "$rid" "$PROJ" "$2" "$3" > "$S/c-$1"
          [ -n "$3" ] && printf 'anonymous' > "$S/v-$3"
        }}
        case "$1 $2" in
          "ps -a")
            for f in "$S"/c-*; do [ -e "$f" ] && basename "$f" | sed 's/^c-//'; done
            exit 0 ;;
          "rm "*)
            if [ -n "{replace_at_teardown}" ] && [ ! -f "$S/replaced" ]; then
              touch "$S/replaced"
              case "{replace_at_teardown}" in
                container) for f in "$S"/c-*; do [ -e "$f" ] && \\
                    sed 's/^id-/OTHERID-/' "$f" > "$f.n" && mv "$f.n" "$f"; done ;;
                network)   for f in "$S"/n-*; do
                             [ -e "$f" ] || continue
                             np="$(cut -d'|' -f2 < "$f")"
                             printf 'FOREIGN|%s' "$np" > "$f"
                           done ;;
                image)     for f in "$S"/i-*; do [ -e "$f" ] && printf 'sha256:OTHER' > "$f"; done ;;
                volume_anon) for f in "$S"/v-*; do [ -e "$f" ] && \\
                    printf 'FOREIGN' > "$f"; done ;;
              esac
              exit 1
            fi
            shift; wantv=0
            for a in "$@"; do [ "$a" = "-v" ] && wantv=1; done
            for a in "$@"; do
              [ "$a" = "-f" ] && continue
              [ "$a" = "-v" ] && continue
              for f in "$S"/c-*; do
                [ -e "$f" ] || continue
                [ "$(cut -d'|' -f1 < "$f")" = "$a" ] || continue
                if [ "$wantv" = "1" ]; then
                  # Anonymous volumes go with their owning container, and only with it.
                  for m in $(cut -d'|' -f5 < "$f" | tr ',' ' '); do
                    [ -n "$m" ] && rm -f "$S/v-$m"
                  done
                fi
                rm -f "$f"
              done
            done
            exit 0 ;;
          "network rm"*)
            shift 2
            for a in "$@"; do
              rm -f "$S/n-$a"
              for f in "$S"/n-*; do
                [ -e "$f" ] || continue
                n="$(basename "$f" | sed 's/^n-//')"
                [ "netid-$n-$(n_label "$n")" = "$a" ] && rm -f "$f"
              done
            done
            exit 0 ;;
          "compose "*)
            PROJ="$(compose_project "$@")"
            if printf '%s' "$*" | grep -q ' down'; then
              # `down` without -v keeps the named volume, and only removes THIS project's own
              # objects -- anything under another project survives exactly as it does in reality.
              for f in "$S"/c-*; do
                [ -e "$f" ] || continue
                [ "$(cut -d'|' -f2 < "$f")" = "$rid" ] || continue
                [ "$(cut -d'|' -f3 < "$f")" = "$PROJ" ] || continue
                rm -f "$f"
              done
              for f in "$S"/n-*; do
                [ -e "$f" ] || continue
                [ "$(cut -d'|' -f1 < "$f")" = "$rid" ] || continue
                [ "$(cut -d'|' -f2 < "$f")" = "$PROJ" ] || continue
                rm -f "$f"
              done
              exit 0
            fi
            mk_net () {{ [ -f "$S/n-$1" ] || printf '%s|%s' "$rid" "$PROJ" > "$S/n-$1"; }}
            NI="${{CKBBENCH_NET_INTERNAL:-ckbbench-net-internal}}"
            NR="${{CKBBENCH_NET_RPC:-ckbbench-net-rpc}}"
            NE="${{CKBBENCH_NET_EGRESS:-ckbbench-net-egress}}"
            if printf '%s' "$*" | grep -q 'ckbbench-proxy'; then
              # The proxy attaches to internal + egress only. Compose's project loader drops
              # resources the selected services do not use, so net-rpc does NOT exist yet.
              mk_net "$NI"; mk_net "$NE"
              mk_container ckbbench-proxy ckbbench-proxy ""
              # Partial creation: image, networks and the proxy exist, then startup fails.
              if [ "{1 if proxy_up_fails else 0}" = "1" ]; then exit 1; fi
              exit 0
            fi
            if printf '%s' "$*" | grep -q 'ckbbench-agent'; then
              mk_net "$NI"
              mk_container ckbbench-agent ckbbench-agent ""
              if [ "{1 if agent_up_fails else 0}" = "1" ]; then exit 1; fi
              exit 0
            fi
            # node/miner render internal + RPC: this create is what introduces net-rpc.
            mk_net "$NI"; mk_net "$NR"
            # The node owns the anonymous data volume; the miner inherits it via volumes_from, so
            # both report the SAME mount and there is no named volume at all.
            mk_container ckbbench-devnet-node ckbbench-devnet-node "{'a'*64}"
            mk_container ckbbench-devnet-miner ckbbench-devnet-miner "{'a'*64}"
            exit 0 ;;
          "container inspect")
            if [ -n "{inspect_error}" ]; then printf '{inspect_error}\\n' >&2; exit 1; fi
            # Deletion-time probes address the container by its exact ID.
            if printf '%s' "$3" | grep -q '^id-\\|^OTHERID-'; then
              for f in "$S"/c-*; do
                [ -e "$f" ] || continue
                if [ "$(cut -d'|' -f1 < "$f")" = "$3" ]; then
                  case "$5" in
                    *service*) cut -d'|' -f1-4 < "$f" ;;
                    *)         cut -d'|' -f1 < "$f" ;;
                  esac
                  exit 0
                fi
              done
              printf 'Error response from daemon: No such container: %s\\n' "$3" >&2; exit 1
            fi
            case "$5" in
              *'|'*)
                if [ "{1 if wrong_service_at_teardown else 0}" = "1" ] && [ ! -f "$S/svc" ]; then
                  touch "$S/svc"
                  for f in "$S"/c-*; do [ -e "$f" ] && \\
                    awk -F'|' '{{print $1"|"$2"|"$3"|other-service|"$5}}' "$f" > "$f.n" \\
                    && mv "$f.n" "$f"; done
                fi
                ;;
            esac
            if printf '%s' "$3" | grep -q '^id-\\|^OTHERID-'; then
              for f in "$S"/c-*; do
                [ -e "$f" ] || continue
                if [ "$(cut -d'|' -f1 < "$f")" = "$3" ]; then
                  case "$5" in
                    *service*) cut -d'|' -f1-4 < "$f" ;;
                    *)         cut -d'|' -f1 < "$f" ;;
                  esac
                  exit 0
                fi
              done
              printf 'Error response from daemon: No such container: %s\\n' "$3" >&2; exit 1
            fi
            case "$5" in *service*) rswap container "$3" ;; esac
            [ -f "$S/c-$3" ] || {{ printf 'Error response from daemon: No such container: %s\\n' "$3" >&2; exit 1; }}
            # The ledger asks for the bare id; the ownership probe asks for the full payload.
            [ "$5" = '{{{{.Id}}}}' ] && {{ cut -d'|' -f1 < "$S/c-$3"; exit 0; }}
            # The label-only probe (no pipe in the format) asks just for the run label.
            case "$5" in
              *'|'*) : ;;
              *validate-run*) cut -d'|' -f2 < "$S/c-$3"; exit 0 ;;
            esac
            cat "$S/c-$3"; exit 0 ;;
          "volume inspect")
            [ -f "$S/v-$3" ] || {{ printf 'Error response from daemon: get %s: no such volume\\n' "$3" >&2; exit 1; }}
            # The volume fingerprint is run-label@mountpoint; a bare mountpoint is used for
            # anonymous volumes, which carry no label.
            case "$5" in
              *validate-run*@*)     printf '%s@/vol/%s' "$(cat "$S/v-$3")" "$3"; exit 0 ;;
              *validate-run*owner*role*Mountpoint*)
                rswap volume "$3"
                printf '%s|ckbbench|devnet-data|/vol/%s' "$(cat "$S/v-$3")" "$3"; exit 0 ;;
              *validate-run*owner*) printf '%s|ckbbench|devnet-data' "$(cat "$S/v-$3")"; exit 0 ;;
              *validate-run*)       cat "$S/v-$3"; exit 0 ;;
              *Mountpoint*)     printf '/vol/%s' "$3"; exit 0 ;;
              *Name*)           printf '%s' "$3"; exit 0 ;;
            esac
            exit 0 ;;
          "volume rm")
            [ "{1 if volume_rm_fails else 0}" = "1" ] && exit 1
            rm -f "$S/v-$3"; exit 0 ;;
          "network inspect")
            # Deletion-time probes address the network by its exact recorded ID.
            if printf '%s' "$3" | grep -q '^netid-'; then
              for f in "$S"/n-*; do
                [ -e "$f" ] || continue
                nn="$(basename "$f" | sed 's/^n-//')"
                if [ "netid-$nn-$(n_label "$nn")" = "$3" ]; then
                  case "$5" in
                    *compose.network*)
                      printf 'netid-%s-%s|%s|%s|%s' "$nn" "$(n_label "$nn")" "$(n_label "$nn")" \\
                        "$(n_project "$nn")" "$(logical_of "$nn")" ;;
                    *) printf 'netid-%s-%s' "$nn" "$(n_label "$nn")" ;;
                  esac
                  exit 0
                fi
              done
              printf 'Error response from daemon: network %s not found\\n' "$3" >&2; exit 1
            fi
            case "$5" in *compose.network*) rswap network "$3" ;; esac
            [ -f "$S/n-$3" ] || {{ printf 'Error response from daemon: network %s not found\\n' "$3" >&2; exit 1; }}
            case "$5" in
              *Id*validate-run*compose.project*compose.network*)
                logical="$(logical_of "$3")"
                printf 'netid-%s-%s|%s|%s|%s' "$3" "$(n_label "$3")" \\
                  "$(n_label "$3")" "$(n_project "$3")" "$logical"; exit 0 ;;
              *validate-run*) n_label "$3"; exit 0 ;;
            esac
            [ "$5" = '{{{{.Id}}}}' ] && {{ printf 'netid-%s-%s' "$3" "$(n_label "$3")"; exit 0; }}
            exit 0 ;;
          "image inspect")
            if printf '%s' "$3" | grep -q '^sha256:'; then
              for f in "$S"/i-*; do
                [ -e "$f" ] && [ "$(cat "$f")" = "$3" ] && {{ printf '%s' "$3"; exit 0; }}
              done
              printf 'Error response from daemon: No such image: %s\\n' "$3" >&2; exit 1
            fi
            [ -f "$S/i-$3" ] || {{ printf 'Error response from daemon: No such image: %s\\n' "$3" >&2; exit 1; }}
            [ "$5" = '{{{{.Id}}}}' ] && {{ cat "$S/i-$3"; exit 0; }}
            exit 0 ;;
          "build "*)
            printf '%s' "$*" | grep -q 'agent.Dockerfile' && [ "{1 if agent_build_fails else 0}" = "1" ] && exit 1
            printf '%s' "$*" | grep -q 'verifier.Dockerfile' && [ "{1 if verifier_build_fails else 0}" = "1" ] && exit 1
            prev=""; btag=""; biid=""
            for a in "$@"; do
              [ "$prev" = "-t" ] && btag="$a"
              [ "$prev" = "--iidfile" ] && biid="$a"
              prev="$a"
            done
            # The run label is part of the build, so the id it produces is unique to this run and a
            # cached content-addressed image cannot masquerade as one this invocation owns.
            [ -n "$btag" ] && printf 'sha256:img-%s-%s' "$btag" "$rid" > "$S/i-$btag"
            # The build writes the id itself; the gate never resolves the tag to learn it.
            if [ -n "$biid" ]; then
              printf '%s' "$(cat "$S/i-$btag")" > "$biid"
              if [ -n "{build_swap}" ] && [ "ckbbench-{build_swap}:validate-$rid" = "$btag" ]; then
                # Post-build, pre-record: the tag is retargeted at a compatible foreign image.
                printf 'sha256:FOREIGN' > "$S/i-$btag"
                printf 'foreign' > "$S/i-FOREIGN-marker"
              fi
            fi
            exit 0 ;;
          "rmi "*)
            [ "{1 if rmi_fails else 0}" = "1" ] && exit 1
            shift
            for a in "$@"; do
              rm -f "$S/i-$a"
              # Removal is issued by recorded ID; drop whichever tag holds that image.
              for f in "$S"/i-*; do
                [ -e "$f" ] && [ "$(cat "$f")" = "$a" ] && rm -f "$f"
              done
            done
            exit 0 ;;
          "exec "*)
            # The no-NAT probe expects curl to fail at L3 with 6/7/28.
            printf '%s' "$*" | grep -q 'curl' && exit 6
            exit 0 ;;
          "run "*)
            printf '%s' "$*" | grep -q 'get_tip_block_number' && {{ echo '{{"result":"0x1"}}'; exit 0; }}
            printf '%s' "$*" | grep -q 'node --version' && {{ echo "v{node_v}"; exit 0; }}
            printf '%s' "$*" | grep -q 'rustc --version' && {{ echo "rustc {rust_v} (x)"; exit 0; }}
            printf '%s' "$*" | grep -q 'tool-versions' && {{
              printf 'image: fake\\nrustc {rust_v}\\nclang 19\\nv{node_v}\\n@ckb-ccc/core: 1.12.5\\nriscv64imac-unknown-none-elf: ok\\n'
              exit 0; }}
            exit 0 ;;
        esac
        exit 0
    """))
    (bindir / "docker").chmod(0o755)
    # The gate also drives the production Python lifecycle controller and the allowlist builder.
    # Stubbing them through the documented CKBBENCH_PYTHON seam is what lets a clean run reach
    # 14/14; without it three checks fail for reasons unrelated to any injected fault.
    (bindir / "fakepy").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf 'PY %s\\n' "$*" >> "{calls}"
        # The production lifecycle controller pins this project on every Compose call, so what it
        # creates carries it whatever COMPOSE_PROJECT_NAME the caller inherited.
        devnet_proj="{_devnet_pinned_project()}"
        for a in "$@"; do
          case "$a" in
            *build_allowlist.py) out=""; shift_next=1 ;;
          esac
        done
        if printf '%s' "$*" | grep -q 'build_allowlist.py'; then
          prev=""
          for a in "$@"; do
            [ "$prev" = "-o" ] && printf 'allowlist\\n' > "$a"
            prev="$a"
          done
          exit 0
        fi
        if printf '%s' "$*" | grep -q 'prepare_devnet'; then
          # Model what the real controller creates: the two services sharing ONE anonymous data
          # volume the node owns (the miner inherits it via volumes_from). No named volume exists.
          rid="${{CKBBENCH_VALIDATE_RUN_ID:-}}"
          # The production lifecycle runs a selected-service compose create for node+miner, and
          # THAT is what introduces net-rpc -- proxy startup renders only internal + egress.
          for n in "${{CKBBENCH_NET_INTERNAL:-ckbbench-net-internal}}" \\
                   "${{CKBBENCH_NET_RPC:-ckbbench-net-rpc}}"; do
            [ -f "{state}/n-$n" ] || printf '%s|%s' "$rid" "$devnet_proj" > "{state}/n-$n"
          done
          printf 'id-ckbbench-devnet-node|%s|%s|ckbbench-devnet-node|%s,' \\
            "$rid" "$devnet_proj" "{'a'*64}" > "{state}/c-ckbbench-devnet-node"
          printf 'anonymous' > "{state}/v-{'a'*64}"
          if [ -n "${{FAKE_DEVNET_PARTIAL:-}}" ]; then exit 1; fi
          printf 'id-ckbbench-devnet-miner|%s|%s|ckbbench-devnet-miner|%s,' \\
            "$rid" "$devnet_proj" "{'a'*64}" > "{state}/c-ckbbench-devnet-miner"
          # Docker ownership is complete; the CHAIN readiness result is what fails.
          if [ "{1 if devnet_ready_fails else 0}" = "1" ]; then
            echo "devnet identity/readiness check failed" >&2; exit 1
          fi
          echo "prepared ckb_dev tip=1 genesis=0xdeadbeefdeadbeef..."
          exit 0
        fi
        if printf '%s' "$*" | grep -q 'remove-data-volume'; then
          rm -f "{state}/v-${{CKBBENCH_DEVNET_VOLUME:-ckbbench-devnet-data}}"; exit 0
        fi
        exit 0
    """))
    (bindir / "fakepy").chmod(0o755)
    return bindir


def _pinned_tool(tool: str) -> str:
    for line in (REPO / ".tool-versions").read_text().splitlines():
        if line.startswith(f"{tool} "):
            return line.split()[1]
    raise AssertionError(tool)


def _compose_file_project() -> str:
    """The project Compose derives from the file itself when nothing overrides it.

    Read from the real compose file rather than restated, so the fake cannot model a default the
    project no longer declares.
    """
    for line in (REPO / "containers" / "compose.yml").read_text().splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("containers/compose.yml declares no top-level project name")


def _devnet_pinned_project() -> str:
    """The project the production DevNet controller pins on every Compose call."""
    text = (REPO / "ckbbench" / "run" / "devnet.py").read_text()
    match = re.search(r'^COMPOSE_PROJECT = "([^"]+)"', text, re.M)
    assert match, "the DevNet controller no longer declares a pinned COMPOSE_PROJECT"
    assert '"-p", COMPOSE_PROJECT' in text, "the DevNet controller no longer pins its project"
    return match.group(1)


def _net_label(record: Path) -> str:
    """The run label from a fake network record, whose payload is `run-label|compose-project`."""
    return record.read_text().split("|", 1)[0]


def _net_project(record: Path) -> str:
    parts = record.read_text().split("|", 1)
    assert len(parts) == 2, f"network record {record.name} carries no project: {parts}"
    return parts[1]


def _mutations(tmp_path: Path) -> list[str]:
    """Every argv this run issued that could destroy an object."""
    return [c for c in _docker_calls(tmp_path)
            if c.startswith(("rm ", "stop ", "rmi ", "image rm", "network rm", "volume rm"))]


def _run_id(tmp_path: Path) -> str:
    """The invocation identity, read back from the run-scoped image tags it built."""
    tags = set(re.findall(r":validate-([0-9a-f]{32})", "\n".join(_docker_calls(tmp_path))))
    assert len(tags) == 1, f"expected one run identity, got {sorted(tags)}"
    return tags.pop()


def _foreign_survivors(tmp_path: Path) -> list[str]:
    return sorted(q.name for q in (tmp_path / "state").glob("*")
                  if q.is_file()
                  and q.read_text().startswith(("FOREIGN", "OTHERID", "sha256:FOREIGN"))
                  and q.name != "i-FOREIGN-marker")


def _dependent_actions(tmp_path: Path) -> dict[str, bool]:
    """Whether each action that DEPENDS on a proved topology actually ran."""
    calls = _docker_calls(tmp_path)
    return {
        "rpc_probe": any("get_tip_block_number" in c for c in calls),
        "agent_start": any(c.startswith("compose") and "ckbbench-agent" in c for c in calls),
        "agent_exec": any(c.startswith("exec ") for c in calls),
    }


def _expected_mutations(rid: str, *, containers: list[str], networks: list[str],
                        images: list[str] | None = None) -> list[str]:
    """The complete destructive argv a run of this shape may issue, in order."""
    images = ALL_IMAGE_ROLES if images is None else images
    return ([f"rm -f -v id-{c}" for c in containers]
            + [f"network rm netid-{n}-{rid}-{rid}" for n in networks]
            + [f"rmi sha256:img-ckbbench-{role}:validate-{rid}-{rid}" for role in images])


ALL_CONTAINERS = ["ckbbench-proxy", "ckbbench-devnet-node", "ckbbench-devnet-miner",
                  "ckbbench-agent"]
ALL_NETWORKS = ["ckbbench-net-internal", "ckbbench-net-rpc", "ckbbench-net-egress"]
ALL_IMAGE_ROLES = ["agent", "verifier", "proxy"]
# What this run had already recorded creating when the swap stops it: the swapped container itself
# is refused at its record boundary and therefore never enters the ledger.
# Networks enter the ledger in creation order: the proxy renders internal + egress, and the DevNet
# create is what introduces rpc.
NETS_AFTER_PROXY = ["ckbbench-net-internal", "ckbbench-net-egress"]
NETS_AFTER_DEVNET = NETS_AFTER_PROXY + ["ckbbench-net-rpc"]
CREATED_BEFORE = {
    "ckbbench-proxy": ([], NETS_AFTER_PROXY),
    "ckbbench-agent": (["ckbbench-proxy", "ckbbench-devnet-node", "ckbbench-devnet-miner"],
                       NETS_AFTER_DEVNET),
}





def _removal_requests(tmp_path: Path) -> list[str]:
    return [c.split()[-1] for c in _docker_calls(tmp_path) if c.startswith("volume rm")]


def _run_state_validate(tmp_path: Path, bindir: Path, env: dict[str, str] | None = None,
                        script: Path | None = None):
    """Run the gate against the faithful fake, with the Python seam stubbed too."""
    return _run_validate(tmp_path, bindir, script=script,
                         env={"CKBBENCH_PYTHON": str(bindir / "fakepy"), **(env or {})})


def test_the_clean_baseline_reaches_all_checks_and_exits_zero(tmp_path: Path):
    """Without this, no fault test is a one-variable proof."""
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode == 0, res.stdout
    assert "SUMMARY: 14/14 checks passed" in res.stdout, res.stdout
    assert "RESULT: ALL CONTAINER CHECKS PASSED" in res.stdout


def test_the_clean_baseline_leaves_no_owned_container_network_or_image(tmp_path: Path):
    """Volumes are deliberately retained: Docker exposes no immutable volume handle to bind a
    deletion to, so the gate reports the scoped artifact rather than issue a name-selected remove."""
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode == 0, res.stdout
    survivors = sorted(p.name for p in (tmp_path / "state").iterdir())
    assert not [n for n in survivors if n.startswith(("c-", "n-", "i-"))], (
        f"the clean run left containers/networks/images: {survivors}"
    )
    assert all(n.startswith("v-") for n in survivors), survivors
    assert list((tmp_path / "tmpdir").glob("ckbbench-validate-*")) == []


def test_the_clean_baseline_reports_every_retained_volume(tmp_path: Path):
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir)
    retained = [n[2:] for n in sorted(p.name for p in (tmp_path / "state").iterdir())
                if n.startswith("v-")]
    for name in retained:
        assert f"retaining" in res.stdout and name in res.stdout, (
            f"volume {name} was retained without being reported"
        )
    assert not _removal_requests(tmp_path), (
        "a name-selected volume deletion was issued despite the retention rule"
    )


@pytest.mark.parametrize("which", ["agent", "verifier"])
def test_a_build_failure_fails_the_gate_and_cleans_up(tmp_path: Path, which):
    bindir = _state_docker(tmp_path, **{f"{which}_build_fails": True})
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert list((tmp_path / "tmpdir").glob("ckbbench-validate-*")) == [], "build failure leaked a log dir"
    images = [p.name for p in (tmp_path / "state").glob("i-*")]
    assert images == [], f"a build failure left validation images behind: {images}"





def test_a_container_inspection_daemon_error_fails_closed(tmp_path: Path):
    """"No such container" and "cannot connect" must not be indistinguishable."""
    bindir = _state_docker(tmp_path, inspect_error="Cannot connect to the Docker daemon")
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "cannot determine ownership of container" in res.stdout, res.stdout
    assert _removal_requests(tmp_path) == [], "volumes were removed despite unreadable state"


def test_an_image_removal_failure_is_reported_and_later_cleanup_still_runs(tmp_path: Path):
    bindir = _state_docker(tmp_path, rmi_fails=True)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "remains after teardown" in res.stdout, res.stdout
    assert list((tmp_path / "tmpdir").glob("ckbbench-validate-*")) == [], (
        "cleanup stopped at the image failure and leaked the log directory"
    )


def test_the_gate_never_issues_a_volume_removal_at_all(tmp_path: Path):
    """With retention in force there is no volume-removal path left to fail."""
    bindir = _state_docker(tmp_path, volume_rm_fails=True)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode == 0, res.stdout
    assert not [c for c in _docker_calls(tmp_path) if c.startswith("volume rm")]
    assert list((tmp_path / "tmpdir").glob("ckbbench-validate-*")) == []


def test_teardown_issues_no_project_wide_compose_down(tmp_path: Path):
    """A project-wide selector can remove any object carrying the project label."""
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode == 0, res.stdout
    downs = [c for c in _docker_calls(tmp_path) if c.startswith("compose") and " down" in c]
    assert downs == [], f"validation still ran a project-wide teardown: {downs}"


def test_teardown_removes_containers_by_recorded_id_only(tmp_path: Path):
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode == 0, res.stdout
    removals = [c for c in _docker_calls(tmp_path) if c.startswith("rm ")]
    assert removals, "no container removal was issued"
    for call in removals:
        assert "id-" in call, f"a container was removed by mutable name: {call}"


def test_a_partial_stack_creation_fails_and_still_tears_down(tmp_path: Path):
    """The controller creates the node, then fails before the miner exists."""
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir, env={"FAKE_DEVNET_PARTIAL": "1"})
    assert res.returncode != 0, res.stdout
    assert list((tmp_path / "tmpdir").glob("ckbbench-validate-*")) == [], "partial run leaked a log dir"
    leftover_containers = [p.name for p in (tmp_path / "state").glob("c-*")]
    assert leftover_containers == [], f"a partial run left containers: {leftover_containers}"


def test_the_gate_never_enumerates_volumes_globally(tmp_path: Path):
    bindir = _state_docker(tmp_path)
    _run_state_validate(tmp_path, bindir)
    assert not [c for c in _docker_calls(tmp_path) if c.startswith("volume ls")]
    assert "ANON_BEFORE" not in VALIDATE.read_text()


def test_no_volume_is_removed_by_a_reusable_name(tmp_path: Path):
    """The after-proof replacement window cannot be closed for a name-selected deletion.

    Disposal goes through `docker rm -v <container-id>` instead, whose selector is immutable.
    """
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode == 0, res.stdout
    assert _removal_requests(tmp_path) == [], "a volume was removed by its reusable name"
    removals = [c for c in _docker_calls(tmp_path) if c.startswith("rm ")]
    assert removals and all(c.startswith("rm -f -v id-") for c in removals), removals


def test_a_clean_run_leaves_no_owned_object_of_any_kind(tmp_path: Path):
    """The task's acceptance criterion: success means zero owned leftovers, not a reported leak."""
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode == 0, res.stdout
    assert "RESULT: ALL CONTAINER CHECKS PASSED" in res.stdout, res.stdout
    left = sorted(q.name for q in (tmp_path / "state").glob("*") if q.is_file())
    assert left == [], f"a successful run left owned Docker objects behind: {left}"
    assert "retaining" not in res.stdout, res.stdout






def test_a_container_replaced_after_recording_is_never_torn_down(tmp_path: Path):
    """The recorded exact ID is the point: a replacement adopting the name is a different object."""
    bindir = _state_docker(tmp_path, replace_at_teardown="container")
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "does not present this run's complete identity" in res.stdout, res.stdout
    assert not [c for c in _docker_calls(tmp_path) if c.startswith("compose") and " down" in c], (
        "project-wide teardown ran against a replaced container"
    )
    survivors = sorted(p.name for p in (tmp_path / "state").glob("c-*"))
    assert survivors, "the replaced containers were removed"


def test_a_wrong_compose_service_label_is_refused(tmp_path: Path):
    """`container_payload()` carries the service field; it must be checked, not discarded."""
    bindir = _state_docker(tmp_path, wrong_service_at_teardown=True)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "not this validation run's" in res.stdout, res.stdout
    # The review's probe: the gate reported "foreign" and then removed all four anyway. The
    # wrong-service container must be refused at RECORDING and survive; only genuinely owned
    # containers may be removed.
    survivors = sorted(p.name for p in (tmp_path / "state").glob("c-*"))
    assert "c-ckbbench-proxy" in survivors, (
        f"the wrong-service container was removed: survivors={survivors}"
    )
    removals = [c for c in _docker_calls(tmp_path) if c.startswith("rm ")]
    assert not any("proxy" in c for c in removals), (
        f"a removal was issued against the wrong-service container: {removals}"
    )
    for call in removals:
        assert "id-" in call, f"a container was removed by mutable name: {call}"


def test_an_image_replaced_after_the_build_is_never_removed(tmp_path: Path):
    bindir = _state_docker(tmp_path, replace_at_teardown="image")
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "no longer the image this run built" in res.stdout, res.stdout
    survivors = [p for p in (tmp_path / "state").glob("i-*") if p.read_text() == "sha256:OTHER"]
    assert survivors, "the gate removed an image that had been replaced"


def test_a_network_replaced_after_recording_is_left_untouched(tmp_path: Path):
    bindir = _state_docker(tmp_path, replace_at_teardown="network")
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "no longer the object this run created" in res.stdout, res.stdout
    survivors = [p for p in (tmp_path / "state").glob("n-*") if _net_label(p) == "FOREIGN"]
    assert survivors, "the gate removed a replaced network"
    assert not [c for c in _docker_calls(tmp_path) if c.startswith("compose") and " down" in c], (
        "a broad teardown ran despite a replaced network"
    )


def test_a_proxy_startup_failure_cleans_every_object_it_had_created(tmp_path: Path):
    """Partial creation must be recorded before `set -e` reaches the trap."""
    bindir = _state_docker(tmp_path, proxy_up_fails=True)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "proxy startup failed" in res.stdout, res.stdout
    survivors = sorted(p.name for p in (tmp_path / "state").iterdir())
    assert not [n for n in survivors if n.startswith(("c-", "n-", "i-"))], (
        f"a failed proxy startup left containers/networks/images: {survivors}"
    )
    assert list((tmp_path / "tmpdir").glob("ckbbench-validate-*")) == []


def test_an_agent_startup_failure_cleans_every_object_it_had_created(tmp_path: Path):
    bindir = _state_docker(tmp_path, agent_up_fails=True)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "agent startup failed" in res.stdout, res.stdout
    survivors = sorted(p.name for p in (tmp_path / "state").iterdir())
    assert not [n for n in survivors if n.startswith(("c-", "n-", "i-"))], (
        f"a failed agent startup left containers/networks/images: {survivors}"
    )


def test_a_partial_startup_removes_by_recorded_id_and_runs_no_broad_teardown(tmp_path: Path):
    bindir = _state_docker(tmp_path, proxy_up_fails=True)
    _run_state_validate(tmp_path, bindir)
    calls = _docker_calls(tmp_path)
    assert not [c for c in calls if c.startswith("compose") and " down" in c], (
        "a partial startup triggered a project-wide teardown"
    )
    for call in [c for c in calls if c.startswith("rm ")]:
        assert "id-" in call, f"a container was removed by mutable name: {call}"


# --- Record-boundary swaps -------------------------------------------------------------------
# An adversary that replaces an object under its fixed name in the window between Compose creating
# it and the gate capturing its ID. The replacement keeps the correct Compose project and service
# labels and changes only the immutable id and the run label, so nothing but a complete
# single-payload identity check can tell the two apart.

@pytest.mark.parametrize("victim,first_dependent", [
    ("ckbbench-proxy", "rpc_probe"),
    ("ckbbench-agent", "agent_exec"),
])
def test_a_container_swapped_at_the_record_boundary_blocks_before_it_is_used(
    tmp_path: Path, victim: str, first_dependent: str,
):
    """Detecting a foreign container is not enough: nothing may then depend on it."""
    bindir = _state_docker(tmp_path, record_swap=f"container:{victim}")
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert not _dependent_actions(tmp_path)[first_dependent], (
        f"the gate still ran {first_dependent} against a foreign {victim}: {res.stdout}"
    )
    rid = _run_id(tmp_path)
    containers, networks = CREATED_BEFORE[victim]
    assert _mutations(tmp_path) == _expected_mutations(
        rid, containers=containers, networks=networks,
    ), res.stdout
    assert _foreign_survivors(tmp_path) == [f"c-{victim}"], (
        f"the foreign replacement of {victim} did not survive: {res.stdout}"
    )


@pytest.mark.parametrize("victim", ALL_NETWORKS)
def test_a_network_swapped_at_the_record_boundary_blocks_before_it_is_used(
    tmp_path: Path, victim: str,
):
    """A foreign network must stop the gate before the topology is used, and stay untouched."""
    bindir = _state_docker(tmp_path, record_swap=f"network:{victim}")
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    ran = _dependent_actions(tmp_path)
    assert not ran["rpc_probe"] and not ran["agent_exec"], (
        f"the gate used a foreign {victim}: {res.stdout}"
    )
    rid = _run_id(tmp_path)
    survivors = _foreign_survivors(tmp_path)
    assert survivors == [f"n-{victim}-{rid}"], (
        f"the foreign replacement of {victim} did not survive: {res.stdout}"
    )
    assert not [q for q in (tmp_path / "state").glob("n-*")
                if _net_label(q) != "FOREIGN"], "an owned network was left behind"
    assert f"netid-{victim}-{rid}" not in " ".join(_mutations(tmp_path)), (
        f"the gate mutated the foreign {victim}: {res.stdout}"
    )


@pytest.mark.parametrize("role", ALL_IMAGE_ROLES)
def test_an_image_swapped_between_build_and_record_is_never_removed(tmp_path: Path, role: str):
    """The recorded image ID comes from the build, so retargeting the tag cannot redirect removal.

    The gate must remove the image the build produced -- or nothing -- never the replacement.
    """
    bindir = _state_docker(tmp_path, build_swap=role)
    res = _run_state_validate(tmp_path, bindir)
    assert (tmp_path / "state" / "i-FOREIGN-marker").exists(), "the swap never fired"
    assert res.returncode != 0, res.stdout
    rid = _run_id(tmp_path)
    # The tag proof fires immediately after the builds, so nothing else exists to clean up yet.
    assert _mutations(tmp_path) == _expected_mutations(
        rid, containers=[], networks=[],
        images=[r for r in ALL_IMAGE_ROLES if r != role],
    ), res.stdout
    assert _foreign_survivors(tmp_path) == [f"i-ckbbench-{role}:validate-{rid}"], (
        f"the foreign replacement image did not survive: {res.stdout}"
    )


def test_anonymous_volumes_are_only_ever_disposed_through_their_container_id(tmp_path: Path):
    """A volume NAME is reusable, so it is never a selector.

    Even with a foreign object sitting at every anonymous volume name, the gate issues no
    name-selected volume deletion: disposal happens only as `docker rm -v <container-id>`, whose
    selector is immutable and whose binding to that volume cannot be re-pointed.
    """
    bindir = _state_docker(tmp_path, replace_at_teardown="volume_anon")
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert _removal_requests(tmp_path) == [], f"a volume removal was issued: {res.stdout}"
    removals = [c for c in _docker_calls(tmp_path) if c.startswith("rm ")]
    assert removals and all(c.startswith("rm -f -v id-") for c in removals), removals


# --- Scoped topology and build provenance -----------------------------------------------------

def test_validation_scopes_every_physical_network_to_the_run():
    """A fixed network name can be created by another client during the build window."""
    text = VALIDATE.read_text()
    assert 'export CKBBENCH_NETWORK_VALIDATE_RUN_ID="$RUN_ID"' in text
    for logical in ("internal", "rpc", "egress"):
        assert f'NET_{logical.upper()}="ckbbench-net-{logical}-$RUN_ID"' in text, logical
    for var in ("CKBBENCH_NET_INTERNAL", "CKBBENCH_NET_RPC", "CKBBENCH_NET_EGRESS"):
        assert f"export {var}=" in text, var


def test_compose_renders_every_network_name_from_the_scoped_variable():
    compose = (REPO / "containers" / "compose.yml").read_text()
    for var, default in (("CKBBENCH_NET_INTERNAL", "ckbbench-net-internal"),
                         ("CKBBENCH_NET_RPC", "ckbbench-net-rpc"),
                         ("CKBBENCH_NET_EGRESS", "ckbbench-net-egress")):
        assert f"name: ${{{var}:-{default}}}" in compose, var


def test_validation_points_production_network_selection_at_its_own_topology(monkeypatch):
    """Otherwise the agent and verifier attach to the fixed network this gate did not build.

    Exercises the real consumers rather than grepping for the variable: the model-agent factory is
    the process that must reach the proxy and DevNet service names.
    """
    assert 'export CKBBENCH_DOCKER_NETWORK="$NET_INTERNAL"' in VALIDATE.read_text()
    monkeypatch.setenv("CKBBENCH_DOCKER_NETWORK", "ckbbench-net-internal-gate-probe")
    from ckbbench.config import resolve_agent_network
    from ckbbench.run.runner import RunnerConfig
    assert resolve_agent_network() == "ckbbench-net-internal-gate-probe"
    assert RunnerConfig().network == "ckbbench-net-internal-gate-probe"
    factory = (REPO / "ckbbench" / "run" / "agent_factory.py").read_text()
    assert "resolve_agent_network()" in factory
    assert '"ckbbench-net-internal"' not in factory, "the factory still hardcodes the fixed network"


def test_the_devnet_data_mount_is_anonymous_under_validation():
    """A named volume has no immutable handle, so its deletion can never be made ownership-safe."""
    assert 'export CKBBENCH_DEVNET_DATA_MOUNT="/var/lib/ckb/data"' in VALIDATE.read_text()
    compose = (REPO / "containers" / "compose.yml").read_text()
    assert "${CKBBENCH_DEVNET_DATA_MOUNT:-devnet-data:/var/lib/ckb/data}" in compose


def test_every_validation_build_captures_its_image_id_and_stamps_the_run():
    """Resolving the tag after the build records whatever a replacement retargeted it to."""
    text = VALIDATE.read_text()
    assert "--iidfile" in text, "builds must capture the id from the build operation"
    assert '--label "com.ckbbench.validate-run=$RUN_ID"' in text
    for role, dockerfile in (("agent", "agent.Dockerfile"), ("verifier", "verifier.Dockerfile"),
                             ("proxy", "proxy/proxy.Dockerfile")):
        assert f'build_image {dockerfile} "${role.upper()}_IMAGE"' in text, role
    # A tag lookup may diagnose, never authorise a removal or a run.
    assert "record_created image" not in text
    # Compose must launch the captured ids too; the tags stay diagnostics.
    assert 'export CKBBENCH_AGENT_COMPOSE_IMAGE="$AGENT_ID"' in text
    assert 'export CKBBENCH_PROXY_IMAGE="$PROXY_ID"' in text
    # Fail closed at the build boundary, and keep using the exact id afterwards.
    assert "assert_tag_still_points_at_build" in text


def test_no_later_container_operation_addresses_a_reusable_name():
    """`docker exec <name>` runs against whatever holds that name at exec time."""
    text = VALIDATE.read_text()
    assert "docker exec ckbbench-agent" not in text
    assert 'docker exec "$0"' in text
    assert "--network ckbbench-net-internal" not in text


def test_the_gate_hard_stops_before_using_an_unproved_topology():
    text = VALIDATE.read_text()
    assert text.count("assert_topology_owned ") >= 4, "each mutating step must be followed by one"
    assert "refusing to use it" in text


# --- Selected-service topology, execution selectors, and lifecycle failure ---------------------

def test_proxy_startup_creates_only_the_networks_that_service_uses(tmp_path: Path):
    """Compose's project loader drops resources the selected services do not use.

    The proxy renders internal + egress only, so net-rpc cannot exist yet; requiring it from the
    ledger at that point would assert an impossible order.
    """
    bindir = _state_docker(tmp_path)
    env = {**os.environ, "CKBBENCH_VALIDATE_RUN_ID": "probe",
           "CKBBENCH_NET_INTERNAL": "net-i", "CKBBENCH_NET_RPC": "net-r",
           "CKBBENCH_NET_EGRESS": "net-e"}
    state = tmp_path / "state"
    subprocess.run([str(bindir / "docker"), "compose", "-f", "compose.yml", "up", "-d",
                    "ckbbench-proxy"], env=env, capture_output=True, text=True)
    after_proxy = sorted(q.name for q in state.glob("n-*"))
    assert after_proxy == ["n-net-e", "n-net-i"], after_proxy
    subprocess.run([str(bindir / "docker"), "compose", "-f", "compose.yml", "create",
                    "ckbbench-devnet-node", "ckbbench-devnet-miner"],
                   env=env, capture_output=True, text=True)
    after_devnet = sorted(q.name for q in state.glob("n-*"))
    assert after_devnet == ["n-net-e", "n-net-i", "n-net-r"], after_devnet


def test_a_clean_run_records_and_removes_the_exact_rpc_network_id(tmp_path: Path):
    """The DevNet-created network must enter the ledger, or teardown cannot remove it."""
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode == 0, res.stdout
    rid = _run_id(tmp_path)
    assert f"network rm netid-ckbbench-net-rpc-{rid}-{rid}" in _mutations(tmp_path), res.stdout
    assert not [q for q in (tmp_path / "state").glob("n-*")], "a network survived a clean run"


def test_a_devnet_readiness_failure_stops_before_every_dependent_action(tmp_path: Path):
    """Docker ownership is not chain readiness.

    The lifecycle creates correctly labelled node and miner containers and then fails its identity
    and readiness result. Nothing may then run against that chain.
    """
    bindir = _state_docker(tmp_path, devnet_ready_fails=True)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode != 0, res.stdout
    assert "devnet lifecycle preparation" in res.stdout, res.stdout
    ran = _dependent_actions(tmp_path)
    assert not ran["rpc_probe"], f"the RPC probe ran after preparation failed: {res.stdout}"
    assert not ran["agent_start"], f"the agent was started after preparation failed: {res.stdout}"
    assert not ran["agent_exec"], f"an exec ran after preparation failed: {res.stdout}"
    # Everything the failed lifecycle created is still cleaned up by exact ID, including net-rpc.
    rid = _run_id(tmp_path)
    assert f"network rm netid-ckbbench-net-rpc-{rid}-{rid}" in _mutations(tmp_path), res.stdout
    left = sorted(q.name for q in (tmp_path / "state").glob("*") if q.is_file())
    assert left == [], f"a failed lifecycle left owned objects behind: {left}"


@pytest.mark.parametrize("role", ALL_IMAGE_ROLES)
def test_a_replaced_tag_is_never_the_selector_of_any_dependent_run(tmp_path: Path, role: str):
    """Capturing the build id fixes deletion authority; it must also fix execution authority.

    A tag retargeted after the build must not be what validation then runs, and the gate must stop
    rather than report passing checks it performed against a foreign image.
    """
    bindir = _state_docker(tmp_path, build_swap=role)
    res = _run_state_validate(tmp_path, bindir)
    assert (tmp_path / "state" / "i-FOREIGN-marker").exists(), "the swap never fired"
    assert res.returncode != 0, res.stdout
    rid = _run_id(tmp_path)
    tag = f"ckbbench-{role}:validate-{rid}"
    for call in _docker_calls(tmp_path):
        if call.startswith(("run ", "exec ", "compose ")):
            assert tag not in call, f"a dependent operation ran the replaced tag: {call}"
            assert "sha256:FOREIGN" not in call, f"a dependent operation ran the foreign image: {call}"
    assert "SUMMARY: 14/14 checks passed" not in res.stdout, (
        f"the gate reported every check passing after an image was replaced: {res.stdout}"
    )


def test_every_image_dependent_stage_runs_the_captured_build_id(tmp_path: Path):
    """The tag proof at the build boundary is not enough on its own.

    It only closes the window up to that point; a later retarget still redirects anything that
    selects by tag. Every stage after the build must name the exact captured ID.
    """
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir)
    assert res.returncode == 0, res.stdout
    rid = _run_id(tmp_path)
    tags = {f"ckbbench-{role}:validate-{rid}" for role in ALL_IMAGE_ROLES}
    # Token-wise: the captured ids embed the tag text, so a substring test cannot tell them apart.
    offenders = [
        c for c in _docker_calls(tmp_path)
        if c.startswith(("run ", "compose ")) and tags & set(c.split())
    ]
    assert offenders == [], f"these stages selected a mutable tag instead of the build id: {offenders}"
    # And the captured IDs really are what ran, rather than nothing running at all.
    ran_by_id = [c for c in _docker_calls(tmp_path) if c.startswith("run ") and "sha256:img-" in c]
    assert len(ran_by_id) >= 8, f"expected the image checks to run by id, saw {len(ran_by_id)}"


# --- Compose project ownership ----------------------------------------------------------------
# Compose gives an inherited COMPOSE_PROJECT_NAME precedence over the file's top-level `name:`,
# while this gate's ownership ledger only accepts objects labelled with the project it expects.
# An unpinned Compose call therefore creates a topology the gate must refuse and cannot remove.

HOSTILE_PROJECT = "ckbbench-foreign-operator-stack"


def _validator_without_the_project_pin(tmp_path: Path) -> Path:
    """A scratch copy of the gate whose Compose calls no longer pin the project.

    The working file is never edited. The copy needs only the two repository files the gate reads
    through `$ROOT`; every other path it names is a string the fake never opens.
    """
    root = tmp_path / "unpinned"
    (root / "containers").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / ".tool-versions").symlink_to(REPO / ".tool-versions")
    (root / "scripts" / "lib" / "lock.sh").symlink_to(LOCK_LIB)
    text = VALIDATE.read_text()
    pinned = 'COMPOSE="docker compose -f compose.yml -p $COMPOSE_PROJECT"'
    assert text.count(pinned) == 1, "the gate no longer builds its Compose command exactly once"
    script = root / "containers" / "validate.sh"
    script.write_text(text.replace(pinned, 'COMPOSE="docker compose -f compose.yml"'))
    return script


def test_an_inherited_compose_project_cannot_move_the_topology_the_gate_owns(tmp_path: Path):
    """The gate must own what it creates even when the caller's environment names a project.

    Nothing is left behind and every check passes, which is only possible if each object was
    created under the pinned project the ownership ledger accepts.
    """
    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir, env={"COMPOSE_PROJECT_NAME": HOSTILE_PROJECT})
    assert res.returncode == 0, res.stdout
    assert "SUMMARY: 14/14 checks passed" in res.stdout, res.stdout
    assert "RESULT: ALL CONTAINER CHECKS PASSED" in res.stdout, res.stdout

    compose_calls = [c for c in _docker_calls(tmp_path) if c.startswith("compose ")]
    assert compose_calls, "the gate issued no compose command"
    for call in compose_calls:
        argv = call.split()
        assert "-p" in argv, f"a compose call did not pin its project: {call}"
        assert argv[argv.index("-p") + 1] == _compose_file_project(), call
        assert HOSTILE_PROJECT not in argv, f"the inherited project reached compose: {call}"

    left = sorted(q.name for q in (tmp_path / "state").glob("*") if q.is_file())
    assert not [n for n in left if n.startswith(("c-", "n-", "i-"))], (
        f"the hostile-environment run left containers/networks/images: {left}"
    )
    for name in [n[2:] for n in left if n.startswith("v-")]:
        assert "retaining" in res.stdout and name in res.stdout, (
            f"volume {name} was retained without being reported"
        )
    assert _removal_requests(tmp_path) == [], "a name-selected volume deletion was issued"
    assert list((tmp_path / "tmpdir").glob("ckbbench-validate-*")) == [], "a log directory leaked"
    assert not list((tmp_path / "tmpdir").rglob("allowlist.*.built")), "an allowlist leaked"
    assert not (REPO / "containers" / "proxy" / "allowlist.validate.built").exists()


def test_dropping_the_project_pin_strands_the_topology_it_just_created(tmp_path: Path):
    """Mutation proof for the regression above: without `-p` the hostile case must fail closed.

    Compose then stamps the inherited project, the ledger refuses every object, and the gate stops
    before using a topology it cannot prove it built -- leaving that foreign-labelled state alone
    rather than removing it.

    The same copy is run first with no ambient project, so the inherited value is the single
    variable and the scratch sandbox itself is proved sound.
    """
    unpinned = _validator_without_the_project_pin(tmp_path)
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    control = _run_state_validate(control_dir, _state_docker(control_dir), script=unpinned,
                                  env={"COMPOSE_PROJECT_NAME": ""})
    assert control.returncode == 0, control.stdout
    assert "SUMMARY: 14/14 checks passed" in control.stdout, control.stdout

    bindir = _state_docker(tmp_path)
    res = _run_state_validate(tmp_path, bindir, script=unpinned,
                              env={"COMPOSE_PROJECT_NAME": HOSTILE_PROJECT})
    assert res.returncode != 0, res.stdout
    compose_calls = [c for c in _docker_calls(tmp_path) if c.startswith("compose ")]
    assert compose_calls and not any("-p" in c.split() for c in compose_calls), (
        f"the mutation did not take effect: {compose_calls}"
    )
    assert "refusing to use it" in res.stdout, res.stdout

    proxy = tmp_path / "state" / "c-ckbbench-proxy"
    assert proxy.exists(), f"the refused container was removed anyway: {res.stdout}"
    assert proxy.read_text().split("|")[2] == HOSTILE_PROJECT, proxy.read_text()
    networks = sorted((tmp_path / "state").glob("n-*"))
    assert networks, f"the refused networks were removed anyway: {res.stdout}"
    for record in networks:
        assert _net_project(record) == HOSTILE_PROJECT, record.read_text()
    assert not [c for c in _docker_calls(tmp_path) if c.startswith(("rm ", "network rm"))], (
        "the gate mutated objects whose ownership it had just refused"
    )
