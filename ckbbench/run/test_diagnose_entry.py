"""Production-entry regressions: what `diagnose_cli.main()` itself wires, with fakes only.

Unit tests that call `cleanup_diagnostic()` or `note_created()` directly cannot catch a CLI that
never passes the frozen image or never observes creation state — exactly how incomplete wiring
passed a green focused suite before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ckbbench.run import diagnose_cli
from ckbbench.run.devnet import MINER_SERVICE, NODE_SERVICE
from ckbbench.run.diagnose import (
    CREATION_MARKERS,
    DiagnosticAbort,
    DiagnosticIdentity,
    mark_created,
    prepare_directory,
    read_created,
)

EXEC_ID = "0123456789abcdef0123456789abcdef"
RUN_ID = "2.0.0-devnet-B-gpt-5.6-sol-s1-1786900000"


def _identity(tmp_path: Path) -> DiagnosticIdentity:
    return DiagnosticIdentity.create(
        run_id=RUN_ID, artifact_root=tmp_path, run_dir=tmp_path / "diagnostic-run" / RUN_ID,
        execution_id=EXEC_ID,
    )


class _Recorder:
    """Stands in for the Supervisor, recording exactly what the CLI asks of it."""

    def __init__(self, *, reaped=True, **_kw):
        self.identity = _kw.get("identity")
        self.deadline = _kw.get("deadline")
        self.run_dir_identity = _kw.get("run_dir_identity")
        self.cleanup_image = "NOT CALLED"
        self.published = False
        self.child_reaped = reaped
        self.worker_ok = True
        self.cleanup_ok = True
        self.outcomes: list = []
        self.acknowledged: set[str] = set()
        self.scrubs = 0

    def scrub_run_dir_once(self):
        self.scrubs += 1

    def inspect_ordinary(self):
        return {}

    def transition_ordinary(self, found):
        return None

    def supervise(self, spawn):
        return 0, False

    def cleanup_diagnostic(self, *, expected_agent_image):
        self.cleanup_image = expected_agent_image

    def publish(self):
        self.published = True
        return b'{"instrumentation_ok":true}'

    def _inspect(self, name):
        return None


@pytest.fixture
def cli_harness(tmp_path, monkeypatch):
    """Run `main()` with the supervisor and worker replaced, never touching Docker."""
    made: dict = {}

    def factory(**kwargs):
        made["supervisor"] = _Recorder(reaped=made.get("reaped", True), **kwargs)
        return made["supervisor"]

    monkeypatch.setattr(diagnose_cli, "Supervisor", factory)
    monkeypatch.setattr(diagnose_cli, "_spawn_worker", lambda *a, **k: object())
    made["root"] = tmp_path
    return made


def test_the_cli_passes_the_frozen_agent_image_to_cleanup(cli_harness, tmp_path):
    """An optional image check is one the production caller can forget; this proves it does not."""
    code = diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])
    supervisor = cli_harness["supervisor"]
    assert code == 0
    assert supervisor.cleanup_image not in ("NOT CALLED", None, "")
    assert supervisor.cleanup_image.startswith("sha256:")


def test_the_cli_refuses_to_clean_up_or_publish_an_unreaped_child(tmp_path, monkeypatch):
    made: dict = {}

    def factory(**kwargs):
        made["supervisor"] = _Recorder(reaped=False, **kwargs)
        return made["supervisor"]

    monkeypatch.setattr(diagnose_cli, "Supervisor", factory)
    monkeypatch.setattr(diagnose_cli, "_spawn_worker", lambda *a, **k: object())

    code = diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])
    supervisor = made["supervisor"]
    assert code == 1
    assert supervisor.cleanup_image == "NOT CALLED", "cleanup ran with a live child"
    assert supervisor.published is False, "published with a live child"
    assert supervisor.scrubs == 0, "scrubbed the run directory alongside a live child"


def test_the_cli_creates_its_owned_directories_exclusively(cli_harness, tmp_path):
    artifacts = tmp_path / "artifacts"
    assert diagnose_cli.main(["--artifact-root", str(artifacts)]) == 0
    identity = cli_harness["supervisor"].identity
    assert identity.run_dir.exists()
    assert identity.created_dir.exists()
    assert identity.mount_dir.exists()


def test_a_preexisting_run_directory_is_refused_before_any_external_action(tmp_path, monkeypatch):
    """A pre-existing directory is somebody else's; a later rmtree must never reach it."""
    called = {"supervisor": False}

    def factory(**kwargs):
        called["supervisor"] = True
        return _Recorder(**kwargs)

    monkeypatch.setattr(diagnose_cli, "Supervisor", factory)
    artifacts = tmp_path / "artifacts"

    # Pre-create exactly the run directory the CLI will choose, with a sentinel inside.
    from ckbbench.run.model_profile import PROFILE_PATH, load_reviewed_profile

    model = load_reviewed_profile(str(PROFILE_PATH)).requested_model
    import time as _t

    monkeypatch.setattr(diagnose_cli.time, "time", lambda: 1786900000.0)
    run_id = diagnose_cli.run_id_for(model, 1786900000.0)
    planted = artifacts / "diagnostic-run" / run_id
    planted.mkdir(parents=True)
    (planted / "sentinel.txt").write_bytes(b"NOT OURS")

    code = diagnose_cli.main(["--artifact-root", str(artifacts)])
    assert code == 1
    assert called["supervisor"] is False, "reached the supervisor despite a refused path"
    assert (planted / "sentinel.txt").read_bytes() == b"NOT OURS"
    del _t


