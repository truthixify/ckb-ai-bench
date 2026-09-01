"""Code-task verifier tests: hidden suite isolation and rebuild-from-sources policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from ckbbench.suite.model import Task
from ckbbench.run.runner import PrepareError
from ckbbench.verify.codetask import (
    BENCH_PASSWORD_ENV,
    RunnerInvocation,
    _assert_build_policy,
    _assert_verify_policy,
    grade_code_task,
    prepare_artifact_dir,
)
from ckbbench.verify.verifier import verify_task


def _code_task(proof_file: str = "build/release/hashlock") -> Task:
    return Task(
        id="hashlock",
        prompt_fragment="Build the contract.",
        score=20,
        proof_file=proof_file,
        kind="code",
        verifier="hidden",
    )


def test_grade_code_task_pass_and_isolation(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite = tmp_path / "registry" / "hashlock" / "hidden"
    suite.mkdir(parents=True)
    # Artifact lives outside agent mount (ownership-clearable host dir).
    artifact = mount.parent / ".ckbbench-artifact"
    binary = artifact / "build" / "release" / "hashlock"

    calls: list[RunnerInvocation] = []

    def fake_runner(inv: RunnerInvocation) -> int:
        calls.append(inv)
        if inv.stage == "build":
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("elf")
            return 0
        return 0

    v = grade_code_task(
        _code_task(),
        mount,
        suite,
        {BENCH_PASSWORD_ENV: "fresh-secret-never-in-agent-stage"},
        fake_runner,
    )
    assert v.passed
    assert len(calls) == 2

    build, verify = calls
    assert build.stage == "build"
    assert verify.stage == "verify"
    assert str(suite.resolve()) not in build.mounts
    assert BENCH_PASSWORD_ENV not in build.env
    assert str(suite.resolve()) in verify.mounts
    assert verify.env[BENCH_PASSWORD_ENV] == "fresh-secret-never-in-agent-stage"
    assert verify.mounts[str(artifact.resolve())].endswith(":ro")


def test_grade_code_task_fail_exit(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite = tmp_path / "suite"
    suite.mkdir()
    art = mount.parent / ".ckbbench-artifact"

    def fake_runner(inv: RunnerInvocation) -> int:
        if inv.stage == "build":
            (art / "build" / "release").mkdir(parents=True)
            (art / "build" / "release" / "hashlock").write_text("x")
            return 0
        return 101

    v = grade_code_task(_code_task(), mount, suite, {BENCH_PASSWORD_ENV: "pw"}, fake_runner)
    assert not v.passed
    assert "exit 101" in v.reason


def test_grade_code_task_build_failure(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite = tmp_path / "suite"
    suite.mkdir()

    v = grade_code_task(
        _code_task(),
        mount,
        suite,
        {BENCH_PASSWORD_ENV: "pw"},
        lambda inv: 1 if inv.stage == "build" else 0,
    )
    assert not v.passed
    assert "rebuild from sources failed" in v.reason


def test_grade_code_task_missing_password(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite = tmp_path / "suite"
    suite.mkdir()
    v = grade_code_task(_code_task(), mount, suite, {}, lambda inv: 0)
    assert not v.passed
    assert BENCH_PASSWORD_ENV in v.reason


def test_grade_code_task_bench_password_alias(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite = tmp_path / "suite"
    suite.mkdir()
    calls: list[RunnerInvocation] = []
    art = mount.parent / ".ckbbench-artifact"

    def runner(inv: RunnerInvocation) -> int:
        calls.append(inv)
        if inv.stage == "build":
            p = art / "build" / "release" / "hashlock"
            p.parent.mkdir(parents=True)
            p.write_text("b")
        return 0

    v = grade_code_task(_code_task(), mount, suite, {"bench_password": "alias-pw"}, runner)
    assert v.passed
    assert calls[1].env[BENCH_PASSWORD_ENV] == "alias-pw"


def test_prepare_artifact_dir_clears_prior(tmp_path: Path):
    path = tmp_path / "artifact"
    path.mkdir()
    stale = path / "old.bin"
    stale.write_text("stale")
    prepare_artifact_dir(path)
    assert path.is_dir()
    assert not stale.exists()


def test_verify_task_prepare_error_propagates(tmp_path: Path):
    """WHY: prepare failures must become infra_fail, not swallowed agent_fail Verdicts."""
    mount = tmp_path / "mount"
    mount.mkdir()
    suite_dir = tmp_path / "hashlock" / "hidden"
    suite_dir.mkdir(parents=True)

    def boom(_inv: RunnerInvocation) -> int:
        raise PrepareError("volume stuck")

    with pytest.raises(PrepareError, match="volume stuck"):
        verify_task(
            _code_task(),
            mount,
            {BENCH_PASSWORD_ENV: "pw"},
            lambda m, p: None,
            registry_root=tmp_path,
            runner=boom,
        )


def test_assert_build_policy_rejects_suite_mount(tmp_path: Path):
    suite = tmp_path / "suite"
    suite.mkdir()
    inv = RunnerInvocation(
        stage="build",
        mounts={str(suite.resolve()): "/suite"},
        env={},
        command=("make", "build"),
    )
    assert "hidden suite" in _assert_build_policy(inv, suite)


def test_assert_build_policy_rejects_suite_disguised_as_sources(tmp_path: Path):
    # The runner's mount-target allowlist permits /sources, so a caller mounting the suite host
    # path there would pass a target-only check. The code-task layer must also check the host path.
    suite = tmp_path / "suite"
    suite.mkdir()
    inv = RunnerInvocation(
        stage="build",
        mounts={str(suite.resolve()): "/sources:ro"},  # suite host path, allowed target
        env={},
        command=("make", "build"),
    )
    assert "hidden suite" in _assert_build_policy(inv, suite)


def test_assert_build_policy_rejects_password_in_build():
    suite = Path("/tmp/suite")
    inv = RunnerInvocation(
        stage="build",
        mounts={},
        env={BENCH_PASSWORD_ENV: "leak"},
        command=("make", "build"),
    )
    assert BENCH_PASSWORD_ENV in (_assert_build_policy(inv, suite) or "")


def test_assert_verify_policy_requires_artifact_mount(tmp_path: Path):
    suite = tmp_path / "suite"
    suite.mkdir()
    art = str((tmp_path / "artifact").resolve())
    inv = RunnerInvocation(
        stage="verify",
        mounts={str(suite.resolve()): "/suite"},
        env={BENCH_PASSWORD_ENV: "x"},
        command=(),
    )
    assert "agent artifact" in (_assert_verify_policy(inv, suite, art) or "")


def test_grade_code_task_build_policy_violation(tmp_path: Path, monkeypatch):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite = tmp_path / "suite"
    suite.mkdir()
    monkeypatch.setattr(
        "ckbbench.verify.codetask._assert_build_policy",
        lambda inv, hidden: "build policy violated",
    )
    v = grade_code_task(_code_task(), mount, suite, {BENCH_PASSWORD_ENV: "pw"}, lambda inv: 0)
    assert not v.passed
    assert "build policy violated" in v.reason


def test_grade_code_task_verify_policy_violation(tmp_path: Path, monkeypatch):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite = tmp_path / "suite"
    suite.mkdir()
    monkeypatch.setattr("ckbbench.verify.codetask._assert_build_policy", lambda inv, hidden: None)
    monkeypatch.setattr(
        "ckbbench.verify.codetask._assert_verify_policy",
        lambda inv, hidden, art: "verify policy violated",
    )
    v = grade_code_task(_code_task(), mount, suite, {BENCH_PASSWORD_ENV: "pw"}, lambda inv: 0)
    assert not v.passed
    assert "verify policy violated" in v.reason


def test_grade_code_task_proof_falls_back_to_relpath(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite = tmp_path / "suite"
    suite.mkdir()

    def runner(inv: RunnerInvocation) -> int:
        if inv.stage == "build":
            return 0
        return 0

    v = grade_code_task(
        _code_task(proof_file="build/release/missing-binary"),
        mount,
        suite,
        {BENCH_PASSWORD_ENV: "pw"},
        runner,
    )
    assert v.passed
    assert v.proof == "build/release/missing-binary"


def test_assert_verify_policy_requires_suite(tmp_path: Path):
    suite = tmp_path / "suite"
    suite.mkdir()
    art = tmp_path / "artifact"
    inv = RunnerInvocation(stage="verify", mounts={}, env={BENCH_PASSWORD_ENV: "x"}, command=())
    assert "hidden suite" in (_assert_verify_policy(inv, suite, str(art)) or "")


def test_assert_verify_policy_requires_ro_artifact(tmp_path: Path):
    suite = tmp_path / "suite"
    suite.mkdir()
    art = str((tmp_path / "artifact").resolve())
    inv = RunnerInvocation(
        stage="verify",
        mounts={str(suite.resolve()): "/suite", art: "/artifact"},
        env={BENCH_PASSWORD_ENV: "x"},
        command=(),
    )
    assert "read-only" in (_assert_verify_policy(inv, suite, art) or "")


def test_assert_verify_policy_requires_password(tmp_path: Path):
    suite = tmp_path / "suite"
    suite.mkdir()
    art = str((tmp_path / "artifact").resolve())
    inv = RunnerInvocation(
        stage="verify",
        mounts={str(suite.resolve()): "/suite", art: "/artifact:ro"},
        env={},
        command=(),
    )
    assert BENCH_PASSWORD_ENV in (_assert_verify_policy(inv, suite, art) or "")


def test_grade_uses_custom_artifact_dir(tmp_path: Path):
    mount = tmp_path / "mount"
    mount.mkdir()
    suite = tmp_path / "suite"
    suite.mkdir()
    artifact = tmp_path / "custom-artifact"
    (artifact / "build" / "release").mkdir(parents=True)
    (artifact / "build" / "release" / "hashlock").write_text("bin")

    v = grade_code_task(
        _code_task(),
        mount,
        suite,
        {BENCH_PASSWORD_ENV: "pw"},
        lambda inv: 0,
        artifact_dir=artifact,
    )
    assert v.passed
    assert str(artifact.resolve()) in v.proof or "hashlock" in v.proof
