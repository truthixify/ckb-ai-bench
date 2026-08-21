"""The creation callback must fire through the REAL `_compose_up()` seam, before chown and start.

A fake `prepare_devnet` that calls the callback itself only certifies the fake's ordering. These
drive the production function with a scripted docker runner and no Docker at all.
"""

from __future__ import annotations

import json

import pytest

from ckbbench.run.devnet import (
    MINER_SERVICE,
    NODE_SERVICE,
    DevnetLifecycleError,
    _bring_up_and_verify,
    _compose_up,
)

NODE_ID = "n" * 64
MINER_ID = "m" * 64


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _payload(name, container_id, image="sha256:image"):
    return {
        "Id": container_id,
        "Image": image,
        "Config": {"Labels": {"com.docker.compose.project": "ckbbench",
                              "com.docker.compose.service": name}},
        "State": {"Running": True},
    }


def _runner(*, fail_chown=False, fail_start=False, fail_second_inspect=False):
    """A scripted docker CLI. Records argv; never touches Docker."""
    calls: list[list[str]] = []

    def run(argv):
        argv = list(argv)
        calls.append(argv)
        joined = " ".join(argv)
        if "compose" in joined and "create" in argv:
            return _Completed(0)
        if argv[:3] == ["docker", "container", "inspect"]:
            name = argv[3]
            if name == MINER_SERVICE and fail_second_inspect:
                return _Completed(1, "", f"Error: No such container: {name}")
            container_id = NODE_ID if name == NODE_SERVICE else MINER_ID
            return _Completed(0, json.dumps(_payload(name, container_id)))
        if argv[:2] == ["docker", "run"]:            # the chown container
            return _Completed(1, "", "chown failed") if fail_chown else _Completed(0)
        if argv[:2] == ["docker", "start"]:
            return _Completed(1, "", "start failed") if fail_start else _Completed(0)
        if argv[:3] == ["docker", "volume", "inspect"]:
            # `_bring_up_and_verify` proves volume absence first; proven-absent is what we model.
            return _Completed(1, "", f"Error: No such volume: {argv[3]}")
        raise AssertionError(f"unexpected docker call: {argv}")

    run.calls = calls
    return run


def _observed():
    seen: list[tuple[str, str]] = []
    return seen, lambda service, container_id: seen.append((service, container_id))


def test_both_services_are_announced_before_chown_and_start():
    run = _runner()
    seen, on_created = _observed()
    proved = _compose_up(run, None, None, on_created=on_created)

    assert proved == {NODE_SERVICE: NODE_ID, MINER_SERVICE: MINER_ID}
    assert seen == [(NODE_SERVICE, NODE_ID), (MINER_SERVICE, MINER_ID)]

    # Ordering proved from the recorded argv: both announcements precede the chown and the start.
    kinds = [argv[1] for argv in run.calls]
    assert kinds.index("container") < kinds.index("run"), "announced after the chown"
    assert kinds.index("container") < kinds.index("start"), "announced after the start"


def test_a_chown_failure_still_leaves_both_services_announced():
    """Service creation is announced before post-start ownership repair can fail."""
    run = _runner(fail_chown=True)
    seen, on_created = _observed()
    with pytest.raises(DevnetLifecycleError):
        _compose_up(run, None, None, on_created=on_created)
    assert seen == [(NODE_SERVICE, NODE_ID), (MINER_SERVICE, MINER_ID)]


def test_a_start_failure_still_leaves_both_services_announced():
    run = _runner(fail_start=True)
    seen, on_created = _observed()
    with pytest.raises(DevnetLifecycleError):
        _compose_up(run, None, None, on_created=on_created)
    assert seen == [(NODE_SERVICE, NODE_ID), (MINER_SERVICE, MINER_ID)]


def test_a_failure_proving_the_second_service_keeps_the_first():
    """The node exists even though the miner could not be proved; its record must survive."""
    run = _runner(fail_second_inspect=True)
    seen, on_created = _observed()
    with pytest.raises(DevnetLifecycleError):
        _compose_up(run, None, None, on_created=on_created)
    assert seen == [(NODE_SERVICE, NODE_ID)]


def test_the_callback_reaches_compose_up_through_bring_up_and_verify():
    """The wiring the worker actually uses, not a hand-rolled call."""
    run = _runner(fail_chown=True)
    seen, on_created = _observed()

    with pytest.raises(DevnetLifecycleError):
        _bring_up_and_verify(
            run, lambda method, params: None, sleep=lambda _s: None, monotonic=lambda: 0.0,
            ready_timeout_s=1.0, miner_timeout_s=1.0, config_sha256="x",
            on_created=on_created,
        )
    assert seen == [(NODE_SERVICE, NODE_ID), (MINER_SERVICE, MINER_ID)]
