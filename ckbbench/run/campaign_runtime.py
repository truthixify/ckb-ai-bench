"""Production adapters for isolated campaign Task execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ckbbench.config import (
    LLM_API_KEY_DEFAULT,
    MCP_URL,
    TESTNET_RPC,
    resolve_agent_image,
    resolve_agent_network,
    resolve_llm_api_key,
    resolve_verifier_image,
)
from ckbbench.run.agent_factory import make_agent_factory
from ckbbench.run.arm import resolve_arm
from ckbbench.run.attempt_store import AttemptEnvelope, AttemptState
from ckbbench.run.campaign import CampaignManifest, CampaignSlot
from ckbbench.run.campaign_operator import CampaignOperatorError, PreparedTaskAttempt
from ckbbench.run.chain_profile import ChainProfile
from ckbbench.run.cleanup import stop_agent_checked
from ckbbench.run.devnet import mentions_exact_name
from ckbbench.run.llm_readiness import check_llm_readiness
from ckbbench.run.metrics import (
    collect_metrics_from_agent,
    correctness_evidence_complete,
    harness_error_count,
    response_model_identity,
)
from ckbbench.run.model_profile import ModelProfile, load_run_profile
from ckbbench.run.runner import RunnerConfig, make_docker_runner, prepare_work_volume
from ckbbench.run.single_task import (
    AgentInfrastructureFailure,
    AgentObservation,
    SetupObservation,
    SingleTaskBackend,
)
from ckbbench.run.suite_release import CampaignReleaseBinding
from ckbbench.run.task_attempt import (
    AttemptIdentity,
    AttemptUsage,
    RetryReference,
    TaskAttemptIntent,
    TaskGrade,
    VERIFIER_PRIVATE_COMMITMENT_SCHEME,
    allocate_attempt_id,
    artifact_sha256,
    canonical_json_bytes,
)
from ckbbench.run.task_preflight import (
    MAX_MODEL_EVIDENCE_AGE_SECONDS,
    QUALIFICATION_KIND,
    READINESS_OPERATION,
    ChainIdentityObservation,
    DependencyObservation,
    FundingObservation,
    FundingRequirement,
    OutputObservation,
    ProviderObservation,
    SignerObservation,
    SourceObservation,
    TaskPreflightRequirements,
)
from ckbbench.run.task_sequence import TaskSequenceController, TaskStage
from ckbbench.run.testnet_integration import (
    CellLease,
    CkbAiPreflightAdapter,
    DependencyPreflightAdapter,
    DeploymentRequirement,
    DirectChainProbe,
    FundingPreflightAdapter,
    HttpJsonRpcClient,
    IntegratedTaskProbe,
    LeasedSignerInput,
    OutputPreflightAdapter,
    OutputTarget,
    PolicyConstrainedSigner,
    SignerInspection,
    SignerPreflightAdapter,
    SigningPolicy,
    TestnetIntegrationError,
)
from ckbbench.run.treatment_surface import (
    ScopedMcpClient,
    TaskMcpSurfacePolicy,
    TreatmentSurfaceProfile,
)
from ckbbench.suite.compose import chain_context_text, compose_stage, pointer_prompt
from ckbbench.suite.execution_contract import TaskExecutionContract
from ckbbench.suite.model import Suite, Task
from ckbbench.suite.runparams import RunParams, generate_run_params
from ckbbench.verify.codetask import BENCH_PASSWORD_ENV, CODE_CHALLENGE_ENV
from ckbbench.verify.onchain import SECP_CODE_HASH, SECP_HASH_TYPE
from ckbbench.verify.verifier import verify_task

SIGNER_POOL_SCHEMA_VERSION = "ckbbench-signer-pool-v1"
SIGNING_POLICY_FILENAME = "SIGNING_POLICY.json"
EXPECTED_IMAGE_PLATFORM = ("linux", "arm64")
RELEASE_FAMILY = "independent-task-suite-v1"
MAX_KEY_HOLDER_OUTPUT_BYTES = 1 << 20
MAX_SIGNER_POOL_BYTES = 1 << 20
MAX_PRIVATE_DOCUMENT_BYTES = 1 << 20
RPC_REQUEST_LIMIT = 256
LOCAL_COMMAND_TIMEOUT_SECONDS = 60
_PRIVATE_KEY = re.compile(r"^0x[0-9a-f]{64}$")
_HASH32 = re.compile(r"^0x[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
class CampaignRuntimeError(CampaignOperatorError):
    """A production runtime input or adapter violates the campaign boundary."""


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CampaignRuntimeError(f"{label} must contain exactly the reviewed fields")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CampaignRuntimeError(f"{label} must be a bounded public identifier")
    return value


def _agent_failure_exit_status(exc: BaseException) -> str:
    try:
        from ckb_model import (
            ProfiledProviderError,
            ProviderCallError,
            ResponseConversionError,
            ResponseHistoryError,
        )
    except Exception:
        return "AgentRuntimeError"
    statuses = {
        ProfiledProviderError: "ProfiledProviderError",
        ProviderCallError: "ProviderCallError",
        ResponseConversionError: "ResponseConversionError",
        ResponseHistoryError: "ResponseHistoryError",
    }
    return statuses.get(type(exc), "AgentRuntimeError")


def _hash32(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH32.fullmatch(value) is None:
        raise CampaignRuntimeError(f"{label} must be a 32-byte lowercase hash")
    return value


def _script(value: Any, label: str) -> dict[str, Any]:
    row = dict(_exact(value, {"args", "code_hash", "hash_type"}, label))
    _hash32(row["code_hash"], f"{label} code hash")
    if row["hash_type"] not in {"data", "data1", "data2", "type"}:
        raise CampaignRuntimeError(f"{label} hash type is unsupported")
    args = row["args"]
    if not isinstance(args, str) or re.fullmatch(r"0x(?:[0-9a-f]{2})*", args) is None:
        raise CampaignRuntimeError(f"{label} args must be canonical bytes")
    return row


@dataclass(frozen=True)
class PrivateSignerEntry:
    slot_id: str
    retry_ordinal: int
    signer_handle: str
    public_address: str
    private_key: str
    own_lock: dict[str, Any]
    lease_resource_id: str
    leased_inputs: tuple[LeasedSignerInput, ...]

    def __post_init__(self) -> None:
        for field in ("slot_id", "signer_handle", "public_address", "lease_resource_id"):
            _identifier(getattr(self, field), f"signer entry {field}")
        if self.retry_ordinal not in {0, 1}:
            raise CampaignRuntimeError("signer entry retry ordinal must be zero or one")
        if not isinstance(self.private_key, str) or _PRIVATE_KEY.fullmatch(self.private_key) is None:
            raise CampaignRuntimeError("signer entry private key is malformed")
        object.__setattr__(self, "own_lock", _script(self.own_lock, "signer entry own lock"))
        if not self.leased_inputs or not all(
            type(row) is LeasedSignerInput for row in self.leased_inputs
        ):
            raise CampaignRuntimeError("signer entry needs typed leased inputs")
        if tuple(row.out_point for row in self.leased_inputs) != tuple(sorted(
            {row.out_point for row in self.leased_inputs}
        )):
            raise CampaignRuntimeError("signer entry leased inputs must be unique and sorted")

    @property
    def lease(self) -> CellLease:
        return CellLease(
            lease_resource_id=self.lease_resource_id,
            signer_handle=self.signer_handle,
            lock_script=self.own_lock,
            out_points=tuple(row.out_point for row in self.leased_inputs),
        )


@dataclass(frozen=True)
class PrivateSignerPool:
    chain_profile_id: str
    chain_profile_sha256: str
    entries: tuple[PrivateSignerEntry, ...]

    def __post_init__(self) -> None:
        _identifier(self.chain_profile_id, "signer pool chain profile")
        if re.fullmatch(r"[0-9a-f]{64}", self.chain_profile_sha256) is None:
            raise CampaignRuntimeError("signer pool chain profile digest is invalid")
        if not isinstance(self.entries, tuple) or not all(
            type(row) is PrivateSignerEntry for row in self.entries
        ):
            raise CampaignRuntimeError("signer pool entries must be immutable typed records")
        order = tuple((row.slot_id, row.retry_ordinal) for row in self.entries)
        if order != tuple(sorted(set(order))):
            raise CampaignRuntimeError("signer pool entries must be unique and sorted")

    def entry_for(self, slot_id: str, retry_ordinal: int) -> PrivateSignerEntry:
        matches = tuple(
            row
            for row in self.entries
            if (row.slot_id, row.retry_ordinal) == (slot_id, retry_ordinal)
        )
        if len(matches) != 1:
            raise CampaignRuntimeError("signer pool does not contain exactly one attempt lease")
        return matches[0]


def _load_signer_entry(value: Any) -> PrivateSignerEntry:
    row = dict(_exact(value, {
        "lease_resource_id",
        "leased_inputs",
        "own_lock",
        "private_key",
        "public_address",
        "retry_ordinal",
        "signer_handle",
        "slot_id",
    }, "signer pool entry"))
    raw_inputs = row["leased_inputs"]
    if not isinstance(raw_inputs, list):
        raise CampaignRuntimeError("signer pool leased inputs must be an array")
    inputs = []
    for value in raw_inputs:
        item = _exact(value, {"capacity_shannons", "index", "tx_hash"}, "leased input")
        try:
            inputs.append(LeasedSignerInput(**item))
        except TestnetIntegrationError as exc:
            raise CampaignRuntimeError("signer pool contains an invalid leased input") from exc
    row["leased_inputs"] = tuple(inputs)
    return PrivateSignerEntry(**row)


def load_private_signer_pool(
    path: Path | str,
    *,
    repository_root: Path | str,
) -> PrivateSignerPool:
    """Read one owner-private pool outside the repository without ever formatting key bytes."""
    source = Path(path)
    root = Path(repository_root).resolve(strict=True)
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise CampaignRuntimeError("signer pool contains a duplicate field")
            document[key] = value
        return document

    descriptor = -1
    try:
        resolved = source.resolve(strict=True)
        if resolved == root or resolved.is_relative_to(root):
            raise CampaignRuntimeError("signer pool must live outside the repository")
        descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CampaignRuntimeError("signer pool must be a regular file")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise CampaignRuntimeError("signer pool must be owned by this user with mode 0600")
        if metadata.st_size <= 0 or metadata.st_size > MAX_SIGNER_POOL_BYTES:
            raise CampaignRuntimeError("signer pool size is outside the accepted boundary")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            payload = stream.read(MAX_SIGNER_POOL_BYTES + 1)
        if len(payload) != metadata.st_size or len(payload) > MAX_SIGNER_POOL_BYTES:
            raise CampaignRuntimeError("signer pool changed while it was being read")
        raw = json.loads(payload, object_pairs_hook=no_duplicate_keys)
    except CampaignRuntimeError:
        raise
    except Exception as exc:
        raise CampaignRuntimeError(
            f"signer pool could not be read safely ({type(exc).__name__})"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    document = _exact(raw, {
        "chain_profile_id", "chain_profile_sha256", "entries", "schema_version",
    }, "signer pool")
    if document["schema_version"] != SIGNER_POOL_SCHEMA_VERSION:
        raise CampaignRuntimeError("signer pool schema is unsupported")
    entries_raw = document["entries"]
    if not isinstance(entries_raw, list):
        raise CampaignRuntimeError("signer pool entries must be an array")
    entries = tuple(_load_signer_entry(row) for row in entries_raw)
    pool = PrivateSignerPool(
        chain_profile_id=_identifier(document["chain_profile_id"], "signer pool chain profile"),
        chain_profile_sha256=document["chain_profile_sha256"],
        entries=entries,
    )
    return pool


_KEY_HOLDER_SCRIPT = r"""
import {ClientPublicTestnet, SignerCkbPrivateKey, Transaction} from '@ckb-ccc/core';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const client = new ClientPublicTestnet({url: 'http://127.0.0.1:1'});
const signer = new SignerCkbPrivateKey(client, payload.private_key);
const address = await signer.getRecommendedAddressObj();
const own = address.script;
const wireScript = (value) => ({code_hash: value.codeHash, hash_type: value.hashType, args: value.args});
const publicBinding = {public_address: address.toString(), own_lock: wireScript(own)};
if (payload.operation === 'inspect') {
  process.stdout.write(JSON.stringify(publicBinding));
  process.exit(0);
}
if (payload.operation !== 'sign') throw new Error('operation');
if (JSON.stringify(publicBinding.own_lock) !== JSON.stringify(payload.own_lock)) throw new Error('lock');
if (publicBinding.public_address !== payload.public_address) throw new Error('address');
const script = (value) => value === null ? undefined : ({
  codeHash: value.code_hash, hashType: value.hash_type, args: value.args,
});
const point = (value) => ({txHash: value.tx_hash, index: value.index});
const transaction = payload.transaction;
const cells = new Map(payload.cells.map((row) => [`${row.tx_hash}:${row.index}`, row]));
const tx = Transaction.from({
  version: transaction.version,
  cellDeps: transaction.cell_deps.map((row) => ({
    outPoint: point(row.out_point), depType: row.dep_type === 'dep_group' ? 'depGroup' : row.dep_type,
  })),
  headerDeps: transaction.header_deps,
  inputs: transaction.inputs.map((row) => {
    const cell = cells.get(`${row.previous_output.tx_hash}:${Number(BigInt(row.previous_output.index))}`);
    if (!cell) throw new Error('cell');
    return {
      previousOutput: point(row.previous_output), since: row.since,
      cellOutput: {capacity: `0x${BigInt(cell.capacity_shannons).toString(16)}`, lock: script(payload.own_lock)},
      outputData: '0x',
    };
  }),
  outputs: transaction.outputs.map((row) => ({
    capacity: row.capacity, lock: script(row.lock), type: script(row.type),
  })),
  outputsData: transaction.outputs_data,
  witnesses: transaction.witnesses,
});
signer.getRelatedScripts = async () => [{script: own}];
const signed = await signer.signOnlyTransaction(tx);
const signedWire = JSON.parse(signed.stringify());
process.stdout.write(JSON.stringify({witnesses: signedWire.witnesses}));
"""


class DockerTransactionKeyHolder:
    """Keep the key on stdin of a networkless pinned-image process, never argv or env."""

    def __init__(
        self,
        entry: PrivateSignerEntry,
        *,
        image: str,
        runtime_namespace: str,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.entry = entry
        self.image = image
        self.runtime_namespace = runtime_namespace
        self._run = run

    def _invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        argv = [
            "docker", "run", "--rm", "--name", f"{self.runtime_namespace}-signer",
            "--network", "none",
            "--user", "65532:65532",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "64",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "-i", "--entrypoint", "node", self.image,
            "--input-type=module", "-e", _KEY_HOLDER_SCRIPT,
        ]
        try:
            completed = self._run(
                argv,
                input=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
        except Exception as exc:
            raise CampaignRuntimeError(
                f"key-holder process failed safely ({type(exc).__name__})"
            ) from None
        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        if completed.returncode != 0 or len(stdout.encode("utf-8")) > MAX_KEY_HOLDER_OUTPUT_BYTES:
            raise CampaignRuntimeError("key-holder process returned no usable result")
        try:
            result = json.loads(stdout)
        except (UnicodeError, json.JSONDecodeError):
            raise CampaignRuntimeError("key-holder process returned malformed JSON") from None
        if not isinstance(result, dict):
            raise CampaignRuntimeError("key-holder process returned a malformed result")
        return result

    def inspect_public_binding(self) -> tuple[str, dict[str, Any]]:
        result = _exact(self._invoke({
            "operation": "inspect",
            "private_key": self.entry.private_key,
        }), {"own_lock", "public_address"}, "key-holder inspection")
        return (
            _identifier(result["public_address"], "key-holder public address"),
            _script(result["own_lock"], "key-holder own lock"),
        )

    def sign_transaction(self, transaction: dict[str, Any]) -> dict[str, Any]:
        result = _exact(self._invoke({
            "cells": [row.to_dict() for row in self.entry.leased_inputs],
            "operation": "sign",
            "own_lock": self.entry.own_lock,
            "private_key": self.entry.private_key,
            "public_address": self.entry.public_address,
            "transaction": transaction,
        }), {"witnesses"}, "key-holder signing result")
        witnesses = result["witnesses"]
        if not isinstance(witnesses, list):
            raise CampaignRuntimeError("key-holder witnesses are malformed")
        signed = deepcopy(transaction)
        signed["witnesses"] = witnesses
        return signed


class PubliclyValidatedSigner:
    """Require the private key's public binding before exposing signer readiness."""

    def __init__(
        self,
        signer: PolicyConstrainedSigner,
        key_holder: DockerTransactionKeyHolder,
        entry: PrivateSignerEntry,
        submitted: Callable[[str], None] | None = None,
    ) -> None:
        self.signer = signer
        self.key_holder = key_holder
        self.entry = entry
        self._submitted = submitted

    @property
    def protocol_violation_count(self) -> int:
        return self.signer.protocol_violation_count

    def inspect(self) -> SignerInspection:
        address, lock = self.key_holder.inspect_public_binding()
        if address != self.entry.public_address or lock != self.entry.own_lock:
            raise CampaignRuntimeError("signer key does not match its public pool binding")
        return self.signer.inspect()

    def sign_and_submit(self, request: dict[str, Any]) -> dict[str, Any]:
        result = self.signer.sign_and_submit(request)
        if self._submitted is not None:
            self._submitted(result["tx_hash"])
        return result


