"""Docker runner tests: command construction and retry policy (no real docker calls).

Encodes WHY (Rule 9): graded cargo stages use image-local cargo (no shared volume),
--network none, non-root --user, ownership-neutral copy, and prepare failures raise
PrepareError for infra_fail scoring — not hot-path chown.
"""

from __future__ import annotations

import pytest

from ckbbench.verify.codetask import BENCH_PASSWORD_ENV, RunnerInvocation

from ckbbench.run.runner import (
    GRADE_NETWORK_NONE,
    PrepareError,
    RunnerConfig,
    build_docker_argv,
    build_stage_argv,
    invoke_runner,
    make_docker_runner,
    prepare_work_volume,
    run_with_retries,
    verify_stage_argv,
)


def _inv(
    stage: str,
    *,
    mounts: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    command: tuple[str, ...] = ("make", "build"),
) -> RunnerInvocation:
    return RunnerInvocation(stage=stage, mounts=mounts or {}, env=env or {}, command=command)


def _cfg() -> RunnerConfig:
    return RunnerConfig(
        agent_image="ckbbench-agent:test",
        verifier_image="ckbbench-verifier:test",
        network="ckbbench-net-internal",
        cargo_volume="ckbbench-cargo-test",
        work_volume="ckbbench-work-test",
        uid=1000,
        gid=1000,
        max_build_retries=3,
    )


def test_build_docker_argv_renders_flags_mounts_env_image_command():
    inv = _inv(
        "verify",
        mounts={"/host/src": "/sources:ro", "/host/out": "/artifact:ro"},
        env={"TOP": "/artifact", BENCH_PASSWORD_ENV: "secret"},
        command=("cargo", "test", "--release"),
    )
    cfg = _cfg()
    argv = build_docker_argv(inv, cfg, image=cfg.verifier_image, workdir="/suite")

    assert argv[:5] == ["docker", "run", "--rm", "--user", "1000:1000"]
    net_idx = argv.index("--network")
    assert argv[net_idx + 1] == GRADE_NETWORK_NONE
    assert "/host/src:/sources:ro" in argv
    assert "/host/out:/artifact:ro" in argv
    env_pairs = [f"{argv[i]} {argv[i + 1]}" for i, token in enumerate(argv) if token == "-e"]
    assert f"-e {BENCH_PASSWORD_ENV}=secret" in env_pairs
    assert "-w" in argv and "/suite" in argv
    assert cfg.verifier_image in argv
    assert argv[-3:] == ["cargo", "test", "--release"]


def test_build_stage_no_cargo_vol_network_none_ownership_neutral_copy():
    """WHY: graded rebuild must not share cargo with verify; offline + non-root copy."""
    inv = _inv(
        "build",
        mounts={"/host/ws": "/sources:ro", "/host/art": "/artifact"},
        env={},
    )
    argv = build_stage_argv(inv, _cfg())

    joined = " ".join(argv)
    assert BENCH_PASSWORD_ENV not in joined
    assert "/suite" not in joined
    assert "ckbbench-agent:test" in argv
    assert "ckbbench-work-test:/work" in argv
    # No shared durable cargo volume on grade argv.
    assert "/cargo" not in joined
    assert "CARGO_HOME=/cargo" not in joined
    assert "ckbbench-cargo-test" not in joined
    net_idx = argv.index("--network")
    assert argv[net_idx + 1] == "none"
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert "0:0" not in argv
    env_pairs = [f"{a} {argv[i+1]}" for i, a in enumerate(argv) if a == "-e"]
    assert "-e CARGO_NET_OFFLINE=true" in env_pairs
    script = argv[argv.index("-c") + 1]
    assert "cp -a --no-preserve=ownership" in script
    assert "chown" not in script
    # Must not use plain ownership-preserving cp -a for sources.
    assert "cp -a /sources" not in script


def test_verify_stage_no_cargo_vol_network_none():
    inv = _inv(
        "verify",
        mounts={"/host/suite": "/suite", "/host/art": "/artifact:ro"},
        env={BENCH_PASSWORD_ENV: "pw", "TOP": "/artifact", "MODE": "release"},
        command=("cargo", "test", "--release"),
    )
    argv = verify_stage_argv(inv, _cfg())

    assert "/host/art:/artifact:ro" in argv
    assert "/host/suite:/suite" in argv
    env_pairs = [f"{a} {argv[i+1]}" for i, a in enumerate(argv) if a == "-e"]
    assert f"-e {BENCH_PASSWORD_ENV}=pw" in env_pairs
    assert "-e CARGO_NET_OFFLINE=true" in env_pairs
    assert "ckbbench-verifier:test" in argv
    assert "-w" in argv and "/suite" in argv
    joined = " ".join(argv)
    assert "/cargo" not in joined
    assert "ckbbench-cargo" not in joined
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "1000:1000"


