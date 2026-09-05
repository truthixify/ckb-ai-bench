from __future__ import annotations

import json
import stat
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from ckbbench.run.campaign_runtime import load_private_signer_pool, validate_private_signer_pool
from ckbbench.run.signer_provisioning import (
    LiveSignerProvisioningBackend,
    MAX_FUNDING_CANDIDATES_PER_POLL,
    MINIMUM_CHANGE_SHANNONS,
    POLL_ROUNDS,
    SignerProvisioningError,
    SignerLeaseTarget,
    _rpc_request_limit,
    build_signer_provisioning_plan,
    provision_signer_pool,
)
from ckbbench.run.test_campaign_runtime import _signed_runtime
from ckbbench.run.testnet_integration import (
    LeasedSignerInput,
    TestnetIntegrationError as IntegrationError,
)
from ckbbench.run.task_attempt import canonical_json_bytes


class FakeBackend:
    def __init__(self, chain: tuple[str, str]) -> None:
        self.chain = chain
        self.rpc_requests = 4
        self.funding_requests = 0
        self.bound: dict[str, tuple[str, dict]] = {}
        self.submissions: list[str] = []
        self.verified: list[tuple[str, int, int]] = []
        self.closed = False
        self._funding_index = 0
        self._transaction_index = 0

    def observe_environment(self):
        return self.chain

    def bind_key(self, private_key):
        if private_key not in self.bound:
            ordinal = len(self.bound) + 1
            self.bound[private_key] = (
                f"ckt1synthetic{ordinal}",
                {
                    "args": "0x" + f"{ordinal:040x}",
                    "code_hash": "0x" + "1" * 64,
                    "hash_type": "type",
                },
            )
        return deepcopy(self.bound[private_key])

    def request_funds(self, _public_address):
        self.funding_requests += 1

    def wait_for_funding(self, _own_lock, required_capacity_shannons):
        self._funding_index += 1
        self.rpc_requests += 1
        return LeasedSignerInput(
            tx_hash="0x" + f"{self._funding_index:064x}",
            index=0,
            capacity_shannons=required_capacity_shannons + MINIMUM_CHANGE_SHANNONS,
        )

    def sign_transaction(self, _donor, transaction):
        self._transaction_index += 1
        signed = deepcopy(transaction)
        signed["witnesses"] = ["0x00"]
        return signed, "0x" + f"{100 + self._transaction_index:064x}"

    def transaction_status(self, _transaction_hash):
        self.rpc_requests += 1
        return "unknown"

    def submit_transaction(self, _transaction, transaction_hash):
        self.rpc_requests += 1
        self.submissions.append(transaction_hash)

    def wait_for_confirmations(self, _transaction_hash, minimum):
        self.rpc_requests += 2
        return minimum

    def verify_output(self, transaction_hash, index, _own_lock, capacity_shannons):
        self.rpc_requests += 1
        self.verified.append((transaction_hash, index, capacity_shannons))

    def close(self):
        self.closed = True


def _fixed_keys():
    ordinal = 0

    def generate():
        nonlocal ordinal
        ordinal += 1
        return "0x" + f"{ordinal:064x}"

    return generate


def test_plan_derives_signed_attempts_and_exact_matched_capacities(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)

    assert plan is not None
    assert len(plan.targets) == 4
    assert len(plan.batches) == 1
    by_ordinal = {}
    for target in plan.targets:
        by_ordinal.setdefault(target.retry_ordinal, []).append(target.required_capacity_shannons)
        assert target.minimum_confirmations == 24
    assert by_ordinal[0][0] == by_ordinal[0][1]
    assert by_ordinal[1][0] == by_ordinal[1][1]


