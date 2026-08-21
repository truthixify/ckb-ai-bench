"""Supervisor, ownership and publication tests. Fakes only: no Docker, socket or subprocess."""

from __future__ import annotations

import contextlib
import json
import os
import signal
from pathlib import Path

import pytest

from ckbbench.run.devnet import COMPOSE_PROJECT, MINER_SERVICE, NODE_SERVICE, VALIDATE_RUN_LABEL
from ckbbench.run.diagnose import (
    AGENT_NAME_PREFIX,
    DirHandle,
    DEVNET_ANONYMOUS_DATA_MOUNT,
    TERMINATE_GRACE_S,
    WORKER_MODE_ENV,
    Deadline,
    DiagnosticAbort,
    DiagnosticIdentity,
    Supervisor,
    agent_container_name,
    prepare_directory,
    new_execution_id,
    write_candidate,
)
from ckbbench.run.diagnostic import artifact_bytes, false_envelope

RUN_ID = "2.0.0-devnet-B-diagnostic-s1-1786900000"
EXEC_ID = "0123456789abcdef0123456789abcdef"


@contextlib.contextmanager
def _watchdog(seconds: int):
    """Turn a hang into a failure: a blocking open would otherwise wait for a reader forever."""

    def fire(_signum, _frame):
        raise AssertionError("the operation blocked instead of failing fast")

    previous = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Proc:
    """A fake subprocess with an explicit exit script and recorded signals."""

    def __init__(self, exits_after: int | None = 0, code: int = 0, ignores_sigterm: bool = False):
        self.pid = 4242
        self._polls = 0
        self._exits_after = exits_after
        self._code = code
        self.ignores_sigterm = ignores_sigterm
        self.signals: list[int] = []
        self._dead = False

    def poll(self):
        self._polls += 1
        if self._dead:
            return self._code
        if self._exits_after is not None and self._polls > self._exits_after:
            return self._code
        return None

    def wait(self, timeout=None):
        self._dead = True
        return self._code

    def receive(self, sig: int) -> None:
        self.signals.append(sig)
        if sig == signal.SIGKILL or (sig == signal.SIGTERM and not self.ignores_sigterm):
            self._dead = True


class Docker:
    """A fake docker CLI. Every call is recorded; an unexpected one fails the test."""

    def __init__(self, containers: dict[str, dict] | None = None,
                 volumes: dict[str, dict] | None = None, running: list[str] | None = None,
                 volume_users: list[str] | None = None):
        self.containers = containers or {}
        self.volumes = volumes if volumes is not None else {}
        self.running = list(running or [])
        self.volume_users = list(volume_users or [])
        self.calls: list[list[str]] = []
        self.fail_on: set[str] = set()

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        assert argv[0] == "docker", f"unexpected executable: {argv[0]!r}"
        if argv[1] == "ps":
            if "--filter" in argv:
                return _Completed(0, "\n".join(self.volume_users), "")
            return _Completed(0, "\n".join(self.running), "")
        if argv[1:3] == ["container", "inspect"]:
            key = argv[3]
            payload = self.containers.get(key)
            if payload is None:
                payload = next((p for p in self.containers.values() if p.get("Id") == key), None)
            if payload is None:
                return _Completed(1, "", f"Error: No such container: {key}")
            return _Completed(0, json.dumps(payload), "")
        if argv[1:3] == ["volume", "inspect"]:
            payload = self.volumes.get(argv[3])
            if payload is None:
                return _Completed(1, "", f"Error: No such volume: {argv[3]}")
            return _Completed(0, json.dumps(payload), "")
        if argv[1:3] == ["volume", "rm"]:
            if argv[3] in self.fail_on:
                return _Completed(1, "", "removal refused")
            self.volumes.pop(argv[3], None)
            return _Completed(0, argv[3], "")
        if argv[1] == "rm":
            target = argv[-1]
            if target in self.fail_on:
                return _Completed(1, "", "removal refused")
            for name, payload in list(self.containers.items()):
                if payload.get("Id") == target:
                    del self.containers[name]
            return _Completed(0, target, "")
        raise AssertionError(f"unexpected docker call: {argv}")

    def removed_ids(self) -> list[str]:
        return [c[-1] for c in self.calls if c[1] == "rm"]

    def rm_flags(self, container_id: str) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "rm" and c[-1] == container_id]


class _Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


OWNED_VOLUME = {"Name": "ckbbench-devnet-data",
                "Labels": {"com.ckbbench.owner": "ckbbench",
                           "com.ckbbench.role": "devnet-data"}}
FROZEN_IMAGE = "sha256:frozen-agent-image"


def _container(container_id: str, *, name: str, run_label: str | None,
               project: str | None = COMPOSE_PROJECT, service: str | None = None,
               running: bool = False, image: str | None = None) -> dict:
    labels: dict[str, str] = {}
    if project is not None:
        labels["com.docker.compose.project"] = project
    if service is not None:
        labels["com.docker.compose.service"] = service
    if run_label is not None:
        labels[VALIDATE_RUN_LABEL] = run_label
    payload = {"Id": container_id, "Config": {"Labels": labels}, "State": {"Running": running}}
    if image is not None:
        payload["Image"] = image
    return payload


def _identity(tmp_path: Path) -> DiagnosticIdentity:
    return DiagnosticIdentity.create(
        run_id=RUN_ID, artifact_root=tmp_path, run_dir=tmp_path / "run", execution_id=EXEC_ID,
    )


def _supervisor(tmp_path: Path, docker: Docker, clock: Clock | None = None) -> Supervisor:
    """Mirrors the production entry: both directory handles are opened up front and retained."""
    clock = clock or Clock()
    deadline = Deadline(total_s=600.0, monotonic=clock)
    deadline.start()
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    if not identity.run_dir.exists():
        prepare_directory(identity.run_dir, exclusive=True)
    return Supervisor(
        identity=identity, deadline=deadline, run=docker,
        kill=lambda pid, sig: None, sleep=lambda _s: None,
        artifact_dir=DirHandle(identity.final_path.parent),
        run_dir_handle=DirHandle(identity.run_dir),
    )


# --- identity ------------------------------------------------------------------------------------


def test_the_execution_id_is_32_lowercase_hex():
    value = new_execution_id()
    assert len(value) == 32
    assert all(c in "0123456789abcdef" for c in value)


def test_every_selector_derives_from_the_execution_id(tmp_path):
    identity = _identity(tmp_path)
    assert identity.agent_name == f"{AGENT_NAME_PREFIX}{EXEC_ID}"
    assert identity.agent_name == agent_container_name(EXEC_ID)
    assert identity.labels == (f"{VALIDATE_RUN_LABEL}={EXEC_ID}",)
    assert identity.candidate_path.name == f".{RUN_ID}.diag.json.candidate"
    assert identity.final_path.name == f"{RUN_ID}.diag.json"
    assert identity.candidate_path.parent == identity.final_path.parent


def test_the_worker_environment_carries_the_run_scoped_devnet_settings(tmp_path):
    env = _identity(tmp_path).worker_env()
    assert env[WORKER_MODE_ENV] == "1"
    assert env["CKBBENCH_VALIDATE_RUN_ID"] == EXEC_ID
    assert env["CKBBENCH_NETWORK_VALIDATE_RUN_ID"] == ""
    assert env["CKBBENCH_DEVNET_DATA_MOUNT"] == DEVNET_ANONYMOUS_DATA_MOUNT


def test_the_worker_environment_invents_no_secret_channel(tmp_path):
    env = _identity(tmp_path).worker_env()
    joined = " ".join(f"{k}={v}" for k, v in env.items()).lower()
    for forbidden in ("api_key", "authorization", "bearer", "sk-", "password", "secret", "token"):
        assert forbidden not in joined


@pytest.mark.parametrize("bad", ["", "short", "g" * 32, "ABCDEF0123456789abcdef0123456789"])
def test_a_malformed_execution_id_is_refused(tmp_path, bad):
    with pytest.raises(DiagnosticAbort):
        DiagnosticIdentity.create(run_id=RUN_ID, artifact_root=tmp_path,
                                  run_dir=tmp_path, execution_id=bad)


# --- deadline ------------------------------------------------------------------------------------


def test_the_deadline_must_start_before_any_external_action():
    deadline = Deadline(total_s=600.0, monotonic=Clock())
    with pytest.raises(DiagnosticAbort):
        deadline.remaining()


def test_the_deadline_shrinks_across_calls():
    clock = Clock()
    deadline = Deadline(total_s=600.0, monotonic=clock)
    deadline.start()
    assert deadline.remaining() == 600.0
    clock.advance(100.0)
    assert deadline.remaining() == 500.0
    clock.advance(600.0)
    assert deadline.expired() is True


def test_a_docker_call_after_the_deadline_is_refused(tmp_path):
    clock = Clock()
    docker = Docker()
    supervisor = _supervisor(tmp_path, docker, clock)
    clock.advance(601.0)
    with pytest.raises(DiagnosticAbort):
        supervisor._docker(["docker", "container", "inspect", "x"])
    assert docker.calls == []


