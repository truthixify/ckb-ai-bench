"""DevNet lifecycle tests: destructive safety, ordering, and fail-closed readiness (plan §9.1).

Every test drives the real controller with fake docker/RPC seams. The invariant that matters is
not "reset works" but "reset refuses": a same-named foreign volume, a running agent, a still-mounted
volume, an unreadable inspect, a wrong chain, a malformed genesis, or a stuck miner must all abort
before anything is deleted or a cell proceeds. A test that could not fail if the ownership check
were dropped would be worthless.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ckbbench.run import devnet
from ckbbench.run.devnet import (
    VALIDATE_RUN_LABEL,
    CONFIG_PATHS,
    DATA_VOLUME,
    DEVNET_CHAIN_ID,
    LIFECYCLE_POLICY,
    MINER_SERVICE,
    NODE_SERVICE,
    OWNER_LABELS,
    DevnetLifecycleError,
    devnet_config_digest,
    prepare_devnet,
    remove_data_volume,
)

GENESIS = "0x" + "ab" * 32
TIP_HASH = "0x" + "cd" * 32


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str, code: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr=stderr)


def _owned_volume(labels: dict | None = None, name: str | None = None) -> str:
    return json.dumps({
        "Name": name or DATA_VOLUME,
        "Labels": labels if labels is not None else OWNER_LABELS,
    })


def _owned_container(name: str, running: bool = True, validate_run: str | None = None) -> str:
    """Both compose identity labels, as a real container carries them: a fixture with only the
    project label would encode the fail-open bug as the happy path.

    ``validate_run`` stamps the per-invocation validation label the way Compose does when that
    identity is exported; without it the container looks like an ordinary developer stack.
    """
    labels = {
        "com.docker.compose.project": "ckbbench",
        "com.docker.compose.service": name,
    }
    if validate_run is not None:
        labels[VALIDATE_RUN_LABEL] = validate_run
    return json.dumps({
        "Id": f"sha256:id-{name}",
        # The chown borrows the proved node's own mounts and runs its exact image id, so the
        # payload must carry the image the way `docker container inspect` does.
        "Image": f"sha256:image-{name}",
        "Config": {"Labels": labels},
        "State": {"Running": running},
    })


class FakeDocker:
    """Records every docker argv and answers from a scripted world."""

    def __init__(self, *, volume: str | None = None, containers_before: bool = True,
                 agents: str = "", volume_users: str = "", fail_on: dict | None = None,
                 volume_after_create: str | None = None, validate_run: str | None = None,
                 validate_run_after_create: str | None = None):
        self.calls: list[list[str]] = []
        self.volume = volume
        self.removed_volume = False
        self.present = {MINER_SERVICE: containers_before, NODE_SERVICE: containers_before}
        self.agents = agents
        self.volume_users = volume_users
        self.fail_on = fail_on or {}
        self.volume_after_create = volume_after_create
        self.started = False
        self.created = False
        # The label Compose stamps on containers it creates for this invocation.
        self.validate_run = validate_run
        # Simulates a replacement between create and start: the container that appears afterwards
        # carries a different (or no) validation identity.
        self.validate_run_after_create = validate_run_after_create

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        for needle, response in self.fail_on.items():
            if needle in joined:
                return response
        if argv[:2] == ["docker", "ps"] and "volume=" in joined:
            return _ok(self.volume_users)
        if argv[:2] == ["docker", "ps"]:
            return _ok(self.agents)
        if argv[:3] == ["docker", "container", "inspect"]:
            name = argv[3]
            # Inspection by exact ID resolves to the same service.
            if name.startswith("sha256:id-"):
                name = name[len("sha256:id-"):]
            if self.started or self.created or self.present.get(name):
                stamp = self.validate_run
                if self.created and self.validate_run_after_create is not None:
                    stamp = self.validate_run_after_create or None
                return _ok(_owned_container(name, validate_run=stamp))
            return _fail(f"Error: No such container: {name}")
        if argv[:2] == ["docker", "rm"]:
            # Removal is by exact ID now; map it back to the service it identifies.
            target = argv[3]
            for name in (MINER_SERVICE, NODE_SERVICE):
                if target in (name, f"sha256:id-{name}"):
                    self.present[name] = False
                    self.created = False
            return _ok()
        if argv[:2] == ["docker", "start"]:
            self.started = True
            return _ok()
        if argv[:2] == ["docker", "run"] and "--volumes-from" in argv:
            # The chown borrows the proved node's own mounts by immutable ID rather than resolving
            # the service name again, which is also what makes an anonymous data volume chownable.
            self.chowned_from = argv[argv.index("--volumes-from") + 1]
            return _ok()
        if argv[:3] == ["docker", "volume", "inspect"]:
            if self.volume is None or self.removed_volume:
                return _fail(f"Error: No such volume: {argv[3]}")
            return _ok(self.volume)
        if argv[:3] == ["docker", "volume", "create"]:  # pragma: no cover - not used today
            return _ok()
        if argv[:3] == ["docker", "volume", "rm"]:
            self.removed_volume = True
            return _ok()
        if argv[:3] == ["docker", "compose", "-f"]:
            if "create" in argv:
                # compose materialises the labelled volume AND the containers, as it does against
                # real docker: they are inspectable before `start`.
                self.removed_volume = False
                self.created = True
                if self.volume_after_create is not None:
                    self.volume = self.volume_after_create
                elif self.volume is None:
                    self.volume = _owned_volume()
            if "start" in argv:
                self.started = True
            return _ok()
        raise AssertionError(f"unexpected docker call: {argv}")

    def index_of(self, *tokens: str) -> int:
        """Match whole argv tokens: substring matching would treat "--format" as an "rm"."""
        for i, argv in enumerate(self.calls):
            if all(token in argv for token in tokens):
                return i
        raise AssertionError(f"no docker call matched {tokens}: {self.calls}")


class FakeRpc:
    """A chain that answers identity questions and, unless stuck, advances one block per read."""

    def __init__(self, *, chain: str = DEVNET_CHAIN_ID, genesis: str = GENESIS,
                 start_tip: int = 7, advances: bool = True, cells: bool = True,
                 tip_hash: str = TIP_HASH):
        self.chain, self.genesis, self.tip_hash, self.cells = chain, genesis, tip_hash, cells
        self.tip, self.advances = start_tip, advances
        self.calls: list[str] = []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "get_tip_block_number":
            value = self.tip
            if self.advances:
                self.tip += 1
            return hex(value)
        if method == "get_blockchain_info":
            return {"chain": self.chain}
        if method == "get_block_hash":
            return self.genesis if params == ["0x0"] else self.tip_hash
        if method == "get_cells":
            return {"objects": [{"out_point": {}}] if self.cells else []}
        raise AssertionError(f"unexpected rpc: {method}")


def _prepare(docker, rpc, **kwargs):
    return prepare_devnet(
        run=docker, rpc=rpc, sleep=lambda _s: None,
        monotonic=iter(range(0, 10_000)).__next__, **kwargs,
    )


# --- the happy path and its ordering ---------------------------------------------------------


def test_prepare_returns_complete_provenance():
    docker, rpc = FakeDocker(volume=_owned_volume()), FakeRpc()
    state = _prepare(docker, rpc)
    assert state.lifecycle_policy == LIFECYCLE_POLICY
    assert state.chain == DEVNET_CHAIN_ID
    assert state.genesis_hash == GENESIS
    assert state.prepared_tip_number == 9
    assert state.prepared_tip_hash == TIP_HASH
    assert state.config_sha256 == devnet_config_digest()


def test_destructive_order_is_inspect_stop_prove_absent_remove_recreate():
    """Ownership is proven before deletion, services are gone before the volume is removed, and
    nothing is recreated until both are true."""
    docker, rpc = FakeDocker(volume=_owned_volume()), FakeRpc()
    _prepare(docker, rpc)

    inspect_volume = docker.index_of("volume", "inspect")
    # Removal is bound to the exact inspected ID, not the mutable name.
    remove_miner = docker.index_of("rm", "-f", f"sha256:id-{MINER_SERVICE}")
    remove_node = docker.index_of("rm", "-f", f"sha256:id-{NODE_SERVICE}")
    remove_volume = docker.index_of("volume", "rm")
    compose_up = docker.index_of("compose", "create")

    assert remove_miner < remove_node < remove_volume < compose_up
    assert inspect_volume < remove_volume
    # the funded/indexer path is proven only after the chain is up
    assert "get_cells" in rpc.calls


def test_repeated_preparation_creates_a_new_generation():
    """Every cell resets: a second invocation must remove and recreate again, not reuse."""
    docker, rpc = FakeDocker(volume=_owned_volume()), FakeRpc()
    _prepare(docker, rpc)
    first = len([c for c in docker.calls if c[:3] == ["docker", "volume", "rm"]])

    docker2, rpc2 = FakeDocker(volume=_owned_volume()), FakeRpc(start_tip=11)
    state2 = _prepare(docker2, rpc2)
    second = len([c for c in docker2.calls if c[:3] == ["docker", "volume", "rm"]])

    assert first == second == 1
    assert state2.prepared_tip_number == 13


# --- destructive safety ------------------------------------------------------------------------


def test_foreign_volume_with_the_same_name_is_refused():
    """A matching name is not permission to delete: an operator volume must survive."""
    docker = FakeDocker(volume=_owned_volume(labels={"com.example.owner": "someone-else"}))
    with pytest.raises(DevnetLifecycleError, match="not benchmark-owned"):
        _prepare(docker, FakeRpc())
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in docker.calls)


def test_unlabelled_volume_is_refused():
    docker = FakeDocker(volume=_owned_volume(labels={}))
    with pytest.raises(DevnetLifecycleError, match="not benchmark-owned"):
        _prepare(docker, FakeRpc())
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in docker.calls)


def test_running_agent_blocks_the_reset_before_anything_is_touched():
    docker = FakeDocker(volume=_owned_volume(), agents="minisweagent-abc123\n")
    with pytest.raises(DevnetLifecycleError, match="agents are running"):
        _prepare(docker, FakeRpc())
    assert not any(c[:2] == ["docker", "rm"] for c in docker.calls)
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in docker.calls)


def test_volume_still_mounted_by_a_container_is_refused():
    docker = FakeDocker(volume=_owned_volume(), volume_users="some-other-container\n")
    with pytest.raises(DevnetLifecycleError, match="still mounted by"):
        _prepare(docker, FakeRpc())
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in docker.calls)


def test_container_from_another_compose_project_is_refused():
    foreign = json.dumps({"Config": {"Labels": {"com.docker.compose.project": "someone-else"}}})
    docker = FakeDocker(volume=_owned_volume(), fail_on={"container inspect": _ok(foreign)})
    with pytest.raises(DevnetLifecycleError, match="compose identity"):
        _prepare(docker, FakeRpc())
    assert not any(c[:2] == ["docker", "rm"] for c in docker.calls)


def test_unreadable_inspect_output_aborts():
    docker = FakeDocker(volume=_owned_volume(), fail_on={"volume inspect": _ok("{not json")})
    with pytest.raises(DevnetLifecycleError, match="unreadable docker inspect"):
        _prepare(docker, FakeRpc())


def test_docker_daemon_error_aborts_instead_of_continuing():
    docker = FakeDocker(volume=_owned_volume(),
                        fail_on={"volume inspect": _fail("Cannot connect to the Docker daemon")})
    with pytest.raises(DevnetLifecycleError, match="could not inspect"):
        _prepare(docker, FakeRpc())


def test_volume_removal_failure_does_not_reach_recreation():
    docker = FakeDocker(volume=_owned_volume(), fail_on={"volume rm": _fail("volume is in use")})
    with pytest.raises(DevnetLifecycleError, match="could not remove volume"):
        _prepare(docker, FakeRpc())
    assert not any("compose" in c for c in docker.calls)


def test_state_volume_is_handed_to_the_node_user_before_start():
    """A fresh named volume is root-owned while the node runs as ckb; starting first dies with
    PermissionDenied, so ownership must be fixed between create and start."""
    docker, _ = FakeDocker(volume=_owned_volume()), None
    _prepare(docker, FakeRpc())
    create = docker.index_of("compose", "create")
    chown = docker.index_of("run", "--volumes-from")
    # Started by exact ID: `compose start <service>` would resolve the name again.
    start = docker.index_of("start", f"sha256:id-{NODE_SERVICE}")
    assert create < chown < start
    assert any("chown ckb:ckb /var/lib/ckb/data" in a for a in docker.calls[chown])
    # Borrowed from the proved node by immutable ID, not by resolving the service name again.
    assert docker.chowned_from == f"sha256:id-{NODE_SERVICE}"


def test_absent_volume_is_not_an_error():
    """First run on a clean machine: nothing to remove, everything still prepared."""
    docker, rpc = FakeDocker(volume=None), FakeRpc()
    assert _prepare(docker, rpc).chain == DEVNET_CHAIN_ID
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in docker.calls)


def test_remove_data_volume_reports_whether_it_removed_anything():
    assert remove_data_volume(FakeDocker(volume=_owned_volume())) is True
    assert remove_data_volume(FakeDocker(volume=None)) is False


# --- readiness fails closed --------------------------------------------------------------------


def test_wrong_chain_fails_closed():
    with pytest.raises(DevnetLifecycleError, match="expected 'ckb_dev'"):
        _prepare(FakeDocker(volume=_owned_volume()), FakeRpc(chain="ckb_testnet"))


def test_malformed_genesis_fails_closed():
    with pytest.raises(DevnetLifecycleError, match="genesis hash is malformed"):
        _prepare(FakeDocker(volume=_owned_volume()), FakeRpc(genesis="0xshort"))


def test_stuck_miner_fails_closed():
    with pytest.raises(DevnetLifecycleError, match="did not advance"):
        _prepare(FakeDocker(volume=_owned_volume()), FakeRpc(advances=False), miner_timeout_s=3)


def test_unreadable_indexer_fails_closed():
    with pytest.raises(DevnetLifecycleError, match="no cells for the genesis-funded sender"):
        _prepare(FakeDocker(volume=_owned_volume()), FakeRpc(cells=False))


def test_rpc_that_never_comes_up_times_out():
    class Dead:
        def __call__(self, method, params):
            raise RuntimeError("connection refused")

    with pytest.raises(DevnetLifecycleError, match="RPC not ready"):
        _prepare(FakeDocker(volume=_owned_volume()), Dead(), ready_timeout_s=3)


def test_non_canonical_rpc_endpoint_is_never_reset():
    """The managed lifecycle owns the local sidecar only; an override elsewhere is not ours."""
    docker = FakeDocker(volume=_owned_volume())
    with pytest.raises(DevnetLifecycleError, match="owns http://127.0.0.1:8114 only"):
        _prepare(docker, FakeRpc(), rpc_url="http://192.168.0.73:18114")
    assert docker.calls == []


# --- the deterministic config digest -----------------------------------------------------------


def test_config_digest_is_stable_and_path_independent(tmp_path: Path):
    copy = tmp_path / "checkout"
    for rel in CONFIG_PATHS:
        dest = copy / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((Path(rel)).read_bytes())
    assert devnet_config_digest(copy) == devnet_config_digest()
    assert devnet_config_digest(copy) == devnet_config_digest(copy)


def test_config_digest_changes_when_tracked_config_changes(tmp_path: Path):
    copy = tmp_path / "checkout"
    for rel in CONFIG_PATHS:
        dest = copy / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((Path(rel)).read_bytes())
    before = devnet_config_digest(copy)
    spec = copy / "containers/devnet/config/specs/dev.toml"
    spec.write_bytes(spec.read_bytes() + b"\n# changed\n")
    assert devnet_config_digest(copy) != before


def test_config_digest_ignores_mutable_state(tmp_path: Path):
    copy = tmp_path / "checkout"
    for rel in CONFIG_PATHS:
        dest = copy / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((Path(rel)).read_bytes())
    before = devnet_config_digest(copy)
    data = copy / "containers/devnet/config/data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "run.log").write_text("blocks and noise")
    assert devnet_config_digest(copy) == before


def test_missing_tracked_config_fails_loud(tmp_path: Path):
    with pytest.raises(DevnetLifecycleError, match="missing tracked DevNet config"):
        devnet_config_digest(tmp_path / "empty")


# --- fail-open regressions found in review 05 --------------------------------------------------


def test_container_with_the_right_project_but_wrong_service_is_refused():
    """Sharing a compose project is not proof of identity: the service label must match too."""
    wrong = json.dumps({"Config": {"Labels": {
        "com.docker.compose.project": "ckbbench",
        "com.docker.compose.service": "some-other-service"}}, "State": {"Running": True}})
    docker = FakeDocker(volume=_owned_volume(), fail_on={"container inspect": _ok(wrong)})
    with pytest.raises(DevnetLifecycleError, match="compose identity"):
        _prepare(docker, FakeRpc())
    assert not any(c[:2] == ["docker", "rm"] for c in docker.calls)


def test_container_without_a_service_label_is_refused():
    payload = json.dumps({"Config": {"Labels": {"com.docker.compose.project": "ckbbench"}}})
    docker = FakeDocker(volume=_owned_volume(), fail_on={"container inspect": _ok(payload)})
    with pytest.raises(DevnetLifecycleError, match="compose identity"):
        _prepare(docker, FakeRpc())


@pytest.mark.parametrize(
    ("stderr", "why"),
    [
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock", "daemon down"),
        ('context "prod" not found', "unrelated not-found"),
        ("permission denied while trying to connect", "permission"),
    ],
)
def test_ambiguous_docker_errors_never_read_as_absence(stderr, why):
    """Treating any failure as 'the volume is gone' would let a transient daemon error authorize a
    reset that then deletes a real operator volume."""
    docker = FakeDocker(volume=_owned_volume(), fail_on={"volume inspect": _fail(stderr)})
    with pytest.raises(DevnetLifecycleError, match="could not inspect"):
        _prepare(docker, FakeRpc())
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in docker.calls), why


def test_empty_successful_inspect_is_not_absence():
    docker = FakeDocker(volume=_owned_volume(), fail_on={"volume inspect": _ok("")})
    with pytest.raises(DevnetLifecycleError, match="returned nothing"):
        _prepare(docker, FakeRpc())


def test_scalar_inspect_payload_is_refused():
    docker = FakeDocker(volume=_owned_volume(), fail_on={"volume inspect": _ok('"just-a-string"')})
    with pytest.raises(DevnetLifecycleError, match="unexpected docker inspect payload"):
        _prepare(docker, FakeRpc())


def test_running_compose_agent_service_blocks_the_reset():
    """The compose `ckbbench-agent` service holds the chain open just as an ephemeral agent does."""
    docker = FakeDocker(volume=_owned_volume(), agents="ckbbench-agent\n")
    with pytest.raises(DevnetLifecycleError, match="agents are running"):
        _prepare(docker, FakeRpc())


@pytest.mark.parametrize(
    ("broken_rpc", "match"),
    [
        ("transport", "RuntimeError"),
        ("malformed-tip", "ValueError"),
    ],
)
def test_rpc_faults_become_lifecycle_errors_not_escapes(broken_rpc, match):
    """run_cell only converts DevnetLifecycleError into infra_fail, so an escaping RuntimeError
    would crash the cell instead of persisting an artifact."""
    class Broken(FakeRpc):
        def __call__(self, method, params):
            if method == "get_blockchain_info" and broken_rpc == "transport":
                raise RuntimeError("malformed RPC transport response")
            if method == "get_tip_block_number" and broken_rpc == "malformed-tip":
                return "not-hex"
            return super().__call__(method, params)

    with pytest.raises(DevnetLifecycleError, match=match):
        _prepare(FakeDocker(volume=_owned_volume()), Broken())


def test_docker_spawn_failure_becomes_a_lifecycle_error():
    def exploding(argv):
        raise OSError("docker executable not found")

    with pytest.raises(DevnetLifecycleError, match="OSError"):
        _prepare(exploding, FakeRpc())


def test_non_canonical_endpoint_is_refused_before_any_docker_call():
    docker = FakeDocker(volume=_owned_volume())
    with pytest.raises(DevnetLifecycleError, match="owns http://127.0.0.1:8114 only"):
        _prepare(docker, FakeRpc(), rpc_url="http://192.0.2.10:8114")
    assert docker.calls == []


def _start(docker, rpc=None, **kw):
    from ckbbench.run.devnet import start_devnet

    return start_devnet(run=docker, rpc=rpc or FakeRpc(), sleep=lambda _s: None,
                        monotonic=iter(range(10_000)).__next__, **kw)


@pytest.mark.parametrize("volume", [None, "owned"], ids=["absent", "owned"])
def test_start_devnet_prepares_ownership_but_destroys_nothing(volume):
    """`./bench up` must survive a first run with no volume AND keep an existing owned one."""
    docker = FakeDocker(volume=_owned_volume() if volume else None)
    state = _start(docker)
    assert state.chain == DEVNET_CHAIN_ID
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in docker.calls)
    assert not any(c[:2] == ["docker", "rm"] for c in docker.calls)
    assert any("create" in c for c in docker.calls) and any("start" in c for c in docker.calls)


@pytest.mark.parametrize(
    ("labels", "match"),
    [
        ({"com.example.owner": "someone-else"}, "not benchmark-owned"),
        ({}, "not benchmark-owned"),
    ],
    ids=["foreign", "unlabelled"],
)
def test_start_devnet_refuses_a_foreign_same_named_volume(labels, match):
    """Starting is not harmless: chown and a running CKB node MUTATE the volume, so a same-named
    foreign volume must be refused before any compose work."""
    docker = FakeDocker(volume=_owned_volume(labels=labels))
    with pytest.raises(DevnetLifecycleError, match=match):
        _start(docker)
    for forbidden in ("create", "run", "start"):
        assert not any(forbidden in c for c in docker.calls if "compose" in c), forbidden


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (_fail("Cannot connect to the Docker daemon"), "could not inspect"),
        (_ok(""), "returned nothing"),
        (_ok('["a", "b"]'), "ambiguous docker inspect payload"),
    ],
    ids=["inspect-failure", "empty-success", "ambiguous-payload"],
)
def test_start_devnet_refuses_ambiguous_volume_inspection(response, match):
    docker = FakeDocker(volume=_owned_volume(), fail_on={"volume inspect": response})
    with pytest.raises(DevnetLifecycleError, match=match):
        _start(docker)
    assert not any("compose" in c for c in docker.calls)


def test_rpc_client_construction_failure_is_a_lifecycle_error(monkeypatch):
    """A failing client factory must not escape the boundary: run_cell only converts
    DevnetLifecycleError into the required early infra_fail."""
    import ckbbench.ckb_rpc as rpc_mod

    def boom(*_a, **_kw):
        raise ValueError("client construction failed")

    monkeypatch.setattr(rpc_mod, "make_rpc_client", boom)
    docker = FakeDocker(volume=_owned_volume())
    with pytest.raises(DevnetLifecycleError, match="client construction failed"):
        prepare_devnet(run=docker, sleep=lambda _s: None,
                       monotonic=iter(range(10_000)).__next__)
    assert docker.calls == []


@pytest.mark.parametrize("entry", ["start", "prepare"])
def test_a_foreign_volume_appearing_after_compose_create_is_refused(entry):
    """The preflight inspect and the post-create inspect answer different questions.

    Between them, compose either materialises the volume or adopts a same-named one that appeared
    in the meantime. Chown and start MUTATE it, so ownership has to be re-proven on the object
    compose actually attached -- not only on what the preflight saw.
    """
    docker = FakeDocker(volume=None,
                        volume_after_create=_owned_volume(labels={"com.example.owner": "someone"}))
    with pytest.raises(DevnetLifecycleError, match="not benchmark-owned"):
        if entry == "start":
            _start(docker)
        else:
            prepare_devnet(run=docker, rpc=FakeRpc(), sleep=lambda _s: None,
                           monotonic=iter(range(10_000)).__next__)
    assert any("create" in c for c in docker.calls), "the race needs compose create to have run"
    for forbidden in ("chown", "start"):
        assert not any(forbidden in " ".join(c) for c in docker.calls), forbidden


@pytest.mark.parametrize(
    ("stderr", "absent"),
    [
        (f"Error: No such volume: {DATA_VOLUME}", True),
        (f"Error response from daemon: get {DATA_VOLUME}: no such volume", True),
        (f"Error: No such volume: {DATA_VOLUME}-backup", False),
        (f"Error: No such volume: old-{DATA_VOLUME}", False),
        (f"Error: No such volume: {DATA_VOLUME}.bak", False),
        (f"Error: No such volume: {DATA_VOLUME}_2", False),
        ("Error: No such volume: something-else", False),
        ("Cannot connect to the Docker daemon", False),
    ],
)
def test_absence_requires_the_exact_docker_name_token(stderr, absent):
    """A similar name proves nothing about ours.

    `ckbbench-devnet-data-backup` contains `ckbbench-devnet-data`, so a substring test would record
    the canonical volume as absent from an error about a different object. The gate would then treat
    live operator state as state it created and remove it.
    """
    from ckbbench.run.devnet import _is_absence

    assert _is_absence(stderr, "volume", DATA_VOLUME) is absent


def test_a_similar_name_error_aborts_instead_of_reading_as_absence():
    """End-to-end through the controller: ambiguity must not authorize destructive work."""
    docker = FakeDocker(volume=_owned_volume(), fail_on={
        "volume inspect": _fail(f"Error: No such volume: {DATA_VOLUME}-backup"),
    })
    with pytest.raises(DevnetLifecycleError, match="could not inspect"):
        _start(docker)
    assert not any("compose" in c for c in docker.calls)


def test_validation_mode_refuses_a_generically_labelled_developer_container(monkeypatch):
    """The gate's preflight cannot protect state that appears while its images build.

    A normal `./bench up` stack carries the ordinary compose labels. In validation mode that is not
    enough: without the run label the lifecycle must refuse rather than destroy operator state.
    """
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    payload = {"Config": {"Labels": {
        "com.docker.compose.project": devnet.COMPOSE_PROJECT,
        "com.docker.compose.service": devnet.NODE_SERVICE,
    }}}
    with pytest.raises(devnet.DevnetLifecycleError, match="validation run's identity"):
        devnet._assert_owned_container(devnet.NODE_SERVICE, payload, devnet.expected_validate_run())


def test_validation_mode_accepts_a_container_carrying_the_run_identity(monkeypatch):
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    payload = {"Config": {"Labels": {
        "com.docker.compose.project": devnet.COMPOSE_PROJECT,
        "com.docker.compose.service": devnet.NODE_SERVICE,
        devnet.VALIDATE_RUN_LABEL: "review-run",
    }}}
    devnet._assert_owned_container(devnet.NODE_SERVICE, payload, devnet.expected_validate_run())


def test_ordinary_lifecycle_keeps_the_generic_ownership_contract(monkeypatch):
    """`./bench up/reset/run` must not start requiring a validation label."""
    monkeypatch.delenv("CKBBENCH_VALIDATE_RUN_ID", raising=False)
    assert devnet.expected_validate_run() is None
    payload = {"Config": {"Labels": {
        "com.docker.compose.project": devnet.COMPOSE_PROJECT,
        "com.docker.compose.service": devnet.NODE_SERVICE,
    }}}
    devnet._assert_owned_container(devnet.NODE_SERVICE, payload, devnet.expected_validate_run())


def test_validation_mode_refuses_a_generically_labelled_state_volume(monkeypatch):
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    payload = {"Name": devnet.DATA_VOLUME, "Labels": dict(devnet.OWNER_LABELS)}
    with pytest.raises(devnet.DevnetLifecycleError, match="validation run's identity"):
        devnet.assert_volume_is_ours(payload, devnet.expected_validate_run())


def test_validation_mode_accepts_a_state_volume_carrying_the_run_identity(monkeypatch):
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    payload = {"Name": devnet.DATA_VOLUME,
               "Labels": {**devnet.OWNER_LABELS, devnet.VALIDATE_RUN_LABEL: "review-run"}}
    devnet.assert_volume_is_ours(payload, devnet.expected_validate_run())


def test_ordinary_volume_removal_still_accepts_the_owner_role_pair(monkeypatch):
    monkeypatch.delenv("CKBBENCH_VALIDATE_RUN_ID", raising=False)
    payload = {"Name": devnet.DATA_VOLUME, "Labels": dict(devnet.OWNER_LABELS)}
    devnet.assert_volume_is_ours(payload, devnet.expected_validate_run())


def test_validation_mode_refuses_a_developer_stack_end_to_end(monkeypatch):
    """The review's reproduction: prepare_devnet must not remove a generically labelled stack."""
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    removals: list[list[str]] = []

    def runner(argv, **kwargs):
        removals.append(list(argv))
        if argv[:3] == ["docker", "container", "inspect"]:
            body = json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": devnet.COMPOSE_PROJECT,
                "com.docker.compose.service": argv[3],
            }}}])
            return subprocess.CompletedProcess(argv, 0, body, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(devnet.DevnetLifecycleError, match="validation run's identity"):
        devnet._remove_services(runner, devnet.expected_validate_run())
    assert not [a for a in removals if a[:3] == ["docker", "rm", "-f"]], (
        "a generically labelled developer container was removed in validation mode"
    )


def _called(docker, *tokens: str) -> bool:
    """Whether any recorded argv contains all of these whole tokens."""
    return any(all(t in argv for t in tokens) for argv in docker.calls)


def _validation_volume(run_id: str) -> str:
    return _owned_volume({**OWNER_LABELS, VALIDATE_RUN_LABEL: run_id})


def test_prepare_in_validation_mode_refuses_containers_without_the_run_label(monkeypatch):
    """The create/start/accept path, not just the deletion path.

    Compose creates the fixed names; if what appears carries only the ordinary developer labels,
    validation must refuse it rather than start someone else's container.
    """
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    docker = FakeDocker(volume=None, volume_after_create=_validation_volume("review-run"),
                        containers_before=False)
    with pytest.raises(DevnetLifecycleError, match="validation run's identity"):
        _prepare(docker, FakeRpc())
    assert not _called(docker, "start"), (
        "validation started a container whose identity was never proved"
    )


def test_prepare_in_validation_mode_accepts_correctly_labelled_containers(monkeypatch):
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    docker = FakeDocker(volume=None, volume_after_create=_validation_volume("review-run"),
                        containers_before=False, validate_run="review-run")
    state = _prepare(docker, FakeRpc())
    assert state.chain == "ckb_dev"
    assert _called(docker, "start", f"sha256:id-{NODE_SERVICE}")


def test_prepare_refuses_a_replacement_between_create_and_start(monkeypatch):
    """A foreign container adopting the fixed name after create must not be started."""
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    docker = FakeDocker(volume=None, volume_after_create=_validation_volume("review-run"),
                        containers_before=False, validate_run="review-run",
                        validate_run_after_create="other-run")
    with pytest.raises(DevnetLifecycleError, match="validation run's identity"):
        _prepare(docker, FakeRpc())
    assert not _called(docker, "start"), "a replacement was started"


def test_prepare_in_validation_mode_refuses_a_generically_labelled_volume(monkeypatch):
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    docker = FakeDocker(volume=_owned_volume(), containers_before=False,
                        validate_run="review-run")
    with pytest.raises(DevnetLifecycleError, match="validation run's identity"):
        _prepare(docker, FakeRpc())
    assert not _called(docker, "compose", "create"), (
        "validation proceeded past a foreign state volume"
    )


def test_ordinary_prepare_still_works_without_any_validation_identity(monkeypatch):
    """`./bench up/reset/run` must be unaffected by the validation contract."""
    monkeypatch.delenv("CKBBENCH_VALIDATE_RUN_ID", raising=False)
    docker = FakeDocker(volume=_owned_volume(), containers_before=False)
    state = _prepare(docker, FakeRpc())
    assert state.chain == "ckb_dev"
    assert _called(docker, "start", f"sha256:id-{NODE_SERVICE}")


class _SwapDocker(FakeDocker):
    """Replaces containers/volumes *after* they are proved, to expose check/use races."""

    def __init__(self, *, swap_on: str, **kwargs):
        super().__init__(**kwargs)
        self.swap_on = swap_on
        self.swapped = False
        self.started_foreign = False

    def __call__(self, argv):
        argv = list(argv)
        # The swap happens at the moment of the destructive/starting call, after every proof.
        if not self.swapped:
            if self.swap_on == "start" and argv[:2] == ["docker", "start"]:
                self.swapped = True
                self.validate_run = None          # the name now holds a foreign container
                if not any(a.startswith("sha256:id-") for a in argv[2:]):
                    self.started_foreign = True
            elif self.swap_on == "rm" and argv[:2] == ["docker", "rm"]:
                self.swapped = True
                if not argv[3].startswith("sha256:id-"):
                    self.started_foreign = True
            elif self.swap_on == "volume_rm" and argv[:3] == ["docker", "volume", "rm"]:
                self.swapped = True
        return super().__call__(argv)


def test_start_targets_the_proved_id_so_a_replacement_is_never_started(monkeypatch):
    """A replacement arriving after the proof must not be the container that actually starts."""
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    docker = _SwapDocker(swap_on="start", volume=None,
                         volume_after_create=_validation_volume("review-run"),
                         containers_before=False, validate_run="review-run")
    with pytest.raises(DevnetLifecycleError):
        _prepare(docker, FakeRpc())
    assert not docker.started_foreign, "start resolved a name instead of the proved id"
    starts = [a for a in docker.calls if a[:2] == ["docker", "start"]]
    assert starts, "no start was attempted"
    for argv in starts:
        assert all(a.startswith("sha256:id-") for a in argv[2:]), (
            f"start was issued against a mutable name: {argv}"
        )


def test_service_removal_targets_the_proved_id(monkeypatch):
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    docker = _SwapDocker(swap_on="rm", volume=None,
                         volume_after_create=_validation_volume("review-run"),
                         validate_run="review-run")
    try:
        _prepare(docker, FakeRpc())
    except DevnetLifecycleError:
        pass
    removals = [a for a in docker.calls if a[:2] == ["docker", "rm"]]
    assert removals, "no removal was attempted"
    for argv in removals:
        assert argv[3].startswith("sha256:id-"), f"removal used a mutable name: {argv}"


def test_validation_uses_an_invocation_scoped_volume_name(monkeypatch):
    """A Docker volume has no immutable ID, so a fixed name can always be swapped."""
    monkeypatch.setenv("CKBBENCH_DEVNET_VOLUME", "ckbbench-devnet-data-abc123")
    assert devnet.data_volume_name() == "ckbbench-devnet-data-abc123"
    monkeypatch.delenv("CKBBENCH_DEVNET_VOLUME", raising=False)
    assert devnet.data_volume_name() == devnet.DATA_VOLUME


def test_volume_operations_follow_the_scoped_name(monkeypatch):
    monkeypatch.setenv("CKBBENCH_DEVNET_VOLUME", "ckbbench-devnet-data-abc123")
    monkeypatch.delenv("CKBBENCH_VALIDATE_RUN_ID", raising=False)
    docker = FakeDocker(volume=json.dumps(
        {"Name": "ckbbench-devnet-data-abc123", "Labels": dict(OWNER_LABELS)}))
    assert devnet.remove_data_volume(docker) is True
    assert _called(docker, "volume", "rm", "ckbbench-devnet-data-abc123"), (
        "removal did not follow the invocation-scoped name"
    )


def test_volume_removal_cannot_switch_targets_when_the_selector_drifts(monkeypatch):
    """The review's probe: changing the env mid-call must not move the removal to another volume."""
    monkeypatch.setenv("CKBBENCH_DEVNET_VOLUME", "ckbbench-devnet-data-A")
    monkeypatch.delenv("CKBBENCH_VALIDATE_RUN_ID", raising=False)
    inspected: list[str] = []
    removed: list[str] = []

    def runner(argv, **kwargs):
        argv = list(argv)
        if argv[:3] == ["docker", "volume", "inspect"]:
            inspected.append(argv[3])
            if argv[3] in removed:
                return subprocess.CompletedProcess(
                    argv, 1, "", f"Error: No such volume: {argv[3]}")
            body = json.dumps({"Name": argv[3], "Labels": dict(OWNER_LABELS)})
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:3] == ["docker", "ps", "-a"]:
            # The drift happens here, between the ownership proof and the removal.
            monkeypatch.setenv("CKBBENCH_DEVNET_VOLUME", "ckbbench-devnet-data-B")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["docker", "volume", "rm"]:
            removed.append(argv[3])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    devnet.remove_data_volume(runner, None, devnet.data_volume_name())
    assert removed == ["ckbbench-devnet-data-A"], (
        f"removal followed a drifting selector: inspected={inspected} removed={removed}"
    )
    assert set(inspected) == {"ckbbench-devnet-data-A"}, inspected


