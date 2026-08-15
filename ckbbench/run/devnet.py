"""Per-cell DevNet lifecycle: one fresh chain state before every production Docker DevNet cell.

Without this, cells run sequentially against a chain that keeps every prior cell's transactions,
spent inputs, and indexer state, so a B/C difference can be caused by execution order rather than
MCP access (plan §9.1; chain-alignment audit C25-C31).

Everything destructive here is exact and fail-closed: the controller stops only the two named
DevNet services, removes only a volume whose name AND ownership labels it has inspected, and
re-inspects afterwards. Ambiguity -- a foreign label, a running agent, an unreadable inspect, a
chain that is not ``ckb_dev``, a miner that does not advance -- raises ``DevnetLifecycleError``,
which the orchestrator records as ``infra_fail`` before any MCP, model, or run-parameter work.
"""

from __future__ import annotations

import hashlib
import re
import json
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Bumping this string is a methodology change: it names the state policy every result carries.
LIFECYCLE_POLICY = "per-cell-fresh-v1"

COMPOSE_PROJECT = "ckbbench"
NODE_SERVICE = "ckbbench-devnet-node"
MINER_SERVICE = "ckbbench-devnet-miner"
DATA_VOLUME = "ckbbench-devnet-data"


def devnet_data_is_anonymous() -> bool:
    """True when the DevNet data mount is an anonymous volume owned by the node container.

    Compose renders ``CKBBENCH_DEVNET_DATA_MOUNT`` as the node's data mount. A bare container path
    (no ``source:`` prefix) is Docker's spelling for an anonymous volume, whose lifetime is bound to
    the exact container that holds it.
    """
    mount = os.getenv("CKBBENCH_DEVNET_DATA_MOUNT", "")
    return bool(mount) and ":" not in mount


def data_volume_name() -> str | None:
    """The DevNet state volume for this process, or None when the data mount is anonymous.

    Ordinary runs use the fixed operator volume. A validation invocation uses an anonymous volume
    instead: a named Docker volume has no immutable ID, so a fixed name can always be swapped
    between the ownership check and the removal, and no label or mountpoint comparison repairs that.
    An anonymous volume is disposed through its container's immutable ID and needs no name.
    """
    if devnet_data_is_anonymous():
        return None
    return os.getenv("CKBBENCH_DEVNET_VOLUME") or DATA_VOLUME
OWNER_LABELS = {"com.ckbbench.owner": "ckbbench", "com.ckbbench.role": "devnet-data"}
# Set only by containers/validate.sh. When present, the lifecycle must additionally prove that every
# container and volume it is about to stop, remove, chown, start or reuse carries this exact value.
# Without it a developer stack that appears under the fixed names between that gate's preflight and
# its lifecycle call would be accepted on its ordinary labels and destroyed.
VALIDATE_RUN_LABEL = "com.ckbbench.validate-run"


def expected_validate_run() -> str | None:
    """The validation identity this process must require, or None for ordinary lifecycle use."""
    value = os.getenv("CKBBENCH_VALIDATE_RUN_ID") or ""
    return value or None
AGENT_CONTAINER_PREFIX = "minisweagent-"
# The compose agent service can also hold the chain open (containers/compose.yml, profile "agent").
AGENT_SERVICE = "ckbbench-agent"
DEVNET_CHAIN_ID = "ckb_dev"
# The managed lifecycle owns exactly this endpoint. An override pointing anywhere else is not
# disposable state this controller may recreate.
CANONICAL_HOST_RPC = "http://127.0.0.1:8114"

# Tracked DevNet configuration, relative to the repository root. Mutable data/, logs, generated
# allowlists, env files and Docker runtime ids are deliberately excluded: the digest identifies the
# deterministic chain definition, not one machine's runtime.
CONFIG_PATHS = (
    "containers/devnet/config/ckb-miner.toml",
    "containers/devnet/config/ckb.toml",
    "containers/devnet/config/default.db-options",
    "containers/devnet/config/specs/dev.toml",
)

READY_TIMEOUT_S = 120.0
MINER_TIMEOUT_S = 60.0