# --- ordinary transition -------------------------------------------------------------------------


def test_all_ordinary_selectors_are_inspected_before_any_delete(tmp_path):
    docker = Docker({
        MINER_SERVICE: _container("m1", name=MINER_SERVICE, run_label="", service=MINER_SERVICE),
        NODE_SERVICE: _container("n1", name=NODE_SERVICE, run_label="", service=NODE_SERVICE),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME})
    supervisor = _supervisor(tmp_path, docker)
    supervisor.inspect_ordinary()
    assert docker.removed_ids() == []
    # every ordinary selector: running agents, the future agent name, miner, node, and the volume
    assert docker.calls[0][1] == "ps"
    assert [c[3] for c in docker.calls if c[1] == "container"] == [
        supervisor.identity.agent_name, MINER_SERVICE, NODE_SERVICE
    ]
    assert any(c[1:3] == ["volume", "inspect"] for c in docker.calls)


def test_ordinary_resources_are_removed_by_immutable_id(tmp_path):
    docker = Docker({
        MINER_SERVICE: _container("m1", name=MINER_SERVICE, run_label="", service=MINER_SERVICE),
        NODE_SERVICE: _container("n1", name=NODE_SERVICE, run_label="", service=NODE_SERVICE),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME})
    supervisor = _supervisor(tmp_path, docker)
    supervisor.transition_ordinary(supervisor.inspect_ordinary())
    assert docker.removed_ids() == ["m1", "n1"]
    assert MINER_SERVICE not in [c[3] for c in docker.calls if c[1] == "rm"]
    # the checked ordinary named volume is removed here, and only here
    assert ["docker", "volume", "rm", "ckbbench-devnet-data"] in docker.calls


def test_a_foreign_ordinary_container_refuses_the_transition(tmp_path):
    docker = Docker({
        NODE_SERVICE: _container("n1", name=NODE_SERVICE, run_label="",
                                 project="someone-else", service=NODE_SERVICE),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME})
    supervisor = _supervisor(tmp_path, docker)
    with pytest.raises(DiagnosticAbort):
        supervisor.inspect_ordinary()
    assert docker.removed_ids() == []
    assert supervisor.cleanup_ok is False


def test_a_taken_agent_name_refuses_the_run(tmp_path):
    identity = _identity(tmp_path)
    docker = Docker({identity.agent_name: _container("a1", name=identity.agent_name,
                                                     run_label="someone")},
                    volumes={"ckbbench-devnet-data": OWNED_VOLUME})
    supervisor = _supervisor(tmp_path, docker)
    with pytest.raises(DiagnosticAbort):
        supervisor.inspect_ordinary()
    assert docker.removed_ids() == []


def test_absent_ordinary_resources_are_a_normal_outcome(tmp_path):
    docker = Docker({})
    supervisor = _supervisor(tmp_path, docker)
    supervisor.transition_ordinary(supervisor.inspect_ordinary())
    assert docker.removed_ids() == []
    assert supervisor.cleanup_ok is True


# --- post-worker cleanup -------------------------------------------------------------------------


def _diagnostic_stack(identity: DiagnosticIdentity) -> dict[str, dict]:
    return {
        identity.agent_name: _container("a1", name=identity.agent_name, run_label=EXEC_ID,
                                        image=FROZEN_IMAGE),
        MINER_SERVICE: _container("m2", name=MINER_SERVICE, run_label=EXEC_ID,
                                  service=MINER_SERVICE),
        NODE_SERVICE: _container("n2", name=NODE_SERVICE, run_label=EXEC_ID,
                                 service=NODE_SERVICE),
    }


def test_cleanup_removes_by_id_and_disposes_each_anonymous_volume_with_its_owner(tmp_path):
    identity = _identity(tmp_path)
    docker = Docker(_diagnostic_stack(identity))
    supervisor = _supervisor(tmp_path, docker)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    assert docker.removed_ids() == ["a1", "m2", "n2"]
    assert docker.rm_flags("n2") == ["-fv"]
    assert docker.rm_flags("a1") == ["-fv"]
    assert docker.rm_flags("m2") == ["-f"]
    assert supervisor.cleanup_ok is True


def test_cleanup_never_addresses_the_fixed_named_volume(tmp_path):
    identity = _identity(tmp_path)
    docker = Docker(_diagnostic_stack(identity))
    supervisor = _supervisor(tmp_path, docker)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    flat = " ".join(" ".join(c) for c in docker.calls)
    assert "ckbbench-devnet-data" not in flat
    assert "volume" not in flat


def test_a_foreign_replacement_at_the_expected_name_is_left_untouched(tmp_path):
    identity = _identity(tmp_path)
    stack = _diagnostic_stack(identity)
    stack[identity.agent_name] = _container("foreign", name=identity.agent_name,
                                            run_label="another-run")
    docker = Docker(stack)
    supervisor = _supervisor(tmp_path, docker)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    assert "foreign" not in docker.removed_ids()
    assert supervisor.cleanup_ok is False


def test_a_wrong_compose_identity_is_refused(tmp_path):
    identity = _identity(tmp_path)
    stack = _diagnostic_stack(identity)
    stack[NODE_SERVICE] = _container("n9", name=NODE_SERVICE, run_label=EXEC_ID,
                                     project="other", service=NODE_SERVICE)
    docker = Docker(stack)
    supervisor = _supervisor(tmp_path, docker)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    assert "n9" not in docker.removed_ids()
    assert supervisor.cleanup_ok is False


def test_cleanup_continues_after_one_removal_fails(tmp_path):
    identity = _identity(tmp_path)
    docker = Docker(_diagnostic_stack(identity))
    docker.fail_on = {"a1"}
    supervisor = _supervisor(tmp_path, docker)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    assert "m2" in docker.removed_ids() and "n2" in docker.removed_ids()
    assert supervisor.cleanup_ok is False


def test_a_resource_never_created_is_a_normal_absence(tmp_path):
    docker = Docker({})
    supervisor = _supervisor(tmp_path, docker)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    assert supervisor.cleanup_ok is True
    assert {o.action for o in supervisor.outcomes} == {"absent"}


def test_absence_is_proved_after_removal(tmp_path):
    identity = _identity(tmp_path)
    docker = Docker(_diagnostic_stack(identity))
    supervisor = _supervisor(tmp_path, docker)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    inspects = [c[3] for c in docker.calls if c[1] == "container"]
    # three before mutation, then one proof per removed resource
    assert len(inspects) == 6


# --- worker supervision --------------------------------------------------------------------------


def test_a_normal_worker_exit_needs_no_signal(tmp_path):
    docker = Docker({})
    supervisor = _supervisor(tmp_path, docker)
    proc = Proc(exits_after=1, code=0)
    supervisor.kill = lambda pid, sig: proc.receive(sig)
    code, timed_out = supervisor.supervise(lambda: proc)
    assert (code, timed_out) == (0, False)
    assert proc.signals == []


def test_a_nonzero_worker_exit_is_reported(tmp_path):
    supervisor = _supervisor(tmp_path, Docker({}))
    proc = Proc(exits_after=1, code=3)
    code, timed_out = supervisor.supervise(lambda: proc)
    assert (code, timed_out) == (3, False)


def test_a_timeout_sends_sigterm_then_reaps(tmp_path):
    clock = Clock()
    supervisor = _supervisor(tmp_path, Docker({}), clock)
    proc = Proc(exits_after=None)
    supervisor.kill = lambda pid, sig: proc.receive(sig)
    supervisor.sleep = lambda _s: clock.advance(120.0)
    code, timed_out = supervisor.supervise(lambda: proc)
    assert timed_out is True
    assert proc.signals[0] == signal.SIGTERM


def test_a_worker_that_ignores_sigterm_is_killed_after_the_grace(tmp_path):
    clock = Clock()
    supervisor = _supervisor(tmp_path, Docker({}), clock)
    proc = Proc(exits_after=None, ignores_sigterm=True)
    supervisor.kill = lambda pid, sig: proc.receive(sig)

    def advance(_s):
        clock.advance(120.0)

    supervisor.sleep = advance
    code, timed_out = supervisor.supervise(lambda: proc)
    assert timed_out is True
    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]
    assert TERMINATE_GRACE_S == 10.0


# --- candidate and publication ---------------------------------------------------------------------


def _write_candidate(identity: DiagnosticIdentity, payload: bytes,
                     supervisor: Supervisor | None = None) -> tuple[int, int]:
    """Write the candidate and hand the parent the receipt the real worker would have sent.

    A supervisor with no receipt is deliberate in the tests that model a worker which never
    completed its transaction.
    """
    prepare_directory(identity.candidate_path.parent)
    created = write_candidate(identity.candidate_path, payload)
    if supervisor is not None:
        supervisor.candidate_identity = created
    return created


