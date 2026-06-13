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


def _mounts_for_target(mounts: Mapping[str, str], target: str) -> list[tuple[str, str]]:
    """All (host, spec) mounts whose CONTAINER target is exactly ``target``.

    The container target is the second colon-field of the spec (Docker -v host:container[:opts]);
    we match on it exactly so a value like "/foo:/artifact:ro" is classified by its real target.
    Returning ALL matches lets callers reject duplicate mounts to the same target (a later RW
    mount could otherwise shadow an earlier :ro one in Docker).
    """
    out: list[tuple[str, str]] = []
    for host, spec in mounts.items():
        parts = spec.split(":")
        container_path = parts[0]
        if container_path == target:
            out.append((host, spec))
    return out


# Build-stage mounts are restricted to exactly these container targets (defense in depth: the
# hidden suite must never reach the build stage under any target name, ADR-0005).
_ALLOWED_BUILD_TARGETS = frozenset({"/sources", "/artifact"})


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


def _assert_internal_network(config: RunnerConfig) -> None:
    """A runner invocation must run on the no-NAT internal network (ADR-0006). Refuse to launch
    a graded container on a NAT network even if the env var was set to one, so a misconfig cannot
    silently give a build/verify stage an off-host route."""
    if config.network != DEFAULT_NETWORK:
        raise ValueError(
            f"runner network must be the internal no-NAT net {DEFAULT_NETWORK!r}, "
            f"got {config.network!r} (ADR-0006)"
        )


def invoke_runner(
    inv: RunnerInvocation,
    config: RunnerConfig,
    run: SubprocessSeam,
) -> int:
    """Execute one RunnerInvocation via docker (the RunnerCallable implementation)."""
    _assert_internal_network(config)
    if inv.stage == "build":
        if BENCH_PASSWORD_ENV in inv.env:
            raise ValueError(f"build stage must not set {BENCH_PASSWORD_ENV} (ADR-0005)")
        # Allowlist the build-stage mount targets: the hidden suite must never reach the build
        # stage under ANY target name (rejecting only "/suite" was insufficient, codex).
        for _host, spec in inv.mounts.items():
            target = spec.split(":")[0]
            if target not in _ALLOWED_BUILD_TARGETS:
                raise ValueError(
                    f"build stage mount target {target!r} not allowed; "
                    f"only {sorted(_ALLOWED_BUILD_TARGETS)} (ADR-0005)"
                )
        argv = build_stage_argv(inv, config)
        return run_with_retries(argv, run, max_attempts=config.max_build_retries)

    if BENCH_PASSWORD_ENV not in inv.env or not inv.env[BENCH_PASSWORD_ENV]:
        raise ValueError(f"verify stage must inject non-empty {BENCH_PASSWORD_ENV}")
    # The hidden suite must be present. It is mounted RW by design: the verifier COMPILES it
    # (cargo test writes target/ into the suite tree), so it cannot be read-only. Integrity does
    # not require it: the suite is the verifier's OWN code, rebuilt each run, and the thing being
    # graded (the agent artifact) is the read-only mount. (This is why codex's "suite :ro" note is
    # not applied: it would break cargo's target/ write; the spike mounts the suite ws RW.)
    suite_mounts = _mounts_for_target(inv.mounts, "/suite")
    if not suite_mounts:
        raise ValueError("verify stage must mount the hidden suite")
    # The agent artifact must be EXACTLY one read-only mount (a duplicate /artifact mount, the
    # second RW, could otherwise shadow the :ro one in Docker, codex).
    artifact_mounts = _mounts_for_target(inv.mounts, "/artifact")
    if len(artifact_mounts) != 1:
        raise ValueError(
            f"verify stage must mount the agent artifact exactly once, found {len(artifact_mounts)}"
        )
    if not artifact_mounts[0][1].endswith(":ro"):
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