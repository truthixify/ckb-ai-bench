"""Docker runner for Code Task build/verify stages (ADR-0004/0005).

Graded pure-cargo stages: named /work volume, image-local CARGO_HOME (no shared cargo
volume), --network none, --user non-root, ownership-neutral source copy. Prepare failures
raise PrepareError for infra_fail scoring.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ckbbench.config import resolve_agent_image, resolve_verifier_image
from ckbbench.verify.codetask import BENCH_PASSWORD_ENV, RunnerInvocation

# Type alias for injectable subprocess seam: argv -> (exit_code, captured_output).
SubprocessSeam = Callable[[Sequence[str]], tuple[int, str]]

DEFAULT_AGENT_IMAGE = "ckbbench-agent:latest"
DEFAULT_VERIFIER_IMAGE = "ckbbench-verifier:latest"
DEFAULT_NETWORK = "ckbbench-net-internal"
# Graded pure-cargo stages use Docker's none network (no NAT, no service DNS).
GRADE_NETWORK_NONE = "none"
# Allowed graded networks: none for cargo grades; internal kept for ADR-0006 compatibility
# if a future non-cargo stage needs RPC on the internal net.
ALLOWED_GRADE_NETWORKS = frozenset({DEFAULT_NETWORK, GRADE_NETWORK_NONE})
DEFAULT_CARGO_VOLUME = "ckbbench-cargo-cache"  # legacy cleanup only; not mounted on grades
DEFAULT_WORK_VOLUME = "ckbbench-work"
BUILD_WORK_SUBDIR = "ckbbench-build"
MAX_BUILD_RETRIES = 3
# Graded docker run wall clock (agent stage has its own budget; grade is separate).
DEFAULT_GRADE_TIMEOUT_SECONDS = 1800


class PrepareError(RuntimeError):
    """Volume/ownership/agent-stop failure; must become infra_fail, not agent_fail."""


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class RunnerConfig:
    """Config-driven image, network, and volume names for docker invocations."""

    agent_image: str = field(default_factory=resolve_agent_image)
    verifier_image: str = field(default_factory=resolve_verifier_image)
    network: str = field(default_factory=lambda: _env("CKBBENCH_DOCKER_NETWORK", DEFAULT_NETWORK))
    cargo_volume: str = field(default_factory=lambda: _env("CKBBENCH_CARGO_VOLUME", DEFAULT_CARGO_VOLUME))
    work_volume: str = field(default_factory=lambda: _env("CKBBENCH_WORK_VOLUME", DEFAULT_WORK_VOLUME))
    uid: int = field(default_factory=lambda: os.getuid())
    gid: int = field(default_factory=lambda: os.getgid())
    max_build_retries: int = MAX_BUILD_RETRIES
    grade_timeout_seconds: int = field(
        default_factory=lambda: int(_env("CKBBENCH_GRADE_TIMEOUT", str(DEFAULT_GRADE_TIMEOUT_SECONDS)))
    )

    @classmethod
    def for_suite(cls, suite: object) -> RunnerConfig:
        """Build a runner config using suite manifest digest when env is unset."""
        digest = getattr(getattr(suite, "pins", None), "docker_image_digest", None)
        return cls(
            agent_image=resolve_agent_image(suite_digest=digest),
            verifier_image=resolve_verifier_image(suite_digest=digest),
        )


def build_docker_argv(
    inv: RunnerInvocation,
    config: RunnerConfig,
    *,
    image: str,
    extra_mounts: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    workdir: str | None = None,
    command: Sequence[str] | None = None,
    network: str | None = None,
) -> list[str]:
    """Construct a ``docker run`` argv from a RunnerInvocation (pure, testable)."""
    net = GRADE_NETWORK_NONE if network is None else network
    _assert_grade_network(net)
    argv: list[str] = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{config.uid}:{config.gid}",
        "--network",
        net,
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
    """Wrap the agent build in a WORK-volume tree with ownership-neutral copy."""
    cmd = " ".join(_shell_quote(part) for part in inv.command)
    script = (
        "set -e\n"
        f"rm -rf /work/{BUILD_WORK_SUBDIR} && mkdir -p /work/{BUILD_WORK_SUBDIR}\n"
        # Agent may leave root-owned sources; preserve mode/mtime but not uid (non-root grade).
        f"cp -a --no-preserve=ownership /sources/. /work/{BUILD_WORK_SUBDIR}/\n"
        f"cd /work/{BUILD_WORK_SUBDIR}\n"
        f"{cmd}\n"
        "mkdir -p /artifact/build\n"
        "if [ -d build ]; then cp -a --no-preserve=ownership build/. /artifact/build/; fi\n"
    )
    return ("sh", "-c", script)


def _shell_quote(arg: str) -> str:
    if not arg:
        return "''"
    if all(c.isalnum() or c in "/._-" for c in arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def build_stage_argv(inv: RunnerInvocation, config: RunnerConfig) -> list[str]:
    """Docker argv for the build stage (agent image, work volume only, network none)."""
    return build_docker_argv(
        inv,
        config,
        image=config.agent_image,
        extra_mounts={config.work_volume: "/work"},
        # Force cargo offline so sparse-index does not attempt the network under --network none.
        extra_env={"CARGO_NET_OFFLINE": "true"},
        network=GRADE_NETWORK_NONE,
        command=_build_shell_command(inv),
    )


def verify_stage_argv(inv: RunnerInvocation, config: RunnerConfig) -> list[str]:
    """Docker argv for the verify stage (verifier image, suite cwd, no cargo volume)."""
    return build_docker_argv(
        inv,
        config,
        image=config.verifier_image,
        extra_env={"CARGO_NET_OFFLINE": "true"},
        network=GRADE_NETWORK_NONE,
        workdir="/suite",
        command=inv.command,
    )


def run_with_retries(
    argv: Sequence[str],
    run: SubprocessSeam,
    *,
    max_attempts: int,
) -> int:
    """Retry transient failures up to ``max_attempts``; surface tail on final failure.

    Exit 124 (grade timeout) is not retried — retries would stack orphaned containers.
    """
    last_code = 1
    last_output = ""
    for attempt in range(1, max_attempts + 1):
        last_code, last_output = run(argv)
        if last_code == 0:
            return 0
        # Do not retry wall-clock timeout: the hung container is force-removed once;
        # re-launch would only stack more holders on the work volume.
        if last_code == 124:
            break
        if attempt < max_attempts:
            continue
    if last_output:
        tail = "\n".join(last_output.strip().splitlines()[-10:])
        if tail:
            print(f"docker run failed after {max_attempts} attempts (exit {last_code}); tail:\n{tail}")
    return last_code


def _assert_grade_network(network: str) -> None:
    """Refuse NAT/bridge networks for graded containers (ADR-0006); allow none for cargo grades."""
    if network not in ALLOWED_GRADE_NETWORKS:
        raise ValueError(
            f"runner network must be one of {sorted(ALLOWED_GRADE_NETWORKS)!r} "
            f"(none for pure-cargo grades, or internal no-NAT), got {network!r} (ADR-0006)"
        )


def _assert_non_root_uid(config: RunnerConfig) -> None:
    if config.uid == 0:
        raise PrepareError("grade must not run as uid 0")


def _default_subprocess(
    argv: Sequence[str],
    *,
    timeout: int | None = None,
    force_rm_name: str | None = None,
) -> tuple[int, str]:
    import subprocess

    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PrepareError(f"failed to execute {argv[0]!r}: {exc}") from exc
    except OSError as exc:
        raise PrepareError(f"subprocess OS error for {argv[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # Kill daemon-side container (CLI timeout does not stop it); then agent_fail-ish exit.
        if force_rm_name:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", force_rm_name],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
            except Exception:
                pass
        return 124, f"timed out after {exc.timeout}s: {' '.join(argv[:6])}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _docker_resource_absent(inspect_code: int, inspect_out: str) -> bool:
    """True only when docker inspect clearly reports a missing *object* (fail-closed otherwise).

    Do not match bare \"No such file\" from a missing docker binary (that would fail-open prepare).
    """
    if inspect_code == 0:
        return False
    low = inspect_out.lower()
    return (
        "no such object" in low
        or "no such container" in low
        or "no such volume" in low
        or "no such image" in low
    )


def prepare_work_volume(
    volume: str,
    run: SubprocessSeam | None = None,
) -> None:
    """Remove and recreate the named work volume; fail if still present after remove.

    Checked path for grade prepare (no fail-open). Call once per cell before grade.
    """
    seam = run or _default_subprocess
    if not volume or not volume.startswith("ckbbench-"):
        raise PrepareError(f"refusing non-ckbbench work volume: {volume!r}")
    try:
        rm_code, rm_out = seam(["docker", "volume", "rm", "-f", volume])
        inspect_code, inspect_out = seam(["docker", "volume", "inspect", volume])
    except PrepareError:
        raise
    except OSError as exc:
        raise PrepareError(f"work volume prepare OS error: {exc}") from exc
    if inspect_code == 0:
        raise PrepareError(
            f"work volume {volume!r} still present after remove "
            f"(rm exit {rm_code}): {rm_out.strip()}"
        )
    if not _docker_resource_absent(inspect_code, inspect_out):
        raise PrepareError(
            f"cannot verify work volume {volume!r} removed "
            f"(rm exit {rm_code}, inspect exit {inspect_code}): {inspect_out.strip()}"
        )
    try:
        create_code, create_out = seam(["docker", "volume", "create", volume])
    except OSError as exc:
        raise PrepareError(f"work volume create OS error: {exc}") from exc
    if create_code != 0:
        raise PrepareError(
            f"docker volume create {volume!r} failed (exit {create_code}): {create_out.strip()}"
        )


def invoke_runner(
    inv: RunnerInvocation,
    config: RunnerConfig,
    run: SubprocessSeam,
) -> int:
    """Execute one RunnerInvocation via docker (the RunnerCallable implementation)."""
    _assert_non_root_uid(config)
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
        # Grade wall clock only on docker run; name the container so timeout can force-rm it.
        if list(argv[:2]) == ["docker", "run"]:
            import uuid

            name = f"ckbbench-grade-{uuid.uuid4().hex[:12]}"
            named = list(argv)
            # Insert after "docker run" (index 2) so --rm/--user stay valid.
            named[2:2] = ["--name", name]
            return _default_subprocess(
                named,
                timeout=cfg.grade_timeout_seconds,
                force_rm_name=name,
            )
        return _default_subprocess(argv)

    seam = run or _default_run
    return lambda inv: invoke_runner(inv, cfg, seam)