def test_a_valid_candidate_is_published_only_after_cleanup(tmp_path):
    identity = _identity(tmp_path)
    docker = Docker(_diagnostic_stack(identity))
    supervisor = _supervisor(tmp_path, docker)
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    payload = supervisor.publish()
    assert json.loads(payload)["instrumentation_ok"] is True
    assert identity.final_path.read_bytes() == payload
    assert not identity.candidate_path.exists()


def test_a_missing_candidate_publishes_the_false_envelope(tmp_path):
    identity = _identity(tmp_path)
    supervisor = _supervisor(tmp_path, Docker({}))
    payload = supervisor.publish()
    assert payload == false_envelope(RUN_ID)
    assert json.loads(identity.final_path.read_bytes())["instrumentation_ok"] is False


def test_a_malformed_candidate_publishes_the_false_envelope(tmp_path):
    identity = _identity(tmp_path)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, b"{not json", supervisor)
    assert supervisor.publish() == false_envelope(RUN_ID)


def test_a_candidate_for_another_run_publishes_the_false_envelope(tmp_path):
    identity = _identity(tmp_path)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes("some-other-run", []), supervisor)
    assert supervisor.publish() == false_envelope(RUN_ID)


def test_an_oversized_candidate_publishes_the_false_envelope(tmp_path):
    identity = _identity(tmp_path)
    supervisor = _supervisor(tmp_path, Docker({}))
    identity.candidate_path.parent.mkdir(parents=True, exist_ok=True)
    identity.candidate_path.write_bytes(b"x" * 40000)
    assert supervisor.publish() == false_envelope(RUN_ID)


def test_failed_cleanup_forces_the_false_envelope_even_with_a_valid_candidate(tmp_path):
    identity = _identity(tmp_path)
    docker = Docker(_diagnostic_stack(identity))
    docker.fail_on = {"a1"}
    supervisor = _supervisor(tmp_path, docker)
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    assert supervisor.publish() == false_envelope(RUN_ID)


def test_a_symlinked_candidate_is_refused(tmp_path):
    identity = _identity(tmp_path)
    supervisor = _supervisor(tmp_path, Docker({}))
    identity.candidate_path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere.json"
    target.write_bytes(artifact_bytes(RUN_ID, []))
    identity.candidate_path.symlink_to(target)
    assert supervisor.publish() == false_envelope(RUN_ID)


def test_the_worker_never_writes_a_partial_candidate(tmp_path):
    identity = _identity(tmp_path)
    payload = artifact_bytes(RUN_ID, [])
    _write_candidate(identity, payload)
    assert identity.candidate_path.read_bytes() == payload
    assert not (identity.candidate_path.parent /
                (identity.candidate_path.name + ".partial")).exists()


def test_publication_is_a_same_directory_replace(tmp_path):
    identity = _identity(tmp_path)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)
    supervisor.publish()
    assert identity.final_path.parent == identity.candidate_path.parent
    leftovers = [p.name for p in identity.final_path.parent.iterdir()]
    assert leftovers == [identity.final_path.name]


def test_the_cleanup_reserve_covers_its_own_worst_case():
    """The reserve is derived from what it must cover, not a round number."""
    from ckbbench.run.diagnose import (
        CLEANUP_CALL_TIMEOUT_S, CLEANUP_RESERVE_S, MAX_CLEANUP_CALLS, REAP_WAIT_S,
    )

    worst_case = MAX_CLEANUP_CALLS * CLEANUP_CALL_TIMEOUT_S + TERMINATE_GRACE_S + REAP_WAIT_S
    assert CLEANUP_RESERVE_S >= worst_case
    assert CLEANUP_RESERVE_S < 600.0


def test_cleanup_completes_when_every_call_consumes_its_full_timeout(tmp_path):
    """A slow fake, not an instant one: the earlier test only proved some time remained."""
    clock = Clock()
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)

    docker = Docker(_diagnostic_stack(identity))
    real_call = docker.__call__

    def slow(argv, timeout):
        clock.advance(timeout)          # consume the entire budget offered to this call
        return real_call(argv, timeout)

    deadline = Deadline(total_s=600.0, monotonic=clock)
    deadline.start()
    supervisor = Supervisor(identity=identity, deadline=deadline, run=slow,
                            kill=lambda pid, sig: None, sleep=lambda _s: clock.advance(1.0),
                            artifact_dir=DirHandle(identity.final_path.parent),
                            run_dir_handle=DirHandle(identity.run_dir))
    # Start cleanup with exactly the reserve remaining, the worst case it must survive.
    from ckbbench.run.diagnose import CLEANUP_RESERVE_S

    clock.advance(600.0 - CLEANUP_RESERVE_S)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    assert docker.removed_ids() == ["a1", "m2", "n2"]
    assert supervisor.cleanup_ok is True


def test_the_worker_budget_stops_before_the_total_deadline():
    from ckbbench.run.diagnose import CLEANUP_RESERVE_S

    clock = Clock()
    deadline = Deadline(total_s=600.0, monotonic=clock)
    deadline.start()
    clock.advance(600.0 - CLEANUP_RESERVE_S - 1.0)
    assert deadline.worker_expired() is False
    clock.advance(2.0)
    assert deadline.worker_expired() is True
    assert deadline.expired() is False


@pytest.mark.parametrize("proc,label", [
    (Proc(exits_after=1, code=3), "nonzero exit"),
    (Proc(exits_after=None), "timeout"),
])
def test_a_failed_worker_forces_the_false_envelope(tmp_path, proc, label):
    clock = Clock()
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    docker = Docker({})
    deadline = Deadline(total_s=600.0, monotonic=clock)
    deadline.start()
    supervisor = Supervisor(identity=identity, deadline=deadline, run=docker,
                            kill=lambda pid, sig: proc.receive(sig),
                            sleep=lambda _s: clock.advance(60.0),
                            artifact_dir=DirHandle(identity.final_path.parent),
                            run_dir_handle=DirHandle(identity.run_dir))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)
    supervisor.supervise(lambda: proc)
    assert supervisor.worker_ok is False, label
    assert supervisor.publish() == false_envelope(RUN_ID)