class SubmissionIntentRpc:
    """Persist the irreversible boundary immediately before a transaction RPC."""

    def __init__(self, rpc: Any, before_submission: Callable[[], None]) -> None:
        self.rpc = rpc
        self.before_submission = before_submission

    def call(self, method: str, params: list[Any]) -> Any:
        if method == "send_transaction":
            self.before_submission()
        return self.rpc.call(method, params)


def _run_checked(
    argv: Sequence[str],
    *,
    cwd: Path,
    binary: bool = False,
) -> bytes | str:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            check=False,
            text=not binary,
            timeout=LOCAL_COMMAND_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise CampaignRuntimeError(
            f"execution-source inspection failed safely ({type(exc).__name__})"
        ) from None
    if completed.returncode != 0:
        raise CampaignRuntimeError("execution-source inspection returned an unusable status")
    return completed.stdout


def _git_names(root: Path, *args: str) -> tuple[str, ...]:
    raw = _run_checked(("git", *args, "-z"), cwd=root, binary=True)
    if not isinstance(raw, bytes):
        raise CampaignRuntimeError("git inspection returned malformed output")
    try:
        return tuple(part.decode("utf-8") for part in raw.split(b"\0") if part)
    except UnicodeDecodeError:
        raise CampaignRuntimeError("git inspection returned a non-UTF-8 path") from None