def test_prepare_resolves_one_volume_selector_for_the_whole_lifecycle(monkeypatch):
    monkeypatch.setenv("CKBBENCH_DEVNET_VOLUME", "ckbbench-devnet-data-A")
    monkeypatch.delenv("CKBBENCH_VALIDATE_RUN_ID", raising=False)
    docker = FakeDocker(volume=json.dumps(
        {"Name": "ckbbench-devnet-data-A", "Labels": dict(OWNER_LABELS)}))
    _prepare(docker, FakeRpc())
    touched = {a[3] for a in docker.calls if a[:3] == ["docker", "volume", "inspect"]}
    assert touched == {"ckbbench-devnet-data-A"}, f"more than one volume selector was used: {touched}"


def test_prepare_certifies_only_the_generation_it_reset(monkeypatch):
    """One call must not reset generation A and then create, start and certify generation B."""
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run-A")
    monkeypatch.setenv("CKBBENCH_DEVNET_VOLUME", "ckbbench-devnet-data-A")
    seen_volumes: list[str] = []

    class _DriftDocker(FakeDocker):
        def __call__(self, argv):
            argv = list(argv)
            if argv[:3] == ["docker", "volume", "inspect"]:
                seen_volumes.append(argv[3])
            if argv[:3] == ["docker", "volume", "rm"]:
                # The drift lands immediately after the destructive reset.
                monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run-B")
                monkeypatch.setenv("CKBBENCH_DEVNET_VOLUME", "ckbbench-devnet-data-B")
            return super().__call__(argv)

    docker = _DriftDocker(
        volume=None,
        volume_after_create=_owned_volume(
            {**OWNER_LABELS, VALIDATE_RUN_LABEL: "review-run-A"}, name="ckbbench-devnet-data-A"),
        containers_before=False, validate_run="review-run-A",
    )
    _prepare(docker, FakeRpc())
    assert set(seen_volumes) == {"ckbbench-devnet-data-A"}, (
        f"the lifecycle followed a drifting selector: {seen_volumes}"
    )