def test_a_failed_spawn_forces_the_false_envelope(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    def boom():
        raise OSError("cannot spawn")

    code, timed_out = supervisor.supervise(boom)
    assert (code, timed_out) == (None, False)
    assert supervisor.worker_ok is False
    assert supervisor.publish() == false_envelope(RUN_ID)


def test_a_failed_signal_still_completes_the_termination_sequence(tmp_path):
    """A failed SIGTERM must not stop SIGKILL and the reap: never return with a live child."""
    clock = Clock()
    supervisor = _supervisor(tmp_path, Docker({}), clock)
    supervisor.sleep = lambda _s: clock.advance(60.0)
    attempts: list[int] = []

    def boom(pid, sig):
        attempts.append(sig)
        raise OSError("no such process")

    supervisor.kill = boom
    proc = Proc(exits_after=None)
    code, timed_out = supervisor.supervise(lambda: proc)
    assert timed_out is True
    assert attempts == [signal.SIGTERM, signal.SIGKILL]
    assert supervisor.worker_ok is False


def test_a_poll_failure_still_terminates_and_reaps(tmp_path):
    clock = Clock()
    supervisor = _supervisor(tmp_path, Docker({}), clock)

    class Broken(Proc):
        def poll(self):
            raise OSError("poll failed")

    proc = Broken(exits_after=None)
    signals: list[int] = []
    supervisor.kill = lambda pid, sig: signals.append(sig)
    supervisor.supervise(lambda: proc)
    assert signal.SIGTERM in signals and signal.SIGKILL in signals
    assert supervisor.worker_ok is False


def test_a_reap_failure_is_recorded_not_raised(tmp_path):
    clock = Clock()
    supervisor = _supervisor(tmp_path, Docker({}), clock)
    supervisor.sleep = lambda _s: clock.advance(60.0)

    class Unreapable(Proc):
        def wait(self, timeout=None):
            raise OSError("cannot reap")

    proc = Unreapable(exits_after=None, ignores_sigterm=True)
    supervisor.kill = lambda pid, sig: proc.receive(sig) if sig == signal.SIGTERM else None
    code, _ = supervisor.supervise(lambda: proc)
    assert supervisor.worker_ok is False
    assert any(o.detail == "unreaped" for o in supervisor.outcomes)


def test_an_active_ordinary_agent_refuses_the_transition(tmp_path):
    docker = Docker({
        MINER_SERVICE: _container("m1", name=MINER_SERVICE, run_label="", service=MINER_SERVICE),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME}, running=["minisweagent-abc123"])
    supervisor = _supervisor(tmp_path, docker)
    with pytest.raises(DiagnosticAbort):
        supervisor.inspect_ordinary()
    assert docker.removed_ids() == []


def test_a_running_ordinary_service_refuses_the_transition(tmp_path):
    docker = Docker({
        NODE_SERVICE: _container("n1", name=NODE_SERVICE, run_label="", service=NODE_SERVICE,
                                 running=True),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME})
    supervisor = _supervisor(tmp_path, docker)
    with pytest.raises(DiagnosticAbort):
        supervisor.inspect_ordinary()
    assert docker.removed_ids() == []


def test_a_foreign_ordinary_volume_refuses_the_transition(tmp_path):
    docker = Docker({}, volumes={"ckbbench-devnet-data": {"Labels": {"owner": "someone-else"}}})
    supervisor = _supervisor(tmp_path, docker)
    with pytest.raises(DiagnosticAbort):
        supervisor.inspect_ordinary()
    assert docker.removed_ids() == []
    assert not any(c[1:3] == ["volume", "rm"] for c in docker.calls)


def test_a_wrong_agent_image_is_refused(tmp_path):
    identity = _identity(tmp_path)
    stack = _diagnostic_stack(identity)
    stack[identity.agent_name] = _container("a9", name=identity.agent_name, run_label=EXEC_ID,
                                            image="sha256:someone-elses-image")
    docker = Docker(stack)
    supervisor = _supervisor(tmp_path, docker)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    assert "a9" not in docker.removed_ids()
    assert supervisor.cleanup_ok is False


def test_a_created_resource_that_disappeared_fails_closed(tmp_path):
    supervisor = _supervisor(tmp_path, Docker({}))
    supervisor.note_created(supervisor.identity.agent_name)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    assert supervisor.cleanup_ok is False
    assert any(o.detail == "disappeared unexplained" for o in supervisor.outcomes)


def test_absence_is_proved_by_the_captured_id_not_the_name(tmp_path):
    """A replacement occupying the reusable name must not be mistaken for a proved removal."""
    identity = _identity(tmp_path)
    stack = _diagnostic_stack(identity)
    docker = Docker(stack)
    docker.fail_on = {"a1"}

    supervisor = _supervisor(tmp_path, docker)
    supervisor.cleanup_diagnostic(expected_agent_image=FROZEN_IMAGE)
    # The agent removal failed, so its captured id is still present and cleanup is not ok.
    assert supervisor.cleanup_ok is False
    assert any(c[3] == "a1" for c in docker.calls if c[1] == "container")


def test_a_forged_false_envelope_carrying_records_is_refused(tmp_path):
    from ckbbench.run.diagnostic import InstrumentationError, validate_artifact_bytes

    document = json.loads(artifact_bytes(RUN_ID, []))
    document["instrumentation_ok"] = False
    document["records_dropped"] = 5
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(InstrumentationError):
        validate_artifact_bytes(payload, run_id=RUN_ID)


def test_a_preexisting_final_is_never_silently_overwritten(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)
    identity.final_path.write_bytes(b"SENTINEL")
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    assert identity.final_path.read_bytes() == b"SENTINEL", "a pre-existing final was replaced"


def test_a_planted_publishing_symlink_is_refused_without_mutation(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"UNRELATED")
    planted = identity.final_path.parent / (identity.final_path.name + ".publishing")
    planted.symlink_to(outside)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    assert outside.read_bytes() == b"UNRELATED"
    assert planted.is_symlink(), "the planted selector was deleted instead of refused"
    assert not identity.final_path.exists()


def test_a_planted_candidate_partial_symlink_is_refused_without_mutation(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)
    outside = tmp_path / "outside2.txt"
    outside.write_bytes(b"UNRELATED")
    planted = identity.candidate_path.parent / (identity.candidate_path.name + ".partial")
    planted.symlink_to(outside)

    with pytest.raises(DiagnosticAbort):
        write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []))
    assert outside.read_bytes() == b"UNRELATED"
    assert planted.is_symlink(), "the planted selector was deleted instead of refused"
    assert not identity.candidate_path.exists()


def test_a_preexisting_candidate_is_refused_not_replaced(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)
    identity.candidate_path.write_bytes(b"SENTINEL")
    with pytest.raises(DiagnosticAbort):
        write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []))
    assert identity.candidate_path.read_bytes() == b"SENTINEL"


def test_the_owned_run_directory_is_scrubbed_exactly_once(tmp_path):
    """Scrubbing is its own step, not a side effect of a successful publication."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir)
    (identity.run_dir / "sentinel.txt").write_bytes(b"RAW WORKSPACE CONTENT")
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    supervisor.scrub_run_dir_once()
    # Entries are deliberately retained: removing any of them would name a reusable pathname after
    # the verified handle stops being authoritative. What must not survive is raw content.
    assert (identity.run_dir / "sentinel.txt").read_bytes() == b"", "raw content survived"

    (identity.run_dir / "sentinel.txt").write_bytes(b"WRITTEN AFTER THE SCRUB")
    supervisor.scrub_run_dir_once()
    assert (identity.run_dir / "sentinel.txt").read_bytes() == b"WRITTEN AFTER THE SCRUB", (
        "the scrub ran a second time"
    )

    assert supervisor.publish() == identity.final_path.read_bytes()


def test_a_symlinked_directory_component_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(DiagnosticAbort):
        prepare_directory(link / "diagnostic")


def test_a_hard_linked_candidate_is_refused(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)
    payload = artifact_bytes(RUN_ID, [])
    write_candidate(identity.candidate_path, payload)
    (identity.candidate_path.parent / "extra-link").hardlink_to(identity.candidate_path)
    supervisor = _supervisor(tmp_path, Docker({}))
    assert supervisor.publish() == false_envelope(RUN_ID)


def test_the_ordinary_node_is_an_accepted_volume_user(tmp_path):
    """Refusing every user rejected exactly the retained ordinary state this transition replaces."""
    docker = Docker({
        MINER_SERVICE: _container("m1", name=MINER_SERVICE, run_label="", service=MINER_SERVICE),
        NODE_SERVICE: _container("n1", name=NODE_SERVICE, run_label="", service=NODE_SERVICE),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME}, volume_users=["n1"])
    supervisor = _supervisor(tmp_path, docker)
    found = supervisor.inspect_ordinary()
    assert found["__volume__"] is not None


def test_both_ordinary_services_are_accepted_volume_users(tmp_path):
    docker = Docker({
        MINER_SERVICE: _container("m1", name=MINER_SERVICE, run_label="", service=MINER_SERVICE),
        NODE_SERVICE: _container("n1", name=NODE_SERVICE, run_label="", service=NODE_SERVICE),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME}, volume_users=["n1", "m1"])
    supervisor = _supervisor(tmp_path, docker)
    supervisor.inspect_ordinary()


def test_a_stopped_foreign_volume_user_refuses_the_transition(tmp_path):
    docker = Docker({
        NODE_SERVICE: _container("n1", name=NODE_SERVICE, run_label="", service=NODE_SERVICE),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME}, volume_users=["n1", "stranger-id"])
    supervisor = _supervisor(tmp_path, docker)
    with pytest.raises(DiagnosticAbort):
        supervisor.inspect_ordinary()
    assert docker.removed_ids() == []
    assert not any(c[1:3] == ["volume", "rm"] for c in docker.calls)


def test_an_unproved_volume_user_refuses_even_with_owned_services(tmp_path):
    docker = Docker({
        MINER_SERVICE: _container("m1", name=MINER_SERVICE, run_label="", service=MINER_SERVICE),
        NODE_SERVICE: _container("n1", name=NODE_SERVICE, run_label="", service=NODE_SERVICE),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME},
        volume_users=["n1", "m1", "unknown-id"])
    supervisor = _supervisor(tmp_path, docker)
    with pytest.raises(DiagnosticAbort):
        supervisor.inspect_ordinary()


def test_a_directory_swapped_after_validation_is_left_untouched(tmp_path):
    """check-then-`rmtree(pathname)` deleted the replacement; the handle-bound removal must not."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    (identity.run_dir / "ours.txt").write_bytes(b"OURS")
    stat_info = identity.run_dir.stat()

    supervisor = Supervisor(
        identity=identity, deadline=_started_deadline(), run=Docker({}),
        kill=lambda pid, sig: None, sleep=lambda _s: None,
        artifact_dir=DirHandle(identity.final_path.parent),
        run_dir_handle=DirHandle(identity.run_dir),
    )
    del stat_info

    # Swap the directory for a stranger's after the parent captured its identity.
    identity.run_dir.rename(tmp_path / "moved-away")
    replacement = identity.run_dir
    replacement.mkdir(parents=True)
    (replacement / "not-ours.txt").write_bytes(b"NOT OURS")

    supervisor.remove_run_dir()

    assert (replacement / "not-ours.txt").read_bytes() == b"NOT OURS", "deleted a replacement"
    assert supervisor.cleanup_ok is False


def _started_deadline():
    clock = Clock()
    deadline = Deadline(total_s=600.0, monotonic=clock)
    deadline.start()
    return deadline