def test_ambient_image_overrides_never_reach_the_child(tmp_path):
    identity = _identity(tmp_path)
    env = diagnose_cli.child_environment(identity, {
        "PATH": "/usr/bin",
        "CKBBENCH_AGENT_IMAGE": "sha256:OPERATOR-OVERRIDE",
        "CKBBENCH_VERIFIER_IMAGE": "sha256:OPERATOR-OVERRIDE-2",
    })
    assert "CKBBENCH_AGENT_IMAGE" not in env
    assert "CKBBENCH_VERIFIER_IMAGE" not in env
    assert "OPERATOR-OVERRIDE" not in " ".join(env.values())


def test_the_child_receives_every_exact_parent_selector(tmp_path):
    identity = _identity(tmp_path)
    env = identity.worker_env()
    assert env["CKBBENCH_DIAGNOSTIC_ALLOWLIST_PATH"] == str(identity.allowlist_path)
    assert env["CKBBENCH_DIAGNOSTIC_CREATED_DIR"] == str(identity.created_dir)
    assert env["CKBBENCH_DIAGNOSTIC_MOUNT_DIR"] == str(identity.mount_dir)
    assert env["CKBBENCH_DIAGNOSTIC_CANDIDATE"] == str(identity.candidate_path)
    # The allowlist filename is fixed by the parent, not chosen by mkstemp in the worker.
    assert identity.allowlist_path.name == f"allowlist.{EXEC_ID}.built"


# --- creation-state protocol ----------------------------------------------------------------------


def test_creation_markers_survive_a_killed_worker(tmp_path):
    """Durable files, not an in-memory flag: the parent must read them after a SIGKILL."""
    identity = _identity(tmp_path)
    prepare_directory(identity.created_dir, exclusive=True)
    assert read_created(identity.created_dir) == set()
    mark_created(identity.created_dir, "node")
    mark_created(identity.created_dir, "agent")
    assert read_created(identity.created_dir) == {"node", "agent"}


def test_an_unknown_creation_marker_is_refused(tmp_path):
    identity = _identity(tmp_path)
    prepare_directory(identity.created_dir, exclusive=True)
    with pytest.raises(DiagnosticAbort):
        mark_created(identity.created_dir, "something-else")


def test_all_three_resources_have_a_marker():
    assert set(CREATION_MARKERS) == {"agent", "miner", "node"}


# --- shared workspace preparation -------------------------------------------------------------------


def test_both_real_callers_enter_the_shared_workspace_helper():
    """Both real callers enter the shared workspace helper."""
    run_path = Path(diagnose_cli.__file__).parent
    orchestrate = (run_path / "orchestrate.py").read_text()
    worker = (run_path / "diagnose_worker.py").read_text()

    assert "def prepare_agent_workspace(" in orchestrate
    # The accepted path calls it, not just defines it.
    assert "pointer = prepare_agent_workspace(" in orchestrate
    assert "pointer = prepare_agent_workspace(" in worker
    # And no second copy of the draw/compose sequence survives in run_cell().
    assert orchestrate.count("write_prompt_injected(params, mount") == 1
    assert orchestrate.count("write_instructions(composed, mount)") == 1