def test_run_with_retries_succeeds_on_third_attempt():
    calls: list[list[str]] = []

    def seam(argv):
        calls.append(list(argv))
        return (1, "fail") if len(calls) < 3 else (0, "ok")

    code = run_with_retries(["docker", "run"], seam, max_attempts=3)
    assert code == 0
    assert len(calls) == 3


def test_run_with_retries_does_not_retry_immediate_success():
    calls: list[list[str]] = []

    def seam(argv):
        calls.append(list(argv))
        return (0, "")

    code = run_with_retries(["docker", "run"], seam, max_attempts=3)
    assert code == 0
    assert len(calls) == 1


def test_run_with_retries_surfaces_failure_after_three_real_failures(capsys):
    def seam(argv):
        return (2, "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\nline11")

    code = run_with_retries(["docker", "run"], seam, max_attempts=3)
    assert code == 2
    out = capsys.readouterr().out
    assert "failed after 3 attempts" in out
    assert "line11" in out


def test_prepare_work_volume_checked_rm_create():
    """WHY: fail-open volume reuse leaves root-owned trees; prepare must check remove."""
    recorded: list[list[str]] = []
    present = {"ckbbench-work-test": True}

    def seam(argv):
        recorded.append(list(argv))
        if argv[:3] == ["docker", "volume", "rm"]:
            present[argv[3] if argv[3] != "-f" else argv[4]] = False
            return (0, "")
        if argv[:3] == ["docker", "volume", "inspect"]:
            name = argv[3]
            return (0, "{}") if present.get(name) else (1, "Error: No such volume: " + name)
        if argv[:3] == ["docker", "volume", "create"]:
            present[argv[3]] = True
            return (0, argv[3])
        return (0, "")

    prepare_work_volume("ckbbench-work-test", seam)
    assert ["docker", "volume", "rm", "-f", "ckbbench-work-test"] in recorded
    assert ["docker", "volume", "create", "ckbbench-work-test"] in recorded
    # No hot-path chown.
    assert not any("chown" in a for a in recorded)


def test_prepare_work_volume_raises_if_still_present():
    def seam(argv):
        if argv[:3] == ["docker", "volume", "inspect"]:
            return (0, "{}")  # still there
        return (0, "")

    with pytest.raises(PrepareError, match="still present"):
        prepare_work_volume("ckbbench-work-test", seam)


def test_prepare_work_volume_fail_closed_on_daemon_error():
    def seam(argv):
        if argv[:3] == ["docker", "volume", "inspect"]:
            return (1, "Cannot connect to the Docker daemon")
        return (0, "")

    with pytest.raises(PrepareError, match="cannot verify"):
        prepare_work_volume("ckbbench-work-test", seam)


def test_prepare_work_volume_fail_closed_on_missing_docker_binary_text():
    """WHY: errno 'No such file' must not look like docker 'object absent' (fail-open stop/prepare)."""
    def seam(argv):
        return (1, "[Errno 2] No such file or directory: 'docker'")

    with pytest.raises(PrepareError, match="cannot verify"):
        prepare_work_volume("ckbbench-work-test", seam)


def test_default_subprocess_missing_binary_is_prepare_error():
    from ckbbench.run.runner import _default_subprocess

    with pytest.raises(PrepareError, match="failed to execute"):
        _default_subprocess(["/nonexistent/ckbbench-docker-binary-xyz", "x"])


def test_default_subprocess_timeout_force_rms_named_container(monkeypatch):
    """WHY: CLI timeout must not leave a daemon container holding /work."""
    import subprocess

    from ckbbench.run.runner import _default_subprocess

    calls: list[list[str]] = []

    def boom(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["docker", "rm"]:
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 1)

    monkeypatch.setattr(subprocess, "run", boom)
    code, out = _default_subprocess(
        ["docker", "run", "--name", "ckbbench-grade-dead", "img"],
        timeout=1,
        force_rm_name="ckbbench-grade-dead",
    )
    assert code == 124
    assert "timed out" in out
    assert any(a[:4] == ["docker", "rm", "-f", "ckbbench-grade-dead"] for a in calls)


