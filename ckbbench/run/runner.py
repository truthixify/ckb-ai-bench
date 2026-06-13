"""Docker runner for Code Task build/verify stages (ADR-0004/0005).

Faithful to spikes/container-verifier/run-spike.sh: build in a WORK volume (not a host bind
mount on target/), shared cargo cache, source mounted :ro, artifact dir for the binary, 3x retry
on transient build failure ONLY. Verify stage mounts hidden suite + artifact :ro and injects
BENCH_PASSWORD only at verify time.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ckbbench.verify.codetask import BENCH_PASSWORD_ENV, RunnerInvocation

# Type alias for injectable subprocess seam: argv -> (exit_code, captured_output).
SubprocessSeam = Callable[[Sequence[str]], tuple[int, str]]

DEFAULT_AGENT_IMAGE = "ckbbench-agent:latest"
DEFAULT_VERIFIER_IMAGE = "ckbbench-verifier:latest"
DEFAULT_NETWORK = "ckbbench-net-internal"
DEFAULT_CARGO_VOLUME = "ckbbench-cargo-cache"
DEFAULT_WORK_VOLUME = "ckbbench-work"
BUILD_WORK_SUBDIR = "ckbbench-build"
MAX_BUILD_RETRIES = 3


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class RunnerConfig:
    """Config-driven image, network, and volume names for docker invocations."""

    agent_image: str = field(default_factory=lambda: _env("CKBBENCH_AGENT_IMAGE", DEFAULT_AGENT_IMAGE))
    verifier_image: str = field(
        default_factory=lambda: _env("CKBBENCH_VERIFIER_IMAGE", DEFAULT_VERIFIER_IMAGE)
    )
    network: str = field(default_factory=lambda: _env("CKBBENCH_DOCKER_NETWORK", DEFAULT_NETWORK))
    cargo_volume: str = field(default_factory=lambda: _env("CKBBENCH_CARGO_VOLUME", DEFAULT_CARGO_VOLUME))
    work_volume: str = field(default_factory=lambda: _env("CKBBENCH_WORK_VOLUME", DEFAULT_WORK_VOLUME))
    uid: int = field(default_factory=lambda: os.getuid())
    gid: int = field(default_factory=lambda: os.getgid())
    max_build_retries: int = MAX_BUILD_RETRIES


def build_docker_argv(
    inv: RunnerInvocation,
    config: RunnerConfig,
    *,
    image: str,
    extra_mounts: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    workdir: str | None = None,
    command: Sequence[str] | None = None,
) -> list[str]:
    """Construct a ``docker run`` argv from a RunnerInvocation (pure, testable)."""
    argv: list[str] = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{config.uid}:{config.gid}",
        "--network",
        config.network,
    ]
    for host, spec in inv.mounts.items():
        argv.extend(["-v", f"{host}:{spec}"])
    if extra_mounts:
        for host, spec in extra_mounts.items():
            argv.extend(["-v", f"{host}:{spec}"])
    merged_env = dict(inv.env)
    if extra_env:
        merged_env.update(extra_env)
    for key, value in merged_env.items():
        argv.extend(["-e", f"{key}={value}"])
    if workdir:
        argv.extend(["-w", workdir])
    argv.append(image)
    argv.extend(command if command is not None else inv.command)
    return argv


def _find_mount_path(mounts: Mapping[str, str], suffix: str) -> str | None:
    for host, spec in mounts.items():
        container_path = spec.split(":")[0]
        if container_path == suffix or spec.startswith(f"{suffix}:"):
            return host
    return None


def _build_shell_command(inv: RunnerInvocation) -> tuple[str, ...]:
    """Wrap the agent build in a WORK-volume tree (spikes/container-verifier/run-spike.sh)."""
    cmd = " ".join(_shell_quote(part) for part in inv.command)
    script = (
        "set -e\n"
        f"rm -rf /work/{BUILD_WORK_SUBDIR} && mkdir -p /work/{BUILD_WORK_SUBDIR}\n"
        f"cp -a /sources/. /work/{BUILD_WORK_SUBDIR}/\n"
        f"cd /work/{BUILD_WORK_SUBDIR}\n"
        f"{cmd}\n"
        "mkdir -p /artifact/build\n"
        "if [ -d build ]; then cp -a build/. /artifact/build/; fi\n"
    )
    return ("sh", "-c", script)


def _shell_quote(arg: str) -> str:
    if not arg:
        return "''"
    if all(c.isalnum() or c in "/._-" for c in arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def build_stage_argv(inv: RunnerInvocation, config: RunnerConfig) -> list[str]:
    """Docker argv for the build stage (agent image, cargo + work volumes, no secrets)."""
    extra_mounts = {
        config.cargo_volume: "/cargo",
        config.work_volume: "/work",
    }
    extra_env = {"CARGO_HOME": "/cargo"}
    return build_docker_argv(
        inv,
        config,
        image=config.agent_image,
        extra_mounts=extra_mounts,
        extra_env=extra_env,
        command=_build_shell_command(inv),
    )


def verify_stage_argv(inv: RunnerInvocation, config: RunnerConfig) -> list[str]:
    """Docker argv for the verify stage (verifier image, suite cwd, BENCH_PASSWORD injected)."""
    return build_docker_argv(
        inv,
        config,
        image=config.verifier_image,
        extra_mounts={config.cargo_volume: "/cargo"},
        extra_env={"CARGO_HOME": "/cargo"},
        workdir="/suite",
        command=inv.command,
    )


def run_with_retries(
    argv: Sequence[str],
    run: SubprocessSeam,
    *,
    max_attempts: int,
) -> int:
    """Retry transient failures up to ``max_attempts``; surface tail on final failure."""
    last_code = 1
    last_output = ""
    for attempt in range(1, max_attempts + 1):
        last_code, last_output = run(argv)
        if last_code == 0:
            return 0
        if attempt < max_attempts:
            continue
    if last_output:
        tail = "\n".join(last_output.strip().splitlines()[-10:])
        if tail:
            print(f"docker run failed after {max_attempts} attempts (exit {last_code}); tail:\n{tail}")
    return last_code


def invoke_runner(
    inv: RunnerInvocation,
    config: RunnerConfig,
    run: SubprocessSeam,
) -> int:
    """Execute one RunnerInvocation via docker (the RunnerCallable implementation)."""
    if inv.stage == "build":
        if BENCH_PASSWORD_ENV in inv.env:
            raise ValueError(f"build stage must not set {BENCH_PASSWORD_ENV} (ADR-0005)")
        if _find_mount_path(inv.mounts, "/suite") is not None:
            raise ValueError("build stage must not mount the hidden suite (ADR-0005)")
        argv = build_stage_argv(inv, config)
        return run_with_retries(argv, run, max_attempts=config.max_build_retries)

    if BENCH_PASSWORD_ENV not in inv.env or not inv.env[BENCH_PASSWORD_ENV]:
        raise ValueError(f"verify stage must inject non-empty {BENCH_PASSWORD_ENV}")
    artifact_key = _find_mount_path(inv.mounts, "/artifact")
    if artifact_key is None:
        raise ValueError("verify stage must mount the agent artifact")
    spec = inv.mounts[artifact_key]
    if not spec.endswith(":ro"):
        raise ValueError("verify stage must mount the agent artifact read-only")
    argv = verify_stage_argv(inv, config)
    code, _ = run(argv)
    return code


def make_docker_runner(
    config: RunnerConfig | None = None,
    *,
    run: SubprocessSeam | None = None,
) -> Callable[[RunnerInvocation], int]:
    """Factory for a RunnerCallable backed by docker."""
    cfg = config or RunnerConfig()

    def _default_run(argv: Sequence[str]) -> tuple[int, str]:
        import subprocess

        proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output

    seam = run or _default_run
    return lambda inv: invoke_runner(inv, cfg, seam)