def test_the_shared_helper_produces_identical_prompt_visible_bytes(tmp_path, monkeypatch):
    """One deterministic fake RPC, two callers' worth of preparation, byte-identical mounts."""
    from ckbbench.run.arm import resolve_arm
    from ckbbench.run.orchestrate import prepare_agent_workspace
    from ckbbench.suite.registry import load_suite

    suite = load_suite(Path("suites/ckb-v1"))
    arm_config = resolve_arm("B")

    def fake_rpc(method, params):
        if method == "get_tip_block_number":
            return hex(4242)
        return None

    # Run params are deliberately randomized per draw, so determinism has to come from the entropy
    # source: the point of this test is that the two CALLERS agree, not that a nonce repeats.
    import secrets as _secrets

    monkeypatch.setattr(_secrets, "randbelow", lambda n: 7 % n)
    monkeypatch.setattr(_secrets, "token_bytes", lambda n: bytes(range(n % 256)) * (n // 256 + 1))
    monkeypatch.setattr(_secrets, "token_hex", lambda n: "ab" * n)

    def build(mount: Path, *, on_params=None) -> str:
        mount.mkdir(parents=True, exist_ok=True)
        return prepare_agent_workspace(
            suite, arm_config, "devnet", mount,
            rpc_client=fake_rpc, harness_tip=4242, on_params=on_params,
        )

    accepted = tmp_path / "accepted"
    diagnostic = tmp_path / "diagnostic"
    kept: list = []

    # The accepted path additionally keeps verifier-private state; that must not change what the
    # AGENT sees.
    accepted_pointer = build(accepted, on_params=lambda task, params: (kept.append(task.id), params)[1])
    diagnostic_pointer = build(diagnostic)

    assert kept == [task.id for task in suite.tasks]
    accepted_files = sorted(p.name for p in accepted.iterdir())
    assert accepted_files == sorted(p.name for p in diagnostic.iterdir())
    for name in accepted_files:
        assert (accepted / name).read_bytes() == (diagnostic / name).read_bytes(), name
    assert accepted_pointer.replace(str(accepted), "") == diagnostic_pointer.replace(
        str(diagnostic), ""
    )


def test_the_cleanup_image_is_the_suite_pin_not_an_ambient_override(cli_harness, tmp_path,
                                                                    monkeypatch):
    """`resolve_agent_image()` gives the ambient override precedence; the parent must not."""
    from ckbbench.suite.registry import load_suite

    pin = load_suite(Path("suites/ckb-v1")).pins.agent_image_digest
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    code = diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])
    assert code == 0
    assert cli_harness["supervisor"].cleanup_image == pin


def test_an_ambient_image_override_is_rejected_before_any_external_action(tmp_path, monkeypatch):
    reached = {"supervisor": False}

    def factory(**kwargs):
        reached["supervisor"] = True
        return _Recorder(**kwargs)

    monkeypatch.setattr(diagnose_cli, "Supervisor", factory)
    monkeypatch.setattr(diagnose_cli, "_spawn_worker", lambda *a, **k: object())
    monkeypatch.setenv("CKBBENCH_AGENT_IMAGE", "sha256:" + "ab" * 32)

    code = diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])
    assert code == 1
    assert reached["supervisor"] is False, "an image override reached the supervisor"


def test_the_worker_and_cleanup_agree_on_the_frozen_image(cli_harness, tmp_path, monkeypatch):
    """The child creates from the suite pin, so the parent must expect exactly that."""
    from ckbbench.suite.registry import load_suite

    pin = load_suite(Path("suites/ckb-v1")).pins.agent_image_digest
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])

    identity = cli_harness["supervisor"].identity
    env = diagnose_cli.child_environment(identity, dict(__import__("os").environ))
    assert "CKBBENCH_AGENT_IMAGE" not in env
    assert cli_harness["supervisor"].cleanup_image == pin


# --- production-entry descriptor inheritance -------------------------------------------------------


def test_spawn_worker_passes_both_descriptors_in_pass_fds_and_env(tmp_path, monkeypatch):
    """Every earlier CLI test stubbed `_spawn_worker`, so nothing proved the real Popen contract.

    Both inherited descriptors are asserted: the artifact directory and the candidate receipt. The
    earlier form called the no-receipt signature, so the receipt half went unproved at this boundary.
    """
    import os as _os
    import subprocess as _subprocess

    from ckbbench.run.diagnose import (
        ARTIFACT_FD_ENV, RECEIPT_FD_ENV, DirHandle, prepare_directory,
    )

    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    handle = DirHandle(identity.final_path.parent)
    read_fd, write_fd = _os.pipe()
    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

    monkeypatch.setattr(_subprocess, "Popen", _FakePopen)
    try:
        diagnose_cli._spawn_worker(identity, Path("."), handle.fd, write_fd)
    finally:
        _os.close(read_fd)
        _os.close(write_fd)

    assert captured["kwargs"]["pass_fds"] == (handle.fd, write_fd), "a descriptor was not passed"
    assert captured["kwargs"]["env"][ARTIFACT_FD_ENV] == str(handle.fd)
    assert captured["kwargs"]["env"][RECEIPT_FD_ENV] == str(write_fd)
    assert captured["argv"][1:] == ["-m", "ckbbench.run.diagnose_worker"]
    # No credential, path or provider material in argv.
    assert not any("sk-" in part or "/diagnostic/" in part for part in captured["argv"])
    handle.close()