def _is_execution_input(path: str) -> bool:
    candidate = Path(path)
    if candidate.name == ".DS_Store" or "__pycache__" in candidate.parts:
        return False
    if "target" in candidate.parts or candidate.parts[:1] in {
        ("research",), ("benchmark-output",), (".vscode",),
    }:
        return False
    if path in {"bench", "pyproject.toml", "uv.lock", ".tool-versions"}:
        return True
    return candidate.parts[:1] in {
        ("agent",), ("ckbbench",), ("configs",), ("containers",), ("scripts",),
        ("suites",),
    }


def _docker_json(root: Path, *argv: str) -> Any:
    raw = _run_checked(("docker", *argv), cwd=root)
    if not isinstance(raw, str):
        raise CampaignRuntimeError("Docker inspection returned malformed output")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise CampaignRuntimeError("Docker inspection returned non-JSON output") from None


def _verify_image(root: Path, image: str, *, role: str) -> None:
    rows = _docker_json(root, "image", "inspect", image)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise CampaignRuntimeError("pinned image inspection is malformed")
    row = rows[0]
    config = row.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        row.get("Id") != image
        or (row.get("Os"), row.get("Architecture")) != EXPECTED_IMAGE_PLATFORM
        or not isinstance(labels, dict)
        or labels.get("org.ckbbench.role") != role
        or labels.get("org.ckbbench.release-family") != RELEASE_FAMILY
    ):
        raise CampaignRuntimeError("pinned image identity, platform or role label differs")


def _verify_network(root: Path, network: str) -> None:
    rows = _docker_json(root, "network", "inspect", network)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise CampaignRuntimeError("agent network inspection is malformed")
    row = rows[0]
    containers = row.get("Containers")
    if row.get("Name") != network or row.get("Internal") is not True or not isinstance(
        containers, dict
    ):
        raise CampaignRuntimeError("agent network is not the expected internal boundary")
    names = {
        item.get("Name") for item in containers.values() if isinstance(item, dict)
    }
    if "ckbbench-proxy" not in names:
        raise CampaignRuntimeError("agent network lacks the benchmark proxy")


def _resource_absent(root: Path, kind: str, name: str) -> bool:
    command = (
        ("docker", "container", "inspect", name)
        if kind == "container"
        else ("docker", "volume", "inspect", name)
    )
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=LOCAL_COMMAND_TIMEOUT_SECONDS,
        )
    except Exception:
        return False
    if completed.returncode == 0:
        return False
    output = (completed.stdout or "") + (completed.stderr or "")
    lowered = output.lower()
    wrong_kind = (
        ("no such volume" in lowered or "no such image" in lowered)
        if kind == "container"
        else ("no such container" in lowered or "no such image" in lowered)
    )
    expected_phrase = f"no such {kind}" in lowered or "no such object" in lowered
    return expected_phrase and not wrong_kind and mentions_exact_name(output, name)


def _validate_private_runtime_root(repository_root: Path, value: Path | str) -> Path:
    source = Path(value)
    if source.is_symlink():
        raise CampaignRuntimeError("private runtime root cannot be a symlink")
    resolved = source.resolve(strict=False)
    if resolved == repository_root:
        raise CampaignRuntimeError("private runtime root cannot be the repository")
    if resolved.is_relative_to(repository_root):
        relative = resolved.relative_to(repository_root)
        if not relative.parts or relative.parts[0] != "benchmark-output":
            raise CampaignRuntimeError(
                "private runtime root inside the repository must be under benchmark-output"
            )
        if len(relative.parts) == 1:
            raise CampaignRuntimeError(
                "private runtime root must not consume the whole generated-output root"
            )
    return resolved


class ProductionSourceObserver:
    """Recompute clean source, role images and runtime network before any paid call."""

    def __init__(self, repository_root: Path, source: Any, suite: Suite) -> None:
        self.root = repository_root
        self.source = source
        self.suite = suite

    def observe(self, runtime_namespace: str) -> SourceObservation:
        revision = str(_run_checked(("git", "rev-parse", "HEAD"), cwd=self.root)).strip()
        tree = _run_checked(
            ("git", "ls-tree", "-r", "--full-tree", "-z", "HEAD"),
            cwd=self.root,
            binary=True,
        )
        if not isinstance(tree, bytes):
            raise CampaignRuntimeError("git tree inspection returned malformed output")
        staged = _git_names(self.root, "diff", "--cached", "--name-only")
        tracked = _git_names(self.root, "diff", "--name-only")
        untracked = _git_names(
            self.root,
            "ls-files",
            "--others",
            "--exclude-standard",
        )
        execution_inputs = tuple(sorted(path for path in untracked if _is_execution_input(path)))
        observed_source = replace(
            self.source,
            repository_revision=revision,
            source_tree_sha256=hashlib.sha256(tree).hexdigest(),
        )

        agent_image = resolve_agent_image(agent_pin=self.suite.pins.agent_image_digest)
        verifier_image = resolve_verifier_image(
            verifier_pin=self.suite.pins.verifier_image_digest
        )
        if (
            agent_image != self.source.agent_image_digest
            or verifier_image != self.source.verifier_image_digest
        ):
            raise CampaignRuntimeError("an image override differs from the frozen role pin")
        _verify_image(self.root, agent_image, role="agent")
        _verify_image(self.root, verifier_image, role="verifier")
        _verify_network(self.root, resolve_agent_network())
        if not all(
            _resource_absent(self.root, kind, name)
            for kind, name in (
                ("container", f"{runtime_namespace}-agent"),
                ("container", f"{runtime_namespace}-signer"),
                ("volume", f"{runtime_namespace}-work"),
            )
        ):
            raise CampaignRuntimeError("attempt runtime namespace is already occupied")
        return SourceObservation(
            execution_source=observed_source,
            staged_change_count=len(staged),
            tracked_change_count=len(tracked),
            untracked_execution_input_count=len(execution_inputs),
            untracked_execution_inputs_sha256=artifact_sha256({
                "execution_inputs": list(execution_inputs),
            }),
        )


def _provider_qualification(profile: ModelProfile) -> tuple[str, str]:
    digest = profile.qualification_source_evidence_sha256 or profile.sha256
    return QUALIFICATION_KIND, digest


