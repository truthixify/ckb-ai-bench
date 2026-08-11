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

from ckbbench.run.devnet import (
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


def _owned_volume(labels: dict | None = None) -> str:
    return json.dumps({"Name": DATA_VOLUME, "Labels": labels if labels is not None else OWNER_LABELS})


def _owned_container(name: str, running: bool = True) -> str:
    """Both compose identity labels, as a real container carries them: a fixture with only the
    project label would encode the fail-open bug as the happy path."""
    return json.dumps({
        "Config": {"Labels": {
            "com.docker.compose.project": "ckbbench",
            "com.docker.compose.service": name,
        }},
        "State": {"Running": running},
    })


class FakeDocker:
    """Records every docker argv and answers from a scripted world."""

    def __init__(self, *, volume: str | None = None, containers_before: bool = True,
                 agents: str = "", volume_users: str = "", fail_on: dict | None = None,
                 volume_after_create: str | None = None):
        self.calls: list[list[str]] = []
        self.volume = volume
        self.removed_volume = False
        self.present = {MINER_SERVICE: containers_before, NODE_SERVICE: containers_before}
        self.agents = agents
        self.volume_users = volume_users
        self.fail_on = fail_on or {}
        self.volume_after_create = volume_after_create
        self.started = False

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
            if self.started or self.present.get(name):
                return _ok(_owned_container(name))
            return _fail(f"Error: No such container: {name}")
        if argv[:2] == ["docker", "rm"]:
            self.present[argv[3]] = False
            return _ok()
        if argv[:3] == ["docker", "volume", "inspect"]:
            if self.volume is None or self.removed_volume:
                return _fail("Error: No such volume: " + DATA_VOLUME)
            return _ok(self.volume)
        if argv[:3] == ["docker", "volume", "create"]:  # pragma: no cover - not used today
            return _ok()
        if argv[:3] == ["docker", "volume", "rm"]:
            self.removed_volume = True
            return _ok()
        if argv[:3] == ["docker", "compose", "-f"]:
            if "create" in argv:
                # compose materialises the labelled volume, as it does against real docker
                self.removed_volume = False
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
    remove_miner = docker.index_of("rm", "-f", MINER_SERVICE)
    remove_node = docker.index_of("rm", "-f", NODE_SERVICE)
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
    chown = docker.index_of("compose", "run")
    start = docker.index_of("compose", "start")
    assert create < chown < start
    assert any("chown ckb:ckb /var/lib/ckb/data" in a for a in docker.calls[chown])


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
