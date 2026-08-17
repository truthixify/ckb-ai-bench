"""The diagnostic worker: one arm-B cell, no grading, one atomic candidate.

Runs only when its parent configured it. It performs **no resource cleanup**: the supervising parent
owns every container, because a worker killed at the deadline cannot run its own `finally` blocks and
would otherwise race the parent's validation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ckbbench.run.diagnose import (
    DiagnosticAbort,
    inherited_artifact_dir,
    inherited_receipt_fd,
    load_worker_identity,
    worker_requested,
    write_candidate,
    write_receipt,
)
from ckbbench.run.diagnostic import DiagnosticSession, false_envelope

FIXED_SUITE_DIR = "suites/ckb-v1"
FIXED_ARM = "B"
FIXED_CHAIN = "devnet"


def _seam_controller():
    """Install the pinned observer for this process and expose the attempt hooks.

    Returns an object with `begin_attempt()` and `end_attempt()`. Ordinary runs never reach here,
    so normal cells never patch HTTPX.
    """
    from importlib.metadata import version

    import httpx

    from ckbbench.run.diagnostic import TransportObserver, client_has_custom_transport

    observer = TransportObserver()

    def install(wrapper) -> None:
        httpx.HTTPTransport.handle_request = wrapper

    observer.validate_and_install(
        litellm_version=version("litellm"),
        httpx_version=version("httpx"),
        seam_func=httpx.HTTPTransport.handle_request,
        client_has_custom_transport=client_has_custom_transport(),
        install=install,
    )

    class _Controller:
        def begin_attempt(self) -> None:
            observer.begin_attempt()

        def end_attempt(self) -> str:
            return observer.end_attempt(current_seam=httpx.HTTPTransport.handle_request)

    return _Controller()


def main() -> int:
    if not worker_requested():
        print("diagnose worker: not configured by a parent", file=sys.stderr)
        return 2
    identity = load_worker_identity()
    run_id = identity["CKBBENCH_DIAGNOSTIC_RUN_ID"]
    candidate = identity["CKBBENCH_DIAGNOSTIC_CANDIDATE"]

    session = DiagnosticSession()
    try:
        _run_cell(session, identity)
    except DiagnosticAbort:
        session.instrumentation_ok = False
    except Exception:
        # Sanitized: no exception text, cause or traceback may reach the candidate or this output.
        session.instrumentation_ok = False

    # The parent's own validated directory object, inherited as a descriptor.
    try:
        artifact_dir = inherited_artifact_dir()
    except DiagnosticAbort:
        return 4
    try:
        receipt_fd = inherited_receipt_fd()
    except DiagnosticAbort:
        artifact_dir.close()
        return 5
    try:
        try:
            created = write_candidate(Path(candidate), session.to_bytes(run_id),
                                      directory=artifact_dir)
        except Exception:
            try:
                created = write_candidate(Path(candidate), false_envelope(run_id),
                                          directory=artifact_dir)
            except Exception:
                return 3
        # The parent has no other way to learn WHICH file this process created. Without the receipt
        # it can only see a name, and a name can be occupied by a different file by the time it
        # looks; the receipt is written last, so it exists only for a completed transaction.
        try:
            write_receipt(receipt_fd, created)
        except DiagnosticAbort:
            return 6
    finally:
        artifact_dir.close()
        os.close(receipt_fd)
    return 0


def _run_cell(session: DiagnosticSession, identity: dict[str, str]) -> None:
    """Prepare and run one arm-B cell exactly as the accepted path prepares it, then stop.

    It reaches `agent.run()` and returns. It never calls `verify_suite()`, never builds a
    `RunResult`, and never touches results, aggregation or a report. Cleanup belongs to the parent,
    so nothing here removes a container, and every path it writes was selected by the parent.
    """
    import json

    from ckbbench.ckb_rpc import make_rpc_client
    from ckbbench.config import MCP_URL, rpc_url_for
    from ckbbench.run.agent_factory import make_agent_factory
    from ckbbench.run.arm import resolve_arm
    from ckbbench.run.diagnose import mark_created, write_allowlist
    from ckbbench.run.devnet import NODE_SERVICE, prepare_devnet
    from ckbbench.run.model_profile import PROFILE_PATH, load_reviewed_profile
    from ckbbench.run.orchestrate import prepare_agent_workspace
    from ckbbench.suite.freeze import freeze
    from ckbbench.suite.registry import load_suite

    suite_dir = Path(FIXED_SUITE_DIR)
    profile = load_reviewed_profile(str(PROFILE_PATH))
    suite = load_suite(suite_dir)

    # The frozen suite must still be the frozen suite: a drifted registry would make the diagnostic
    # describe a different prompt than the accepted B cell.
    tracked = json.loads((suite_dir / "suite.freeze.json").read_text())
    if freeze(suite, suite_dir) != tracked:
        raise DiagnosticAbort("the tracked suite freeze does not match a fresh freeze")

    arm_config = resolve_arm(FIXED_ARM)
    if arm_config.mcp_enabled:
        raise DiagnosticAbort("the diagnostic arm must have no MCP surface")

    # Every parent-selected path is bound BEFORE its first use.
    mount = Path(identity["CKBBENCH_DIAGNOSTIC_MOUNT_DIR"])
    created_dir = Path(identity["CKBBENCH_DIAGNOSTIC_CREATED_DIR"])
    allowlist_path = Path(identity["CKBBENCH_DIAGNOSTIC_ALLOWLIST_PATH"])

    def announce_devnet(service: str, _container_id: str) -> None:
        # Called per service, the instant THAT service's identity is ownership-proved and before any
        # chown or start, so a failure while proving the next one still records this one.
        mark_created(created_dir, "node" if service == NODE_SERVICE else "miner")

    prepare_devnet(rpc_url=rpc_url_for(FIXED_CHAIN), on_created=announce_devnet)

    # Written directly to the exact parent-selected leaf, exclusively: a random temp name plus
    # `replace()` would both invent a selector and clobber whatever occupied the target.
    write_allowlist(FIXED_ARM, FIXED_CHAIN, MCP_URL, allowlist_path)

    rpc_client = make_rpc_client(rpc_url_for(FIXED_CHAIN))
    harness_tip = int(rpc_client("get_tip_block_number", []), 16)
    pointer = prepare_agent_workspace(
        suite, arm_config, FIXED_CHAIN, mount, rpc_client=rpc_client, harness_tip=harness_tip,
    )

    factory = make_agent_factory(
        profile=profile,
        container_name=identity["CKBBENCH_DIAGNOSTIC_AGENT_NAME"],
        container_labels=tuple(
            label for label in os.environ.get("CKBBENCH_DIAGNOSTIC_LABELS", "").split(",") if label
        ),
        auto_cleanup=False,
    )
    agent = factory(
        mount_dir=mount,
        pointer=pointer,
        arm_config=arm_config,
        mcp_client=None,
        model=profile.requested_model,
        suite=suite,
        chain=FIXED_CHAIN,
    )
    mark_created(created_dir, "agent")
    agent.model.attach_diagnostic(session, _seam_controller())
    try:
        agent.run(pointer)
    except Exception:
        # An ordinary agent stop (limits, format errors, a provider failure already recorded per
        # attempt) is not instrumentation failure. Instrumentation failures poison the session at
        # their own boundary, so they are not masked by this.
        pass


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