def test_validation_mode_refuses_name_selected_volume_deletion(monkeypatch):
    """Docker exposes no immutable volume handle, so the scoped artifact is retained instead."""
    monkeypatch.setenv("CKBBENCH_VALIDATE_RUN_ID", "review-run")
    monkeypatch.setenv("CKBBENCH_DEVNET_VOLUME", "ckbbench-devnet-data-review")
    docker = FakeDocker(volume=_owned_volume(
        {**OWNER_LABELS, VALIDATE_RUN_LABEL: "review-run"}, name="ckbbench-devnet-data-review"))
    with pytest.raises(devnet.DevnetVolumeRetained, match="retained"):
        devnet.remove_data_volume(docker, "review-run", "ckbbench-devnet-data-review")
    assert not _called(docker, "volume", "rm"), (
        "a name-selected volume deletion was issued in validation mode"
    )


def test_ordinary_mode_still_removes_the_volume(monkeypatch):
    """`./bench reset` must keep working: the retention rule is validation-only."""
    monkeypatch.delenv("CKBBENCH_VALIDATE_RUN_ID", raising=False)
    monkeypatch.delenv("CKBBENCH_DEVNET_VOLUME", raising=False)
    docker = FakeDocker(volume=_owned_volume())
    assert devnet.remove_data_volume(docker, None, devnet.DATA_VOLUME) is True
    assert _called(docker, "volume", "rm", devnet.DATA_VOLUME)