def _provider_observation(profile: ModelProfile, api_key: str) -> ProviderObservation:
    readiness = check_llm_readiness(api_base=profile.api_base, api_key=api_key)
    qualification_kind, qualification_digest = _provider_qualification(profile)
    return ProviderObservation(
        model_profile_id=profile.profile_id,
        model_profile_sha256=profile.sha256,
        qualification_kind=qualification_kind,
        qualification_evidence_sha256=qualification_digest,
        qualification_utc=profile.evidence_utc,
        operation=READINESS_OPERATION,
        authenticated=readiness.ready,
        credential_present=bool(api_key),
        ready=readiness.ready,
        request_count=1,
        generation_request_count=0,
        body_sent=False,
        redirect_followed=False,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chain_identity_sha256(profile: ChainProfile) -> str:
    if profile.chain_id is None or profile.genesis_hash is None:
        raise CampaignRuntimeError("a signing policy requires a live chain identity")
    return artifact_sha256({
        "chain_id": profile.chain_id,
        "genesis_hash": profile.genesis_hash,
    })


def _seed_for(slot: CampaignSlot, retry_ordinal: int) -> int:
    material = canonical_json_bytes({
        "derivation": slot.run_params_derivation,
        "retry_ordinal": retry_ordinal,
        "task_id": slot.task_id,
        "trial_challenge_sha256": slot.trial_challenge_sha256,
    })
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _attempt_challenge(slot: CampaignSlot, retry_ordinal: int) -> str:
    return "0x" + hashlib.sha256(canonical_json_bytes({
        "purpose": "prompt-attempt-challenge-v1",
        "retry_ordinal": retry_ordinal,
        "task_id": slot.task_id,
        "trial_challenge_sha256": slot.trial_challenge_sha256,
    })).hexdigest()


def _run_params(task: Task, slot: CampaignSlot, retry_ordinal: int) -> RunParams:
    params = generate_run_params(
        task,
        "",
        seed=_seed_for(slot, retry_ordinal),
        rpc=lambda _method, _params: "0x0",
    )
    prompt = deepcopy(params.prompt_injected)
    prompt["attempt_challenge"] = _attempt_challenge(slot, retry_ordinal)
    private = deepcopy(params.verifier_private)
    if task.kind == "code":
        challenge = secrets.token_hex(32)
        private[CODE_CHALLENGE_ENV] = challenge
        private[BENCH_PASSWORD_ENV] = challenge
    return RunParams(prompt_injected=prompt, verifier_private=private)


def _private_commitment(params: RunParams) -> str:
    planned = deepcopy(params.verifier_private)
    if "harness_tip" in planned:
        planned["harness_tip"] = {"source": "direct-preflight-tip-v1"}
    return hashlib.sha256(canonical_json_bytes({
        "blinding": secrets.token_hex(32),
        "verifier_private_plan": planned,
    })).hexdigest()


def _retry_reference(predecessor: AttemptEnvelope | None) -> RetryReference | None:
    if predecessor is None:
        return None
    if predecessor.result is None or not predecessor.receipts:
        raise CampaignRuntimeError("retry predecessor is not sealed and cleaned")
    return RetryReference(
        predecessor_attempt_id=predecessor.intent.attempt_id,
        predecessor_intent_sha256=predecessor.intent.sha256,
        predecessor_result_sha256=predecessor.result.sha256,
        predecessor_cleanup_receipt_sha256=predecessor.receipts[-1].sha256,
    )


def _identity(
    manifest: CampaignManifest,
    slot: CampaignSlot,
    params: RunParams,
) -> AttemptIdentity:
    return AttemptIdentity(
        campaign_id=manifest.campaign_id,
        campaign_manifest_sha256=manifest.sha256,
        batch_id=slot.batch_id,
        execution_plan_id=manifest.execution_plan_id,
        execution_plan_sha256=manifest.execution_plan_sha256,
        trial_id=slot.trial_id,
        suite_semver=manifest.suite_semver,
        suite_freeze_sha256=manifest.suite_freeze_sha256,
        task_id=slot.task_id,
        task_content_sha256=slot.task_content_sha256,
        arm=slot.arm,
        treatment_profile_id=slot.treatment_profile_id,
        treatment_profile_sha256=slot.treatment_profile_sha256,
        chain_track=slot.chain_track,
        chain_profile_id=slot.chain_profile_id,
        chain_profile_sha256=slot.chain_profile_sha256,
        requested_model=slot.requested_model,
        thinking_level=slot.thinking_level,
        model_variant_id=slot.model_variant_id,
        model_profile_id=slot.model_profile_id,
        model_profile_sha256=slot.model_profile_sha256,
        budget=slot.budget,
        trial_challenge_id=slot.trial_challenge_id,
        trial_challenge_sha256=slot.trial_challenge_sha256,
        run_params_derivation=slot.run_params_derivation,
        prompt_params_sha256=artifact_sha256(params.prompt_injected),
        verifier_private_commitment_scheme=VERIFIER_PRIVATE_COMMITMENT_SCHEME,
        verifier_private_commitment_sha256=_private_commitment(params),
        resource_equivalence_policy_id=slot.resource_equivalence_policy_id,
        resource_equivalence_policy_sha256=slot.resource_equivalence_policy_sha256,
        retry_policy_id=manifest.retry_policy_id,
        retry_policy_sha256=manifest.retry_policy_sha256,
        execution_source=manifest.execution_source,
    )


def _deployment_requirements(
    contract: TaskExecutionContract,
) -> tuple[DeploymentRequirement, ...]:
    return tuple(
        DeploymentRequirement(
            dependency_id=row.dependency_id,
            out_point=(row.transaction_hash, row.output_index),
            expected_cell_sha256=row.expected_cell_sha256,
        )
        for row in contract.required_dependencies
    )


def _funding_requirement(contract: TaskExecutionContract) -> FundingRequirement | None:
    funding = contract.funding
    if funding is None:
        return None
    return FundingRequirement(
        maximum_transfer_shannons=funding.maximum_transfer_shannons,
        fee_reserve_shannons=funding.fee_reserve_shannons,
        safety_margin_shannons=funding.safety_margin_shannons,
        minimum_cell_count=funding.minimum_cell_count,
        minimum_confirmations=funding.minimum_confirmations,
    )


def _signing_policy(
    contract: TaskExecutionContract,
    chain: ChainProfile,
    entry: PrivateSignerEntry,
    params: RunParams,
) -> SigningPolicy:
    if contract.signing_policy_id is None or contract.funding is None:
        raise CampaignRuntimeError("signed Task lacks its released policy and funding contract")
    recipient = params.prompt_injected.get("recipient_args")
    amount = params.prompt_injected.get("send_amount_shannons")
    if (
        not isinstance(recipient, str)
        or re.fullmatch(r"0x[0-9a-f]{40}", recipient) is None
        or not isinstance(amount, str)
        or not amount.isdigit()
    ):
        raise CampaignRuntimeError("signed Task parameters do not define a bounded transfer")
    transfer = int(amount)
    if transfer <= 0 or transfer > contract.funding.maximum_transfer_shannons:
        raise CampaignRuntimeError("signed Task transfer exceeds its released ceiling")
    dependencies = tuple(sorted(
        (
            {
                "dep_type": "dep_group",
                "out_point": {
                    "index": hex(row.output_index),
                    "tx_hash": row.transaction_hash,
                },
            }
            for row in contract.required_dependencies
        ),
        key=canonical_json_bytes,
    ))
    return SigningPolicy(
        policy_id=contract.signing_policy_id,
        signer_handle=entry.signer_handle,
        public_address=entry.public_address,
        chain_identity_sha256=_chain_identity_sha256(chain),
        leased_inputs=entry.leased_inputs,
        own_lock=entry.own_lock,
        permitted_destination_locks=({
            "args": recipient,
            "code_hash": SECP_CODE_HASH,
            "hash_type": SECP_HASH_TYPE,
        },),
        permitted_output_types=(None,),
        cell_deps=dependencies,
        header_deps=(),
        maximum_transfer_shannons=transfer,
        maximum_fee_shannons=contract.funding.fee_reserve_shannons,
        maximum_transactions=1,
        maximum_output_data_bytes=0,
    )


@dataclass(frozen=True)
class AttemptMaterial:
    task: Task
    contract: TaskExecutionContract
    chain: ChainProfile
    surface: TreatmentSurfaceProfile
    params: RunParams
    runtime_namespace: str
    runtime_dir: Path
    workspace: Path
    private_dir: Path
    resource_claims: tuple[tuple[str, str], ...]
    output_resources: tuple[tuple[str, str], ...]
    dependencies: tuple[DeploymentRequirement, ...]
    signer_entry: PrivateSignerEntry | None
    signing_policy: SigningPolicy | None


@dataclass
class PreflightState:
    direct_chain: ChainIdentityObservation | None = None


def _require_time(timeout_seconds: float | None) -> None:
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise TimeoutError


def _write_private_json(path: Path, document: dict[str, Any]) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(document))
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _read_private_json(path: Path, label: str) -> dict[str, Any]:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > MAX_PRIVATE_DOCUMENT_BYTES
        ):
            raise CampaignRuntimeError(f"{label} is outside the private-file boundary")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            payload = stream.read(MAX_PRIVATE_DOCUMENT_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (
            len(payload) != before.st_size
            or len(payload) > MAX_PRIVATE_DOCUMENT_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise CampaignRuntimeError(f"{label} changed while it was being read")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            document: dict[str, Any] = {}
            for key, value in pairs:
                if key in document:
                    raise CampaignRuntimeError(f"{label} contains a duplicate field")
                document[key] = value
            return document

        document = json.loads(payload, object_pairs_hook=unique_object)
        if not isinstance(document, dict) or canonical_json_bytes(document) != payload:
            raise CampaignRuntimeError(f"{label} is not canonical private JSON")
        return document
    except CampaignRuntimeError:
        raise
    except Exception as exc:
        raise CampaignRuntimeError(
            f"{label} could not be read safely ({type(exc).__name__})"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _optional_private_json(path: Path, label: str) -> dict[str, Any] | None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CampaignRuntimeError(
            f"{label} could not be inspected safely ({type(exc).__name__})"
        ) from None
    return _read_private_json(path, label)


def _resource_id(attempt_id: str, kind: str) -> str:
    return f"{attempt_id}-{kind}"


def _material_for(
    release_binding: CampaignReleaseBinding,
    signer_pool: PrivateSignerPool | None,
    private_runtime_root: Path,
    slot: CampaignSlot,
    attempt_id: str,
    retry_ordinal: int,
    *,
    recovery_requirements: TaskPreflightRequirements | None = None,
) -> AttemptMaterial:
    task = release_binding.release.tasks.get(slot.task_id)
    if task is None:
        raise CampaignRuntimeError("campaign slot does not name a released Task")
    contract = release_binding.execution_contract_for(slot)
    chain = next(
        (
            row
            for row in release_binding.chain_profiles
            if (row.profile_id, row.sha256)
            == (slot.chain_profile_id, slot.chain_profile_sha256)
        ),
        None,
    )
    surface = next(
        (
            row
            for row in release_binding.treatment_profiles
            if (row.profile_id, row.sha256)
            == (slot.treatment_profile_id, slot.treatment_profile_sha256)
        ),
        None,
    )
    if chain is None or surface is None:
        raise CampaignRuntimeError("campaign slot lacks its released chain or treatment profile")

    params = (
        RunParams(prompt_injected={}, verifier_private={})
        if recovery_requirements is not None
        else _run_params(task, slot, retry_ordinal)
    )
    signer_entry = None
    policy = None
    if contract.signer_required and recovery_requirements is None:
        if signer_pool is None:
            raise CampaignRuntimeError("signed campaign Task needs an operator-private signer pool")
        signer_entry = signer_pool.entry_for(slot.slot_id, retry_ordinal)
        policy = _signing_policy(contract, chain, signer_entry, params)

    runtime_namespace = f"ckbbench-{attempt_id}"
    runtime_dir = private_runtime_root / attempt_id
    workspace = runtime_dir / "workspace"
    private_dir = runtime_dir / "private"
    if recovery_requirements is None:
        ids: dict[str, list[str]] = {}
        for kind in contract.required_resource_kinds:
            if kind == "runtime-name":
                values = [runtime_namespace]
            elif kind == "workspace":
                values = [_resource_id(attempt_id, kind)]
            elif kind == "signer":
                if signer_entry is None:
                    raise CampaignRuntimeError("released signer claim lacks a signer entry")
                values = [signer_entry.signer_handle]
            elif kind == "spendable-input":
                if signer_entry is None:
                    raise CampaignRuntimeError("released input claim lacks a signer entry")
                values = [signer_entry.lease_resource_id]
            else:
                values = [_resource_id(attempt_id, kind)]
            ids[kind] = values
        claims = tuple(sorted(
            (kind, resource_id)
            for kind, resource_ids in ids.items()
            for resource_id in resource_ids
        ))
        outputs = tuple(sorted(
            (kind, ids[kind][0]) for kind in contract.expected_output_resource_kinds
        ))
    else:
        claims = recovery_requirements.required_resource_claims
        outputs = recovery_requirements.expected_output_resources
    return AttemptMaterial(
        task=task,
        contract=contract,
        chain=chain,
        surface=surface,
        params=params,
        runtime_namespace=runtime_namespace,
        runtime_dir=runtime_dir,
        workspace=workspace,
        private_dir=private_dir,
        resource_claims=claims,
        output_resources=outputs,
        dependencies=_deployment_requirements(contract),
        signer_entry=signer_entry,
        signing_policy=policy,
    )


def _output_path(material: AttemptMaterial, kind: str) -> Path:
    if kind == "workspace":
        return material.workspace
    if kind in {"proof-file", "binary"}:
        return material.workspace / material.task.proof_file
    return material.private_dir / f"{kind}.marker"


def _attempt_usage(agent: Any, wall_seconds: float) -> AttemptUsage:
    metrics = collect_metrics_from_agent(agent, wall_seconds=wall_seconds)
    ledger = getattr(getattr(agent, "model", None), "usage_ledger", None)
    counts: Counter[str] = Counter()
    if ledger is not None:
        for attempt in getattr(ledger, "attempts", ()):
            if getattr(attempt, "responded", False):
                model = getattr(attempt, "model", None)
                counts[model if isinstance(model, str) and model else "unreported"] += 1
    return AttemptUsage(
        token_usage_status=metrics.token_usage_status,
        cost_status="unavailable",
        provider_reported_cost_usd=None,
        model_calls=metrics.model_calls,
        provider_attempts=metrics.provider_attempts,
        provider_responses=metrics.provider_responses,
        provider_retry_count=metrics.provider_retry_count,
        provider_retry_delay_seconds=metrics.provider_retry_delay_seconds,
        prompt_tokens=metrics.prompt_tokens,
        completion_tokens=metrics.completion_tokens,
        total_tokens=metrics.total_tokens,
        provider_failure_category=metrics.provider_failure_category,
        provider_failure_counts=tuple(sorted(metrics.provider_failure_counts.items())),
        provider_response_model_counts=tuple(sorted(counts.items())),
    )


class ProductionTaskBackend(SingleTaskBackend):
    """Concrete one-Task backend; every external adapter starts after intent publication."""

    def __init__(
        self,
        material: AttemptMaterial,
        *,
        repository_root: Path,
        suite: Suite,
        model_profile: ModelProfile,
        api_key: str,
        rpc_endpoint: str = TESTNET_RPC,
        mcp_endpoint: str = MCP_URL,
        mcp_client_factory: Callable[..., Any] | None = None,
        rpc_factory: Callable[..., HttpJsonRpcClient] = HttpJsonRpcClient,
    ) -> None:
        self.material = material
        self.repository_root = repository_root
        self.suite = suite
        self.model_profile = model_profile
        self.api_key = api_key
        self.rpc_endpoint = rpc_endpoint
        self.mcp_endpoint = mcp_endpoint
        self.mcp_client_factory = mcp_client_factory
        self.rpc_factory = rpc_factory
        self.preflight = PreflightState()
        self._rpc: HttpJsonRpcClient | None = None
        self._signer: PubliclyValidatedSigner | None = None
        self._surface_policy: TaskMcpSurfacePolicy | None = None
        self._controller: TaskSequenceController | None = None
        self._pointer: str | None = None
        self._agent: Any | None = None
        self._submission_attempted = False
        self._submitted_transaction: str | None = None

    def _rpc_client(self) -> HttpJsonRpcClient:
        if self.material.chain.chain_track == "local-hermetic":
            raise CampaignRuntimeError("a local-hermetic Task cannot construct an RPC client")
        if self._rpc is None:
            self._rpc = self.rpc_factory(
                self.rpc_endpoint,
                request_limit=RPC_REQUEST_LIMIT,
            )
        return self._rpc

    def _mcp_client(self, *, request_limit: int | None = None) -> Any:
        factory = self.mcp_client_factory
        if factory is None:
            from ckb_mcp import CkbMcpClient

            factory = CkbMcpClient
        kwargs: dict[str, Any] = {"url": self.mcp_endpoint}
        if request_limit is not None:
            kwargs["request_limit"] = request_limit
        return factory(**kwargs)

    def observe_source(self, timeout_seconds: float | None) -> SourceObservation:
        _require_time(timeout_seconds)
        return ProductionSourceObserver(
            self.repository_root,
            self.material_source,
            self.suite,
        ).observe(self.material.runtime_namespace)

    @property
    def material_source(self) -> Any:
        return self._intent.identity.execution_source

    def observe_provider(self, timeout_seconds: float | None) -> ProviderObservation:
        _require_time(timeout_seconds)
        return _provider_observation(self.model_profile, self.api_key)

    def observe_ckb_ai(self, timeout_seconds: float | None) -> Any:
        _require_time(timeout_seconds)
        limit = 7 if self.material.surface.claims_live_chain else 3
        return CkbAiPreflightAdapter(
            self._mcp_client(request_limit=limit),
            self.material.surface,
        ).observe()

    def observe_rpc(self, timeout_seconds: float | None) -> ChainIdentityObservation:
        _require_time(timeout_seconds)
        observed = DirectChainProbe(self._rpc_client()).observe()
        self.preflight.direct_chain = observed
        return observed

    def _require_direct_chain(self) -> ChainIdentityObservation:
        observed = self.preflight.direct_chain
        if observed is None:
            raise CampaignRuntimeError("chain-dependent preflight ran before direct identity")
        return observed

    def _record_submission(self, tx_hash: str) -> None:
        if self._submitted_transaction is not None:
            raise CampaignRuntimeError("attempt submitted more than one transaction")
        _hash32(tx_hash, "submitted transaction hash")
        self._submitted_transaction = tx_hash
        marker = self.material.private_dir / "transaction.marker"
        _write_private_json(marker, {"tx_hash": tx_hash})

    def _record_submission_attempt(self) -> None:
        if self._submission_attempted:
            raise CampaignRuntimeError("attempt crossed the submission boundary more than once")
        marker = self.material.private_dir / "submission-intent.marker"
        _write_private_json(marker, {"state": "submission-attempted"})
        self._submission_attempted = True

    def _constrained_signer(self) -> PubliclyValidatedSigner:
        if self._signer is not None:
            return self._signer
        entry = self.material.signer_entry
        policy = self.material.signing_policy
        if entry is None or policy is None:
            raise CampaignRuntimeError("unsigned Task cannot construct a signer")
        key_holder = DockerTransactionKeyHolder(
            entry,
            image=self._intent.identity.execution_source.agent_image_digest,
            runtime_namespace=self.material.runtime_namespace,
        )
        constrained = PolicyConstrainedSigner(
            policy,
            key_holder,
            SubmissionIntentRpc(self._rpc_client(), self._record_submission_attempt),
        )
        self._signer = PubliclyValidatedSigner(
            constrained,
            key_holder,
            entry,
            self._record_submission,
        )
        return self._signer

    def observe_signer(self, timeout_seconds: float | None) -> SignerObservation:
        _require_time(timeout_seconds)
        return SignerPreflightAdapter(self._constrained_signer()).observe()

    def observe_funding(self, timeout_seconds: float | None) -> FundingObservation:
        _require_time(timeout_seconds)
        signer = self._constrained_signer()
        entry = self.material.signer_entry
        policy = self.material.signing_policy
        if entry is None or policy is None:
            raise CampaignRuntimeError("unsigned Task cannot inspect funding")
        return FundingPreflightAdapter(
            self._rpc_client(),
            entry.lease,
            policy,
            self._require_direct_chain(),
        ).observe()

    def observe_dependencies(self, timeout_seconds: float | None) -> DependencyObservation:
        _require_time(timeout_seconds)
        live = self.material.chain.chain_track != "local-hermetic"
        return DependencyPreflightAdapter(
            self.material.dependencies,
            rpc=self._rpc_client() if live else None,
            chain=self._require_direct_chain() if live else None,
        ).observe()

    def observe_outputs(self, timeout_seconds: float | None) -> OutputObservation:
        _require_time(timeout_seconds)
        targets = tuple(
            OutputTarget(kind, resource_id, _output_path(self.material, kind))
            for kind, resource_id in self.material.output_resources
        )
        return OutputPreflightAdapter(targets).observe()

    def bind_intent(self, intent: TaskAttemptIntent) -> None:
        self._intent = intent

    def setup(
        self,
        intent: TaskAttemptIntent,
        requirements: TaskPreflightRequirements,
        *,
        timeout_seconds: float | None,
    ) -> SetupObservation:
        _require_time(timeout_seconds)
        if (
            intent.sha256 != self._intent.sha256
            or requirements.required_resource_claims != self.material.resource_claims
        ):
            raise CampaignRuntimeError("setup inputs differ from the prepared attempt")
        root = self.material.runtime_dir.parent
        if root.is_symlink():
            raise CampaignRuntimeError("private runtime root cannot be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_status = root.stat()
        if (
            not root.is_dir()
            or root_status.st_uid != os.geteuid()
            or stat.S_IMODE(root_status.st_mode) & 0o077
        ):
            raise CampaignRuntimeError("private runtime root must be owner-private")
        if self.material.runtime_dir.exists() or self.material.runtime_dir.is_symlink():
            raise CampaignRuntimeError("attempt runtime directory is not fresh")
        self.material.runtime_dir.mkdir(mode=0o700, parents=False)
        self.material.workspace.mkdir(mode=0o755)
        self.material.private_dir.mkdir(mode=0o700)

        verifier_private = deepcopy(self.material.params.verifier_private)
        if "harness_tip" in verifier_private:
            verifier_private["harness_tip"] = self._require_direct_chain().tip_number
        _write_private_json(
            self.material.private_dir / "verifier-private.json",
            verifier_private,
        )
        if self.material.signing_policy is not None:
            policy_path = self.material.workspace / SIGNING_POLICY_FILENAME
            descriptor = os.open(
                policy_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_json_bytes(self.material.signing_policy.to_dict()))

        one_task_suite = replace(
            self.suite,
            chain_profile=self.material.chain.chain_track,
            tasks=(self.material.task,),
        )
        arm = resolve_arm(intent.identity.arm)
        stage = TaskStage(
            task_id=self.material.task.id,
            proof_file=self.material.task.proof_file,
            param_filename=f"{self.material.task.id}.json",
            prompt_injected=deepcopy(self.material.params.prompt_injected),
            instructions=compose_stage(
                one_task_suite,
                0,
                extra_preamble=arm.prompt_preamble,
                chain_context=chain_context_text(
                    self.material.chain.chain_track,
                    broker_bound=self.material.signing_policy is not None,
                ),
            ),
        )
        self._controller = TaskSequenceController(self.material.workspace, (stage,))
        instructions = self._controller.start()
        self._pointer = pointer_prompt(instructions)
        equivalence = artifact_sha256({
            "leased_capacities": sorted(
                row.capacity_shannons
                for row in (() if self.material.signer_entry is None else self.material.signer_entry.leased_inputs)
            ),
            "prompt_params_sha256": intent.identity.prompt_params_sha256,
            "resource_equivalence_policy_sha256": intent.identity.resource_equivalence_policy_sha256,
            "trial_challenge_sha256": intent.identity.trial_challenge_sha256,
        })
        return SetupObservation(equivalence)

    def start_agent(
        self,
        intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> object:
        _require_time(timeout_seconds)
        if (
            intent.sha256 != self._intent.sha256
            or self._controller is None
            or self._pointer is None
        ):
            raise CampaignRuntimeError("agent start requires a completed attempt setup")
        if os.getenv("CKBBENCH_DOCKER") != "1":
            raise CampaignRuntimeError("accepted campaign agents require isolated Docker execution")
        arm = resolve_arm(intent.identity.arm)
        policy = TaskMcpSurfacePolicy(self.material.surface)
        self._surface_policy = policy
        mcp_client = (
            ScopedMcpClient(self._mcp_client(), policy) if arm.mcp_enabled else None
        )
        factory = make_agent_factory(
            api_base=self.model_profile.api_base,
            api_key=self.api_key,
            profile=self.model_profile,
            step_limit=intent.identity.budget.step_limit,
            cost_limit=0.0,
            wall_time_limit_seconds=intent.identity.budget.wall_time_limit_seconds,
            treatment_surface=policy,
            container_name=f"{self.material.runtime_namespace}-agent",
            container_labels=(
                f"org.ckbbench.attempt={intent.attempt_id}",
                "org.ckbbench.owner=campaign",
            ),
            auto_cleanup=False,
        )
        one_task_suite = replace(self.suite, tasks=(self.material.task,))
        self._agent = factory(
            mount_dir=self.material.workspace,
            pointer=self._pointer,
            task_sequence=self._controller,
            arm_config=arm,
            mcp_client=mcp_client,
            model=self.model_profile.requested_model,
            suite=one_task_suite,
            chain=self.material.chain.chain_track,
            signer=None if self.material.signing_policy is None else self._constrained_signer(),
        )
        return self._agent

    def run_agent(
        self,
        agent: object,
        *,
        step_limit: int,
        wall_time_limit_seconds: int,
        provider_call_limit: int | None,
        output_token_limit: int | None,
    ) -> AgentObservation:
        budget = self._intent.identity.budget
        if (
            agent is not self._agent
            or (step_limit, wall_time_limit_seconds, provider_call_limit, output_token_limit)
            != (
                budget.step_limit,
                budget.wall_time_limit_seconds,
                budget.provider_call_limit,
                budget.output_token_limit,
            )
        ):
            raise CampaignRuntimeError("agent execution limits differ from the frozen attempt")
        started = time.monotonic()
        raised = False
        try:
            result = agent.run(self._pointer)
            exit_status = result.get("exit_status") if isinstance(result, dict) else None
        except Exception as exc:
            raised = True
            exit_status = _agent_failure_exit_status(exc)
        usage = _attempt_usage(agent, time.monotonic() - started)
        observation = AgentObservation(
            exit_status if isinstance(exit_status, str) and exit_status else "unknown",
            usage,
        )
        if (
            raised
            or harness_error_count(agent) > 0
            or not correctness_evidence_complete(agent)
            or response_model_identity(agent) != self.model_profile.probed_response_model
        ):
            raise AgentInfrastructureFailure(observation)
        return observation

    def stop_agent_checked(
        self,
        agent: object,
        *,
        timeout_seconds: float | None,
    ) -> None:
        _require_time(timeout_seconds)
        if agent is not self._agent:
            raise CampaignRuntimeError("attempt cannot stop a foreign agent")
        stop_agent_checked(agent)

    def grade(
        self,
        intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> TaskGrade:
        _require_time(timeout_seconds)
        if intent.sha256 != self._intent.sha256:
            raise CampaignRuntimeError("attempt cannot grade a foreign intent")
        private_path = self.material.private_dir / "verifier-private.json"
        verifier_private = _read_private_json(
            private_path,
            "verifier-private material",
        )
        runner = None
        if self.material.task.kind == "code":
            work_volume = f"{self.material.runtime_namespace}-work"
            prepare_work_volume(work_volume)
            config = replace(
                RunnerConfig.for_suite(self.suite),
                work_volume=work_volume,
                grade_timeout_seconds=max(1, int(timeout_seconds or 1)),
            )
            runner = make_docker_runner(config)
        rpc = (
            self._rpc_client().call
            if self.material.chain.chain_track != "local-hermetic"
            else lambda _method, _params: (_ for _ in ()).throw(
                CampaignRuntimeError("local verifier attempted live RPC")
            )
        )
        verdict = verify_task(
            self.material.task,
            self.material.workspace,
            verifier_private,
            rpc,
            registry_root=self.suite_root,
            runner=runner,
        )
        passed = verdict.passed is True
        return TaskGrade(
            status="passed" if passed else "failed",
            verifier_score=self.material.task.score if passed else 0,
            score_awarded=self.material.task.score if passed else 0,
            max_score=self.material.task.score,
            reason="Verifier passed." if passed else "Verifier failed.",
            proof="",
        )

    @property
    def suite_root(self) -> Path:
        return self.repository_root / self.suite_registry_relative

    @property
    def suite_registry_relative(self) -> Path:
        try:
            return self._release_root.relative_to(self.repository_root)
        except ValueError:
            return self._release_root

    def bind_release_root(self, root: Path) -> None:
        self._release_root = root

    def protocol_violated(
        self,
        intent: TaskAttemptIntent,
        *,
        timeout_seconds: float | None,
    ) -> bool:
        _require_time(timeout_seconds)
        if intent.sha256 != self._intent.sha256:
            raise CampaignRuntimeError("attempt cannot inspect a foreign protocol boundary")
        count = getattr(self._agent, "protocol_violation_count", None)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise CampaignRuntimeError("agent returned malformed protocol telemetry")
        return count > 0

    def _remove_docker_resource(
        self,
        kind: str,
        name: str,
        timeout_seconds: float | None,
    ) -> str:
        if _resource_absent(self.repository_root, kind, name):
            return "absent"
        noun = "container" if kind == "container" else "volume"
        command = ["docker", noun, "rm", "-f", name]
        completed = subprocess.run(
            command,
            cwd=self.repository_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=(
                LOCAL_COMMAND_TIMEOUT_SECONDS
                if timeout_seconds is None
                else min(LOCAL_COMMAND_TIMEOUT_SECONDS, timeout_seconds)
            ),
        )
        if completed.returncode != 0 or not _resource_absent(
            self.repository_root, kind, name
        ):
            raise CampaignRuntimeError(f"attempt-owned Docker {kind} could not be removed")
        return "released"

    def cleanup_resource(
        self,
        intent: TaskAttemptIntent,
        resource_kind: str,
        resource_id: str,
        *,
        timeout_seconds: float | None,
    ) -> str:
        _require_time(timeout_seconds)
        if (
            intent.sha256 != self._intent.sha256
            or (resource_kind, resource_id) not in self.material.resource_claims
        ):
            raise CampaignRuntimeError("attempt cleanup refused an undeclared resource")
        transaction_marker = _optional_private_json(
            self.material.private_dir / "transaction.marker",
            "transaction marker",
        )
        if transaction_marker is not None:
            row = _exact(transaction_marker, {"tx_hash"}, "transaction marker")
            _hash32(row["tx_hash"], "transaction marker hash")
        intent_marker = _optional_private_json(
            self.material.private_dir / "submission-intent.marker",
            "submission-intent marker",
        )
        if intent_marker is not None and _exact(
            intent_marker, {"state"}, "submission-intent marker"
        )["state"] != "submission-attempted":
            raise CampaignRuntimeError("submission-intent marker state is invalid")
        submitted = self._submitted_transaction is not None or transaction_marker is not None
        submission_attempted = self._submission_attempted or intent_marker is not None
        if resource_kind == "runtime-name":
            agent = self._remove_docker_resource(
                "container", f"{self.material.runtime_namespace}-agent", timeout_seconds
            )
            signer = self._remove_docker_resource(
                "container", f"{self.material.runtime_namespace}-signer", timeout_seconds
            )
            return "released" if "released" in {agent, signer} else "absent"
        if resource_kind == "binary":
            self._remove_docker_resource(
                "volume", f"{self.material.runtime_namespace}-work", timeout_seconds
            )
        if resource_kind in {"transaction", "data-cell"}:
            if submitted:
                return "permanent"
            return "retired" if submission_attempted else "absent"
        if resource_kind == "spendable-input":
            if submitted:
                return "permanent"
            return "retired" if submission_attempted else "released"
        if resource_kind == "signer":
            return "retired"
        if resource_kind == "workspace":
            if self.material.runtime_dir.is_symlink():
                raise CampaignRuntimeError("attempt runtime directory became a symlink")
            existed = self.material.runtime_dir.exists()
            if existed:
                shutil.rmtree(self.material.runtime_dir)
            return "released" if existed else "absent"
        path = _output_path(self.material, resource_kind)
        if path.is_symlink():
            raise CampaignRuntimeError("attempt output became a symlink")
        existed = path.exists()
        if existed and path.is_file():
            path.unlink()
        return "released" if existed else "absent"


class ProductionCampaignRuntime:
    """Inert factory for release-bound production Task backends."""

    def __init__(
        self,
        release_binding: CampaignReleaseBinding,
        model_profile: ModelProfile,
        *,
        repository_root: Path | str,
        private_runtime_root: Path | str,
        signer_pool: PrivateSignerPool | None = None,
        rpc_endpoint: str = TESTNET_RPC,
        mcp_endpoint: str = MCP_URL,
        mcp_client_factory: Callable[..., Any] | None = None,
        rpc_factory: Callable[..., HttpJsonRpcClient] = HttpJsonRpcClient,
    ) -> None:
        self.release_binding = release_binding
        self.model_profile = model_profile
        self.repository_root = Path(repository_root).resolve(strict=True)
        self.private_runtime_root = _validate_private_runtime_root(
            self.repository_root,
            private_runtime_root,
        )
        self.signer_pool = signer_pool
        self.rpc_endpoint = rpc_endpoint
        self.mcp_endpoint = mcp_endpoint
        self.mcp_client_factory = mcp_client_factory
        self.rpc_factory = rpc_factory
        self.api_key = resolve_llm_api_key(
            model_profile.credential_env,
            default=LLM_API_KEY_DEFAULT,
        )
        self._backends: dict[str, ProductionTaskBackend] = {}

    def _validate_manifest_identity(self, manifest: CampaignManifest) -> None:
        self.release_binding.validate_manifest(manifest)
        for slot in manifest.slots:
            if (
                slot.requested_model,
                slot.thinking_level,
                slot.model_variant_id,
                slot.model_profile_id,
                slot.model_profile_sha256,
            ) != (
                self.model_profile.requested_model,
                self.model_profile.thinking_level,
                self.model_profile.model_variant_id,
                self.model_profile.profile_id,
                self.model_profile.sha256,
            ):
                raise CampaignRuntimeError("model profile differs from a frozen campaign slot")

    def _validate_manifest(self, manifest: CampaignManifest) -> None:
        self._validate_manifest_identity(manifest)
        self._validate_signer_pool(manifest)

    def _validate_signer_pool(self, manifest: CampaignManifest) -> None:
        signed = tuple(
            slot
            for slot in manifest.slots
            if self.release_binding.execution_contract_for(slot).signer_required
        )
        expected = tuple(sorted(
            (slot.slot_id, retry_ordinal)
            for slot in signed
            for retry_ordinal in (0, 1)
        ))
        if not expected:
            if self.signer_pool is not None and self.signer_pool.entries:
                raise CampaignRuntimeError("unsigned campaign cannot carry signer leases")
            return
        if self.signer_pool is None:
            raise CampaignRuntimeError("signed campaign needs an operator-private signer pool")
        observed = tuple(
            (entry.slot_id, entry.retry_ordinal) for entry in self.signer_pool.entries
        )
        if observed != expected:
            raise CampaignRuntimeError("signer pool does not exactly cover every possible attempt")
        chain_keys = {
            (slot.chain_profile_id, slot.chain_profile_sha256) for slot in signed
        }
        if chain_keys != {(
            self.signer_pool.chain_profile_id,
            self.signer_pool.chain_profile_sha256,
        )}:
            raise CampaignRuntimeError("signer pool chain differs from the signed campaign slots")
        entries = self.signer_pool.entries
        dimensions = (
            [entry.signer_handle for entry in entries],
            [entry.public_address for entry in entries],
            [hashlib.sha256(entry.private_key.encode("ascii")).hexdigest() for entry in entries],
            [entry.lease_resource_id for entry in entries],
            [row.out_point for entry in entries for row in entry.leased_inputs],
        )
        if any(len(values) != len(set(values)) for values in dimensions):
            raise CampaignRuntimeError("signer pool reuses an identity, key, lease or input")
        by_pair: dict[tuple[str, str, str], dict[str, CampaignSlot]] = {}
        for slot in signed:
            by_pair.setdefault(
                (slot.trial_id, slot.task_id, slot.model_variant_id), {}
            )[slot.arm] = slot
        for pair in by_pair.values():
            for ordinal in (0, 1):
                capacities = [
                    tuple(sorted(
                        row.capacity_shannons
                        for row in self.signer_pool.entry_for(pair[arm].slot_id, ordinal).leased_inputs
                    ))
                    for arm in ("B", "C")
                ]
                if capacities[0] != capacities[1]:
                    raise CampaignRuntimeError("matched B/C signer leases have unequal capacity")

    def _backend(
        self,
        material: AttemptMaterial,
        intent: TaskAttemptIntent,
    ) -> ProductionTaskBackend:
        backend = ProductionTaskBackend(
            material,
            repository_root=self.repository_root,
            suite=self.release_binding.release.suite,
            model_profile=self.model_profile,
            api_key=self.api_key,
            rpc_endpoint=self.rpc_endpoint,
            mcp_endpoint=self.mcp_endpoint,
            mcp_client_factory=self.mcp_client_factory,
            rpc_factory=self.rpc_factory,
        )
        backend.bind_intent(intent)
        backend.bind_release_root(self.release_binding.release.registry_root)
        self._backends[intent.attempt_id] = backend
        return backend

    def _requirements(
        self,
        intent: TaskAttemptIntent,
        material: AttemptMaterial,
    ) -> TaskPreflightRequirements:
        qualification_kind, qualification_digest = _provider_qualification(self.model_profile)
        policy = material.signing_policy
        entry = material.signer_entry
        return TaskPreflightRequirements(
            requirements_id=f"requirements-{intent.attempt_id}",
            intent_sha256=intent.sha256,
            model_qualification_kind=qualification_kind,
            model_qualification_evidence_sha256=qualification_digest,
            model_qualification_utc=self.model_profile.evidence_utc,
            model_evidence_max_age_seconds=MAX_MODEL_EVIDENCE_AGE_SECONDS,
            provider_readiness_operation=READINESS_OPERATION,
            provider_readiness_request_limit=1,
            ckb_ai_surface_id=material.surface.profile_id,
            ckb_ai_surface_sha256=material.surface.sha256,
            ckb_ai_server_version=material.surface.server_version,
            ckb_ai_catalog_sha256=material.surface.catalog_sha256,
            ckb_ai_request_limit=7 if material.surface.claims_live_chain else 3,
            ckb_ai_claims_live_chain=material.surface.claims_live_chain,
            expected_chain_id=material.chain.chain_id,
            expected_genesis_hash=material.chain.genesis_hash,
            signer_required=material.contract.signer_required,
            expected_signer_handle=None if entry is None else entry.signer_handle,
            expected_signer_address=None if entry is None else entry.public_address,
            signing_policy_id=None if policy is None else policy.policy_id,
            signing_policy_sha256=None if policy is None else policy.sha256,
            funding=_funding_requirement(material.contract),
            required_dependencies=material.contract.dependency_evidence,
            required_resource_claims=material.resource_claims,
            expected_output_resources=material.output_resources,
        )

    @staticmethod
    def _probe(backend: ProductionTaskBackend) -> IntegratedTaskProbe:
        return IntegratedTaskProbe(
            source_call=backend.observe_source,
            provider_call=backend.observe_provider,
            ckb_ai_call=backend.observe_ckb_ai,
            rpc_call=backend.observe_rpc,
            signer_call=backend.observe_signer,
            funding_call=backend.observe_funding,
            dependencies_call=backend.observe_dependencies,
            outputs_call=backend.observe_outputs,
        )

    def prepare(
        self,
        manifest: CampaignManifest,
        slot: CampaignSlot,
        predecessor: AttemptEnvelope | None,
    ) -> PreparedTaskAttempt:
        self._validate_manifest(manifest)
        retry_ordinal = 0 if predecessor is None else 1
        attempt_id = allocate_attempt_id()
        material = _material_for(
            self.release_binding,
            self.signer_pool,
            self.private_runtime_root,
            slot,
            attempt_id,
            retry_ordinal,
        )
        identity = _identity(manifest, slot, material.params)
        intent = TaskAttemptIntent(
            attempt_id=attempt_id,
            created_utc=_utc_now(),
            identity=identity,
            retry_ordinal=retry_ordinal,
            retry=_retry_reference(predecessor),
        )
        backend = self._backend(material, intent)
        requirements = self._requirements(intent, material)
        return PreparedTaskAttempt(
            intent=intent,
            requirements=requirements,
            preflight_probe=self._probe(backend),
            backend=backend,
            max_score=slot.max_score,
        )

    def prepare_recovery(
        self,
        manifest: CampaignManifest,
        slot: CampaignSlot,
        state: AttemptState,
    ) -> tuple[TaskPreflightRequirements, SingleTaskBackend, int]:
        self._validate_manifest_identity(manifest)
        requirements = state.preflight_requirements
        if requirements is None:
            self._validate_signer_pool(manifest)
            material = _material_for(
                self.release_binding,
                self.signer_pool,
                self.private_runtime_root,
                slot,
                state.intent.attempt_id,
                state.intent.retry_ordinal,
            )
            requirements = self._requirements(state.intent, material)
        else:
            material = _material_for(
                self.release_binding,
                None,
                self.private_runtime_root,
                slot,
                state.intent.attempt_id,
                state.intent.retry_ordinal,
                recovery_requirements=requirements,
            )
        backend = self._backend(material, state.intent)
        return requirements, backend, slot.max_score
