"""Compose topology tests for the DevNet state split (plan §9.1).

The lifecycle can only reset a cell if mutable chain state lives in one removable named volume and
tracked configuration is read-only. If a future edit re-mounted the repository's `config/data`
directory, resets would silently stop working and every cell would inherit the previous chain --
exactly the confound this milestone removes -- so the topology is asserted here rather than trusted.
"""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parent / "compose.yml"
DEVNET_SERVICES = ("ckbbench-devnet-node", "ckbbench-devnet-miner")
DATA_VOLUME = "ckbbench-devnet-data"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _mounts(service: str) -> list[str]:
    return list(_compose()["services"][service].get("volumes", []))


def test_devnet_services_write_only_to_the_named_state_volume():
    for service in DEVNET_SERVICES:
        writable = [m for m in _mounts(service) if not m.endswith(":ro")]
        assert writable == ["devnet-data:/var/lib/ckb/data"], (service, writable)


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
    assert definition["name"] == DATA_VOLUME
    assert definition["labels"] == {
        "com.ckbbench.owner": "ckbbench",
        "com.ckbbench.role": "devnet-data",
    }


def test_host_rpc_is_published_on_loopback_only():
    """The dev chain's keys are public: binding 0.0.0.0 would offer it to the LAN."""
    ports = _compose()["services"]["ckbbench-devnet-node"]["ports"]
    assert ports == ["127.0.0.1:8114:8114"], ports


def test_agent_reachable_service_name_and_networks_are_unchanged():
    node = _compose()["services"]["ckbbench-devnet-node"]
    assert set(node["networks"]) == {"net-internal", "net-rpc"}
    assert node["image"] == "nervos/ckb:v0.207.0"