def _failing_unlink(names):
    """Fail `unlink` for the given leaf names, succeed otherwise."""
    import os as _os

    real = _os.unlink

    def patched(path, *, dir_fd=None):
        if str(path) in names:
            raise OSError("cannot unlink")
        return real(path, dir_fd=dir_fd)

    return patched


def test_a_failed_candidate_unlink_downgrades_the_result(tmp_path, monkeypatch):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    monkeypatch.setattr("os.unlink", _failing_unlink({identity.candidate_path.name}))
    payload = supervisor.publish()

    assert supervisor.cleanup_ok is False
    assert payload == false_envelope(RUN_ID), "a healthy payload survived a failed cleanup"
    assert json.loads(identity.final_path.read_bytes())["instrumentation_ok"] is False


def test_a_failed_final_staging_unlink_withdraws_the_final(tmp_path, monkeypatch):
    """A healthy final must never outlive a failure that can only occur after the link."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    monkeypatch.setattr("os.unlink", _failing_unlink({identity.final_staging_path.name}))
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()

    assert not identity.final_path.exists(), "a healthy final survived a failed publication"


def test_a_failed_candidate_partial_unlink_is_reported(tmp_path, monkeypatch):
    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)
    monkeypatch.setattr("os.unlink", _failing_unlink({identity.candidate_staging_path.name}))
    with pytest.raises(DiagnosticAbort):
        write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []))


def test_a_short_write_never_publishes_truncated_evidence(tmp_path, monkeypatch):
    """`os.write()` may accept fewer bytes; one call published a 73-byte 'healthy' 147-byte final."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    import os as _os

    real_write = _os.write
    state = {"first": True}

    def short_write(fd, data):
        if state["first"] and len(data) > 8:
            state["first"] = False
            return real_write(fd, data[: len(data) // 2])
        return real_write(fd, data)

    monkeypatch.setattr("os.write", short_write)
    payload = supervisor.publish()

    installed = identity.final_path.read_bytes()
    assert installed == payload, "the returned payload differs from the installed bytes"
    from ckbbench.run.diagnostic import validate_artifact_bytes

    validate_artifact_bytes(installed, run_id=RUN_ID)


def test_a_short_write_fails_the_candidate(tmp_path, monkeypatch):
    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)

    import os as _os

    real_write = _os.write
    monkeypatch.setattr("os.write", lambda fd, data: real_write(fd, data[: max(1, len(data) // 2)]))

    payload = artifact_bytes(RUN_ID, [])
    try:
        write_candidate(identity.candidate_path, payload)
    except DiagnosticAbort:
        assert not identity.candidate_path.exists()
        return
    assert identity.candidate_path.read_bytes() == payload


def test_a_shortened_docker_id_is_not_mistaken_for_a_foreign_user(tmp_path):
    """`docker ps` truncates IDs by default; comparing the two forms refused our own node."""
    full_node = "n" * 64
    docker = Docker({
        NODE_SERVICE: _container(full_node, name=NODE_SERVICE, run_label="",
                                 service=NODE_SERVICE),
    }, volumes={"ckbbench-devnet-data": OWNED_VOLUME}, volume_users=[full_node])
    supervisor = _supervisor(tmp_path, docker)
    supervisor.inspect_ordinary()
    # The request must ask for untruncated ids, or the comparison is meaningless.
    assert any("--no-trunc" in call for call in docker.calls if call[1] == "ps")


def test_a_run_directory_replaced_after_validation_is_left_untouched(tmp_path):
    """The vulnerable window: replacement AFTER the handle's identity check."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    (identity.run_dir / "ours.txt").write_bytes(b"OURS")
    info = identity.run_dir.stat()

    supervisor = Supervisor(
        identity=identity, deadline=_started_deadline(), run=Docker({}),
        kill=lambda pid, sig: None, sleep=lambda _s: None,
        artifact_dir=DirHandle(identity.final_path.parent),
        run_dir_handle=DirHandle(identity.run_dir),
    )
    del info

    identity.run_dir.rename(tmp_path / "moved")
    identity.run_dir.mkdir(parents=True)
    (identity.run_dir / "stranger.txt").write_bytes(b"NOT OURS")

    supervisor.remove_run_dir()

    assert (identity.run_dir / "stranger.txt").read_bytes() == b"NOT OURS"
    assert supervisor.cleanup_ok is False


def test_publication_validates_the_installed_final_not_the_intended_payload(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)
    payload = supervisor.publish()
    assert identity.final_path.read_bytes() == payload
    assert json.loads(payload)["run_id"] == RUN_ID


def test_the_worker_and_parent_bind_to_the_same_directory_object(tmp_path):
    """A pathname alone let the two processes act on different objects."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    with DirHandle(identity.final_path.parent) as parent_handle:
        inherited = DirHandle.adopt(os.dup(parent_handle.fd))
        try:
            assert inherited.identity == parent_handle.identity
            write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []),
                            directory=inherited)
        finally:
            inherited.close()
        assert parent_handle.read(identity.candidate_path.name,
                                  max_bytes=40000) == artifact_bytes(RUN_ID, [])


def test_an_artifact_directory_swapped_after_the_candidate_fails_closed(tmp_path):
    """A final that exists only under a moved name is not a successful publication.

    The retained descriptor decides WHICH object is written; it does not make that object the
    directory the operator was told about.
    """
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    original_dir = identity.final_path.parent

    artifact_dir = DirHandle(original_dir)
    supervisor = Supervisor(
        identity=identity, deadline=_started_deadline(), run=Docker({}),
        kill=lambda pid, sig: None, sleep=lambda _s: None,
        artifact_dir=artifact_dir, run_dir_handle=DirHandle(identity.run_dir),
    )
    write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []), directory=artifact_dir)

    moved = tmp_path / "moved-artifact-dir"
    original_dir.rename(moved)
    original_dir.mkdir(parents=True)
    (original_dir / "stranger.txt").write_bytes(b"NOT OURS")
    replacement_before = sorted(p.name for p in original_dir.iterdir())

    with pytest.raises(DiagnosticAbort):
        supervisor.publish()

    assert supervisor.cleanup_ok is False
    # No healthy final in EITHER object.
    assert not (original_dir / identity.final_path.name).exists()
    assert not (moved / identity.final_path.name).exists()
    # The replacement is byte-untouched.
    assert sorted(p.name for p in original_dir.iterdir()) == replacement_before
    assert (original_dir / "stranger.txt").read_bytes() == b"NOT OURS"


def test_publication_requires_the_canonical_final_to_exist_and_validate(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    payload = supervisor.publish()

    assert identity.final_path.is_file(), "healthy return without a canonical final"
    assert identity.final_path.read_bytes() == payload
    from ckbbench.run.diagnostic import validate_artifact_bytes

    validate_artifact_bytes(identity.final_path.read_bytes(), run_id=RUN_ID)


def test_a_moved_run_directory_fails_closed(tmp_path):
    """Moving the run directory away with raw data inside is not a clean cleanup."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    (identity.run_dir / "raw.txt").write_bytes(b"RAW")
    handle = DirHandle(identity.run_dir)
    supervisor = Supervisor(
        identity=identity, deadline=_started_deadline(), run=Docker({}),
        kill=lambda pid, sig: None, sleep=lambda _s: None,
        artifact_dir=DirHandle(identity.final_path.parent), run_dir_handle=handle,
    )

    moved = tmp_path / "moved-run"
    identity.run_dir.rename(moved)

    supervisor.remove_run_dir()
    assert supervisor.cleanup_ok is False, "a moved run directory reported success"


def test_a_run_directory_replaced_after_the_handle_is_opened_fails_closed(tmp_path):
    """The window the earlier test missed: replacement AFTER the handle exists."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    (identity.run_dir / "ours.txt").write_bytes(b"OURS")
    handle = DirHandle(identity.run_dir)
    supervisor = Supervisor(
        identity=identity, deadline=_started_deadline(), run=Docker({}),
        kill=lambda pid, sig: None, sleep=lambda _s: None,
        artifact_dir=DirHandle(identity.final_path.parent), run_dir_handle=handle,
    )

    moved = tmp_path / "moved-run-2"
    identity.run_dir.rename(moved)
    identity.run_dir.mkdir(parents=True)
    (identity.run_dir / "stranger.txt").write_bytes(b"NOT OURS")

    supervisor.remove_run_dir()

    assert (identity.run_dir / "stranger.txt").read_bytes() == b"NOT OURS", "deleted a replacement"
    assert supervisor.cleanup_ok is False


def test_a_nested_child_replaced_between_stat_and_open_is_not_mutated(tmp_path, monkeypatch):
    """`_scrub_at` must prove the object it opened is the one it selected."""
    from ckbbench.run import diagnose as mod

    root = tmp_path / "root"
    (root / "child").mkdir(parents=True)
    (root / "child" / "ours.txt").write_bytes(b"OURS")

    handle = DirHandle(root)
    real_open = os.open
    swapped = {"done": False}

    def swapping_open(path, flags, *a, **kw):
        if not swapped["done"] and path == "child" and kw.get("dir_fd") == handle.fd:
            swapped["done"] = True
            (root / "child").rename(tmp_path / "moved-child")
            (root / "child").mkdir()
            (root / "child" / "stranger.txt").write_bytes(b"NOT OURS")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(mod.os, "open", swapping_open)
    ok = mod._scrub_at(handle.fd)
    monkeypatch.undo()
    handle.close()

    assert ok is False, "a replaced child was treated as cleanly scrubbed"
    assert (root / "child" / "stranger.txt").read_bytes() == b"NOT OURS", "mutated a replacement"


def test_a_regular_file_replaced_between_stat_and_open_is_not_mutated(tmp_path, monkeypatch):
    """The simpler form of the same defect: stat a name, then mutate that name."""
    from ckbbench.run import diagnose as mod

    root = tmp_path / "root2"
    root.mkdir()
    (root / "ours.txt").write_bytes(b"OURS")

    handle = DirHandle(root)
    real_open = os.open
    swapped = {"done": False}

    def swapping_open(path, flags, *a, **kw):
        if not swapped["done"] and path == "ours.txt" and kw.get("dir_fd") == handle.fd:
            swapped["done"] = True
            (root / "ours.txt").rename(tmp_path / "moved-file.txt")
            (root / "ours.txt").write_bytes(b"NOT OURS")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(mod.os, "open", swapping_open)
    ok = mod._scrub_at(handle.fd)
    monkeypatch.undo()
    handle.close()

    assert ok is False, "a replaced file was treated as cleanly scrubbed"
    assert (root / "ours.txt").read_bytes() == b"NOT OURS", "mutated a replacement"
    assert (tmp_path / "moved-file.txt").read_bytes() == b"OURS"


@pytest.mark.parametrize("mode", ["zero", "partial-then-zero"])
def test_a_failed_marker_write_leaves_no_false_acknowledgement(tmp_path, monkeypatch, mode):
    from ckbbench.run.diagnose import mark_created, read_created

    created = tmp_path / "created"
    prepare_directory(created, exclusive=True)

    import os as _os

    real_write = _os.write
    state = {"calls": 0}

    def broken(fd, data):
        state["calls"] += 1
        if mode == "zero":
            return 0
        return real_write(fd, data[:1]) if state["calls"] == 1 else 0

    monkeypatch.setattr("os.write", broken)
    with pytest.raises(DiagnosticAbort):
        mark_created(created, "node")
    monkeypatch.undo()

    assert read_created(created) == set(), "an empty marker was accepted as an acknowledgement"
    assert not (created / "node").exists(), "a partial marker leaf survived"


def test_a_failed_allowlist_write_leaves_no_partial_leaf(tmp_path, monkeypatch):
    from ckbbench.run.diagnose import write_allowlist

    target = tmp_path / "allowlist" / "allowlist.x.built"
    prepare_directory(target.parent, exclusive=True)
    monkeypatch.setattr("os.write", lambda fd, data: 0)
    with pytest.raises(DiagnosticAbort):
        write_allowlist("B", "devnet", "https://mcp.example", target)
    monkeypatch.undo()
    assert not target.exists(), "a partial allowlist leaf survived"


def test_a_failed_candidate_write_leaves_no_partial_leaf(tmp_path, monkeypatch):
    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)
    monkeypatch.setattr("os.write", lambda fd, data: 0)
    with pytest.raises(DiagnosticAbort):
        write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []))
    monkeypatch.undo()
    assert not identity.candidate_staging_path.exists(), "the .partial leaf survived"
    assert not identity.candidate_path.exists()


