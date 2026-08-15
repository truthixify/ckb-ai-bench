"""Compose topology tests for the DevNet state split (plan §9.1).

The lifecycle can only reset a cell if mutable chain state lives in one removable named volume and
tracked configuration is read-only. If a future edit re-mounted the repository's `config/data`
directory, resets would silently stop working and every cell would inherit the previous chain --
exactly the confound this milestone removes -- so the topology is asserted here rather than trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parent / "compose.yml"
DEVNET_SERVICES = ("ckbbench-devnet-node", "ckbbench-devnet-miner")
DATA_VOLUME = "ckbbench-devnet-data"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _mounts(service: str) -> list[str]:
    """The mounts a service actually gets, following `volumes_from` inheritance."""
    spec = _compose()["services"][service]
    inherited: list[str] = []
    for source in spec.get("volumes_from", []):
        inherited += _mounts(source)
    return inherited + list(spec.get("volumes", []))


# Fixed for ordinary operation; validation substitutes a bare path, making it an anonymous volume
# owned by the node container, which is the only DevNet storage Docker can dispose by immutable ID.
STATE_MOUNT = "${CKBBENCH_DEVNET_DATA_MOUNT:-devnet-data:/var/lib/ckb/data}"


def test_devnet_services_write_only_to_the_state_mount():
    for service in DEVNET_SERVICES:
        writable = [m for m in _mounts(service) if not m.endswith(":ro")]
        assert writable == [STATE_MOUNT], (service, writable)


def test_the_miner_inherits_the_nodes_mounts_instead_of_restating_them():
    """Both services must share ONE data volume.

    An anonymous mount declared twice creates two separate volumes, and the miner would then run
    against storage the node never writes.
    """
    miner = _compose()["services"]["ckbbench-devnet-miner"]
    assert miner.get("volumes_from") == ["ckbbench-devnet-node"], miner.get("volumes_from")
    assert not miner.get("volumes"), "the miner must not declare its own mounts"


def test_tracked_devnet_config_is_mounted_read_only():
    for service in DEVNET_SERVICES:
        config_mounts = [m for m in _mounts(service) if m.startswith("./devnet/config/")]
        assert config_mounts, service
        for mount in config_mounts:
            assert mount.endswith(":ro"), f"{service}: {mount} must be read-only"


def test_legacy_bind_data_directory_is_not_mounted():
    """The Task 04 evidence lives in that directory; it must be left alone AND unused."""
    for service in _compose()["services"]:
        for mount in _mounts(service):
            assert "config/data" not in mount, f"{service} still mounts the legacy bind: {mount}"
            assert mount != "./devnet/config:/var/lib/ckb", f"{service} mounts the whole config dir"


def test_state_volume_is_named_once_and_labelled_for_ownership():
    volumes = _compose()["volumes"]
    assert list(volumes) == ["devnet-data"], "exactly one state volume definition"
    definition = volumes["devnet-data"]
    # Fixed for ordinary operation; a validation invocation exports an unguessable per-run name,
    # because a Docker volume has no immutable ID and a fixed name can always be replaced.
    assert definition["name"] == "${CKBBENCH_DEVNET_VOLUME:-" + DATA_VOLUME + "}"
    # The owner/role pair is the durable ownership contract the lifecycle controller asserts. The
    # validate-run label is a discriminator: empty for an ordinary `./bench up`, stamped only by a
    # validation invocation so that gate can prove it created this volume rather than borrowing
    # operator state.
    assert definition["labels"] == {
        "com.ckbbench.owner": "ckbbench",
        "com.ckbbench.role": "devnet-data",
        "com.ckbbench.validate-run": "${CKBBENCH_VALIDATE_RUN_ID:-}",
    }


def test_the_validate_run_label_is_empty_for_an_ordinary_bring_up():
    """A developer stack must never look like a validation run's disposable resource."""
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != "CKBBENCH_VALIDATE_RUN_ID"}
    out = subprocess.run(
        ["docker", "compose", "-f", "compose.yml", "config"],
        cwd=Path(__file__).resolve().parent, capture_output=True, text=True, env=env,
    )
    if out.returncode != 0:
        pytest.skip("docker compose config unavailable")
    rendered = yaml.safe_load(out.stdout)
    label = rendered["volumes"]["devnet-data"]["labels"]["com.ckbbench.validate-run"]
    assert label == "", f"an ordinary bring-up stamped a validation run label: {label!r}"


def test_host_rpc_is_published_on_loopback_only():
    """The dev chain's keys are public: binding 0.0.0.0 would offer it to the LAN."""
    ports = _compose()["services"]["ckbbench-devnet-node"]["ports"]
    assert ports == ["127.0.0.1:8114:8114"], ports


def test_agent_reachable_service_name_and_networks_are_unchanged():
    node = _compose()["services"]["ckbbench-devnet-node"]
    assert set(node["networks"]) == {"net-internal", "net-rpc"}
    assert node["image"] == "nervos/ckb:v0.207.0"