# The genesis-funded sender lock (dev.toml issued cell, public fixture). Querying it proves the
# indexer is live and the funded path task 04 needs is readable from the fresh state.
SECP_CODE_HASH = "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8"
SENDER_LOCK_ARGS = "0xc8328aabcd9b9e8e64fbc566c4385c3bdeb219d7"

RunCallable = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
RpcCallable = Callable[[str, list[Any]], Any]


class DevnetVolumeRetained(RuntimeError):
    """Validation declined a name-selected volume deletion and kept the scoped artifact."""


class DevnetLifecycleError(RuntimeError):
    """Reset, identity, readiness or ownership failure. Always classified as infra_fail."""


@dataclass(frozen=True)
class DevnetState:
    """Chain-state provenance for one prepared cell.

    Immutable identity (policy, chain, genesis, config digest) must match across a suite's managed
    results; the prepared tip is observed, not fixed -- the miner runs continuously, so equal tips
    would be a false claim.
    """

    lifecycle_policy: str
    chain: str
    genesis_hash: str
    config_sha256: str
    prepared_tip_number: int
    prepared_tip_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_policy": self.lifecycle_policy,
            "chain": self.chain,
            "genesis_hash": self.genesis_hash,
            "config_sha256": self.config_sha256,
            "prepared_tip_number": self.prepared_tip_number,
            "prepared_tip_hash": self.prepared_tip_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DevnetState:
        return cls(
            lifecycle_policy=str(data["lifecycle_policy"]),
            chain=str(data["chain"]),
            genesis_hash=str(data["genesis_hash"]),
            config_sha256=str(data["config_sha256"]),
            prepared_tip_number=int(data["prepared_tip_number"]),
            prepared_tip_hash=str(data["prepared_tip_hash"]),
        )


def _default_run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def compose_file() -> Path:
    return repo_root() / "containers" / "compose.yml"


def devnet_config_digest(root: Path | None = None) -> str:
    """SHA-256 over the tracked DevNet configuration, path-independent and order-independent.

    Hashes ``relative path + content digest`` for a fixed sorted file list, so the same checkout
    on another machine (or in another directory) yields the same value, while a changed
    configuration byte does not.
    """
    base = Path(root) if root is not None else repo_root()
    outer = hashlib.sha256()
    for rel in sorted(CONFIG_PATHS):
        path = base / rel
        if not path.is_file():
            raise DevnetLifecycleError(f"missing tracked DevNet config file: {rel}")
        outer.update(rel.encode())
        outer.update(b"\0")
        outer.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
        outer.update(b"\n")
    return outer.hexdigest()


# Docker object names are letters, digits, underscore, period and hyphen. A name is only "mentioned"
# if it appears as a whole token: `ckbbench-devnet-data-backup` contains `ckbbench-devnet-data`, and
# a substring test would read an error about the backup as proof the canonical volume is gone.
_NAME_CHAR = r"[A-Za-z0-9_.-]"


def mentions_exact_name(text: str, name: str) -> bool:
    """True only if `name` appears in `text` as a complete Docker-name token."""
    return re.search(rf"(?<!{_NAME_CHAR}){re.escape(name)}(?!{_NAME_CHAR})", text) is not None


def _is_absence(stderr: str, kind: str, name: str) -> bool:
    """Only an object-specific "no such <kind>" naming THIS object proves absence.

    Docker words the two cases differently -- `Error: No such container: <name>` but
    `Error response from daemon: get <name>: no such volume` -- so both the kind phrase and the
    exact name are required rather than a fixed order. A daemon, context or permission failure
    mentions neither, and ambiguity must never be read as permission to proceed.
    """
    return f"no such {kind}" in stderr.lower() and mentions_exact_name(stderr, name)