def test_a_failed_final_write_leaves_no_staging_leaf(tmp_path, monkeypatch):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    monkeypatch.setattr("os.write", lambda fd, data: 0)
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    monkeypatch.undo()
    assert not identity.final_staging_path.exists(), "the .publishing leaf survived"
    assert not identity.final_path.exists()


# --- transaction rollback failure cases -----------------------------------------------------------


def _failing_link():
    import os as _os

    real = _os.link

    def patched(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        raise OSError("cannot link")

    del real
    return patched


def test_a_candidate_link_failure_leaves_no_staging_leaf(tmp_path, monkeypatch):
    """A link failure occurs AFTER the staging file is fully written, outside the write handler."""
    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)
    monkeypatch.setattr("os.link", _failing_link())
    with pytest.raises(DiagnosticAbort):
        write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []))
    monkeypatch.undo()
    assert not identity.candidate_staging_path.exists(), "the .partial leaf survived a link failure"
    assert not identity.candidate_path.exists()


def test_a_final_link_failure_leaves_no_staging_leaf(tmp_path, monkeypatch):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)
    monkeypatch.setattr("os.link", _failing_link())
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    monkeypatch.undo()
    assert not identity.final_staging_path.exists(), "the .publishing leaf survived a link failure"
    assert not identity.final_path.exists()


def test_a_failed_rollback_unlink_is_recorded_and_fails(tmp_path, monkeypatch):
    """A rollback failure is recorded instead of being swallowed."""
    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)

    import os as _os

    real_unlink = _os.unlink
    monkeypatch.setattr("os.link", _failing_link())
    monkeypatch.setattr("os.unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("no unlink")))
    with pytest.raises(DiagnosticAbort) as raised:
        write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []))
    monkeypatch.undo()
    assert "rolled back" in str(raised.value)
    del real_unlink


def test_the_parent_accounts_for_a_worker_candidate_partial(tmp_path):
    """A surviving `.partial` must be accounted for — and, without a receipt, never deleted.

    The parent has no proof it created that file. Unlinking it by name has the same defect as reading
    the candidate by name: the object behind the name may not belong to this run.
    """
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    identity.candidate_staging_path.write_bytes(b"PARTIAL")

    payload = supervisor.publish()

    assert identity.candidate_staging_path.read_bytes() == b"PARTIAL", "deleted an unproved selector"
    assert supervisor.cleanup_ok is False, "an unaccounted partial reported clean cleanup"
    assert any(o.name == "candidate_staging" and o.action == "failed"
               for o in supervisor.outcomes)
    assert payload == false_envelope(RUN_ID)


def test_a_final_staging_unlink_failure_withdraws_and_reports(tmp_path, monkeypatch):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    import os as _os

    real_unlink = _os.unlink

    def selective(path, *, dir_fd=None):
        if str(path) == identity.final_staging_path.name:
            raise OSError("cannot unlink staging")
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr("os.unlink", selective)
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    monkeypatch.undo()

    assert not identity.final_path.exists(), "a healthy final survived a failed publication"
    assert any(o.detail == "rollback" for o in supervisor.outcomes)


# --- scrub separation, inode ownership and foreign-entry refusal ----------------------------------


def _identical_bytes_replacement(directory: Path, name: str, payload: bytes) -> None:
    """Put a DIFFERENT inode carrying identical bytes at `name`."""
    decoy = directory / f"{name}.decoy"
    decoy.write_bytes(payload)
    decoy.replace(directory / name)


def test_a_candidate_replaced_by_identical_bytes_is_refused(tmp_path, monkeypatch):
    """Matching bytes are not ownership: a different inode is a different file."""
    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)
    payload = artifact_bytes(RUN_ID, [])

    import os as _os

    real_link = _os.link

    def swapping_link(src, dst, **kw):
        real_link(src, dst, **kw)
        _identical_bytes_replacement(identity.candidate_path.parent,
                                     identity.candidate_path.name, payload)

    monkeypatch.setattr("os.link", swapping_link)
    with pytest.raises(DiagnosticAbort):
        write_candidate(identity.candidate_path, payload)
    monkeypatch.undo()

    assert identity.candidate_path.read_bytes() == payload
    assert not identity.candidate_staging_path.exists(), "our own staging leaf survived"


def test_a_final_replaced_by_identical_bytes_is_refused(tmp_path, monkeypatch):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    payload = artifact_bytes(RUN_ID, [])
    _write_candidate(identity, payload, supervisor)

    import os as _os

    real_link = _os.link

    def swapping_link(src, dst, **kw):
        real_link(src, dst, **kw)
        _identical_bytes_replacement(identity.final_path.parent, identity.final_path.name, payload)

    monkeypatch.setattr("os.link", swapping_link)
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    monkeypatch.undo()

    # The foreign file is left exactly as it was: it is not this transaction's to withdraw.
    assert identity.final_path.read_bytes() == payload
    assert not identity.final_staging_path.exists(), "our own staging leaf survived"


