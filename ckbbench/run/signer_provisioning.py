"""Prepare campaign-bound TestNet signer leases from frozen task contracts."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from ckbbench.config import MCP_URL, TESTNET_RPC
from ckbbench.run.campaign import CampaignManifest, publish_document
from ckbbench.run.campaign_runtime import (
    SIGNER_POOL_SCHEMA_VERSION,
    DockerTransactionKeyHolder,
    PrivateSignerEntry,
    PrivateSignerPool,
    _verify_image,
    load_private_signer_pool,
    validate_private_signer_pool,
)
from ckbbench.run.chain_profile import ChainProfile
from ckbbench.run.suite_release import CampaignReleaseBinding
from ckbbench.run.task_attempt import canonical_json_bytes
from ckbbench.run.testnet_integration import (
    CkbAiPreflightAdapter,
    DirectChainProbe,
    HttpJsonRpcClient,
    LeasedSignerInput,
    TestnetIntegrationError,
)
from ckbbench.run.treatment_surface import TreatmentSurfaceProfile


PROVISIONING_SCHEMA_VERSION = "ckbbench-signer-provisioning-v2"
PROVISIONING_RECEIPT_SCHEMA_VERSION = "ckbbench-signer-provisioning-receipt-v2"
SPLIT_FEE_SHANNONS = 100_000
MINIMUM_CHANGE_SHANNONS = 6_100_000_000
MAX_SPLIT_TARGETS = 16
MAX_FUNDING_CANDIDATES_PER_POLL = 4
POLL_ROUNDS = 80
POLL_INTERVAL_SECONDS = 15
_PRIVATE_KEY = re.compile(r"0x[0-9a-f]{64}\Z")
_HASH32 = re.compile(r"0x[0-9a-f]{64}\Z")
_HEX_BYTES = re.compile(r"0x(?:[0-9a-f]{2})*\Z")


class SignerProvisioningError(RuntimeError):
    """Signer resources cannot be prepared without violating their frozen contract."""


@dataclass(frozen=True)
class SignerLeaseTarget:
    slot_id: str
    retry_ordinal: int
    required_capacity_shannons: int
    minimum_confirmations: int

    @property
    def key(self) -> tuple[str, int]:
        return self.slot_id, self.retry_ordinal


@dataclass(frozen=True)
class SignerProvisioningPlan:
    chain: ChainProfile
    treatment_surface: TreatmentSurfaceProfile
    agent_image: str
    secp_dep_tx_hash: str
    secp_dep_index: int
    targets: tuple[SignerLeaseTarget, ...]

    @property
    def batches(self) -> tuple[tuple[SignerLeaseTarget, ...], ...]:
        return tuple(
            self.targets[index:index + MAX_SPLIT_TARGETS]
            for index in range(0, len(self.targets), MAX_SPLIT_TARGETS)
        )


class SignerProvisioningBackend(Protocol):
    @property
    def rpc_requests(self) -> int: ...

    @property
    def funding_requests(self) -> int: ...

    def observe_environment(self) -> tuple[str, str]: ...

    def bind_key(self, private_key: str) -> tuple[str, dict[str, Any]]: ...

    def request_funds(self, public_address: str) -> None: ...

    def wait_for_funding(
        self,
        own_lock: dict[str, Any],
        required_capacity_shannons: int,
    ) -> LeasedSignerInput: ...

    def sign_transaction(
        self,
        donor: PrivateSignerEntry,
        transaction: dict[str, Any],
    ) -> tuple[dict[str, Any], str]: ...

    def transaction_status(self, transaction_hash: str) -> str: ...

    def submit_transaction(self, transaction: dict[str, Any], transaction_hash: str) -> None: ...

    def wait_for_confirmations(self, transaction_hash: str, minimum: int) -> int: ...

    def verify_output(
        self,
        transaction_hash: str,
        index: int,
        own_lock: dict[str, Any],
        capacity_shannons: int,
    ) -> None: ...

    def close(self) -> None: ...


def _rpc_request_limit(plan: SignerProvisioningPlan) -> int:
    environment_requests = 4
    funding_poll_requests = POLL_ROUNDS * (
        1 + 2 * MAX_FUNDING_CANDIDATES_PER_POLL
    )
    transaction_requests = 3
    confirmation_requests = 2 * POLL_ROUNDS
    output_requests = MAX_SPLIT_TARGETS
    return environment_requests + len(plan.batches) * (
        funding_poll_requests
        + transaction_requests
        + confirmation_requests
        + output_requests
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SignerProvisioningError(f"{label} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SignerProvisioningError(f"{label} is not a UTC timestamp") from None
    if parsed.tzinfo != timezone.utc or parsed.microsecond != 0:
        raise SignerProvisioningError(f"{label} is not a UTC timestamp")
    return parsed


def _private_key() -> str:
    while True:
        value = secrets.token_bytes(32)
        if any(value):
            return "0x" + value.hex()


def _hex_int(value: Any, label: str) -> int:
    if not isinstance(value, str):
        raise SignerProvisioningError(f"{label} is not canonical hexadecimal")
    try:
        parsed = int(value, 16)
    except ValueError:
        raise SignerProvisioningError(f"{label} is not canonical hexadecimal") from None
    if parsed < 0 or value != hex(parsed):
        raise SignerProvisioningError(f"{label} is not canonical hexadecimal")
    return parsed


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SignerProvisioningError(f"{label} does not contain the expected fields")
    return value


def build_signer_provisioning_plan(
    manifest: CampaignManifest,
    release_binding: CampaignReleaseBinding,
) -> SignerProvisioningPlan | None:
    release_binding.validate_manifest(manifest)
    signed = tuple(
        slot
        for slot in manifest.ordered_slots
        if release_binding.execution_contract_for(slot).signer_required
    )
    if not signed:
        return None
    chain_keys = {(slot.chain_profile_id, slot.chain_profile_sha256) for slot in signed}
    if len(chain_keys) != 1 or any(slot.chain_track != "testnet" for slot in signed):
        raise SignerProvisioningError("signed campaign slots need one TestNet chain profile")
    chain_key = next(iter(chain_keys))
    chains = tuple(
        profile
        for profile in release_binding.chain_profiles
        if (profile.profile_id, profile.sha256) == chain_key
    )
    if len(chains) != 1 or chains[0].chain_id is None or chains[0].genesis_hash is None:
        raise SignerProvisioningError("signed campaign chain identity is incomplete")
    surfaces = tuple(
        profile
        for profile in release_binding.treatment_profiles
        if profile.claims_live_chain and any(
            slot.arm == "C"
            and (slot.treatment_profile_id, slot.treatment_profile_sha256)
            == (profile.profile_id, profile.sha256)
            for slot in signed
        )
    )
    if len(surfaces) != 1:
        raise SignerProvisioningError("signed campaign treatment surface is ambiguous")

    dependencies: set[tuple[str, int]] = set()
    targets = []
    for slot in signed:
        contract = release_binding.execution_contract_for(slot)
        if contract.funding is None or contract.funding.minimum_cell_count != 1:
            raise SignerProvisioningError("signed campaign funding needs one dedicated input cell")
        matches = tuple(
            dependency
            for dependency in contract.required_dependencies
            if dependency.dependency_id == "secp256k1-blake160-dep-group"
        )
        if len(matches) != 1:
            raise SignerProvisioningError("signed campaign lacks one released secp dep group")
        dependencies.add((matches[0].transaction_hash, matches[0].output_index))
        for retry_ordinal in range(manifest.retry_limit + 1):
            targets.append(SignerLeaseTarget(
                slot_id=slot.slot_id,
                retry_ordinal=retry_ordinal,
                required_capacity_shannons=contract.funding.required_capacity_shannons,
                minimum_confirmations=contract.funding.minimum_confirmations,
            ))
    if len(dependencies) != 1:
        raise SignerProvisioningError("signed campaign uses inconsistent secp dep groups")
    targets.sort(key=lambda target: target.key)

    by_pair: dict[tuple[str, str, str], dict[str, Any]] = {}
    for slot in signed:
        by_pair.setdefault(
            (slot.trial_id, slot.task_id, slot.model_variant_id), {}
        )[slot.arm] = slot
    if any(set(pair) != {"B", "C"} for pair in by_pair.values()):
        raise SignerProvisioningError("signed campaign slots do not form matched B/C pairs")
    target_by_key = {target.key: target for target in targets}
    for pair in by_pair.values():
        for retry_ordinal in range(manifest.retry_limit + 1):
            left = target_by_key[(pair["B"].slot_id, retry_ordinal)]
            right = target_by_key[(pair["C"].slot_id, retry_ordinal)]
            if (
                left.required_capacity_shannons,
                left.minimum_confirmations,
            ) != (
                right.required_capacity_shannons,
                right.minimum_confirmations,
            ):
                raise SignerProvisioningError("matched B/C signer requirements differ")
    dependency = next(iter(dependencies))
    return SignerProvisioningPlan(
        chain=chains[0],
        treatment_surface=surfaces[0],
        agent_image=manifest.execution_source.agent_image_digest,
        secp_dep_tx_hash=dependency[0],
        secp_dep_index=dependency[1],
        targets=tuple(targets),
    )


def _placeholder_entry(
    *,
    slot_id: str,
    retry_ordinal: int,
    private_key: str,
    public_address: str,
    own_lock: dict[str, Any],
    leased_input: LeasedSignerInput,
) -> PrivateSignerEntry:
    return PrivateSignerEntry(
        slot_id=slot_id,
        retry_ordinal=retry_ordinal,
        signer_handle=f"signer-{slot_id}-r{retry_ordinal}",
        public_address=public_address,
        private_key=private_key,
        own_lock=own_lock,
        lease_resource_id=f"lease-{slot_id}-r{retry_ordinal}",
        leased_inputs=(leased_input,),
    )


def _secure_private_root(path: Path | str, repository_root: Path | str) -> Path:
    root = Path(path)
    if not root.is_absolute() or root.is_symlink():
        raise SignerProvisioningError("private campaign root must be an absolute non-symlink path")
    repository = Path(repository_root).resolve(strict=True)
    resolved = root.resolve(strict=False)
    if resolved == repository or resolved.is_relative_to(repository):
        raise SignerProvisioningError("private campaign root must stay outside the repository")
    try:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise SignerProvisioningError("private campaign root cannot be prepared") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SignerProvisioningError("private campaign root must be owner-owned mode 0700")
    return resolved


def _read_private(path: Path) -> dict[str, Any]:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 0 < metadata.st_size <= (1 << 20)
        ):
            raise SignerProvisioningError("private provisioning state is not owner-private data")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read((1 << 20) + 1)
            after = os.fstat(handle.fileno())
        stable_before = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        stable_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_size,
            after.st_mtime_ns,
        )
        if len(payload) != metadata.st_size or stable_after != stable_before:
            raise SignerProvisioningError("private provisioning state changed while reading")
        document = json.loads(payload)
    except SignerProvisioningError:
        raise
    except Exception as exc:
        raise SignerProvisioningError(
            f"private provisioning state could not be read ({type(exc).__name__})"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(document, dict) or canonical_json_bytes(document) != payload:
        raise SignerProvisioningError("private provisioning state is not canonical JSON")
    return document


def _write_private(path: Path, document: dict[str, Any], *, create_only: bool) -> None:
    payload = canonical_json_bytes(document)
    if len(payload) > (1 << 20):
        raise SignerProvisioningError("private provisioning state exceeds its byte limit")
    if create_only and path.exists():
        raise SignerProvisioningError("private provisioning destination already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise SignerProvisioningError(
                    "private provisioning destination already exists"
                ) from None
            temporary.unlink()
        else:
            os.replace(temporary, path)
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except SignerProvisioningError:
        raise
    except OSError as exc:
        raise SignerProvisioningError("private provisioning state could not be published") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise SignerProvisioningError("private provisioning state has unsafe permissions")


@contextmanager
def _provisioning_lock(root: Path) -> Iterator[None]:
    descriptor = os.open(
        root / ".provisioning.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise SignerProvisioningError("signer provisioning lock is not owner-private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SignerProvisioningError(
                "another signer provisioning command is running"
            ) from None
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _entry_document(
    target: SignerLeaseTarget,
    private_key: str,
    public_address: str,
    own_lock: dict[str, Any],
) -> dict[str, Any]:
    return {
        "lease_resource_id": f"lease-{target.slot_id}-r{target.retry_ordinal}",
        "minimum_confirmations": target.minimum_confirmations,
        "own_lock": deepcopy(own_lock),
        "private_key": private_key,
        "public_address": public_address,
        "required_capacity_shannons": target.required_capacity_shannons,
        "retry_ordinal": target.retry_ordinal,
        "signer_handle": f"signer-{target.slot_id}-r{target.retry_ordinal}",
        "slot_id": target.slot_id,
    }


def _new_state(
    manifest: CampaignManifest,
    plan: SignerProvisioningPlan,
    backend: SignerProvisioningBackend,
    clock: Callable[[], str],
    key_factory: Callable[[], str],
) -> dict[str, Any]:
    entries = []
    for target in plan.targets:
        key = key_factory()
        address, own_lock = backend.bind_key(key)
        entries.append(_entry_document(target, key, address, own_lock))
    batches = []
    for index, targets in enumerate(plan.batches):
        key = key_factory()
        address, own_lock = backend.bind_key(key)
        batches.append({
            "batch_index": index,
            "donor_input": None,
            "funding_status": "not_requested",
            "own_lock": deepcopy(own_lock),
            "private_key": key,
            "public_address": address,
            "split_status": "not_prepared",
            "target_keys": [[target.slot_id, target.retry_ordinal] for target in targets],
            "transaction": None,
            "transaction_hash": None,
        })
    return {
        "campaign_id": manifest.campaign_id,
        "chain_profile_id": plan.chain.profile_id,
        "chain_profile_sha256": plan.chain.sha256,
        "completed_utc": None,
        "created_utc": clock(),
        "entries": entries,
        "manifest_sha256": manifest.sha256,
        "provisioning_batches": batches,
        "rpc_requests_at_completion": None,
        "schema_version": PROVISIONING_SCHEMA_VERSION,
    }


def _validate_state(
    document: dict[str, Any],
    manifest: CampaignManifest,
    plan: SignerProvisioningPlan,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    state = _exact(document, {
        "campaign_id", "chain_profile_id", "chain_profile_sha256", "completed_utc",
        "created_utc", "entries", "manifest_sha256", "provisioning_batches",
        "rpc_requests_at_completion", "schema_version",
    }, "private provisioning state")
    if (
        state["schema_version"] != PROVISIONING_SCHEMA_VERSION
        or state["campaign_id"] != manifest.campaign_id
        or state["manifest_sha256"] != manifest.sha256
        or (state["chain_profile_id"], state["chain_profile_sha256"])
        != (plan.chain.profile_id, plan.chain.sha256)
        or not isinstance(state["entries"], list)
        or not isinstance(state["provisioning_batches"], list)
    ):
        raise SignerProvisioningError("private provisioning state differs from the campaign")
    created = _utc(state["created_utc"], "private provisioning creation time")
    completed = state["completed_utc"]
    rpc_at_completion = state["rpc_requests_at_completion"]
    if (completed is None) != (rpc_at_completion is None):
        raise SignerProvisioningError("private provisioning completion fields disagree")
    if completed is not None:
        completed_at = _utc(completed, "private provisioning completion time")
        if completed_at < created:
            raise SignerProvisioningError(
                "private provisioning completion precedes its creation"
            )
        if type(rpc_at_completion) is not int or rpc_at_completion < 0:
            raise SignerProvisioningError("private provisioning completion fields are malformed")
    targets = {target.key: target for target in plan.targets}
    entries = state["entries"]
    expected_entry_fields = {
        "lease_resource_id", "minimum_confirmations", "own_lock", "private_key",
        "public_address", "required_capacity_shannons", "retry_ordinal", "signer_handle",
        "slot_id",
    }
    observed: set[tuple[str, int]] = set()
    for entry in entries:
        row = _exact(entry, expected_entry_fields, "private signer entry")
        key = row["slot_id"], row["retry_ordinal"]
        target = targets.get(key)
        if (
            target is None
            or key in observed
            or row["required_capacity_shannons"] != target.required_capacity_shannons
            or row["minimum_confirmations"] != target.minimum_confirmations
            or row["signer_handle"] != f"signer-{target.slot_id}-r{target.retry_ordinal}"
            or row["lease_resource_id"] != f"lease-{target.slot_id}-r{target.retry_ordinal}"
            or not isinstance(row["private_key"], str)
            or _PRIVATE_KEY.fullmatch(row["private_key"]) is None
        ):
            raise SignerProvisioningError("private signer entry differs from its frozen target")
        _placeholder_entry(
            slot_id=target.slot_id,
            retry_ordinal=target.retry_ordinal,
            private_key=row["private_key"],
            public_address=row["public_address"],
            own_lock=row["own_lock"],
            leased_input=LeasedSignerInput(
                tx_hash="0x" + "01" * 32,
                index=0,
                capacity_shannons=target.required_capacity_shannons,
            ),
        )
        observed.add(key)
    if observed != set(targets):
        raise SignerProvisioningError("private signer entries do not cover the campaign")

    batches = state["provisioning_batches"]
    if len(batches) != len(plan.batches):
        raise SignerProvisioningError("private funding batches do not cover the campaign")
    expected_batch_fields = {
        "batch_index", "donor_input", "funding_status", "own_lock", "private_key",
        "public_address", "split_status", "target_keys", "transaction", "transaction_hash",
    }
    entry_by_key = {
        (row["slot_id"], row["retry_ordinal"]): row for row in entries
    }
    for index, (batch, planned) in enumerate(zip(batches, plan.batches, strict=True)):
        row = _exact(batch, expected_batch_fields, "private funding batch")
        expected_keys = [[target.slot_id, target.retry_ordinal] for target in planned]
        if (
            row["batch_index"] != index
            or row["target_keys"] != expected_keys
            or row["funding_status"] not in {"not_requested", "in_flight", "funded"}
            or row["split_status"]
            not in {"not_prepared", "prepared", "in_flight", "submitted", "confirmed"}
            or not isinstance(row["private_key"], str)
            or _PRIVATE_KEY.fullmatch(row["private_key"]) is None
        ):
            raise SignerProvisioningError("private funding batch is malformed")
        donor_input = None
        if row["donor_input"] is not None:
            donor = _exact(
                row["donor_input"],
                {"capacity_shannons", "index", "tx_hash"},
                "private funding input",
            )
            donor_input = LeasedSignerInput(**donor)
            if row["funding_status"] != "funded":
                raise SignerProvisioningError("private funding status contradicts its input")
        elif row["funding_status"] == "funded":
            raise SignerProvisioningError("private funding batch lacks its donor input")
        transaction_present = isinstance(row["transaction"], dict)
        hash_present = isinstance(row["transaction_hash"], str) and _HASH32.fullmatch(
            row["transaction_hash"]
        ) is not None
        if row["split_status"] == "not_prepared":
            if transaction_present or row["transaction_hash"] is not None:
                raise SignerProvisioningError("unprepared funding batch carries a transaction")
        elif not transaction_present or not hash_present or row["donor_input"] is None:
            raise SignerProvisioningError("prepared funding batch lacks transaction evidence")
        _placeholder_entry(
            slot_id=f"provision-{manifest.campaign_id[9:17]}-{index:03d}",
            retry_ordinal=0,
            private_key=row["private_key"],
            public_address=row["public_address"],
            own_lock=row["own_lock"],
            leased_input=LeasedSignerInput(
                tx_hash="0x" + "02" * 32,
                index=0,
                capacity_shannons=MINIMUM_CHANGE_SHANNONS,
            ),
        )
        if row["split_status"] != "not_prepared":
            split_rows = [
                entry_by_key[(target.slot_id, target.retry_ordinal)]
                for target in planned
            ]
            expected = _build_split_transaction(plan, split_rows, row, donor_input)
            transaction = row["transaction"]
            if set(transaction) != set(expected):
                raise SignerProvisioningError("saved funding transaction is malformed")
            witnesses = transaction.get("witnesses")
            if (
                not isinstance(witnesses, list)
                or len(witnesses) != len(expected["witnesses"])
                or any(
                    not isinstance(witness, str)
                    or _HEX_BYTES.fullmatch(witness) is None
                    for witness in witnesses
                )
            ):
                raise SignerProvisioningError("saved funding witnesses are malformed")
            if any(
                transaction[field] != expected[field]
                for field in expected
                if field != "witnesses"
            ):
                raise SignerProvisioningError(
                    "saved funding transaction differs from its signer plan"
                )
    identities = [row["private_key"] for row in entries + batches]
    identities += [row["public_address"] for row in entries + batches]
    if len(identities) != len(set(identities)):
        raise SignerProvisioningError("private provisioning state reuses a key or address")
    if completed is not None and any(row["split_status"] != "confirmed" for row in batches):
        raise SignerProvisioningError("completed provisioning state has an unfinished split")
    return entries, batches


def _build_split_transaction(
    plan: SignerProvisioningPlan,
    rows: list[dict[str, Any]],
    donor: dict[str, Any],
    donor_input: LeasedSignerInput,
) -> dict[str, Any]:
    target_capacity = sum(row["required_capacity_shannons"] for row in rows)
    change = donor_input.capacity_shannons - target_capacity - SPLIT_FEE_SHANNONS
    if change < MINIMUM_CHANGE_SHANNONS:
        raise SignerProvisioningError("faucet input cannot fund the exact signer split")
    outputs = [{
        "capacity": hex(row["required_capacity_shannons"]),
        "lock": deepcopy(row["own_lock"]),
        "type": None,
    } for row in rows]
    outputs.append({
        "capacity": hex(change),
        "lock": deepcopy(donor["own_lock"]),
        "type": None,
    })
    return {
        "cell_deps": [{
            "dep_type": "dep_group",
            "out_point": {
                "index": hex(plan.secp_dep_index),
                "tx_hash": plan.secp_dep_tx_hash,
            },
        }],
        "header_deps": [],
        "inputs": [{
            "previous_output": {
                "index": hex(donor_input.index),
                "tx_hash": donor_input.tx_hash,
            },
            "since": "0x0",
        }],
        "outputs": outputs,
        "outputs_data": ["0x"] * len(outputs),
        "version": "0x0",
        "witnesses": ["0x"],
    }


def _pool_document(
    plan: SignerProvisioningPlan,
    entries: list[dict[str, Any]],
    batches: list[dict[str, Any]],
) -> dict[str, Any]:
    points: dict[tuple[str, int], LeasedSignerInput] = {}
    for batch in batches:
        if batch["split_status"] != "confirmed":
            raise SignerProvisioningError("signer pool cannot be published before funding confirms")
        for index, raw_key in enumerate(batch["target_keys"]):
            key = raw_key[0], raw_key[1]
            entry = next(
                row for row in entries if (row["slot_id"], row["retry_ordinal"]) == key
            )
            points[key] = LeasedSignerInput(
                tx_hash=batch["transaction_hash"],
                index=index,
                capacity_shannons=entry["required_capacity_shannons"],
            )
    rows = []
    for entry in sorted(entries, key=lambda row: (row["slot_id"], row["retry_ordinal"])):
        point = points[(entry["slot_id"], entry["retry_ordinal"])]
        rows.append({
            "lease_resource_id": entry["lease_resource_id"],
            "leased_inputs": [point.to_dict()],
            "own_lock": deepcopy(entry["own_lock"]),
            "private_key": entry["private_key"],
            "public_address": entry["public_address"],
            "retry_ordinal": entry["retry_ordinal"],
            "signer_handle": entry["signer_handle"],
            "slot_id": entry["slot_id"],
        })
    return {
        "chain_profile_id": plan.chain.profile_id,
        "chain_profile_sha256": plan.chain.sha256,
        "entries": rows,
        "schema_version": SIGNER_POOL_SCHEMA_VERSION,
    }


def _receipt_document(
    manifest: CampaignManifest,
    plan: SignerProvisioningPlan,
    state: dict[str, Any],
    pool: PrivateSignerPool,
) -> dict[str, Any]:
    public_entries = [{
        "lease_resource_id": entry.lease_resource_id,
        "leased_inputs": [leased.to_dict() for leased in entry.leased_inputs],
        "own_lock": deepcopy(entry.own_lock),
        "public_address": entry.public_address,
        "retry_ordinal": entry.retry_ordinal,
        "signer_handle": entry.signer_handle,
        "slot_id": entry.slot_id,
    } for entry in pool.entries]
    return {
        "campaign_id": manifest.campaign_id,
        "chain_id": plan.chain.chain_id,
        "chain_profile_id": plan.chain.profile_id,
        "chain_profile_sha256": plan.chain.sha256,
        "completed_utc": state["completed_utc"],
        "entries": public_entries,
        "funding_request_count": len(state["provisioning_batches"]),
        "genesis_hash": plan.chain.genesis_hash,
        "manifest_sha256": manifest.sha256,
        "private_pool_ready": True,
        "rpc_requests_at_completion": state["rpc_requests_at_completion"],
        "schema_version": PROVISIONING_RECEIPT_SCHEMA_VERSION,
        "split_fee_shannons": len(state["provisioning_batches"]) * SPLIT_FEE_SHANNONS,
        "split_transaction_count": len(state["provisioning_batches"]),
        "split_transaction_hashes": [
            batch["transaction_hash"] for batch in state["provisioning_batches"]
        ],
        "total_leased_capacity_shannons": sum(
            leased.capacity_shannons
            for entry in pool.entries
            for leased in entry.leased_inputs
        ),
    }


def _publish_or_validate_receipt(path: Path | str, receipt: dict[str, Any]) -> None:
    destination = Path(path)
    if destination.exists():
        try:
            payload = destination.read_bytes()
            existing = json.loads(payload)
        except Exception:
            raise SignerProvisioningError("existing provisioning receipt is unreadable") from None
        if canonical_json_bytes(existing) != payload or existing != receipt:
            raise SignerProvisioningError("existing provisioning receipt differs")
        return
    publish_document(destination, receipt, "signer provisioning receipt")


class LiveSignerProvisioningBackend:
    """Bounded network, MCP and networkless key-holder adapters for provisioning."""

    def __init__(
        self,
        plan: SignerProvisioningPlan,
        *,
        repository_root: Path | str,
        rpc_endpoint: str = TESTNET_RPC,
        mcp_endpoint: str = MCP_URL,
        rpc_factory: Callable[..., HttpJsonRpcClient] = HttpJsonRpcClient,
        mcp_client_factory: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.plan = plan
        self.repository_root = Path(repository_root).resolve(strict=True)
        self.rpc = rpc_factory(
            rpc_endpoint,
            request_limit=_rpc_request_limit(plan),
            timeout_seconds=30.0,
        )
        self.mcp_endpoint = mcp_endpoint
        self.mcp_client_factory = mcp_client_factory
        self.sleep = sleep
        self._funding_requests = 0
        self._image_checked = False

    @property
    def rpc_requests(self) -> int:
        return self.rpc.request_count

    @property
    def funding_requests(self) -> int:
        return self._funding_requests

    def _mcp(self, request_limit: int) -> Any:
        factory = self.mcp_client_factory
        if factory is None:
            from ckb_mcp import CkbMcpClient

            factory = CkbMcpClient
        return factory(url=self.mcp_endpoint, timeout=60.0, request_limit=request_limit)

    def observe_environment(self) -> tuple[str, str]:
        if not self._image_checked:
            _verify_image(self.repository_root, self.plan.agent_image, role="agent")
            self._image_checked = True
        chain = DirectChainProbe(self.rpc).observe()
        client = self._mcp(8)
        product = CkbAiPreflightAdapter(client, self.plan.treatment_surface).observe()
        if product.chain_identity is None:
            raise SignerProvisioningError("CKB AI did not report its TestNet identity")
        if (
            product.chain_identity.chain_id,
            product.chain_identity.genesis_hash,
        ) != (chain.chain_id, chain.genesis_hash):
            raise SignerProvisioningError("CKB AI and direct RPC report different networks")
        return chain.chain_id, chain.genesis_hash

    def bind_key(self, private_key: str) -> tuple[str, dict[str, Any]]:
        placeholder = _placeholder_entry(
            slot_id=f"key-{secrets.token_hex(8)}",
            retry_ordinal=0,
            private_key=private_key,
            public_address="ckt1placeholder",
            own_lock={
                "args": "0x" + "00" * 20,
                "code_hash": "0x" + "00" * 32,
                "hash_type": "type",
            },
            leased_input=LeasedSignerInput(
                tx_hash="0x" + "01" * 32,
                index=0,
                capacity_shannons=MINIMUM_CHANGE_SHANNONS,
            ),
        )
        holder = DockerTransactionKeyHolder(
            placeholder,
            image=self.plan.agent_image,
            runtime_namespace=f"ckbbench-provision-{secrets.token_hex(8)}",
        )
        return holder.inspect_public_binding()

    def request_funds(self, public_address: str) -> None:
        client = self._mcp(1)
        self._funding_requests += 1
        result = client.call_tool("dev_request_testnet_funds", {"address": public_address})
        if not isinstance(result, dict) or result.get("isError") is True:
            raise SignerProvisioningError("TestNet funding request was not accepted")
        content = result.get("content")
        if not isinstance(content, list) or not any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
            for block in content
        ):
            raise SignerProvisioningError("TestNet funding request returned no acknowledgement")

    def wait_for_funding(
        self,
        own_lock: dict[str, Any],
        required_capacity_shannons: int,
    ) -> LeasedSignerInput:
        for round_index in range(POLL_ROUNDS):
            page = self.rpc.call("get_cells", [{
                "script": own_lock,
                "script_type": "lock",
                "script_search_mode": "exact",
            }, "asc", hex(64)])
            objects = page.get("objects") if isinstance(page, dict) else None
            if not isinstance(objects, list) or len(objects) > 64:
                raise SignerProvisioningError("TestNet indexer returned malformed funding data")
            candidates = []
            for value in objects:
                output = value.get("output") if isinstance(value, dict) else None
                point = value.get("out_point") if isinstance(value, dict) else None
                if (
                    isinstance(output, dict)
                    and output.get("lock") == own_lock
                    and output.get("type") is None
                    and value.get("output_data") == "0x"
                    and isinstance(point, dict)
                ):
                    capacity = _hex_int(output.get("capacity"), "faucet cell capacity")
                    tx_hash = point.get("tx_hash")
                    index = _hex_int(point.get("index"), "faucet cell index")
                    if (
                        capacity >= required_capacity_shannons
                        and isinstance(tx_hash, str)
                        and _HASH32.fullmatch(tx_hash) is not None
                    ):
                        candidates.append(LeasedSignerInput(tx_hash, index, capacity))
            if len(candidates) > MAX_FUNDING_CANDIDATES_PER_POLL:
                raise SignerProvisioningError(
                    "TestNet indexer returned too many matching funding cells"
                )
            for candidate in sorted(
                candidates,
                key=lambda row: (row.capacity_shannons, row.out_point),
            ):
                if self._verify_funding_input(candidate, own_lock):
                    return candidate
            if round_index + 1 < POLL_ROUNDS:
                self.sleep(POLL_INTERVAL_SECONDS)
        raise SignerProvisioningError("confirmed TestNet funding did not arrive in time")

    def _verify_funding_input(
        self,
        candidate: LeasedSignerInput,
        own_lock: dict[str, Any],
    ) -> bool:
        out_point = {
            "index": hex(candidate.index),
            "tx_hash": candidate.tx_hash,
        }
        live = self.rpc.call("get_live_cell", [out_point, True])
        cell = (
            live.get("cell")
            if isinstance(live, dict) and live.get("status") == "live"
            else None
        )
        output = cell.get("output") if isinstance(cell, dict) else None
        data = cell.get("data") if isinstance(cell, dict) else None
        transaction = self.rpc.call("get_transaction", [candidate.tx_hash])
        status = transaction.get("tx_status") if isinstance(transaction, dict) else None
        if not isinstance(status, dict) or status.get("status") != "committed":
            return False
        return bool(
            isinstance(output, dict)
            and output.get("lock") == own_lock
            and output.get("type") is None
            and _hex_int(output.get("capacity"), "faucet cell capacity")
            == candidate.capacity_shannons
            and isinstance(data, dict)
            and data.get("content") == "0x"
        )

    def sign_transaction(
        self,
        donor: PrivateSignerEntry,
        transaction: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        holder = DockerTransactionKeyHolder(
            donor,
            image=self.plan.agent_image,
            runtime_namespace=f"ckbbench-funding-{secrets.token_hex(8)}",
        )
        return holder.sign_transaction_with_hash(transaction)

    def transaction_status(self, transaction_hash: str) -> str:
        response = self.rpc.call("get_transaction", [transaction_hash])
        if response is None:
            return "unknown"
        status = response.get("tx_status") if isinstance(response, dict) else None
        name = status.get("status") if isinstance(status, dict) else None
        if name not in {"unknown", "pending", "proposed", "committed", "rejected"}:
            raise SignerProvisioningError("funding transaction status is malformed")
        return name

    def submit_transaction(self, transaction: dict[str, Any], transaction_hash: str) -> None:
        observed = self.rpc.call("send_transaction", [transaction, "passthrough"])
        if observed != transaction_hash:
            raise SignerProvisioningError(
                "funding submission returned a different transaction hash"
            )

    def wait_for_confirmations(self, transaction_hash: str, minimum: int) -> int:
        for round_index in range(POLL_ROUNDS):
            response = self.rpc.call("get_transaction", [transaction_hash])
            status = response.get("tx_status") if isinstance(response, dict) else None
            name = status.get("status") if isinstance(status, dict) else None
            if name == "rejected":
                raise SignerProvisioningError("funding transaction was rejected")
            if name == "committed":
                block_number = _hex_int(
                    status.get("block_number"), "funding transaction block number"
                )
                header = self.rpc.call("get_tip_header", [])
                tip = _hex_int(
                    header.get("number") if isinstance(header, dict) else None,
                    "TestNet tip number",
                )
                confirmations = tip - block_number + 1
                if confirmations >= minimum:
                    return confirmations
            if round_index + 1 < POLL_ROUNDS:
                self.sleep(POLL_INTERVAL_SECONDS)
        raise SignerProvisioningError("funding transaction did not confirm in time")

    def verify_output(
        self,
        transaction_hash: str,
        index: int,
        own_lock: dict[str, Any],
        capacity_shannons: int,
    ) -> None:
        response = self.rpc.call(
            "get_live_cell",
            [{"index": hex(index), "tx_hash": transaction_hash}, True],
        )
        cell = (
            response.get("cell")
            if isinstance(response, dict) and response.get("status") == "live"
            else None
        )
        output = cell.get("output") if isinstance(cell, dict) else None
        data = cell.get("data") if isinstance(cell, dict) else None
        if (
            not isinstance(output, dict)
            or output.get("lock") != own_lock
            or output.get("type") is not None
            or _hex_int(output.get("capacity"), "funding output capacity")
            != capacity_shannons
            or not isinstance(data, dict)
            or data.get("content") != "0x"
        ):
            raise SignerProvisioningError("funding output differs from its signer lease")

    def close(self) -> None:
        self.rpc.close()


def provision_signer_pool(
    manifest: CampaignManifest,
    release_binding: CampaignReleaseBinding,
    *,
    repository_root: Path | str,
    private_root: Path | str,
    receipt_path: Path | str,
    authorized_by_user: bool,
    backend: SignerProvisioningBackend | None = None,
    clock: Callable[[], str] = _utc_now,
    key_factory: Callable[[], str] = _private_key,
) -> Path | None:
    plan = build_signer_provisioning_plan(manifest, release_binding)
    if plan is None:
        return None
    if not authorized_by_user:
        raise SignerProvisioningError("signer provisioning needs explicit live authorization")
    if backend is None and os.getenv("CKBBENCH_DOCKER") != "1":
        raise SignerProvisioningError("signer provisioning requires CKBBENCH_DOCKER=1")
    repository = Path(repository_root).resolve(strict=True)
    root = _secure_private_root(private_root, repository)
    state_path = root / "signer-provisioning.json"
    pool_path = root / "signer-pool.json"
    own_backend = backend is None
    if backend is None:
        backend = LiveSignerProvisioningBackend(plan, repository_root=repository)
    try:
        with _provisioning_lock(root):
            observed_chain = backend.observe_environment()
            if observed_chain != (plan.chain.chain_id, plan.chain.genesis_hash):
                raise SignerProvisioningError("live environment differs from the frozen TestNet")
            if pool_path.exists():
                pool = load_private_signer_pool(pool_path, repository_root=repository)
                validate_private_signer_pool(manifest, release_binding, pool)
                if not state_path.exists():
                    raise SignerProvisioningError(
                        "signer pool lacks its private provisioning state"
                    )
                state = _read_private(state_path)
                _entries, _batches = _validate_state(state, manifest, plan)
                if state["completed_utc"] is None:
                    state["completed_utc"] = clock()
                    state["rpc_requests_at_completion"] = backend.rpc_requests
                    _write_private(state_path, state, create_only=False)
                receipt = _receipt_document(manifest, plan, state, pool)
                _publish_or_validate_receipt(receipt_path, receipt)
                return pool_path
            if state_path.exists():
                state = _read_private(state_path)
                entries, batches = _validate_state(state, manifest, plan)
                for row in entries + batches:
                    address, own_lock = backend.bind_key(row["private_key"])
                    if (address, own_lock) != (row["public_address"], row["own_lock"]):
                        raise SignerProvisioningError(
                            "private key does not match its saved binding"
                        )
            else:
                state = _new_state(manifest, plan, backend, clock, key_factory)
                _write_private(state_path, state, create_only=True)
                entries, batches = _validate_state(state, manifest, plan)

            entry_by_key = {
                (row["slot_id"], row["retry_ordinal"]): row for row in entries
            }
            for batch in batches:
                rows = [entry_by_key[(key[0], key[1])] for key in batch["target_keys"]]
                required = (
                    sum(row["required_capacity_shannons"] for row in rows)
                    + SPLIT_FEE_SHANNONS
                    + MINIMUM_CHANGE_SHANNONS
                )
                if batch["donor_input"] is None:
                    if batch["funding_status"] == "not_requested":
                        batch["funding_status"] = "in_flight"
                        _write_private(state_path, state, create_only=False)
                        backend.request_funds(batch["public_address"])
                    donor_input = backend.wait_for_funding(batch["own_lock"], required)
                    batch["donor_input"] = donor_input.to_dict()
                    batch["funding_status"] = "funded"
                    _write_private(state_path, state, create_only=False)
                else:
                    donor_input = LeasedSignerInput(**batch["donor_input"])

                donor = _placeholder_entry(
                    slot_id=f"provision-{manifest.campaign_id[9:17]}-{batch['batch_index']:03d}",
                    retry_ordinal=0,
                    private_key=batch["private_key"],
                    public_address=batch["public_address"],
                    own_lock=batch["own_lock"],
                    leased_input=donor_input,
                )
                if batch["split_status"] == "not_prepared":
                    unsigned = _build_split_transaction(plan, rows, batch, donor_input)
                    signed, transaction_hash = backend.sign_transaction(donor, unsigned)
                    batch["transaction"] = signed
                    batch["transaction_hash"] = transaction_hash
                    batch["split_status"] = "prepared"
                    _write_private(state_path, state, create_only=False)
                transaction_hash = batch["transaction_hash"]
                if batch["split_status"] in {"prepared", "in_flight"}:
                    known = (
                        "unknown"
                        if batch["split_status"] == "prepared"
                        else backend.transaction_status(transaction_hash)
                    )
                    if known == "rejected":
                        raise SignerProvisioningError("funding transaction was rejected")
                    if known == "unknown":
                        batch["split_status"] = "in_flight"
                        _write_private(state_path, state, create_only=False)
                        try:
                            backend.submit_transaction(batch["transaction"], transaction_hash)
                        except TestnetIntegrationError:
                            if backend.transaction_status(transaction_hash) == "unknown":
                                raise SignerProvisioningError(
                                    "funding submission is unresolved; rerun the same "
                                    "campaign to reconcile it"
                                ) from None
                    batch["split_status"] = "submitted"
                    _write_private(state_path, state, create_only=False)
                if batch["split_status"] == "submitted":
                    minimum = max(row["minimum_confirmations"] for row in rows)
                    backend.wait_for_confirmations(transaction_hash, minimum)
                    for index, row in enumerate(rows):
                        backend.verify_output(
                            transaction_hash,
                            index,
                            row["own_lock"],
                            row["required_capacity_shannons"],
                        )
                    batch["split_status"] = "confirmed"
                    _write_private(state_path, state, create_only=False)

            pool_document = _pool_document(plan, entries, batches)
            _write_private(pool_path, pool_document, create_only=True)
            pool = load_private_signer_pool(pool_path, repository_root=repository)
            validate_private_signer_pool(manifest, release_binding, pool)
            state["completed_utc"] = clock()
            state["rpc_requests_at_completion"] = backend.rpc_requests
            _write_private(state_path, state, create_only=False)
            receipt = _receipt_document(manifest, plan, state, pool)
            _publish_or_validate_receipt(receipt_path, receipt)
            return pool_path
    except (SignerProvisioningError, TestnetIntegrationError):
        raise
    except Exception as exc:
        raise SignerProvisioningError(
            f"signer provisioning failed safely ({type(exc).__name__})"
        ) from None
    finally:
        if own_backend:
            backend.close()