def test_the_worker_adopts_the_inherited_descriptor_rather_than_reopening(tmp_path, monkeypatch):
    """The worker must never call `DirHandle(path.parent)`; it adopts what the parent passed."""
    import os as _os

    from ckbbench.run.diagnose import (
        ARTIFACT_FD_ENV,
        DirHandle,
        inherited_artifact_dir,
        prepare_directory,
    )

    identity = _identity(tmp_path)
    prepare_directory(identity.final_path.parent)
    parent_handle = DirHandle(identity.final_path.parent)
    inherited_fd = _os.dup(parent_handle.fd)
    monkeypatch.setenv(ARTIFACT_FD_ENV, str(inherited_fd))

    adopted = inherited_artifact_dir()
    try:
        assert adopted.identity == parent_handle.identity
        assert adopted.fd == inherited_fd
        # Its recorded path is the descriptor, not a pathname it could reopen.
        assert "inherited fd" in str(adopted.path)
    finally:
        adopted.close()
        parent_handle.close()


def test_the_worker_source_never_reopens_the_artifact_directory():
    source = Path(diagnose_cli.__file__).parent.joinpath("diagnose_worker.py").read_text()
    assert "inherited_artifact_dir()" in source
    assert "DirHandle(" not in source, "the worker opened its own handle"


def test_a_missing_inherited_descriptor_refuses_the_worker(monkeypatch):
    from ckbbench.run.diagnose import ARTIFACT_FD_ENV, DiagnosticAbort, inherited_artifact_dir

    monkeypatch.delenv(ARTIFACT_FD_ENV, raising=False)
    with pytest.raises(DiagnosticAbort):
        inherited_artifact_dir()


# --- scrubbing is not a side effect of publication -------------------------------------------------


def _refusing_factory(made: dict, *, publish_raises: bool):
    def factory(**kwargs):
        recorder = _Recorder(**kwargs)
        if publish_raises:
            def refuse():
                raise DiagnosticAbort("publication refused")

            recorder.publish = refuse
        made["supervisor"] = recorder
        return recorder

    return factory


def test_the_cli_scrubs_the_run_directory_exactly_once_when_healthy(cli_harness, tmp_path):
    assert diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")]) == 0
    assert cli_harness["supervisor"].scrubs == 1


def test_the_cli_scrubs_the_run_directory_when_publication_is_refused(tmp_path, monkeypatch):
    """A canonical-path mismatch or a planted selector must not leave raw run data behind."""
    made: dict = {}
    monkeypatch.setattr(diagnose_cli, "Supervisor", _refusing_factory(made, publish_raises=True))
    monkeypatch.setattr(diagnose_cli, "_spawn_worker", lambda *a, **k: object())

    code = diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])

    assert code == 1
    assert made["supervisor"].scrubs == 1, "a refused publication skipped the scrub"


def test_the_cli_scrubs_the_run_directory_when_the_deadline_leaves_no_budget(tmp_path, monkeypatch):
    """An expired deadline returns before publication; raw data must still be gone."""
    made: dict = {}
    monkeypatch.setattr(diagnose_cli, "Supervisor", _refusing_factory(made, publish_raises=False))
    monkeypatch.setattr(diagnose_cli, "_spawn_worker", lambda *a, **k: object())
    monkeypatch.setattr(diagnose_cli, "DIAGNOSTIC_DEADLINE_S", 0.0)

    code = diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])

    assert code == 1
    assert made["supervisor"].published is False, "published without a deadline budget"
    assert made["supervisor"].scrubs == 1, "an expired deadline skipped the scrub"


# --- cleanup consumes creation evidence before the scrub destroys it -------------------------------