def test_run_with_retries_does_not_retry_timeout_124():
    """WHY: retrying timeout stacks orphan grade containers on the work volume."""
    calls: list[int] = []

    def seam(argv):
        calls.append(1)
        return 124, "timed out"

    code = run_with_retries(["docker", "run"], seam, max_attempts=3)
    assert code == 124
    assert len(calls) == 1


def test_prepare_work_volume_rejects_non_ckbbench():
    with pytest.raises(PrepareError, match="non-ckbbench"):
        prepare_work_volume("other-vol", lambda a: (0, ""))


def test_invoke_runner_build_and_verify_paths():
    cfg = _cfg()
    recorded: list[list[str]] = []

    def seam(argv):
        recorded.append(list(argv))
        return (0, "")

    build_inv = _inv(
        "build",
        mounts={"/ws": "/sources:ro", "/art": "/artifact"},
    )
    assert invoke_runner(build_inv, cfg, seam) == 0
    build_runs = [a for a in recorded if a[:4] == ["docker", "run", "--rm", "--user"]]
    assert build_runs and "ckbbench-agent:test" in build_runs[0]
    assert build_runs[0][build_runs[0].index("--network") + 1] == "none"
    assert not any("chown" in a for a in recorded)
    assert not any("/cargo" in " ".join(a) for a in build_runs)

    recorded.clear()
    verify_inv = _inv(
        "verify",
        mounts={"/suite": "/suite", "/art": "/artifact:ro"},
        env={BENCH_PASSWORD_ENV: "pw"},
        command=("cargo", "test"),
    )
    assert invoke_runner(verify_inv, cfg, seam) == 0
    verify_runs = [a for a in recorded if a[:4] == ["docker", "run", "--rm", "--user"]]
    assert verify_runs and "ckbbench-verifier:test" in verify_runs[0]
    assert verify_runs[0][verify_runs[0].index("--network") + 1] == "none"


def test_invoke_runner_rejects_uid_zero():
    cfg = RunnerConfig(
        agent_image="img",
        verifier_image="v",
        uid=0,
        gid=0,
        work_volume="ckbbench-work",
    )
    inv = _inv("build", mounts={"/ws": "/sources:ro", "/art": "/artifact"})
    with pytest.raises(PrepareError, match="uid 0"):
        invoke_runner(inv, cfg, lambda a: (0, ""))


def test_invoke_runner_build_rejects_password():
    inv = _inv("build", env={BENCH_PASSWORD_ENV: "leak"})
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert BENCH_PASSWORD_ENV in str(exc)


def test_invoke_runner_build_rejects_suite_mount():
    inv = _inv("build", mounts={"/host/s": "/suite"})
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not allowed" in str(exc)


def test_invoke_runner_build_rejects_unexpected_mount_target():
    inv = _inv("build", mounts={"/host/sneaky": "/hidden", "/ws": "/sources:ro"})
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "/hidden" in str(exc) and "not allowed" in str(exc)


def test_build_docker_argv_rejects_nat_network():
    """ADR-0006: graded containers must not use NAT bridge even if misconfigured."""
    inv = _inv("build", mounts={"/ws": "/sources:ro", "/art": "/artifact"})
    with pytest.raises(ValueError, match="ADR-0006"):
        build_docker_argv(inv, _cfg(), image="img", network="bridge")


def test_invoke_runner_verify_rejects_duplicate_artifact_mount():
    inv = _inv(
        "verify",
        mounts={
            "/host/suite": "/suite",
            "/host/art-ro": "/artifact:ro",
            "/host/art-rw": "/artifact",
        },
        env={BENCH_PASSWORD_ENV: "pw"},
    )
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "exactly once" in str(exc)


def test_invoke_runner_verify_rejects_missing_suite_mount():
    inv = _inv("verify", mounts={"/art": "/artifact:ro"}, env={BENCH_PASSWORD_ENV: "pw"})
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "hidden suite" in str(exc)


def test_invoke_runner_verify_rejects_missing_password():
    inv = _inv("verify", mounts={"/art": "/artifact:ro"})
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert BENCH_PASSWORD_ENV in str(exc)


def test_invoke_runner_verify_rejects_rw_artifact():
    inv = _inv(
        "verify",
        mounts={"/host/suite": "/suite", "/art": "/artifact"},
        env={BENCH_PASSWORD_ENV: "pw"},
    )
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "read-only" in str(exc)