def test_plan_splits_large_campaigns_into_bounded_funding_transactions(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None
    expanded = replace(
        plan,
        targets=tuple(
            SignerLeaseTarget(
                slot_id=f"slot-{index:02d}",
                retry_ordinal=0,
                required_capacity_shannons=10_000_000_000,
                minimum_confirmations=24,
            )
            for index in range(33)
        ),
    )

    assert [len(batch) for batch in expanded.batches] == [16, 16, 1]
    expected_per_batch = (
        POLL_ROUNDS * (1 + 2 * MAX_FUNDING_CANDIDATES_PER_POLL)
        + 3
        + 2 * POLL_ROUNDS
        + 16
    )
    assert _rpc_request_limit(expanded) == 4 + 3 * expected_per_batch


def test_provisioner_creates_private_pool_and_public_receipt_without_keys(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None
    backend = FakeBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    private_root = tmp_path / "private"
    receipt = tmp_path / "public" / "signer-provisioning.json"

    pool_path = provision_signer_pool(
        manifest,
        runtime.release_binding,
        repository_root=Path.cwd(),
        private_root=private_root,
        receipt_path=receipt,
        authorized_by_user=True,
        backend=backend,
        clock=lambda: "2026-09-05T12:00:00Z",
        key_factory=_fixed_keys(),
    )

    assert pool_path == private_root / "signer-pool.json"
    assert stat.S_IMODE(private_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(pool_path.stat().st_mode) == 0o600
    pool = load_private_signer_pool(pool_path, repository_root=Path.cwd())
    validate_private_signer_pool(manifest, runtime.release_binding, pool)
    assert len(pool.entries) == 4
    assert backend.funding_requests == 1
    assert len(backend.submissions) == 1
    assert len(backend.verified) == 4
    public_bytes = receipt.read_bytes()
    assert b"private_key" not in public_bytes
    for entry in pool.entries:
        assert entry.private_key.encode() not in public_bytes
    public = json.loads(public_bytes)
    assert public["private_pool_ready"] is True
    assert public["funding_request_count"] == 1
    assert public["split_fee_shannons"] == 100_000
    assert public["split_transaction_count"] == 1
    assert public["split_transaction_hashes"] == backend.submissions
    assert public["total_leased_capacity_shannons"] == sum(
        leased.capacity_shannons
        for entry in pool.entries
        for leased in entry.leased_inputs
    )


def test_provisioner_requires_authorization_before_private_or_external_activity(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None
    backend = FakeBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    private_root = tmp_path / "private"

    with pytest.raises(SignerProvisioningError, match="authorization"):
        provision_signer_pool(
            manifest,
            runtime.release_binding,
            repository_root=Path.cwd(),
            private_root=private_root,
            receipt_path=tmp_path / "receipt.json",
            authorized_by_user=False,
            backend=backend,
        )

    assert not private_root.exists()
    assert backend.rpc_requests == 4
    assert backend.bound == {}


def test_provisioner_refuses_the_wrong_network_before_generating_keys_or_funding(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None
    backend = FakeBackend(("wrong-chain", "0x" + "9" * 64))

    with pytest.raises(SignerProvisioningError, match="frozen TestNet"):
        provision_signer_pool(
            manifest,
            runtime.release_binding,
            repository_root=Path.cwd(),
            private_root=tmp_path / "private",
            receipt_path=tmp_path / "receipt.json",
            authorized_by_user=True,
            backend=backend,
        )

    assert backend.bound == {}
    assert backend.funding_requests == 0


def test_split_preserves_capacity_and_uses_the_released_dependency(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None

    class CapturingBackend(FakeBackend):
        transaction = None

        def sign_transaction(self, donor, transaction):
            self.transaction = deepcopy(transaction)
            return super().sign_transaction(donor, transaction)

    backend = CapturingBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    provision_signer_pool(
        manifest,
        runtime.release_binding,
        repository_root=Path.cwd(),
        private_root=tmp_path / "private",
        receipt_path=tmp_path / "receipt.json",
        authorized_by_user=True,
        backend=backend,
        key_factory=_fixed_keys(),
    )
    transaction = backend.transaction
    assert transaction is not None
    targets = [int(output["capacity"], 16) for output in transaction["outputs"][:-1]]
    change = int(transaction["outputs"][-1]["capacity"], 16)
    assert targets == [target.required_capacity_shannons for target in plan.batches[0]]
    assert change >= MINIMUM_CHANGE_SHANNONS
    assert transaction["cell_deps"] == [{
        "dep_type": "dep_group",
        "out_point": {
            "index": hex(plan.secp_dep_index),
            "tx_hash": plan.secp_dep_tx_hash,
        },
    }]


def test_existing_pool_is_revalidated_without_requesting_more_funds(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None
    private_root = tmp_path / "private"
    first = FakeBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    path = provision_signer_pool(
        manifest,
        runtime.release_binding,
        repository_root=Path.cwd(),
        private_root=private_root,
        receipt_path=tmp_path / "receipt.json",
        authorized_by_user=True,
        backend=first,
        key_factory=_fixed_keys(),
    )
    (tmp_path / "receipt.json").unlink()
    second = FakeBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    assert provision_signer_pool(
        manifest,
        runtime.release_binding,
        repository_root=Path.cwd(),
        private_root=private_root,
        receipt_path=tmp_path / "receipt.json",
        authorized_by_user=True,
        backend=second,
    ) == path
    assert second.funding_requests == 0
    assert second.submissions == []
    assert (tmp_path / "receipt.json").is_file()


def test_unresolved_submission_resumes_the_same_signed_transaction(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None
    private_root = tmp_path / "private"

    class InterruptedBackend(FakeBackend):
        def submit_transaction(self, _transaction, _transaction_hash):
            self.rpc_requests += 1
            raise IntegrationError("synthetic transport loss")

    first = InterruptedBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    with pytest.raises(SignerProvisioningError, match="unresolved"):
        provision_signer_pool(
            manifest,
            runtime.release_binding,
            repository_root=Path.cwd(),
            private_root=private_root,
            receipt_path=tmp_path / "receipt.json",
            authorized_by_user=True,
            backend=first,
            key_factory=_fixed_keys(),
        )
    state = json.loads((private_root / "signer-provisioning.json").read_bytes())
    transaction_hash = state["provisioning_batches"][0]["transaction_hash"]
    assert state["provisioning_batches"][0]["split_status"] == "in_flight"

    class ReconciledBackend(FakeBackend):
        def transaction_status(self, observed_hash):
            self.rpc_requests += 1
            assert observed_hash == transaction_hash
            return "committed"

    second = ReconciledBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    pool_path = provision_signer_pool(
        manifest,
        runtime.release_binding,
        repository_root=Path.cwd(),
        private_root=private_root,
        receipt_path=tmp_path / "receipt.json",
        authorized_by_user=True,
        backend=second,
    )
    assert pool_path == private_root / "signer-pool.json"
    assert second.funding_requests == 0
    assert second.submissions == []


def test_interrupted_funding_request_is_not_repeated_on_resume(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None
    private_root = tmp_path / "private"

    class InterruptedBackend(FakeBackend):
        def request_funds(self, _public_address):
            self.funding_requests += 1
            raise OSError("synthetic transport loss")

    first = InterruptedBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    with pytest.raises(SignerProvisioningError, match="OSError"):
        provision_signer_pool(
            manifest,
            runtime.release_binding,
            repository_root=Path.cwd(),
            private_root=private_root,
            receipt_path=tmp_path / "receipt.json",
            authorized_by_user=True,
            backend=first,
            key_factory=_fixed_keys(),
        )
    state = json.loads((private_root / "signer-provisioning.json").read_bytes())
    assert state["provisioning_batches"][0]["funding_status"] == "in_flight"
    assert first.funding_requests == 1

    second = FakeBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    assert provision_signer_pool(
        manifest,
        runtime.release_binding,
        repository_root=Path.cwd(),
        private_root=private_root,
        receipt_path=tmp_path / "receipt.json",
        authorized_by_user=True,
        backend=second,
    ) == private_root / "signer-pool.json"
    assert second.funding_requests == 0


def test_resumption_refuses_a_changed_saved_split_before_chain_activity(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None
    private_root = tmp_path / "private"

    class InterruptedBackend(FakeBackend):
        def submit_transaction(self, _transaction, _transaction_hash):
            raise IntegrationError("synthetic transport loss")

    first = InterruptedBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    with pytest.raises(SignerProvisioningError, match="unresolved"):
        provision_signer_pool(
            manifest,
            runtime.release_binding,
            repository_root=Path.cwd(),
            private_root=private_root,
            receipt_path=tmp_path / "receipt.json",
            authorized_by_user=True,
            backend=first,
            key_factory=_fixed_keys(),
        )
    state_path = private_root / "signer-provisioning.json"
    state = json.loads(state_path.read_bytes())
    state["provisioning_batches"][0]["transaction"]["outputs"][0]["capacity"] = "0x1"
    state_path.write_bytes(canonical_json_bytes(state))
    second = FakeBackend((plan.chain.chain_id, plan.chain.genesis_hash))

    with pytest.raises(SignerProvisioningError, match="differs from its signer plan"):
        provision_signer_pool(
            manifest,
            runtime.release_binding,
            repository_root=Path.cwd(),
            private_root=private_root,
            receipt_path=tmp_path / "receipt.json",
            authorized_by_user=True,
            backend=second,
        )
    assert second.submissions == []


def test_malformed_submission_result_is_not_reconciled_as_transport_loss(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    plan = build_signer_provisioning_plan(manifest, runtime.release_binding)
    assert plan is not None

    class MalformedBackend(FakeBackend):
        def submit_transaction(self, _transaction, _transaction_hash):
            raise SignerProvisioningError(
                "funding submission returned a different transaction hash"
            )

        def transaction_status(self, _transaction_hash):
            raise AssertionError("a definite protocol violation must not be reconciled")

    backend = MalformedBackend((plan.chain.chain_id, plan.chain.genesis_hash))
    with pytest.raises(SignerProvisioningError, match="different transaction hash"):
        provision_signer_pool(
            manifest,
            runtime.release_binding,
            repository_root=Path.cwd(),
            private_root=tmp_path / "private",
            receipt_path=tmp_path / "receipt.json",
            authorized_by_user=True,
            backend=backend,
            key_factory=_fixed_keys(),
        )


def test_live_backend_rechecks_indexer_candidates_through_node_rpc():
    own_lock = {
        "args": "0x" + "1" * 40,
        "code_hash": "0x" + "2" * 64,
        "hash_type": "type",
    }
    candidate = LeasedSignerInput(
        tx_hash="0x" + "3" * 64,
        index=1,
        capacity_shannons=10_000_000_000,
    )

    class Rpc:
        request_count = 0

        def call(self, method, params):
            self.request_count += 1
            if method == "get_live_cell":
                assert params[0] == {"index": "0x1", "tx_hash": candidate.tx_hash}
                return {
                    "status": "live",
                    "cell": {
                        "output": {
                            "capacity": hex(candidate.capacity_shannons),
                            "lock": own_lock,
                            "type": None,
                        },
                        "data": {"content": "0x"},
                    },
                }
            assert method == "get_transaction"
            return {"tx_status": {"status": "committed"}}

    backend = object.__new__(LiveSignerProvisioningBackend)
    backend.rpc = Rpc()

    assert backend._verify_funding_input(candidate, own_lock) is True
    assert backend.rpc.request_count == 2


def test_live_backend_bounds_matching_indexer_candidates_before_node_queries():
    own_lock = {
        "args": "0x" + "1" * 40,
        "code_hash": "0x" + "2" * 64,
        "hash_type": "type",
    }

    class Rpc:
        request_count = 0

        def call(self, method, _params):
            self.request_count += 1
            assert method == "get_cells"
            return {
                "objects": [
                    {
                        "out_point": {
                            "index": hex(index),
                            "tx_hash": "0x" + f"{index + 1:064x}",
                        },
                        "output": {
                            "capacity": hex(20_000_000_000),
                            "lock": own_lock,
                            "type": None,
                        },
                        "output_data": "0x",
                    }
                    for index in range(MAX_FUNDING_CANDIDATES_PER_POLL + 1)
                ]
            }

    backend = object.__new__(LiveSignerProvisioningBackend)
    backend.rpc = Rpc()
    backend.sleep = lambda _seconds: None

    with pytest.raises(SignerProvisioningError, match="too many matching"):
        backend.wait_for_funding(own_lock, 10_000_000_000)
    assert backend.rpc.request_count == 1
