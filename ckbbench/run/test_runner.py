"""Docker runner tests: command construction and retry policy (no real docker calls).

Encodes WHY (Rule 9): the load-bearing guarantees from ADR-0005/0009 are enforced by WHAT
is mounted and WHICH env vars appear in the constructed ``docker run`` argv, and transient
build failures get exactly three attempts without masking deterministic success.
"""

from __future__ import annotations

from ckbbench.verify.codetask import BENCH_PASSWORD_ENV, RunnerInvocation

from ckbbench.run.runner import (
    RunnerConfig,
    build_docker_argv,
    build_stage_argv,
    invoke_runner,
    make_docker_runner,
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
    assert argv[net_idx + 1] == "ckbbench-net-internal"
    assert "/host/src:/sources:ro" in argv
    assert "/host/out:/artifact:ro" in argv
    env_pairs = [f"{argv[i]} {argv[i + 1]}" for i, token in enumerate(argv) if token == "-e"]
    assert f"-e {BENCH_PASSWORD_ENV}=secret" in env_pairs
    assert "-w" in argv and "/suite" in argv
    assert cfg.verifier_image in argv
    assert argv[-3:] == ["cargo", "test", "--release"]


def test_build_stage_has_no_password_no_suite_mount():
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
    assert "ckbbench-cargo-test:/cargo" in argv
    assert "ckbbench-work-test:/work" in argv
    assert "-e CARGO_HOME=/cargo" in [f"{a} {argv[i+1]}" for i, a in enumerate(argv) if a == "-e"]
    assert "sh" in argv and "-c" in argv


def test_verify_stage_mounts_artifact_ro_and_injects_password():
    inv = _inv(
        "verify",
        mounts={"/host/suite": "/suite", "/host/art": "/artifact:ro"},
        env={BENCH_PASSWORD_ENV: "pw", "TOP": "/artifact", "MODE": "release"},
        command=("cargo", "test", "--release"),
    )
    argv = verify_stage_argv(inv, _cfg())

    assert "/host/art:/artifact:ro" in argv
    assert "/host/suite:/suite" in argv
    assert f"-e {BENCH_PASSWORD_ENV}=pw" in [f"{a} {argv[i+1]}" for i, a in enumerate(argv) if a == "-e"]
    assert "ckbbench-verifier:test" in argv
    assert "-w" in argv and "/suite" in argv


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
    assert "ckbbench-agent:test" in recorded[0]

    verify_inv = _inv(
        "verify",
        mounts={"/suite": "/suite", "/art": "/artifact:ro"},
        env={BENCH_PASSWORD_ENV: "pw"},
        command=("cargo", "test"),
    )
    assert invoke_runner(verify_inv, cfg, seam) == 0
    assert "ckbbench-verifier:test" in recorded[1]


def test_invoke_runner_build_rejects_password():
    inv = _inv("build", env={BENCH_PASSWORD_ENV: "leak"})
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert BENCH_PASSWORD_ENV in str(exc)


def test_invoke_runner_build_rejects_suite_mount():
    # /suite is not in the build-stage mount allowlist (/sources, /artifact).
    inv = _inv("build", mounts={"/host/s": "/suite"})
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not allowed" in str(exc)


def test_invoke_runner_build_rejects_unexpected_mount_target():
    # Defense in depth (codex): the hidden suite must not reach build under ANY target name.
    inv = _inv("build", mounts={"/host/sneaky": "/hidden", "/ws": "/sources:ro"})
    try:
        invoke_runner(inv, _cfg(), lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "/hidden" in str(exc) and "not allowed" in str(exc)


def test_invoke_runner_rejects_non_internal_network():
    # A graded container must never run on a NAT network even if the env var was set (ADR-0006).
    cfg = RunnerConfig(network="bridge")  # a NAT network
    inv = _inv("build", mounts={"/ws": "/sources:ro", "/art": "/artifact"})
    try:
        invoke_runner(inv, cfg, lambda a: (0, ""))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "internal" in str(exc)


def test_invoke_runner_verify_rejects_duplicate_artifact_mount():
    # codex: a second RW /artifact mount could shadow the :ro one in Docker; require exactly one.
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
        extra_mounts={"/vol": "/cargo"},
        extra_env={"CARGO_HOME": "/cargo"},
        command=("echo", "hi"),
    )
    assert "/vol:/cargo" in argv
    assert "-e CARGO_HOME=/cargo" in [f"{a} {argv[i+1]}" for i, a in enumerate(argv) if a == "-e"]
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
    assert cfg.agent_image == "ckbbench-agent@sha256:deadbeef"
    assert cfg.verifier_image == "ckbbench-verifier@sha256:deadbeef"


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