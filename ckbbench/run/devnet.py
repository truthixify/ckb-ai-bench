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
OWNER_LABELS = {"com.ckbbench.owner": "ckbbench", "com.ckbbench.role": "devnet-data"}
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


def _assert_owned_container(name: str, payload: dict) -> None:
    """Both identity labels must match: a container that merely shares the project is not proof
    that this exact DevNet service is what we are about to remove."""
    labels = (payload.get("Config") or {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    if project != COMPOSE_PROJECT or service != name:
        raise DevnetLifecycleError(
            f"refusing to act on {name}: compose identity is project={project!r} "
            f"service={service!r}, expected project={COMPOSE_PROJECT!r} service={name!r}"
        )


def _remove_services(run: RunCallable) -> None:
    """Stop and remove exactly the miner and node, miner first, then prove both are absent."""
    for name in (MINER_SERVICE, NODE_SERVICE):
        payload = _container_state(run, name)
        if payload is None:
            continue
        _assert_owned_container(name, payload)
        proc = run(["docker", "rm", "-f", name])
        if proc.returncode != 0:
            raise DevnetLifecycleError(f"could not remove {name}: {(proc.stderr or '').strip()}")
    for name in (MINER_SERVICE, NODE_SERVICE):
        if _container_state(run, name) is not None:
            raise DevnetLifecycleError(f"{name} still present after removal")


def _volume_payload(run: RunCallable) -> dict | None:
    return _docker_json(
        run, ["docker", "volume", "inspect", DATA_VOLUME, "--format", "{{json .}}"],
        what=DATA_VOLUME, kind="volume", name=DATA_VOLUME,
    )


def assert_volume_is_ours(payload: dict) -> None:
    """A matching name is not permission to delete: the ownership labels must match too."""
    if payload.get("Name") != DATA_VOLUME:
        raise DevnetLifecycleError(
            f"volume inspect returned {payload.get('Name')!r}, expected {DATA_VOLUME!r}"
        )
    labels = payload.get("Labels") or {}
    missing = {k: v for k, v in OWNER_LABELS.items() if labels.get(k) != v}
    if missing:
        raise DevnetLifecycleError(
            f"refusing to remove volume {DATA_VOLUME}: it is not benchmark-owned "
            f"(expected labels {OWNER_LABELS}, got {labels or 'none'})"
        )


def _assert_volume_unused(run: RunCallable) -> None:
    proc = run([
        "docker", "ps", "-a", "--filter", f"volume={DATA_VOLUME}", "--format", "{{.Names}}",
    ])
    if proc.returncode != 0:
        raise DevnetLifecycleError(f"could not list volume users: {(proc.stderr or '').strip()}")
    users = [name for name in (proc.stdout or "").split() if name]
    if users:
        raise DevnetLifecycleError(
            f"refusing to remove {DATA_VOLUME}: still mounted by {', '.join(users)}"
        )


def remove_data_volume(run: RunCallable | None = None) -> bool:
    """Remove the DevNet state volume after proving name, labels, and that nothing mounts it.

    Returns True when a volume was removed, False when there was nothing to remove. Shared by the
    per-cell lifecycle and ``./bench reset`` so there is only one destructive path.
    """
    runner = run or _default_run
    payload = _volume_payload(runner)
    if payload is None:
        return False
    assert_volume_is_ours(payload)
    _assert_volume_unused(runner)
    proc = runner(["docker", "volume", "rm", DATA_VOLUME])
    if proc.returncode != 0:
        raise DevnetLifecycleError(
            f"could not remove volume {DATA_VOLUME}: {(proc.stderr or '').strip()}"
        )
    if _volume_payload(runner) is not None:
        raise DevnetLifecycleError(f"volume {DATA_VOLUME} still present after removal")
    return True


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file()), "-p", COMPOSE_PROJECT, *args]


def _compose_up(run: RunCallable) -> None:
    """Create the labelled volume, make it writable by the node, then start.

    ``/var/lib/ckb/data`` does not exist in the pinned image, so Docker creates a fresh named
    volume owned by root while the node runs as ``ckb``; starting straight away dies with
    "IO Error: PermissionDenied". Creating first lets one throwaway container (the same pinned
    service, so no second image reference) hand the volume to ``ckb`` before the node boots.
    """
    proc = run(_compose("create", NODE_SERVICE, MINER_SERVICE))
    if proc.returncode != 0:
        raise DevnetLifecycleError(
            f"could not create DevNet services: {(proc.stderr or '').strip()}"
        )
    # Re-prove ownership on the volume compose just materialised: between the pre-flight check and
    # now, an absent volume could have been created by something else under the same name.
    created = _volume_payload(run)
    if created is None:
        raise DevnetLifecycleError(
            f"{DATA_VOLUME} was not created by compose; refusing to chown an unknown target"
        )
    assert_volume_is_ours(created)
    chown = run(_compose(
        "run", "--rm", "--no-deps", "--user", "0:0", "--entrypoint", "sh",
        NODE_SERVICE, "-c", "chown ckb:ckb /var/lib/ckb/data",
    ))
    if chown.returncode != 0:
        raise DevnetLifecycleError(
            f"could not hand the state volume to the node user: {(chown.stderr or '').strip()}"
        )
    proc = run(_compose("start", NODE_SERVICE, MINER_SERVICE))
    if proc.returncode != 0:
        raise DevnetLifecycleError(f"could not start DevNet services: {(proc.stderr or '').strip()}")


def _assert_services_running(run: RunCallable) -> None:
    for name in (NODE_SERVICE, MINER_SERVICE):
        payload = _container_state(run, name)
        if payload is None:
            raise DevnetLifecycleError(f"{name} is not present after startup")
        _assert_owned_container(name, payload)
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


def _assert_volume_absent_or_ours(run: RunCallable) -> None:
    """A volume carrying the canonical NAME is not necessarily ours.

    Compose will happily attach that name to a volume it did not create, and the startup path then
    chowns it to the node user and runs a chain on top -- mutating foreign state without ever
    deleting it. Absent is fine (it will be created); present must be label-owned.
    """
    payload = _volume_payload(run)
    if payload is not None:
        assert_volume_is_ours(payload)


def _bring_up_and_verify(
    run: RunCallable, rpc: RpcCallable, *, sleep, monotonic,
    ready_timeout_s: float, miner_timeout_s: float, config_sha256: str,
) -> DevnetState:
    """Create, hand over the volume, start, then prove identity, miner progress and the funded path."""
    _assert_volume_absent_or_ours(run)
    _compose_up(run)
    _assert_services_running(run)
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
        _remove_services(runner)
        remove_data_volume(runner)
        return _bring_up_and_verify(
            runner, client, sleep=nap, monotonic=clock,
            ready_timeout_s=ready_timeout_s, miner_timeout_s=miner_timeout_s,
            config_sha256=config_sha256,
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
        return _bring_up_and_verify(
            runner, client, sleep=nap, monotonic=clock,
            ready_timeout_s=ready_timeout_s, miner_timeout_s=miner_timeout_s,
            config_sha256=config_sha256,
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