def _docker_json(
    run: RunCallable, argv: Sequence[str], *, what: str, kind: str, name: str
) -> Any | None:
    """Run a docker inspect-style command. None means proven absent; anything unclear aborts."""
    proc = run(argv)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if _is_absence(stderr, kind, name):
            return None
        raise DevnetLifecycleError(f"docker could not inspect {what}: {stderr}")
    text = (proc.stdout or "").strip()
    if not text:
        raise DevnetLifecycleError(
            f"docker reported success but returned nothing for {what}; refusing to treat an "
            "empty inspect as absence"
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DevnetLifecycleError(f"unreadable docker inspect output for {what}: {exc}") from exc
    if isinstance(payload, list):  # some docker versions answer with a single-element list
        if len(payload) != 1:
            raise DevnetLifecycleError(f"ambiguous docker inspect payload for {what}: {payload!r}")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise DevnetLifecycleError(f"unexpected docker inspect payload for {what}: {payload!r}")
    return payload


def _running_agent_containers(run: RunCallable) -> list[str]:
    proc = run(["docker", "ps", "--format", "{{.Names}}"])
    if proc.returncode != 0:
        raise DevnetLifecycleError(f"docker ps failed: {(proc.stderr or '').strip()}")
    return [
        name for name in (proc.stdout or "").split()
        if name.startswith(AGENT_CONTAINER_PREFIX) or name == AGENT_SERVICE
    ]


def _assert_no_agent_running(run: RunCallable) -> None:
    agents = _running_agent_containers(run)
    if agents:
        raise DevnetLifecycleError(
            f"refusing to reset DevNet while benchmark agents are running: {', '.join(agents)}"
        )


def _container_state(run: RunCallable, name: str) -> dict | None:
    return _docker_json(
        run, ["docker", "container", "inspect", name, "--format", "{{json .}}"],
        what=name, kind="container", name=name,
    )


def _assert_owned_container(name: str, payload: dict, expect_run: str | None = None) -> None:
    """Both identity labels must match: a container that merely shares the project is not proof
    that this exact DevNet service is what we are about to remove.

    In validation mode the container must ALSO carry that invocation's run label, so an ordinary
    developer stack occupying the same fixed name is refused instead of destroyed.
    """
    labels = (payload.get("Config") or {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    if project != COMPOSE_PROJECT or service != name:
        raise DevnetLifecycleError(
            f"refusing to act on {name}: compose identity is project={project!r} "
            f"service={service!r}, expected project={COMPOSE_PROJECT!r} service={name!r}"
        )
    if expect_run is not None and labels.get(VALIDATE_RUN_LABEL) != expect_run:
        raise DevnetLifecycleError(
            f"refusing to act on {name}: it does not carry this validation run's identity; "
            "it belongs to another user of this Docker host"
        )


def _remove_services(run: RunCallable, expect_run: str | None = None) -> None:
    """Stop and remove exactly the miner and node, miner first, then prove both are absent."""
    for name in (MINER_SERVICE, NODE_SERVICE):
        payload = _container_state(run, name)
        if payload is None:
            continue
        _assert_owned_container(name, payload, expect_run)
        # By ID, not by name: between the inspect and the removal another client can put a
        # different container at this name, and `docker rm -f <name>` would destroy that one.
        container_id = payload.get("Id")
        if not container_id:
            raise DevnetLifecycleError(f"could not read the id of {name} before removing it")
        proc = run(["docker", "rm", "-f", container_id])
        if proc.returncode != 0:
            raise DevnetLifecycleError(f"could not remove {name}: {(proc.stderr or '').strip()}")
    for name in (MINER_SERVICE, NODE_SERVICE):
        if _container_state(run, name) is not None:
            raise DevnetLifecycleError(f"{name} still present after removal")


def _volume_payload(run: RunCallable, volume: str | None = None) -> dict | None:
    volume = volume or data_volume_name()
    if volume is None:
        return None
    return _docker_json(
        run, ["docker", "volume", "inspect", volume, "--format", "{{json .}}"],
        what=volume, kind="volume", name=volume,
    )


def assert_volume_is_ours(
    payload: dict, expect_run: str | None = None, volume: str | None = None
) -> None:
    """A matching name is not permission to delete: the ownership labels must match too.

    In validation mode the volume must ALSO carry that invocation's run label.
    """
    volume = volume or data_volume_name()
    if payload.get("Name") != volume:
        raise DevnetLifecycleError(
            f"volume inspect returned {payload.get('Name')!r}, expected {volume!r}"
        )
    labels = payload.get("Labels") or {}
    missing = {k: v for k, v in OWNER_LABELS.items() if labels.get(k) != v}
    if missing:
        raise DevnetLifecycleError(
            f"refusing to remove volume {volume}: it is not benchmark-owned "
            f"(expected labels {OWNER_LABELS}, got {labels or 'none'})"
        )
    if expect_run is not None and labels.get(VALIDATE_RUN_LABEL) != expect_run:
        raise DevnetLifecycleError(
            f"refusing to remove volume {volume}: it does not carry this validation "
            "run's "
            "identity; it is ordinary operator state"
        )


def _assert_volume_unused(run: RunCallable, volume: str | None = None) -> None:
    volume = volume or data_volume_name()
    proc = run([
        "docker", "ps", "-a", "--filter", f"volume={volume}", "--format", "{{.Names}}",
    ])
    if proc.returncode != 0:
        raise DevnetLifecycleError(f"could not list volume users: {(proc.stderr or '').strip()}")
    users = [name for name in (proc.stdout or "").split() if name]
    if users:
        raise DevnetLifecycleError(
            f"refusing to remove {volume}: still mounted by {', '.join(users)}"
        )


def remove_data_volume(
    run: RunCallable | None = None, expect_run: str | None = None, volume: str | None = None
) -> bool:
    """Remove the DevNet state volume after proving name, labels, and that nothing mounts it.

    Returns True when a volume was removed, False when there was nothing to remove. Shared by the
    per-cell lifecycle and ``./bench reset`` so there is only one destructive path.
    """
    runner = run or _default_run
    # Resolved ONCE: every subsequent step uses this exact selector, so an environment change
    # mid-call cannot make the removal target a different volume than the one proved.
    volume = volume or data_volume_name()
    if volume is None:
        # Anonymous data: there is no name to select and nothing to remove here. The volume is
        # disposed with its owning container by immutable ID.
        return False
    payload = _volume_payload(runner, volume)
    if payload is None:
        return False
    assert_volume_is_ours(payload, expect_run, volume)
    _assert_volume_unused(runner, volume)
    if expect_run is not None:
        # `docker volume rm` selects by a reusable name: between the proof above and the call, the
        # name can be re-pointed at a different volume, and Docker offers no immutable volume
        # handle to bind the mutation to. Validation therefore retains its scoped, disposable
        # volume instead of issuing a deletion it cannot make ownership-safe.
        raise DevnetVolumeRetained(
            f"refusing name-selected deletion of {volume} in validation mode; "
            "the scoped volume is retained"
        )
    proc = runner(["docker", "volume", "rm", volume])
    if proc.returncode != 0:
        raise DevnetLifecycleError(
            f"could not remove volume {volume}: {(proc.stderr or '').strip()}"
        )
    if _volume_payload(runner, volume) is not None:
        raise DevnetLifecycleError(f"volume {volume} still present after removal")
    return True


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file()), "-p", COMPOSE_PROJECT, *args]


def _compose_up(
    run: RunCallable, expect_run: str | None = None, volume: str | None = None
) -> dict[str, str]:
    """Create the services, make the data mount writable by the node, then start.

    ``/var/lib/ckb/data`` does not exist in the pinned image, so Docker creates a fresh volume
    owned by root while the node runs as ``ckb``; starting straight away dies with
    "IO Error: PermissionDenied". Creating first lets one throwaway container hand the volume to
    ``ckb`` before the node boots.

    The containers are proved BEFORE the chown, and the chown then borrows the proved node's own
    mounts by immutable ID. Resolving the service name again for the chown would let a replacement
    receive it, and with an anonymous data volume it would also chown a different volume than the
    one the node actually holds.
    """
    proc = run(_compose("create", NODE_SERVICE, MINER_SERVICE))
    if proc.returncode != 0:
        raise DevnetLifecycleError(
            f"could not create DevNet services: {(proc.stderr or '').strip()}"
        )
    # Every container Compose just created must be proved BEFORE it is chowned or started: acting
    # on a replacement that carries only the generic labels is already acting on someone else's
    # object.
    proved: dict[str, str] = {}
    node_image = ""
    for name in (NODE_SERVICE, MINER_SERVICE):
        payload = _container_state(run, name)
        if payload is None:
            raise DevnetLifecycleError(f"{name} was not created by compose")
        _assert_owned_container(name, payload, expect_run)
        if name == NODE_SERVICE:
            node_image = str(payload.get("Image") or "")
        container_id = payload.get("Id")
        if not container_id:
            raise DevnetLifecycleError(f"could not read the id of {name} before starting it")
        proved[name] = container_id
    if volume is not None:
        # Re-prove ownership on the named volume compose just materialised: between the pre-flight
        # check and now, an absent volume could have been created by something else at that name.
        created = _volume_payload(run, volume)
        if created is None:
            raise DevnetLifecycleError(
                f"{volume} was not created by compose; refusing to chown an unknown target"
            )
        assert_volume_is_ours(created, expect_run, volume)
    if not node_image:
        raise DevnetLifecycleError("could not read the node image id before the chown")
    chown = run([
        "docker", "run", "--rm", "--user", "0:0",
        "--volumes-from", proved[NODE_SERVICE], "--entrypoint", "sh",
        node_image, "-c", "chown ckb:ckb /var/lib/ckb/data",
    ])
    if chown.returncode != 0:
        raise DevnetLifecycleError(
            f"could not hand the state volume to the node user: {(chown.stderr or '').strip()}"
        )
    # Started by exact ID. `docker compose start <service>` resolves the service name again, so a
    # replacement arriving after the proof would be the container that actually starts.
    proc = run(["docker", "start", proved[NODE_SERVICE], proved[MINER_SERVICE]])
    if proc.returncode != 0:
        raise DevnetLifecycleError(f"could not start DevNet services: {(proc.stderr or '').strip()}")
    return proved


def _assert_services_running(
    run: RunCallable, expect_run: str | None = None, proved: dict[str, str] | None = None
) -> None:
    for name in (NODE_SERVICE, MINER_SERVICE):
        # Inspected by the exact started ID where one is known, so this cannot silently describe a
        # different container that has since taken the name.
        target = (proved or {}).get(name, name)
        payload = _container_state(run, target)
        if payload is None:
            raise DevnetLifecycleError(f"{name} is not present after startup")
        _assert_owned_container(name, payload, expect_run)
        if proved and payload.get("Id") not in (None, proved[name]):
            raise DevnetLifecycleError(
                f"{name} is a different container after startup than the one this run started"
            )
        if not ((payload.get("State") or {}).get("Running")):
            raise DevnetLifecycleError(f"{name} is not running after startup")


def _is_hash32(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("0x")
        and len(value) == 66
        and all(c in "0123456789abcdefABCDEF" for c in value[2:])
    )


def _await_rpc(rpc: RpcCallable, *, timeout_s: float, sleep, monotonic) -> None:
    deadline = monotonic() + timeout_s
    last: Exception | None = None
    while monotonic() < deadline:
        try:
            rpc("get_tip_block_number", [])
            return
        except Exception as exc:  # node still starting
            last = exc
            sleep(1.0)
    raise DevnetLifecycleError(f"DevNet RPC not ready within {timeout_s:.0f}s: {last}")


def _await_miner(rpc: RpcCallable, *, timeout_s: float, sleep, monotonic) -> int:
    """Require the miner to advance the tip by at least one block."""
    start = int(rpc("get_tip_block_number", []), 16)
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        current = int(rpc("get_tip_block_number", []), 16)
        if current > start:
            return current
        sleep(1.0)
    raise DevnetLifecycleError(
        f"miner did not advance the tip past {start} within {timeout_s:.0f}s"
    )


def _assert_funded_path_readable(rpc: RpcCallable) -> None:
    """The indexer must answer for the genesis-funded sender lock task 04 spends from."""
    search_key = {
        "script": {
            "code_hash": SECP_CODE_HASH,
            "hash_type": "type",
            "args": SENDER_LOCK_ARGS,
        },
        "script_type": "lock",
    }
    try:
        page = rpc("get_cells", [search_key, "asc", "0x1"])
    except Exception as exc:
        raise DevnetLifecycleError(f"indexer/funded-sender path not readable: {exc}") from exc
    if not isinstance(page, dict) or not page.get("objects"):
        raise DevnetLifecycleError("indexer returned no cells for the genesis-funded sender lock")


def _assert_volume_absent_or_ours(
    run: RunCallable, expect_run: str | None = None, volume: str | None = None
) -> None:
    """A volume carrying the canonical NAME is not necessarily ours.

    Compose will happily attach that name to a volume it did not create, and the startup path then
    chowns it to the node user and runs a chain on top -- mutating foreign state without ever
    deleting it. Absent is fine (it will be created); present must be label-owned.
    """
    if volume is None and devnet_data_is_anonymous():
        return
    payload = _volume_payload(run, volume)
    if payload is not None:
        assert_volume_is_ours(payload, expect_run, volume)


def _bring_up_and_verify(
    run: RunCallable, rpc: RpcCallable, *, sleep, monotonic,
    ready_timeout_s: float, miner_timeout_s: float, config_sha256: str,
    expect_run: str | None = None, volume: str | None = None,
) -> DevnetState:
    """Create, hand over the volume, start, then prove identity, miner progress and the funded path."""
    # Identities are supplied by the public entry, never re-read here: resolving them again would
    # let one call reset generation A and then create, start and certify generation B.
    _assert_volume_absent_or_ours(run, expect_run, volume)
    proved = _compose_up(run, expect_run, volume)
    _assert_services_running(run, expect_run, proved)
    _await_rpc(rpc, timeout_s=ready_timeout_s, sleep=sleep, monotonic=monotonic)

    info = rpc("get_blockchain_info", [])
    chain = (info or {}).get("chain") if isinstance(info, dict) else None
    if chain != DEVNET_CHAIN_ID:
        raise DevnetLifecycleError(f"prepared chain reports {chain!r}, expected {DEVNET_CHAIN_ID!r}")

    genesis_hash = rpc("get_block_hash", ["0x0"])
    if not _is_hash32(genesis_hash):
        raise DevnetLifecycleError(f"genesis hash is malformed: {genesis_hash!r}")

    tip_number = _await_miner(rpc, timeout_s=miner_timeout_s, sleep=sleep, monotonic=monotonic)
    tip_hash = rpc("get_block_hash", [hex(tip_number)])
    if not _is_hash32(tip_hash):
        raise DevnetLifecycleError(f"prepared tip hash is malformed: {tip_hash!r}")

    _assert_funded_path_readable(rpc)

    return DevnetState(
        lifecycle_policy=LIFECYCLE_POLICY,
        chain=DEVNET_CHAIN_ID,
        genesis_hash=str(genesis_hash),
        config_sha256=config_sha256,
        prepared_tip_number=tip_number,
        prepared_tip_hash=str(tip_hash),
    )


def _lifecycle(fn, *, rpc_url: str, rpc: RpcCallable | None):
    """Run a lifecycle body with the endpoint guard and uniform failure classification.

    Every expected failure inside the controller -- a raised RPC transport error, a malformed tip,
    a docker spawn error, an unreadable config -- must reach ``run_cell`` as DevnetLifecycleError,
    because that is the only type the cell boundary converts into an early infra_fail. Relying on
    each dependency to raise the right type is how a reset failure escapes as a crash instead.
    """
    if rpc_url != CANONICAL_HOST_RPC:
        raise DevnetLifecycleError(
            f"managed DevNet lifecycle owns {CANONICAL_HOST_RPC} only; refusing to manage "
            f"{rpc_url} (set CKBBENCH_DEVNET_RPC back, or run this chain unmanaged)"
        )
    try:
        client = rpc
        if client is None:
            # inside the boundary: a client-factory failure must also become a lifecycle error,
            # since that is the only type run_cell converts into an early infra_fail
            from ckbbench.ckb_rpc import make_rpc_client

            client = make_rpc_client(rpc_url, timeout=10.0)
        return fn(client)
    except DevnetLifecycleError:
        raise
    except Exception as exc:
        raise DevnetLifecycleError(f"{type(exc).__name__}: {exc}") from exc


def prepare_devnet(
    *,
    run: RunCallable | None = None,
    rpc: RpcCallable | None = None,
    rpc_url: str = CANONICAL_HOST_RPC,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    ready_timeout_s: float = READY_TIMEOUT_S,
    miner_timeout_s: float = MINER_TIMEOUT_S,
    root: Path | None = None,
) -> DevnetState:
    """Recreate the DevNet from a newly created state volume and prove it is ready.

    Order is load-bearing: refuse while an agent runs, remove services, prove them gone, prove the
    volume is ours and unused, remove it, prove it gone, recreate, then verify identity, miner
    progress and the funded path before returning provenance.
    """
    runner = run or _default_run
    nap = sleep or time.sleep
    clock = monotonic or time.monotonic

    def body(client: RpcCallable) -> DevnetState:
        config_sha256 = devnet_config_digest(root)
        _assert_no_agent_running(runner)
        # Resolved ONCE for this whole call.
        expect_run = expected_validate_run()
        volume = data_volume_name()
        _remove_services(runner, expect_run)
        remove_data_volume(runner, expect_run, volume)
        return _bring_up_and_verify(
            runner, client, sleep=nap, monotonic=clock,
            ready_timeout_s=ready_timeout_s, miner_timeout_s=miner_timeout_s,
            config_sha256=config_sha256, expect_run=expect_run, volume=volume,
        )

    return _lifecycle(body, rpc_url=rpc_url, rpc=rpc)


def start_devnet(
    *,
    run: RunCallable | None = None,
    rpc: RpcCallable | None = None,
    rpc_url: str = CANONICAL_HOST_RPC,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    ready_timeout_s: float = READY_TIMEOUT_S,
    miner_timeout_s: float = MINER_TIMEOUT_S,
    root: Path | None = None,
) -> DevnetState:
    """Start DevNet WITHOUT destroying state: the ``./bench up`` path.

    A first `up` on a machine with no volume still needs the create/chown/start sequence, or the
    node dies with PermissionDenied on the root-owned volume; an existing owned volume is reused,
    because `up` is documented as retaining state.
    """
    runner = run or _default_run
    nap = sleep or time.sleep
    clock = monotonic or time.monotonic

    def body(client: RpcCallable) -> DevnetState:
        config_sha256 = devnet_config_digest(root)
        # Resolved ONCE for this whole call, like prepare_devnet().
        return _bring_up_and_verify(
            runner, client, sleep=nap, monotonic=clock,
            ready_timeout_s=ready_timeout_s, miner_timeout_s=miner_timeout_s,
            config_sha256=config_sha256,
            expect_run=expected_validate_run(), volume=data_volume_name(),
        )

    return _lifecycle(body, rpc_url=rpc_url, rpc=rpc)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator entry point
    """Operator entry point so ``./bench reset`` reuses this safety boundary, not a shell rm."""
    import argparse

    parser = argparse.ArgumentParser(description="Managed DevNet state lifecycle.")
    parser.add_argument(
        "--remove-data-volume", action="store_true",
        help="remove the inspected, benchmark-owned DevNet state volume (used by ./bench reset)",
    )
    parser.add_argument(
        "--start", action="store_true",
        help="start DevNet without destroying state, with the ownership-prepared sequence "
             "and full readiness checks (used by ./bench up)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.start:
            state = start_devnet()
            print(f"devnet ready: chain={state.chain} tip={state.prepared_tip_number} "
                  f"genesis={state.genesis_hash[:18]}...")
            return 0
        if args.remove_data_volume:
            removed = remove_data_volume()
            print(f"removed volume {DATA_VOLUME}" if removed
                  else f"no {DATA_VOLUME} volume to remove")
            return 0
    except DevnetLifecycleError as exc:
        print(f"FAIL: {exc}")
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