def test_a_final_swapped_after_validation_is_refused(tmp_path, monkeypatch):
    """Validating the installed bytes and then returning leaves a swap window wide open."""
    from ckbbench.run import diagnose as mod

    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    foreign = artifact_bytes("2.0.0-devnet-B-other-s1-1786900001", [])
    real_validate = mod.validate_artifact_bytes

    def validate_then_swap(payload, *, run_id):
        result = real_validate(payload, run_id=run_id)
        # Only the FINAL validation, not the candidate one: the window under test opens after the
        # canonical name already holds this transaction's file.
        if identity.final_path.exists():
            decoy = identity.final_path.parent / "swapped-in"
            decoy.write_bytes(foreign)
            decoy.replace(identity.final_path)
        return result

    monkeypatch.setattr(mod, "validate_artifact_bytes", validate_then_swap)
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    monkeypatch.undo()

    assert identity.final_path.read_bytes() == foreign, "withdrew a file it did not create"


def test_an_unrollbackable_initial_final_write_refuses_publication(tmp_path, monkeypatch):
    """Created-but-not-rolled-back is a recorded failure, not an invisible raise."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    import os as _os

    real_unlink = _os.unlink
    staging_name = identity.final_staging_path.name

    def selective_unlink(path, *, dir_fd=None):
        if str(path) == staging_name:
            raise OSError("cannot unlink the final staging selector")
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr("os.write", lambda fd, data: 0)
    monkeypatch.setattr("os.unlink", selective_unlink)
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    monkeypatch.undo()

    assert supervisor.cleanup_ok is False, "an unrollbackable selector reported clean cleanup"
    assert any(o.name == "final_staging" and o.action == "failed" for o in supervisor.outcomes)
    assert not identity.final_path.exists(), "published despite a stranded staging selector"


def test_a_start_mismatch_withdraws_nothing_it_did_not_create(tmp_path):
    """The fixed-name rollback deleted a final this invocation never wrote."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    original_dir = identity.final_path.parent
    artifact_dir = DirHandle(original_dir)
    supervisor = Supervisor(
        identity=identity, deadline=_started_deadline(), run=Docker({}),
        kill=lambda pid, sig: None, sleep=lambda _s: None,
        artifact_dir=artifact_dir, run_dir_handle=DirHandle(identity.run_dir),
    )

    # The retained handle now addresses the moved object; a stranger occupies the canonical path.
    moved = tmp_path / "moved-artifact-dir-8"
    original_dir.rename(moved)
    original_dir.mkdir(parents=True)
    foreign = b"SOMEONE ELSE'S FINAL"
    (moved / identity.final_path.name).write_bytes(foreign)
    (moved / identity.candidate_path.name).write_bytes(b"SOMEONE ELSE'S CANDIDATE")

    with pytest.raises(DiagnosticAbort):
        supervisor.publish()

    assert (moved / identity.final_path.name).read_bytes() == foreign, "deleted a foreign final"
    assert (moved / identity.candidate_path.name).exists(), "deleted a foreign candidate"
    assert supervisor.cleanup_ok is False


def test_a_planted_final_staging_selector_is_refused_and_never_deleted(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)
    identity.final_staging_path.write_bytes(b"PLANTED")

    with pytest.raises(DiagnosticAbort):
        supervisor.publish()

    assert identity.final_staging_path.read_bytes() == b"PLANTED", "deleted a planted selector"
    assert not identity.final_path.exists()


def test_a_hard_linked_file_in_the_run_directory_is_not_truncated(tmp_path):
    """Truncating through our link destroys the outside file's data, which is not cleanup."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"SOMEONE ELSE'S DATA")
    os.link(str(outside), str(identity.run_dir / "linked-in.txt"))
    supervisor = _supervisor(tmp_path, Docker({}))

    from ckbbench.run import diagnose as mod

    assert mod._scrub_at(supervisor.run_dir_handle.fd) is False, "hard link reported clean"
    supervisor.scrub_run_dir_once()

    assert outside.read_bytes() == b"SOMEONE ELSE'S DATA", "destroyed an outside file's content"
    assert supervisor.cleanup_ok is False, "a hard-linked entry reported a clean scrub"


def test_a_fifo_in_the_run_directory_is_refused_without_blocking(tmp_path):
    """A special entry cannot be proved clean, and must never be opened blocking."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    os.mkfifo(str(identity.run_dir / "pipe"))
    supervisor = _supervisor(tmp_path, Docker({}))

    from ckbbench.run import diagnose as mod

    with _watchdog(10):
        assert mod._scrub_at(supervisor.run_dir_handle.fd) is False, "a FIFO reported clean"
        assert mod._raw_bytes_remain(supervisor.run_dir_handle.fd) is True
        supervisor.scrub_run_dir_once()

    assert supervisor.cleanup_ok is False, "an unproved special entry reported a clean scrub"


def test_a_file_replaced_by_a_fifo_between_stat_and_open_does_not_block(tmp_path, monkeypatch):
    """The window O_NONBLOCK exists for: a blocking open would wait for a reader forever."""
    from ckbbench.run import diagnose as mod

    root = tmp_path / "root-fifo"
    root.mkdir()
    (root / "ours.txt").write_bytes(b"OURS")

    handle = DirHandle(root)
    real_open = os.open
    swapped = {"done": False}

    def swapping_open(path, flags, *a, **kw):
        if not swapped["done"] and path == "ours.txt" and kw.get("dir_fd") == handle.fd:
            swapped["done"] = True
            (root / "ours.txt").unlink()
            os.mkfifo(str(root / "ours.txt"))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(mod.os, "open", swapping_open)
    with _watchdog(10):
        ok = mod._scrub_at(handle.fd)
    monkeypatch.undo()
    handle.close()

    assert swapped["done"], "the swap window was never reached"
    assert ok is False, "a name replaced by a FIFO was treated as cleanly scrubbed"


# --- cross-process candidate ownership -------------------------------------------------------------


def _replace_with_new_inode(path: Path, payload: bytes) -> tuple[int, int]:
    """Put a different inode at `path`, returning its identity."""
    decoy = path.parent / f"{path.name}.decoy"
    decoy.write_bytes(payload)
    decoy.replace(path)
    info = os.stat(path)
    return (info.st_dev, info.st_ino)


@pytest.mark.parametrize("bytes_kind", ["identical-valid", "invalid-sentinel"])
def test_a_candidate_replaced_after_the_worker_returned_is_never_published(tmp_path, bytes_kind):
    """The window between `write_candidate()` returning and the parent reading the name.

    The worker's local inode check cannot see it: that process has already finished. Without the
    receipt the parent read whichever file occupied the name, published it as this run's evidence,
    and deleted it.
    """
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    payload = artifact_bytes(RUN_ID, [])
    _write_candidate(identity, payload, supervisor)

    foreign_bytes = payload if bytes_kind == "identical-valid" else b"NOT THIS RUN'S OUTPUT"
    foreign = _replace_with_new_inode(identity.candidate_path, foreign_bytes)
    assert foreign != supervisor.candidate_identity

    published = supervisor.publish()

    assert published == false_envelope(RUN_ID), "published a file the worker did not create"
    assert identity.candidate_path.read_bytes() == foreign_bytes, "mutated a foreign candidate"
    assert (os.stat(identity.candidate_path).st_dev,
            os.stat(identity.candidate_path).st_ino) == foreign, "deleted a foreign candidate"
    assert supervisor.cleanup_ok is False
    assert any(o.name == "candidate" and o.action == "failed" for o in supervisor.outcomes)


