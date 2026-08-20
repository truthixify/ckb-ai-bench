"""Executable worker tests: the real `_run_cell()` driven against in-memory fakes.

Source-text assertions certified calls that could not execute — the worker raised
`UnboundLocalError` before writing a marker, allowlist or workspace. These invoke the real function.
No Docker, socket, provider, MCP or RPC path is reached.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ckbbench.run import diagnose_worker
from ckbbench.run.diagnose import (
    DiagnosticAbort,
    DiagnosticIdentity,
    mark_created,
    prepare_directory,
    read_created,
)
from ckbbench.run.diagnostic import DiagnosticSession

EXEC_ID = "0123456789abcdef0123456789abcdef"
RUN_ID = "2.0.0-devnet-B-gpt-5.6-sol-s1-1786900000"


class FakeAgent:
    def __init__(self):
        self.model = _FakeModel()
        self.ran_with = None

    def run(self, pointer):
        self.ran_with = pointer
        return {}


class _FakeModel:
    def __init__(self):
        self.attached = None

    def attach_diagnostic(self, session, seam):
        self.attached = (session, seam)


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """Parent-selected identity plus every external boundary replaced in memory."""
    identity = DiagnosticIdentity.create(
        run_id=RUN_ID, artifact_root=tmp_path, run_dir=tmp_path / "run", execution_id=EXEC_ID,
    )
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    prepare_directory(identity.mount_dir, exclusive=True)
    prepare_directory(identity.allowlist_dir, exclusive=True)
    prepare_directory(identity.created_dir, exclusive=True)

    events: dict = {"devnet": 0, "agent": None, "seam": 0}

    def fake_prepare_devnet(*, rpc_url, on_created=None, **_kw):
        """Announces per service, exactly as the real `_compose_up()` seam does."""
        events["devnet"] += 1
        if on_created is not None:
            on_created("ckbbench-devnet-node", "n" * 64)
            if events.get("fail_between_services"):
                raise RuntimeError("failed while proving the second service")
            on_created("ckbbench-devnet-miner", "m" * 64)
        if events.get("devnet_fails"):
            raise RuntimeError("devnet failed after creation")
        return object()

    def fake_rpc_factory(url):
        def call(method, params):
            if method == "get_tip_block_number":
                return hex(4242)
            return None

        return call

    def fake_factory(**_kw):
        def build(**_cell):
            events["agent"] = FakeAgent()
            return events["agent"]

        return build

    monkeypatch.setattr("ckbbench.run.devnet.prepare_devnet", fake_prepare_devnet)
    monkeypatch.setattr("ckbbench.ckb_rpc.make_rpc_client", fake_rpc_factory)
    monkeypatch.setattr("ckbbench.run.agent_factory.make_agent_factory", fake_factory)
    monkeypatch.setattr(diagnose_worker, "_seam_controller",
                        lambda: events.__setitem__("seam", events["seam"] + 1) or _Seam())

    env = identity.worker_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return identity, env, events


class _Seam:
    def begin_attempt(self):
        return None

    def end_attempt(self):
        return "not_started"


def test_the_real_worker_cell_runs_end_to_end(worker_env):
    """This is the test that would have caught the UnboundLocalError."""
    identity, env, events = worker_env
    session = DiagnosticSession()

    diagnose_worker._run_cell(session, env)

    assert events["devnet"] == 1
    assert events["agent"] is not None, "the worker never reached the agent"
    assert events["agent"].ran_with, "agent.run() was not called with a pointer"
    assert events["agent"].model.attached is not None
    assert events["agent"].model.attached[0] is session


def test_the_worker_records_every_creation_event(worker_env):
    identity, env, events = worker_env
    diagnose_worker._run_cell(DiagnosticSession(), env)
    assert read_created(identity.created_dir) == {"node", "miner", "agent"}


def test_devnet_creation_is_recorded_before_preparation_can_fail(worker_env):
    """A failure after the containers exist must still leave them recorded."""
    identity, env, events = worker_env
    events["devnet_fails"] = True

    with pytest.raises(RuntimeError):
        diagnose_worker._run_cell(DiagnosticSession(), env)

    assert read_created(identity.created_dir) == {"node", "miner"}
    assert "agent" not in read_created(identity.created_dir)


def test_a_failure_between_services_keeps_the_first_acknowledgement(worker_env):
    """Proving the second service can fail; the first one still exists and must stay recorded."""
    identity, env, events = worker_env
    events["fail_between_services"] = True

    with pytest.raises(RuntimeError):
        diagnose_worker._run_cell(DiagnosticSession(), env)

    assert read_created(identity.created_dir) == {"node"}


def test_the_worker_writes_the_exact_parent_selected_allowlist(worker_env):
    identity, env, events = worker_env
    diagnose_worker._run_cell(DiagnosticSession(), env)

    assert identity.allowlist_path.is_file()
    assert identity.allowlist_path.name == f"allowlist.{EXEC_ID}.built"
    # No random leaf beside it: the parent chose the only name.
    assert [p.name for p in identity.allowlist_dir.iterdir()] == [identity.allowlist_path.name]
    assert identity.allowlist_path.read_text().strip(), "the allowlist is empty"


def test_the_worker_writes_the_agent_visible_workspace(worker_env):
    identity, env, events = worker_env
    diagnose_worker._run_cell(DiagnosticSession(), env)

    written = sorted(p.name for p in identity.mount_dir.iterdir())
    assert "INSTRUCTIONS.md" in written
    assert any(name.endswith(".json") for name in written), "no prompt-injected task files"
    # Verifier-private material never reaches the agent's mount.
    for name in written:
        if name.endswith(".json"):
            payload = json.loads((identity.mount_dir / name).read_text())
            assert "BENCH_PASSWORD" not in json.dumps(payload)


def test_an_allowlist_that_already_exists_is_refused(worker_env):
    identity, env, events = worker_env
    identity.allowlist_path.write_text("NOT OURS")
    with pytest.raises(DiagnosticAbort):
        diagnose_worker._run_cell(DiagnosticSession(), env)
    assert identity.allowlist_path.read_text() == "NOT OURS"


# --- marker protocol -------------------------------------------------------------------------------


def test_a_marker_write_failure_fails_closed(tmp_path):
    """Swallowing every error as 'already recorded' would lose creation state silently."""
    missing = tmp_path / "never-created"
    with pytest.raises(DiagnosticAbort):
        mark_created(missing, "node")


def test_a_foreign_marker_is_refused(tmp_path):
    created = tmp_path / "created"
    prepare_directory(created, exclusive=True)
    (created / "node").write_bytes(b"SOMEONE ELSE")
    with pytest.raises(DiagnosticAbort):
        mark_created(created, "node")


def test_marking_twice_is_accepted(tmp_path):
    created = tmp_path / "created"
    prepare_directory(created, exclusive=True)
    mark_created(created, "node")
    mark_created(created, "node")
    assert read_created(created) == {"node"}


# --- review-revision-9: the worker reports the inode it created ------------------------------------


def _worker_main_env(monkeypatch, identity, receipt_fd, artifact_fd):
    """`main()` takes ownership of the artifact descriptor, so it is handed a duplicate."""
    import os

    from ckbbench.run.diagnose import ARTIFACT_FD_ENV, RECEIPT_FD_ENV, WORKER_MODE_ENV

    monkeypatch.setenv(WORKER_MODE_ENV, "1")
    if artifact_fd is not None:
        monkeypatch.setenv(ARTIFACT_FD_ENV, str(os.dup(artifact_fd)))
    else:
        monkeypatch.delenv(ARTIFACT_FD_ENV, raising=False)
    if receipt_fd is not None:
        monkeypatch.setenv(RECEIPT_FD_ENV, str(receipt_fd))
    else:
        monkeypatch.delenv(RECEIPT_FD_ENV, raising=False)
    for key, value in identity.worker_env().items():
        monkeypatch.setenv(key, value)


def test_the_worker_reports_the_exact_inode_it_created(tmp_path, monkeypatch):
    """The parent has no other way to learn WHICH file the worker wrote."""
    import os

    from ckbbench.run.diagnose import DirHandle, decode_receipt, read_receipt

    identity = DiagnosticIdentity.create(
        run_id=RUN_ID, artifact_root=tmp_path, run_dir=tmp_path / "run", execution_id=EXEC_ID,
    )
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    prepare_directory(identity.created_dir, exclusive=True)

    artifact_dir = DirHandle(identity.final_path.parent)
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    _worker_main_env(monkeypatch, identity, write_fd, artifact_dir.fd)
    monkeypatch.setattr(diagnose_worker, "_run_cell",
                        lambda session, ident: (_ for _ in ()).throw(DiagnosticAbort("stop")))
    # `main()` owns and closes both inherited descriptors, exactly as the real child does.
    try:
        code = diagnose_worker.main()
        received = read_receipt(read_fd)
    finally:
        os.close(read_fd)
        artifact_dir.close()

    info = os.stat(identity.candidate_path)
    assert code == 0
    assert received == (info.st_dev, info.st_ino), "the receipt does not name the created file"
    assert decode_receipt(b"") is None


def test_a_worker_without_a_receipt_descriptor_refuses(tmp_path, monkeypatch):
    import os

    from ckbbench.run.diagnose import DirHandle

    identity = DiagnosticIdentity.create(
        run_id=RUN_ID, artifact_root=tmp_path, run_dir=tmp_path / "run", execution_id=EXEC_ID,
    )
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    prepare_directory(identity.created_dir, exclusive=True)

    artifact_dir = DirHandle(identity.final_path.parent)
    _worker_main_env(monkeypatch, identity, None, artifact_dir.fd)
    monkeypatch.setattr(
        diagnose_worker, "_run_cell",
        lambda *_args: pytest.fail("the worker entered the cell without a receipt capability"),
    )
    try:
        code = diagnose_worker.main()
    finally:
        artifact_dir.close()

    assert code == 5
    assert not identity.candidate_path.exists(), "wrote a candidate it could never report"
    del os


def test_a_worker_without_an_artifact_descriptor_refuses_before_the_cell(tmp_path, monkeypatch):
    import os

    identity = DiagnosticIdentity.create(
        run_id=RUN_ID, artifact_root=tmp_path, run_dir=tmp_path / "run", execution_id=EXEC_ID,
    )
    prepare_directory(identity.final_path.parent)
    prepare_directory(identity.run_dir, exclusive=True)
    prepare_directory(identity.created_dir, exclusive=True)

    read_fd, write_fd = os.pipe()
    _worker_main_env(monkeypatch, identity, write_fd, None)
    monkeypatch.setattr(
        diagnose_worker, "_run_cell",
        lambda *_args: pytest.fail("the worker entered the cell without an artifact capability"),
    )
    try:
        code = diagnose_worker.main()
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert code == 4
    assert not identity.candidate_path.exists(), "wrote a candidate without an artifact capability"