def test_make_docker_runner_factory():
    runner = make_docker_runner(_cfg(), run=lambda argv: (0, ""))
    inv = _inv("build", mounts={"/ws": "/sources:ro", "/art": "/artifact"})
    assert runner(inv) == 0


def test_build_docker_argv_extra_mounts_and_env():
    inv = _inv("build", mounts={"/a": "/sources:ro"})
    argv = build_docker_argv(
        inv,
        _cfg(),
        image="img",
        extra_mounts={"/vol": "/data"},
        extra_env={"FOO": "bar"},
        command=("echo", "hi"),
    )
    assert "/vol:/data" in argv
    assert "-e FOO=bar" in [f"{a} {argv[i+1]}" for i, a in enumerate(argv) if a == "-e"]
    assert argv[-2:] == ["echo", "hi"]


def test_run_with_retries_empty_output_on_failure():
    code = run_with_retries(["x"], lambda a: (1, ""), max_attempts=1)
    assert code == 1


def test_run_with_retries_whitespace_only_output_no_tail_print(capsys):
    code = run_with_retries(["x"], lambda a: (1, "   \n  "), max_attempts=1)
    assert code == 1
    assert capsys.readouterr().out == ""


def test_invoke_runner_verify_rejects_missing_artifact_mount():
    inv = _inv("verify", mounts={"/host/suite": "/suite"}, env={BENCH_PASSWORD_ENV: "pw"})
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "agent artifact" in str(exc)


def test_build_stage_shell_quotes_special_command_args():
    inv = _inv(
        "build",
        mounts={"/ws": "/sources:ro", "/art": "/artifact"},
        command=("echo", "has space", ""),
    )
    argv = build_stage_argv(inv, _cfg())
    script = argv[argv.index("-c") + 1]
    assert "'has space'" in script
    assert "''" in script


def test_runner_config_for_suite_uses_manifest_digest(monkeypatch):
    monkeypatch.delenv("CKBBENCH_AGENT_IMAGE", raising=False)
    monkeypatch.delenv("CKBBENCH_VERIFIER_IMAGE", raising=False)
    from ckbbench.suite.model import Suite, SuitePins

    suite = Suite(
        suite_semver="1.0.0",
        chain_profile="devnet",
        mcp_server_version="1.6.12",
        tasks=(),
        pins=SuitePins(docker_image_digest="sha256:deadbeef"),
    )
    cfg = RunnerConfig.for_suite(suite)
    # Separate agent vs verifier pins (same digest suffix only when suite has one pin field).
    assert cfg.agent_image == "ckbbench-agent@sha256:deadbeef"
    assert cfg.verifier_image == "ckbbench-verifier@sha256:deadbeef"
    assert cfg.agent_image != cfg.verifier_image


def test_runner_config_reads_env_overrides(monkeypatch):
    monkeypatch.setenv("CKBBENCH_AGENT_IMAGE", "custom-agent:1")
    monkeypatch.setenv("CKBBENCH_VERIFIER_IMAGE", "custom-verifier:1")
    monkeypatch.setenv("CKBBENCH_DOCKER_NETWORK", "custom-net")
    monkeypatch.setenv("CKBBENCH_CARGO_VOLUME", "custom-cargo")
    monkeypatch.setenv("CKBBENCH_WORK_VOLUME", "custom-work")
    cfg = RunnerConfig()
    assert cfg.agent_image == "custom-agent:1"
    assert cfg.verifier_image == "custom-verifier:1"
    assert cfg.network == "custom-net"
    assert cfg.cargo_volume == "custom-cargo"
    assert cfg.work_volume == "custom-work"
    # Separate env pins stay separate.
    assert cfg.agent_image != cfg.verifier_image


def test_make_docker_runner_default_subprocess_seam(monkeypatch):
    recorded: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return FakeProc()

    monkeypatch.setattr("subprocess.run", fake_run)
    runner = make_docker_runner(_cfg())
    inv = _inv(
        "verify",
        mounts={"/host/suite": "/suite", "/art": "/artifact:ro"},
        env={BENCH_PASSWORD_ENV: "pw"},
        command=("true",),
    )
    assert runner(inv) == 0
    assert recorded
    assert any("ckbbench-verifier:test" in a for a in recorded)
    assert not any("chown" in a for a in recorded)