def test_a_candidate_with_no_receipt_is_never_read_or_removed(tmp_path):
    """A worker killed before it reported leaves the parent with a name and no proof."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []))
    assert supervisor.candidate_identity is None

    assert supervisor.publish() == false_envelope(RUN_ID)
    assert identity.candidate_path.exists(), "removed a selector it could not prove it created"
    assert supervisor.cleanup_ok is False


def test_a_replaced_candidate_partial_is_left_untouched(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)
    identity.candidate_staging_path.write_bytes(b"SOMEONE ELSE'S PARTIAL")

    supervisor.publish()

    assert identity.candidate_staging_path.read_bytes() == b"SOMEONE ELSE'S PARTIAL"
    assert supervisor.cleanup_ok is False


def test_the_receipt_carries_the_exact_created_inode(tmp_path):
    from ckbbench.run.diagnose import RECEIPT_BYTES, decode_receipt, encode_receipt

    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)
    created = write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []))
    info = os.stat(identity.candidate_path)

    receipt = encode_receipt(created)
    assert len(receipt) == RECEIPT_BYTES
    assert decode_receipt(receipt) == (info.st_dev, info.st_ino) == created


@pytest.mark.parametrize("corrupt", [b"", b"short", b"x" * 72, b"\x00" * 72])
def test_only_an_exact_receipt_is_accepted(corrupt):
    from ckbbench.run.diagnose import decode_receipt

    assert decode_receipt(corrupt) is None


def test_a_receipt_pipe_read_never_blocks_without_a_writer(tmp_path):
    """The spawn-failed shape: the parent still holds the write end and must not wait for it."""
    from ckbbench.run.diagnose import read_receipt

    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    try:
        with _watchdog(10):
            assert read_receipt(read_fd) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_an_oversized_receipt_is_refused(tmp_path):
    from ckbbench.run.diagnose import RECEIPT_BYTES, encode_receipt, read_receipt

    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    try:
        os.write(write_fd, encode_receipt((1, 2)) + b"EXTRA")
        os.close(write_fd)
        assert read_receipt(read_fd) is None, "accepted more than one fixed-width receipt"
        del RECEIPT_BYTES
    finally:
        os.close(read_fd)


# --- identity helpers must not delete or accept unproved aliases ----------------------------------


def test_a_created_partial_replaced_before_rollback_is_not_deleted(tmp_path, monkeypatch):
    """Name-based rollback deleted a foreign file and reported an ordinary abort."""
    from ckbbench.run.diagnose import CreatedButNotRolledBack

    identity = _identity(tmp_path)
    prepare_directory(identity.candidate_path.parent)
    staging = identity.candidate_staging_path

    import os as _os

    real_write = _os.write

    def swapping_write(fd, data):
        _replace_with_new_inode(staging, b"SOMEONE ELSE'S FILE")
        return 0

    monkeypatch.setattr("os.write", swapping_write)
    with pytest.raises(CreatedButNotRolledBack):
        write_candidate(identity.candidate_path, artifact_bytes(RUN_ID, []))
    monkeypatch.undo()
    del real_write

    assert staging.read_bytes() == b"SOMEONE ELSE'S FILE", "rollback deleted a replacement"


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_a_non_regular_replacement_is_never_unlinked_by_rollback(tmp_path, kind):
    """`identity_of` answered None for both absence and a non-regular file; only absence is safe."""
    from ckbbench.run.diagnose import ABSENT, REGULAR, UNPROVED, CreatedButNotRolledBack

    directory = tmp_path / "artifacts"
    directory.mkdir()
    handle = DirHandle(directory)
    try:
        created = handle.write_exclusive_identified("owned", b"OURS")
        (directory / "owned").unlink()
        if kind == "symlink":
            (directory / "owned").symlink_to(tmp_path / "outside-target")
        else:
            os.mkfifo(str(directory / "owned"))

        with _watchdog(10):
            assert handle.probe("owned") == (UNPROVED, None, 0)
            assert handle.probe("absent-name") == (ABSENT, None, 0)
            with pytest.raises(CreatedButNotRolledBack):
                handle.roll_back_created("owned", created)

        assert os.path.lexists(str(directory / "owned")), "unlinked an unproved selector"
        assert handle.probe("owned").state == UNPROVED
        del REGULAR
    finally:
        handle.close()


def test_withdrawal_leaves_an_unproved_selector_and_reports_it(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    directory = supervisor.artifact_dir

    created = directory.write_exclusive_identified("owned", b"OURS")
    _replace_with_new_inode(identity.final_path.parent / "owned", b"NOT OURS")

    supervisor._withdraw_created(directory, "owned", created)

    assert (identity.final_path.parent / "owned").read_bytes() == b"NOT OURS"
    assert supervisor.cleanup_ok is False
    assert any(o.name == "owned" and o.action == "failed" for o in supervisor.outcomes)


def test_an_externally_hard_linked_final_is_never_published_healthy(tmp_path, monkeypatch):
    """A second link is a second path able to mutate the artifact after it was validated."""
    from ckbbench.run.diagnostic import validate_artifact_bytes as real_validate

    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    outside = tmp_path / "outside-link.json"

    import os as _os

    real_unlink = _os.unlink
    staging_name = identity.final_staging_path.name

    def linking_unlink(path, *, dir_fd=None):
        result = real_unlink(path, dir_fd=dir_fd)
        # Immediately after the staging link is gone, so the final is momentarily single-linked.
        if str(path) == staging_name and not outside.exists():
            _os.link(str(identity.final_path), str(outside))
        return result

    monkeypatch.setattr("os.unlink", linking_unlink)
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    monkeypatch.undo()
    del real_validate

    assert outside.exists(), "the reversal never created the outside link"
    assert not identity.final_path.exists(), "a two-link final was published healthy"


def test_a_verified_read_requires_a_single_link(tmp_path):
    directory = tmp_path / "artifacts"
    directory.mkdir()
    handle = DirHandle(directory)
    try:
        created = handle.write_exclusive_identified("owned", b"OURS")
        assert handle.read_verified("owned", identity=created, max_bytes=64) == b"OURS"
        os.link(str(directory / "owned"), str(tmp_path / "outside"))
        with pytest.raises(DiagnosticAbort):
            handle.read_verified("owned", identity=created, max_bytes=64)
        # The staging window is the one place two links are the intended state.
        assert handle.read_verified("owned", identity=created, max_bytes=64,
                                    expect_links=2) == b"OURS"
    finally:
        handle.close()


def test_the_parent_reads_the_candidate_only_through_the_receipt(tmp_path):
    """The read itself must be identity-bound, not just the decision made afterwards."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    payload = artifact_bytes(RUN_ID, [])
    _write_candidate(identity, payload, supervisor)

    assert supervisor._read_candidate(supervisor.artifact_dir) == payload

    _replace_with_new_inode(identity.candidate_path, payload)
    with pytest.raises(DiagnosticAbort):
        supervisor._read_candidate(supervisor.artifact_dir)

    supervisor.candidate_identity = None
    with pytest.raises(DiagnosticAbort):
        supervisor._read_candidate(supervisor.artifact_dir)


# --- retirement must account for disappearance and surviving links --------------------------------


def test_a_receipt_proved_candidate_that_disappears_is_not_clean(tmp_path):
    """A receipt proves the worker created that exact file; gone anyway is a disappearance.

    Treating it as ordinary absence let a healthy artifact be published while the evidence the run
    was built from had vanished without any parent action.
    """
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    _write_candidate(identity, artifact_bytes(RUN_ID, []), supervisor)

    read_back = supervisor._read_candidate(supervisor.artifact_dir)
    identity.candidate_path.unlink()

    payload = supervisor.publish()

    assert read_back == artifact_bytes(RUN_ID, []), "the reversal never reached the read"
    assert payload == false_envelope(RUN_ID), "published evidence that had disappeared"
    assert supervisor.cleanup_ok is False
    assert any(o.name == "candidate" and o.action == "failed"
               and o.detail == "disappeared unexplained" for o in supervisor.outcomes)


def test_an_externally_aliased_candidate_is_reported_not_silently_retired(tmp_path):
    """Unlinking our name does not destroy the file while an outside link keeps the inode alive."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    payload = artifact_bytes(RUN_ID, [])
    _write_candidate(identity, payload, supervisor)

    outside = tmp_path / "outside-candidate.json"
    os.link(str(identity.candidate_path), str(outside))

    published = supervisor.publish()

    assert published == false_envelope(RUN_ID), "published a candidate with an outside alias"
    assert outside.read_bytes() == payload, "the outside alias did not survive"
    assert not identity.candidate_path.exists(), "our own selector was not removed"
    assert supervisor.cleanup_ok is False, "an unaccounted alias reported clean cleanup"
    assert any(o.name == "candidate" and o.action == "failed" and o.detail == "alias survives"
               for o in supervisor.outcomes)


def test_an_externally_aliased_final_records_the_incomplete_withdrawal(tmp_path, monkeypatch):
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    payload = artifact_bytes(RUN_ID, [])
    _write_candidate(identity, payload, supervisor)

    outside = tmp_path / "outside-final.json"

    import os as _os

    real_unlink = _os.unlink
    staging_name = identity.final_staging_path.name

    def linking_unlink(path, *, dir_fd=None):
        result = real_unlink(path, dir_fd=dir_fd)
        if str(path) == staging_name and not outside.exists():
            _os.link(str(identity.final_path), str(outside))
        return result

    monkeypatch.setattr("os.unlink", linking_unlink)
    with pytest.raises(DiagnosticAbort):
        supervisor.publish()
    monkeypatch.undo()

    assert outside.read_bytes() == payload, "the outside alias did not survive"
    assert not identity.final_path.exists(), "a two-link final survived under the canonical name"
    assert supervisor.cleanup_ok is False, "an unaccounted alias reported clean cleanup"
    assert any(o.name == identity.final_path.name and o.action == "failed"
               and o.detail == "alias survives" for o in supervisor.outcomes)


def test_the_staging_window_is_not_treated_as_an_outside_alias(tmp_path):
    """Two OWNED names on one inode is the intended publication state, not an alias."""
    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    supervisor = _supervisor(tmp_path, Docker({}))
    directory = supervisor.artifact_dir

    created = directory.write_exclusive_identified("owned.publishing", b"OURS")
    directory.link("owned.publishing", "owned")
    assert directory.probe("owned").links == 2

    supervisor._withdraw_created(directory, "owned.publishing", created,
                                 owned_names=("owned.publishing", "owned"))

    assert supervisor.cleanup_ok is True, "the deliberate staging window was reported as an alias"
    assert not (identity.final_path.parent / "owned.publishing").exists()
    assert (identity.final_path.parent / "owned").read_bytes() == b"OURS"
