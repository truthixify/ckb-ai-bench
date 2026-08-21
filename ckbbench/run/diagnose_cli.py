"""Entry point for `./bench diagnose`: the parent supervisor.

Deliberately not part of the matrix path. `run_matrix()` types every cell function as returning a
`RunResult`, appends it, then validates and rebuilds the report; a no-grade cell cannot satisfy that
contract, and bending it would let diagnostic output leak into accepted reporting. This module is the
separate command instead.

The parent owns everything that can outlive the worker: the deadline, every resource selector,
signals, cleanup and publication. The worker owns only the agent run and one atomic candidate.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from ckbbench.run.diagnose import (
    ARTIFACT_FD_ENV,
    DIAGNOSTIC_DEADLINE_S,
    RECEIPT_FD_ENV,
    Deadline,
    DirHandle,
    DiagnosticAbort,
    DiagnosticIdentity,
    Supervisor,
    prepare_directory,
    read_receipt,
    refuse_existing,
    summarize,
)

FIXED_SUITE = "suites/ckb-v1"
FIXED_ARM = "B"
FIXED_SEED = 1
FIXED_CHAIN = "devnet"

# Only these reach the worker, plus the parent-selected diagnostic settings. Copying the whole
# environment would forward unrelated operator secrets into the child.
_CHILD_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH", "PYTHONHASHSEED",
    "CKBBENCH_LLM_API_BASE", "CKBBENCH_LLM_API_KEY", "BENCH_API_BASE", "BENCH_API_KEY",
    "CKBBENCH_MCP_URL", "CKBBENCH_DEVNET_RPC",
    "CKBBENCH_DOCKER", "LITELLM_LOCAL_MODEL_COST_MAP",
    "MSWEA_GLOBAL_CONFIG_DIR", "MSWEA_SILENT_STARTUP",
)


def run_id_for(model: str, now: float) -> str:
    """The same shape accepted rows use, with the reviewed model identity."""
    safe_model = model.replace("/", "-")
    return f"2.0.0-{FIXED_CHAIN}-{FIXED_ARM}-{safe_model}-s{FIXED_SEED}-{int(now)}"


def child_environment(identity: DiagnosticIdentity, source: dict[str, str]) -> dict[str, str]:
    """A minimal allowlisted environment plus the parent-selected diagnostic settings.

    `CKBBENCH_AGENT_IMAGE` / `CKBBENCH_VERIFIER_IMAGE` are deliberately excluded: they take
    precedence over the frozen suite pin, and this command promises no image override.
    """
    env = {k: source[k] for k in _CHILD_ENV_ALLOWLIST if k in source}
    env.update(identity.worker_env())
    return env


def _spawn_worker(identity: DiagnosticIdentity, repo_root: Path,
                  artifact_fd: int | None = None,
                  receipt_fd: int | None = None) -> subprocess.Popen:
    """Start the worker with an explicit configuration and no shell.

    Child output is DISCARDED. It can contain model output, commands, command output or exception
    material, none of which this task may retain; a pipe would also let a verbose child block before
    the deadline. The artifact, not a log, is the evidence channel.

    `receipt_fd` is the write end of the parent's fixed-width candidate receipt pipe.
    """
    argv = [sys.executable, "-m", "ckbbench.run.diagnose_worker"]
    env = child_environment(identity, dict(os.environ))
    pass_fds: list[int] = []
    if artifact_fd is not None:
        env[ARTIFACT_FD_ENV] = str(artifact_fd)
        pass_fds.append(artifact_fd)
    if receipt_fd is not None:
        env[RECEIPT_FD_ENV] = str(receipt_fd)
        pass_fds.append(receipt_fd)
    return subprocess.Popen(
        argv, cwd=str(repo_root), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        pass_fds=tuple(pass_fds),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ckbbench diagnose", add_help=True)
    parser.add_argument("--artifact-root", required=True)
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]
    artifact_root = Path(args.artifact_root)
    now = time.time()

    try:
        from ckbbench.run.model_profile import PROFILE_PATH, load_reviewed_profile

        from ckbbench.suite.freeze import freeze
        from ckbbench.suite.registry import load_suite

        profile = load_reviewed_profile(str(PROFILE_PATH))
        suite_dir = Path(FIXED_SUITE)
        suite = load_suite(suite_dir)
        # Freshly validated before anything external: a drifted registry would make the diagnostic
        # describe a different prompt than the accepted cell.
        import json as _json

        if freeze(suite, suite_dir) != _json.loads((suite_dir / "suite.freeze.json").read_text()):
            raise DiagnosticAbort("the tracked suite freeze does not match a fresh freeze")
        # The suite pin itself. `resolve_agent_image()` documents ambient `CKBBENCH_AGENT_IMAGE` as
        # taking precedence, so using it here would make the parent expect an override the child was
        # correctly forbidden from honouring — and this command promises no image override at all.
        for override in ("CKBBENCH_AGENT_IMAGE", "CKBBENCH_VERIFIER_IMAGE"):
            if os.environ.get(override):
                raise DiagnosticAbort(f"{override} is set; diagnose accepts no image override")
        expected_agent_image = suite.pins.agent_image_digest
        run_id = run_id_for(profile.requested_model, now)
        identity = DiagnosticIdentity.create(
            run_id=run_id, artifact_root=artifact_root,
            run_dir=artifact_root / "diagnostic-run" / run_id,
        )
        # Every owned path is validated BEFORE the first external action.
        prepare_directory(identity.final_path.parent)
        # Exclusive: this run must be the creator, or a later removal would delete someone else's
        # directory. Every owned leaf is refused if it already exists.
        prepare_directory(identity.run_dir, exclusive=True)
        prepare_directory(identity.mount_dir, exclusive=True)
        prepare_directory(identity.allowlist_dir, exclusive=True)
        prepare_directory(identity.created_dir, exclusive=True)
        # Opened ONCE, before the first external action, and retained through worker execution,
        # cleanup and publication. Both processes bind to these exact objects.
        artifact_dir = DirHandle(identity.final_path.parent)
        run_dir_handle = DirHandle(identity.run_dir)
        # The candidate receipt channel. Created here, before the first external action, so the
        # worker cannot choose it. Non-blocking on the read end: if the spawn fails, the parent
        # still holds the write end and a blocking read would wait for a writer that never comes.
        receipt_r, receipt_w = os.pipe()
        os.set_blocking(receipt_r, False)
        for name in (identity.candidate_path.name, identity.candidate_staging_path.name,
                     identity.final_path.name, identity.final_staging_path.name):
            artifact_dir.refuse_existing(name)
        refuse_existing(identity.allowlist_path.parent, identity.allowlist_path.name)
    except Exception as exc:
        print(f"diagnose: refused before any external action: {_sanitized(exc)}", file=sys.stderr)
        return 1

    deadline = Deadline(total_s=DIAGNOSTIC_DEADLINE_S, monotonic=time.monotonic)
    supervisor = Supervisor(
        identity=identity, deadline=deadline,
        run_dir_handle=run_dir_handle, artifact_dir=artifact_dir,
    )
    # Started before the FIRST external action, so preflight, docker and the worker share one budget.
    deadline.start()

    def spawn() -> subprocess.Popen:
        proc = _spawn_worker(identity, repo_root, artifact_dir.fd, receipt_w)
        # The child now holds the only write end; otherwise the parent's own copy keeps the pipe
        # open and the receipt read cannot reach end of file.
        os.close(receipt_w)
        return proc

    try:
        found = supervisor.inspect_ordinary()
        supervisor.transition_ordinary(found)
        code, timed_out = supervisor.supervise(spawn)
        print(f"worker exit={code} timed_out={timed_out}")
    except DiagnosticAbort as exc:
        supervisor.worker_ok = False
        print(f"diagnose: refused: {exc}", file=sys.stderr)
    except Exception as exc:
        # Normalized: no raw traceback, path or provider material reaches operator output.
        supervisor.worker_ok = False
        print(f"diagnose: internal failure: {_sanitized(exc)}", file=sys.stderr)

    if not supervisor.child_reaped:
        # The child may still be making provider, Docker and filesystem changes. Removing its
        # resources or publishing now would race a live process, so neither happens.
        print("diagnose: the worker could not be reaped; refusing to clean up or publish",
              file=sys.stderr)
        os.close(receipt_r)
        return 1

    # Read once the child is reaped, so the write end is closed and the read cannot block.
    supervisor.candidate_identity = read_receipt(receipt_r)
    os.close(receipt_r)

    try:
        # Cleanup runs FIRST: the creation acknowledgements it reads live inside the run directory,
        # and scrubbing them first turns "created and then disappeared" into ordinary absence.
        supervisor.cleanup_diagnostic(expected_agent_image=expected_agent_image)
    except Exception as exc:
        supervisor.cleanup_ok = False
        print(f"diagnose: cleanup failure: {_sanitized(exc)}", file=sys.stderr)
    finally:
        # Exactly once, on every reaped-child path: a cleanup failure, an exhausted deadline or a
        # refused publication must not leave the worker's raw workspace content on disk.
        supervisor.scrub_run_dir_once()

    if deadline.expired():
        print("diagnose: no budget remained for publication", file=sys.stderr)
        return 1

    try:
        payload = supervisor.publish()
    except Exception as exc:
        print(f"diagnose: could not publish: {_sanitized(exc)}", file=sys.stderr)
        return 1

    artifact_dir.close()
    run_dir_handle.close()
    print(f"cleanup: {summarize(supervisor.outcomes)}")
    print(f"artifact: {identity.final_path}")
    if b'"instrumentation_ok":true' not in payload:
        print("diagnose: published the fixed instrumentation_ok=false envelope", file=sys.stderr)
        return 1
    return 0


def _sanitized(exc: BaseException) -> str:
    """Only the failure class of the failure, never its text."""
    return type(exc).__name__ if isinstance(exc, DiagnosticAbort) else "internal error"


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