class _FakeDocker:
    """Just enough docker for the cleanup path: nothing this run created is present."""

    def __init__(self, raises: bool = False):
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if self.raises:
            raise OSError("synthetic docker failure")
        if argv[1] == "ps":
            return _Completed(0, "", "")
        if argv[1:3] == ["container", "inspect"]:
            return _Completed(1, "", f"Error: No such container: {argv[3]}")
        if argv[1:3] == ["volume", "inspect"]:
            return _Completed(1, "", f"Error: No such volume: {argv[3]}")
        raise AssertionError(f"unexpected docker call: {argv}")


class _Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _ExitedProc:
    pid = 4242

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _real_supervisor_harness(monkeypatch, docker, *, marker: str | None = "node"):
    """`main()` with the REAL Supervisor, a fake docker runner and a worker that only marks state."""
    from ckbbench.run.diagnose import Supervisor, mark_created as real_mark_created

    made: dict = {}
    real_supervisor = Supervisor

    def factory(**kwargs):
        supervisor = real_supervisor(run=docker, kill=lambda pid, sig: None,
                                     sleep=lambda _s: None, **kwargs)
        made["supervisor"] = supervisor
        made["identity"] = kwargs["identity"]
        made["scrubs"] = 0
        real_scrub = supervisor.scrub_run_dir_once

        def counting_scrub():
            made["scrubs"] += 1
            return real_scrub()

        supervisor.scrub_run_dir_once = counting_scrub
        return supervisor

    def spawn(identity, *_a, **_k):
        if marker is not None:
            real_mark_created(identity.created_dir, marker)
        return _ExitedProc()

    monkeypatch.setattr(diagnose_cli, "Supervisor", factory)
    monkeypatch.setattr(diagnose_cli, "_spawn_worker", spawn)
    return made


def test_the_cli_reads_creation_markers_before_the_scrub_destroys_them(tmp_path, monkeypatch):
    """`created/` lives inside the run directory, so scrubbing first erases the evidence.

    A container the worker created and that then disappeared must fail as an unexplained
    disappearance, not be recorded as ordinary absence.
    """
    docker = _FakeDocker()
    made = _real_supervisor_harness(monkeypatch, docker, marker="node")

    code = diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])
    supervisor = made["supervisor"]

    assert code == 1
    assert any(o.name == NODE_SERVICE and o.action == "failed"
               and o.detail == "disappeared unexplained" for o in supervisor.outcomes), (
        f"creation evidence was lost before cleanup read it: {supervisor.outcomes}"
    )
    assert supervisor.cleanup_ok is False
    assert made["scrubs"] == 1, "the run directory was not scrubbed exactly once"
    assert supervisor.run_dir_scrubbed is True


def test_a_resource_never_created_is_ordinary_absence(tmp_path, monkeypatch):
    """The other half of the distinction: no marker means absent, not a failure."""
    docker = _FakeDocker()
    made = _real_supervisor_harness(monkeypatch, docker, marker=None)

    diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])
    supervisor = made["supervisor"]

    named = {made["identity"].agent_name, MINER_SERVICE, NODE_SERVICE}
    assert all(o.action == "absent" for o in supervisor.outcomes if o.name in named)
    assert made["scrubs"] == 1


def test_the_markers_are_scrubbed_after_cleanup_has_consumed_them(tmp_path, monkeypatch):
    """Ordering, not omission: the acknowledgements must not survive the command either."""
    docker = _FakeDocker()
    made = _real_supervisor_harness(monkeypatch, docker, marker="node")

    diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])
    identity = made["identity"]

    assert (identity.created_dir / "node").read_bytes() == b"", "a raw marker survived the command"
    assert read_created(identity.created_dir) == set()


def test_the_cli_still_scrubs_when_docker_cleanup_raises(tmp_path, monkeypatch):
    docker = _FakeDocker(raises=True)
    made = _real_supervisor_harness(monkeypatch, docker, marker="node")

    code = diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])

    assert code == 1
    assert made["scrubs"] == 1, "a raising cleanup skipped the scrub"
    assert made["supervisor"].cleanup_ok is False


def test_the_cli_still_scrubs_when_the_deadline_is_exhausted(tmp_path, monkeypatch):
    docker = _FakeDocker()
    made = _real_supervisor_harness(monkeypatch, docker, marker="node")
    monkeypatch.setattr(diagnose_cli, "DIAGNOSTIC_DEADLINE_S", 0.0)

    code = diagnose_cli.main(["--artifact-root", str(tmp_path / "artifacts")])

    assert code == 1
    assert made["scrubs"] == 1, "an exhausted deadline skipped the scrub"
    assert not made["identity"].final_path.exists(), "published without a deadline budget"
