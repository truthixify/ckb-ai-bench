"""Concrete, bounded TestNet and CKB AI preflight adapters."""

from __future__ import annotations

import json
import math
import re
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from ckbbench.run.task_preflight import (
    ChainIdentityObservation,
    CkbAiObservation,
    DependencyObservation,
    FundingObservation,
    OutputObservation,
    TaskPreflightProbe,
    ProviderObservation,
    SignerObservation,
    SourceObservation,
)
from ckbbench.run.treatment_surface import (
    TreatmentSurfaceError,
    TreatmentSurfaceProfile,
    TaskMcpSurfacePolicy,
    combined_catalog_sha256,
    normalize_resource_catalog,
    normalize_tool_catalog,
)
from ckbbench.run.task_attempt import (
    AttemptSchemaError,
    artifact_sha256,
    canonical_json_bytes,
    validate_public_artifact_values,
)
from ckbbench.verify.onchain import VerificationInfrastructureError, type_id_args

MAX_RPC_RESPONSE_BYTES = 1 << 20
MAX_MCP_TOOL_TEXT_BYTES = 1 << 16
MAX_FUNDING_CELLS = 4
MAX_SIGNING_REQUEST_BYTES = 1 << 20
MAX_TRANSACTION_INPUTS = 16
MAX_TRANSACTION_OUTPUTS = 16
MAX_TRANSACTION_DATA_BYTES = 1 << 16

_METHOD = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_HASH32 = re.compile(r"^0x[0-9a-f]{64}$")
_HEX_NUMBER = re.compile(r"^0x(?:0|[1-9a-f][0-9a-f]*)$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_SIGNING_REQUEST_FIELDS = ("transaction",)
_UNSIGNED_TRANSACTION_FIELDS = (
    "cell_deps",
    "header_deps",
    "inputs",
    "outputs",
    "outputs_data",
    "version",
    "witnesses",
)
_TRANSACTION_INPUT_FIELDS = ("previous_output", "since")
_OUT_POINT_FIELDS = ("index", "tx_hash")
_TRANSACTION_OUTPUT_FIELDS = ("capacity", "lock", "type")
_SIGNING_REFUSAL_CATEGORIES = frozenset({
    "balance",
    "cell-deps",
    "fee-floor",
    "fee-limit",
    "header-deps",
    "input-policy",
    "input-reference",
    "input-shape",
    "input-since",
    "io-shape",
    "output-capacity",
    "output-data",
    "output-lock",
    "output-shape",
    "output-type",
    "request-shape",
    "transaction-limit",
    "transaction-shape",
    "transfer-limit",
    "type-id",
    "version",
    "witness",
})
SIGNING_INFRASTRUCTURE_CATEGORIES = frozenset({
    "chain-check",
    "key-holder",
    "signed-transaction",
    "submission",
    "submission-result",
})


class TestnetIntegrationError(RuntimeError):
    """A concrete integration returned an unusable or unsafe observation."""


class SigningRequestRefused(TestnetIntegrationError):
    """A sanitized, policy-level rejection of an unsigned transaction."""

    def __init__(self, category: str) -> None:
        if category not in _SIGNING_REFUSAL_CATEGORIES:
            raise ValueError("unknown signing refusal category")
        self.category = category
        super().__init__(f"signing request violates the attempt policy ({category})")


class SigningInfrastructureError(TestnetIntegrationError):
    """A sanitized failure at a fixed signer infrastructure boundary."""

    def __init__(self, category: str) -> None:
        if category not in SIGNING_INFRASTRUCTURE_CATEGORIES:
            raise ValueError("unknown signing infrastructure category")
        self.category = category
        super().__init__(f"signer infrastructure failed ({category})")


class RpcCallable(Protocol):
    @property
    def request_count(self) -> int: ...

    def call(self, method: str, params: list[Any]) -> Any: ...


class CkbAiClient(Protocol):
    @property
    def request_count(self) -> int: ...

    def initialize(self) -> dict[str, Any]: ...

    def list_tools(self) -> list[dict[str, Any]]: ...

    def list_resources(self) -> list[dict[str, Any]]: ...

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]: ...


def _hash32(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH32.fullmatch(value) is None:
        raise TestnetIntegrationError(f"{label} is not a 32-byte hash")
    return value


def _hex_int(value: Any, label: str) -> int:
    if not isinstance(value, str) or _HEX_NUMBER.fullmatch(value) is None:
        raise TestnetIntegrationError(f"{label} is not a canonical hexadecimal number")
    return int(value, 16)


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise TestnetIntegrationError(f"{label} is not a bounded public identifier")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TestnetIntegrationError(f"{label} is not a SHA-256 digest")
    return value


def _safe_rpc_url(value: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 2048:
        raise TestnetIntegrationError("RPC endpoint is unusable")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise TestnetIntegrationError("RPC endpoint is unusable")
    return value


class HttpJsonRpcClient:
    """One no-retry, no-redirect, bounded JSON-RPC transport."""

    def __init__(
        self,
        endpoint: str,
        *,
        request_limit: int,
        timeout_seconds: float = 30.0,
        client: Any | None = None,
    ) -> None:
        if isinstance(request_limit, bool) or not isinstance(request_limit, int) or request_limit <= 0:
            raise TestnetIntegrationError("RPC request limit must be positive")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TestnetIntegrationError("RPC timeout must be positive")
        if not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) <= 0:
            raise TestnetIntegrationError("RPC timeout must be positive")
        self.endpoint = _safe_rpc_url(endpoint)
        self.request_limit = request_limit
        self.timeout_seconds = float(timeout_seconds)
        self.request_count = 0
        self._owned = client is None
        if client is None:
            import httpx

            client = httpx.Client(
                transport=httpx.HTTPTransport(retries=0),
                follow_redirects=False,
                timeout=self.timeout_seconds,
            )
        elif getattr(client, "follow_redirects", False):
            raise TestnetIntegrationError("RPC client must not follow redirects")
        self._client = client

    def close(self) -> None:
        if self._owned:
            try:
                self._client.close()
            except Exception:
                pass

    def call(self, method: str, params: list[Any]) -> Any:
        if not isinstance(method, str) or _METHOD.fullmatch(method) is None:
            raise TestnetIntegrationError("RPC method is invalid")
        if not isinstance(params, list):
            raise TestnetIntegrationError("RPC params must be an array")
        if self.request_count >= self.request_limit:
            raise TestnetIntegrationError("RPC request ceiling reached")
        request_id = self.request_count + 1
        try:
            body = json.dumps(
                {"id": request_id, "jsonrpc": "2.0", "method": method, "params": params},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        except (TypeError, ValueError):
            raise TestnetIntegrationError("RPC params are not canonical JSON") from None
        if len(body) > MAX_RPC_RESPONSE_BYTES:
            raise TestnetIntegrationError("RPC request exceeded the byte limit")
        self.request_count += 1
        try:
            with self._client.stream(
                "POST",
                self.endpoint,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                content=body,
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise TestnetIntegrationError("RPC endpoint attempted a redirect")
                if status != 200:
                    raise TestnetIntegrationError("RPC endpoint returned an unusable status")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"application/json", "application/json-rpc"}:
                    raise TestnetIntegrationError("RPC endpoint returned a non-JSON response")
                chunks: list[bytes] = []
                observed = 0
                for chunk in response.iter_bytes():
                    observed += len(chunk)
                    if observed > MAX_RPC_RESPONSE_BYTES:
                        raise TestnetIntegrationError("RPC response exceeded the byte limit")
                    chunks.append(chunk)
        except TestnetIntegrationError:
            raise
        except Exception as exc:
            raise TestnetIntegrationError(
                f"RPC transport failed ({type(exc).__name__})"
            ) from None
        try:
            envelope = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TestnetIntegrationError("RPC response was not valid JSON") from None
        if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0":
            raise TestnetIntegrationError("RPC response envelope is malformed")
        if type(envelope.get("id")) is not int or envelope["id"] != request_id:
            raise TestnetIntegrationError("RPC response ID does not match")
        has_result = "result" in envelope
        has_error = "error" in envelope
        if has_result == has_error or has_error:
            raise TestnetIntegrationError("RPC response reported an error")
        if set(envelope) != {"id", "jsonrpc", "result"}:
            raise TestnetIntegrationError("RPC response envelope has unexpected fields")
        return envelope["result"]


class DirectChainProbe:
    """Observe stable identity and one coherent tip from direct CKB RPC."""

    def __init__(self, rpc: RpcCallable) -> None:
        self.rpc = rpc

    def observe(self) -> ChainIdentityObservation:
        before = self.rpc.request_count
        info = self.rpc.call("get_blockchain_info", [])
        genesis = self.rpc.call("get_block_hash", ["0x0"])
        header = self.rpc.call("get_tip_header", [])
        if not isinstance(info, dict) or not isinstance(header, dict):
            raise TestnetIntegrationError("direct RPC returned a malformed chain observation")
        chain_id = _identifier(info.get("chain"), "direct chain ID")
        genesis_hash = _hash32(genesis, "direct genesis")
        tip_number_raw = header.get("number")
        tip_hash = _hash32(header.get("hash"), "direct tip hash")
        tip_number = _hex_int(tip_number_raw, "direct tip number")
        confirmed_tip_hash = self.rpc.call("get_block_hash", [tip_number_raw])
        if _hash32(confirmed_tip_hash, "direct confirmed tip hash") != tip_hash:
            raise TestnetIntegrationError("direct RPC returned an incoherent tip")
        return ChainIdentityObservation(
            chain_id=chain_id,
            genesis_hash=genesis_hash,
            tip_number=tip_number,
            tip_hash=tip_hash,
            request_count=self.rpc.request_count - before,
        )


def _mcp_text(result: Any) -> str:
    if not isinstance(result, dict) or result.get("isError") is True:
        raise TestnetIntegrationError("CKB AI identity tool failed")
    content = result.get("content")
    if not isinstance(content, list):
        raise TestnetIntegrationError("CKB AI identity tool returned malformed content")
    parts = []
    observed = 0
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            observed += len(block["text"].encode("utf-8"))
            if observed > MAX_MCP_TOOL_TEXT_BYTES:
                raise TestnetIntegrationError("CKB AI identity tool returned unusable text")
            parts.append(block["text"])
    text = "\n".join(parts).strip()
    if not text or len(text.encode("utf-8")) > MAX_MCP_TOOL_TEXT_BYTES:
        raise TestnetIntegrationError("CKB AI identity tool returned unusable text")
    return text


def _json_or_scalar(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class CkbAiPreflightAdapter:
    """Validate the frozen product catalog and, when declared, its TestNet identity."""

    def __init__(self, client: CkbAiClient, profile: TreatmentSurfaceProfile) -> None:
        self.client = client
        self.profile = profile

    def observe(self) -> CkbAiObservation:
        before = self.client.request_count

        def request(call: Callable[[], Any]) -> Any:
            previous = self.client.request_count
            result = call()
            if self.client.request_count != previous + 1:
                raise TestnetIntegrationError("CKB AI client request accounting is invalid")
            return result

        initialized = request(self.client.initialize)
        tools = request(self.client.list_tools)
        resources = request(self.client.list_resources)
        normalized_tools = normalize_tool_catalog(tools)
        normalized_resources = normalize_resource_catalog(resources)
        tool_digest = artifact_sha256({"tools": list(normalized_tools)})
        resource_digest = artifact_sha256({"resources": list(normalized_resources)})
        catalog_digest = combined_catalog_sha256(tool_digest, resource_digest)

        server_info = initialized.get("serverInfo") if isinstance(initialized, dict) else None
        server_name = server_info.get("name") if isinstance(server_info, dict) else None
        server_version = server_info.get("version") if isinstance(server_info, dict) else None
        valid_server = (
            server_name == self.profile.server_name
            and server_version == self.profile.server_version
        )
        valid_catalog = (
            tool_digest == self.profile.tool_catalog_sha256
            and resource_digest == self.profile.resource_catalog_sha256
            and catalog_digest == self.profile.catalog_sha256
        )
        try:
            policy = TaskMcpSurfacePolicy(self.profile)
            policy.filter_tools(tools)
            policy.validate_resources(resources)
            valid_surface = True
        except TreatmentSurfaceError:
            valid_surface = False

        if not (valid_server and valid_catalog and valid_surface):
            return CkbAiObservation(
                surface_id=self.profile.profile_id,
                surface_sha256=self.profile.sha256,
                server_version=(
                    server_version
                    if isinstance(server_version, str) and _ID.fullmatch(server_version)
                    else self.profile.server_version
                ),
                catalog_sha256=catalog_digest,
                ready=False,
                request_count=self.client.request_count - before,
                chain_identity=None,
            )
        if not self.profile.claims_live_chain:
            return CkbAiObservation(
                surface_id=self.profile.profile_id,
                surface_sha256=self.profile.sha256,
                server_version=self.profile.server_version,
                catalog_sha256=catalog_digest,
                ready=True,
                request_count=self.client.request_count - before,
                chain_identity=None,
            )

        chain_info = _json_or_scalar(_mcp_text(request(lambda: self.client.call_tool(
            "rpc_get_blockchain_info", {}
        ))))
        genesis = _json_or_scalar(_mcp_text(request(lambda: self.client.call_tool(
            "dev_get_genesis_hash", {}
        ))))
        tip_raw = _json_or_scalar(_mcp_text(request(lambda: self.client.call_tool(
            "rpc_get_tip_block_number", {}
        ))))
        if not isinstance(chain_info, dict):
            raise TestnetIntegrationError("CKB AI chain info is malformed")
        tip_number = _hex_int(tip_raw, "CKB AI tip number")
        tip_hash = _json_or_scalar(_mcp_text(request(lambda: self.client.call_tool(
            "rpc_get_block_hash", {"block_number": tip_number}
        ))))
        chain = ChainIdentityObservation(
            chain_id=_identifier(chain_info.get("chain"), "CKB AI chain ID"),
            genesis_hash=_hash32(genesis, "CKB AI genesis"),
            tip_number=tip_number,
            tip_hash=_hash32(tip_hash, "CKB AI tip hash"),
            request_count=4,
        )
        return CkbAiObservation(
            surface_id=self.profile.profile_id,
            surface_sha256=self.profile.sha256,
            server_version=self.profile.server_version,
            catalog_sha256=catalog_digest,
            ready=True,
            request_count=self.client.request_count - before,
            chain_identity=chain,
        )


@dataclass(frozen=True)
class SignerInspection:
    signer_handle: str
    public_address: str
    signing_policy_id: str
    signing_policy_sha256: str
    chain_identity_sha256: str
    single_assignment: bool
    agent_accessible: bool
    check_count: int


class ConstrainedSigner(Protocol):
    def inspect(self) -> SignerInspection: ...

    def sign_and_submit(self, request: dict[str, Any]) -> dict[str, Any]: ...


class TransactionKeyHolder(Protocol):
    """Private key boundary. Implementations return a signed copy and expose no key material."""

    def sign_transaction(self, transaction: dict[str, Any]) -> dict[str, Any]: ...


class SignerPreflightAdapter:
    def __init__(self, signer: ConstrainedSigner) -> None:
        self.signer = signer

    def observe(self) -> SignerObservation:
        inspected = self.signer.inspect()
        if type(inspected) is not SignerInspection:
            raise TestnetIntegrationError("signer inspection is malformed")
        return SignerObservation(
            signer_handle=inspected.signer_handle,
            public_address=inspected.public_address,
            signing_policy_id=inspected.signing_policy_id,
            signing_policy_sha256=inspected.signing_policy_sha256,
            chain_identity_sha256=inspected.chain_identity_sha256,
            single_assignment=inspected.single_assignment,
            agent_accessible=inspected.agent_accessible,
            check_count=inspected.check_count,
        )


def _canonical_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TestnetIntegrationError(f"{label} must be an object")
    try:
        encoded = canonical_json_bytes(value)
    except Exception:
        raise TestnetIntegrationError(f"{label} must be canonical JSON") from None
    if len(encoded) > MAX_SIGNING_REQUEST_BYTES:
        raise TestnetIntegrationError(f"{label} exceeds the byte limit")
    return json.loads(encoded)


def _script(value: Any, label: str) -> dict[str, Any]:
    row = _canonical_object(value, label)
    if set(row) != {"args", "code_hash", "hash_type"}:
        raise TestnetIntegrationError(f"{label} has an unsupported shape")
    _hash32(row["code_hash"], f"{label} code hash")
    if row["hash_type"] not in {"data", "data1", "data2", "type"}:
        raise TestnetIntegrationError(f"{label} hash type is unsupported")
    args = row["args"]
    if not isinstance(args, str) or re.fullmatch(r"0x(?:[0-9a-f]{2})*", args) is None:
        raise TestnetIntegrationError(f"{label} args are not canonical bytes")
    return row


def _script_sha256(value: dict[str, Any]) -> str:
    return artifact_sha256({"script": value})


@dataclass(frozen=True)
class LeasedSignerInput:
    tx_hash: str
    index: int
    capacity_shannons: int

    def __post_init__(self) -> None:
        _hash32(self.tx_hash, "signer input transaction hash")
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise TestnetIntegrationError("signer input index is invalid")
        if (
            isinstance(self.capacity_shannons, bool)
            or not isinstance(self.capacity_shannons, int)
            or self.capacity_shannons <= 0
        ):
            raise TestnetIntegrationError("signer input capacity must be positive")

    @property
    def out_point(self) -> tuple[str, int]:
        return self.tx_hash, self.index

    def to_dict(self) -> dict[str, Any]:
        return {
            "capacity_shannons": self.capacity_shannons,
            "index": self.index,
            "tx_hash": self.tx_hash,
        }


@dataclass(frozen=True)
class TypeIdOutputConstraint:
    code_hash: str
    hash_type: str
    output_index: int

    def __post_init__(self) -> None:
        _hash32(self.code_hash, "Type-ID constraint code hash")
        if self.hash_type != "type":
            raise TestnetIntegrationError("Type-ID constraint hash type must be type")
        if (
            isinstance(self.output_index, bool)
            or not isinstance(self.output_index, int)
            or not 0 <= self.output_index < MAX_TRANSACTION_OUTPUTS
        ):
            raise TestnetIntegrationError("Type-ID constraint output index is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_hash": self.code_hash,
            "hash_type": self.hash_type,
            "output_index": self.output_index,
        }


@dataclass(frozen=True)
class SigningPolicy:
    """Exact public policy enforced before an attempt-owned key can sign anything."""

    policy_id: str
    signer_handle: str
    public_address: str
    chain_identity_sha256: str
    leased_inputs: tuple[LeasedSignerInput, ...]
    own_lock: dict[str, Any]
    permitted_destination_locks: tuple[dict[str, Any], ...]
    permitted_output_types: tuple[dict[str, Any] | None, ...]
    cell_deps: tuple[dict[str, Any], ...]
    header_deps: tuple[str, ...]
    maximum_transfer_shannons: int
    minimum_fee_shannons: int
    maximum_fee_shannons: int
    maximum_transactions: int
    maximum_output_data_bytes: int
    required_type_id_output: TypeIdOutputConstraint | None = None

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "signing policy ID")
        _identifier(self.signer_handle, "signer handle")
        _identifier(self.public_address, "signer public address")
        _sha(self.chain_identity_sha256, "signing policy chain identity")
        if (
            not isinstance(self.leased_inputs, tuple)
            or not self.leased_inputs
            or len(self.leased_inputs) > MAX_TRANSACTION_INPUTS
            or not all(type(row) is LeasedSignerInput for row in self.leased_inputs)
        ):
            raise TestnetIntegrationError("signing policy needs bounded typed inputs")
        points = tuple(row.out_point for row in self.leased_inputs)
        if points != tuple(sorted(set(points))):
            raise TestnetIntegrationError("signing policy inputs must be unique and sorted")
        own_lock = _script(self.own_lock, "signing policy own lock")
        if not isinstance(self.permitted_destination_locks, tuple):
            raise TestnetIntegrationError("signing policy destination locks must be immutable")
        destinations = tuple(
            _script(row, "signing policy destination lock")
            for row in self.permitted_destination_locks
        )
        destination_digests = tuple(_script_sha256(row) for row in destinations)
        if destination_digests != tuple(sorted(set(destination_digests))):
            raise TestnetIntegrationError("signing policy destination locks must be unique and sorted")
        if _script_sha256(own_lock) in destination_digests:
            raise TestnetIntegrationError("change lock cannot also be a destination lock")
        if not isinstance(self.permitted_output_types, tuple) or not self.permitted_output_types:
            raise TestnetIntegrationError("signing policy needs output type constraints")
        output_types = tuple(
            None if row is None else _script(row, "signing policy output type")
            for row in self.permitted_output_types
        )
        type_digests = tuple(
            "none" if row is None else _script_sha256(row) for row in output_types
        )
        if type_digests != tuple(sorted(set(type_digests))):
            raise TestnetIntegrationError("signing policy output types must be unique and sorted")
        if not isinstance(self.cell_deps, tuple) or not isinstance(self.header_deps, tuple):
            raise TestnetIntegrationError("signing policy dependencies must be immutable")
        normalized_cell_deps = tuple(
            _canonical_object(row, "signing policy cell dependency") for row in self.cell_deps
        )
        normalized_header_deps = tuple(
            _hash32(row, "signing policy header dependency") for row in self.header_deps
        )
        if tuple(canonical_json_bytes(row) for row in normalized_cell_deps) != tuple(sorted(set(
            canonical_json_bytes(row) for row in normalized_cell_deps
        ))):
            raise TestnetIntegrationError("signing policy cell dependencies must be unique and sorted")
        if normalized_header_deps != tuple(sorted(set(normalized_header_deps))):
            raise TestnetIntegrationError("signing policy header dependencies must be unique and sorted")
        for field_name in ("maximum_transfer_shannons", "maximum_output_data_bytes"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TestnetIntegrationError(
                    f"signing policy {field_name} must be non-negative"
                )
        for field_name in (
            "minimum_fee_shannons",
            "maximum_fee_shannons",
            "maximum_transactions",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise TestnetIntegrationError(f"signing policy {field_name} must be positive")
        if self.minimum_fee_shannons > self.maximum_fee_shannons:
            raise TestnetIntegrationError("signing policy fee floor exceeds its ceiling")
        if bool(destinations) != (self.maximum_transfer_shannons > 0):
            raise TestnetIntegrationError(
                "signing policy destinations and transfer ceiling must agree"
            )
        if (
            self.maximum_transfer_shannons + self.maximum_fee_shannons
            > sum(row.capacity_shannons for row in self.leased_inputs)
        ):
            raise TestnetIntegrationError("signing policy exceeds its leased capacity")
        if self.maximum_transactions > len(self.leased_inputs):
            raise TestnetIntegrationError("signing policy transaction ceiling exceeds its inputs")
        if self.maximum_output_data_bytes > MAX_TRANSACTION_DATA_BYTES:
            raise TestnetIntegrationError("signing policy output data ceiling is too high")
        if (
            self.required_type_id_output is not None
            and type(self.required_type_id_output) is not TypeIdOutputConstraint
        ):
            raise TestnetIntegrationError("signing policy Type-ID constraint must be typed")
        object.__setattr__(self, "own_lock", own_lock)
        object.__setattr__(self, "permitted_destination_locks", destinations)
        object.__setattr__(self, "permitted_output_types", output_types)
        object.__setattr__(self, "cell_deps", normalized_cell_deps)
        object.__setattr__(self, "header_deps", normalized_header_deps)
        try:
            validate_public_artifact_values(self.to_dict())
        except AttemptSchemaError:
            raise TestnetIntegrationError("signing policy contains secret-shaped data") from None

    def to_dict(self) -> dict[str, Any]:
        unsigned_template = {
            "cell_deps": [deepcopy(row) for row in self.cell_deps],
            "header_deps": list(self.header_deps),
            "inputs": [
                {
                    "previous_output": {
                        "index": hex(row.index),
                        "tx_hash": row.tx_hash,
                    },
                    "since": "0x0",
                }
                for row in self.leased_inputs
            ],
            "outputs": [],
            "outputs_data": [],
            "version": "0x0",
            "witnesses": ["0x" for _row in self.leased_inputs],
        }
        document = {
            "cell_deps": [deepcopy(row) for row in self.cell_deps],
            "chain_identity_sha256": self.chain_identity_sha256,
            "header_deps": list(self.header_deps),
            "leased_inputs": [row.to_dict() for row in self.leased_inputs],
            "maximum_fee_shannons": self.maximum_fee_shannons,
            "maximum_output_data_bytes": self.maximum_output_data_bytes,
            "maximum_transactions": self.maximum_transactions,
            "maximum_transfer_shannons": self.maximum_transfer_shannons,
            "minimum_fee_shannons": self.minimum_fee_shannons,
            "own_lock": deepcopy(self.own_lock),
            "permitted_destination_locks": [
                deepcopy(row) for row in self.permitted_destination_locks
            ],
            "permitted_output_types": [
                None if row is None else deepcopy(row) for row in self.permitted_output_types
            ],
            "policy_id": self.policy_id,
            "public_address": self.public_address,
            "request_format": {
                "input_keys": list(_TRANSACTION_INPUT_FIELDS),
                "integer_encoding": "canonical-lowercase-0x-hex",
                "output_data_encoding": "0x-prefixed-even-length-lowercase-hex",
                "output_keys": list(_TRANSACTION_OUTPUT_FIELDS),
                "outputs_data_count": "exactly-one-per-output",
                "previous_output_keys": list(_OUT_POINT_FIELDS),
                "request_keys": list(_SIGNING_REQUEST_FIELDS),
                "schema_version": "ckbbench-signing-request-v1",
                "transaction_keys": list(_UNSIGNED_TRANSACTION_FIELDS),
                "unsigned_transaction_template": unsigned_template,
                "witness_rule": "at-least-one-per-input; use-0x-placeholder",
            },
            "signer_handle": self.signer_handle,
        }
        if self.required_type_id_output is not None:
            document["required_type_id_output"] = self.required_type_id_output.to_dict()
        return document

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())


class PolicyConstrainedSigner:
    """Fail-closed broker for one attempt-owned key and one immutable signing policy."""

    def __init__(
        self,
        policy: SigningPolicy,
        key_holder: TransactionKeyHolder,
        submit_rpc: RpcCallable,
    ) -> None:
        if type(policy) is not SigningPolicy:
            raise TestnetIntegrationError("signer policy must be typed")
        self.policy = policy
        self.key_holder = key_holder
        self.submit_rpc = submit_rpc
        self.protocol_violation_count = 0
        self._attempted_transactions = 0
        self._used_inputs: set[tuple[str, int]] = set()
        self._transferred_shannons = 0
        self._fee_shannons = 0
        self._lock = threading.Lock()

    def inspect(self) -> SignerInspection:
        return SignerInspection(
            signer_handle=self.policy.signer_handle,
            public_address=self.policy.public_address,
            signing_policy_id=self.policy.policy_id,
            signing_policy_sha256=self.policy.sha256,
            chain_identity_sha256=self.policy.chain_identity_sha256,
            single_assignment=True,
            agent_accessible=False,
            check_count=1,
        )

    def _refuse(self, category: str) -> None:
        self.protocol_violation_count += 1
        raise SigningRequestRefused(category)

    def _verify_submission_chain(self) -> None:
        chain_check_failed = False
        try:
            observed = DirectChainProbe(self.submit_rpc).observe()
        except Exception:
            chain_check_failed = True
            observed = None
        if chain_check_failed:
            raise SigningInfrastructureError("chain-check")
        if observed.stable_identity_sha256 != self.policy.chain_identity_sha256:
            raise SigningInfrastructureError("chain-check")

    def _validate_transaction(
        self,
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], set[tuple[str, int]], int, int]:
        if self._attempted_transactions >= self.policy.maximum_transactions:
            self._refuse("transaction-limit")
        try:
            row = _canonical_object(request, "signing request")
        except Exception:
            self._refuse("request-shape")
        if set(row) != set(_SIGNING_REQUEST_FIELDS):
            self._refuse("request-shape")
        try:
            transaction = _canonical_object(row["transaction"], "unsigned transaction")
        except Exception:
            self._refuse("transaction-shape")
        if set(transaction) != set(_UNSIGNED_TRANSACTION_FIELDS):
            self._refuse("transaction-shape")
        if transaction["version"] != "0x0":
            self._refuse("version")
        if transaction["cell_deps"] != list(self.policy.cell_deps):
            self._refuse("cell-deps")
        if transaction["header_deps"] != list(self.policy.header_deps):
            self._refuse("header-deps")
        inputs = transaction["inputs"]
        outputs = transaction["outputs"]
        output_data = transaction["outputs_data"]
        witnesses = transaction["witnesses"]
        if (
            not isinstance(inputs, list)
            or not inputs
            or len(inputs) > MAX_TRANSACTION_INPUTS
            or not isinstance(outputs, list)
            or not outputs
            or len(outputs) > MAX_TRANSACTION_OUTPUTS
            or not isinstance(output_data, list)
            or len(output_data) != len(outputs)
            or not isinstance(witnesses, list)
            or len(witnesses) < len(inputs)
        ):
            self._refuse("io-shape")

        capacities = {row.out_point: row.capacity_shannons for row in self.policy.leased_inputs}
        used: set[tuple[str, int]] = set()
        for tx_input in inputs:
            if (
                not isinstance(tx_input, dict)
                or set(tx_input) != set(_TRANSACTION_INPUT_FIELDS)
            ):
                self._refuse("input-shape")
            if tx_input["since"] != "0x0":
                self._refuse("input-since")
            point = tx_input["previous_output"]
            if not isinstance(point, dict) or set(point) != set(_OUT_POINT_FIELDS):
                self._refuse("input-shape")
            try:
                out_point = (
                    _hash32(point["tx_hash"], "input transaction hash"),
                    _hex_int(point["index"], "input index"),
                )
            except Exception:
                self._refuse("input-reference")
            if out_point not in capacities or out_point in used or out_point in self._used_inputs:
                self._refuse("input-policy")
            used.add(out_point)

        own_lock_digest = _script_sha256(self.policy.own_lock)
        destination_digests = {
            _script_sha256(row) for row in self.policy.permitted_destination_locks
        }
        type_digests = {
            "none" if row is None else _script_sha256(row)
            for row in self.policy.permitted_output_types
        }
        type_id_constraint = self.policy.required_type_id_output
        required_type_id_digest = None
        if type_id_constraint is not None:
            try:
                args = type_id_args(inputs[0], type_id_constraint.output_index)
            except VerificationInfrastructureError:
                self._refuse("type-id")
            required_type_id_digest = _script_sha256({
                "args": "0x" + args.hex(),
                "code_hash": type_id_constraint.code_hash,
                "hash_type": type_id_constraint.hash_type,
            })
            if type_id_constraint.output_index >= len(outputs):
                self._refuse("type-id")
        total_output = 0
        transfer = 0
        for output_index, output in enumerate(outputs):
            if (
                not isinstance(output, dict)
                or set(output) != set(_TRANSACTION_OUTPUT_FIELDS)
            ):
                self._refuse("output-shape")
            try:
                capacity = _hex_int(output["capacity"], "output capacity")
            except Exception:
                self._refuse("output-capacity")
            if capacity == 0:
                self._refuse("output-capacity")
            try:
                lock_digest = _script_sha256(_script(output["lock"], "output lock"))
            except Exception:
                self._refuse("output-lock")
            output_type = output["type"]
            try:
                type_digest = (
                    "none"
                    if output_type is None
                    else _script_sha256(_script(output_type, "output type"))
                )
            except Exception:
                self._refuse("output-type")
            if lock_digest != own_lock_digest and lock_digest not in destination_digests:
                self._refuse("output-lock")
            if type_id_constraint is not None and output_index == type_id_constraint.output_index:
                if type_digest != required_type_id_digest:
                    self._refuse("type-id")
            elif type_digest not in type_digests:
                self._refuse("output-type")
            total_output += capacity
            if lock_digest != own_lock_digest:
                transfer += capacity
        data_bytes = 0
        for value in output_data:
            if not isinstance(value, str) or re.fullmatch(r"0x(?:[0-9a-f]{2})*", value) is None:
                self._refuse("output-data")
            data_bytes += (len(value) - 2) // 2
        if data_bytes > self.policy.maximum_output_data_bytes:
            self._refuse("output-data")
        for value in witnesses:
            if not isinstance(value, str) or re.fullmatch(r"0x(?:[0-9a-f]{2})*", value) is None:
                self._refuse("witness")
        total_input = sum(capacities[point] for point in used)
        fee = total_input - total_output
        if fee < 0:
            self._refuse("balance")
        if fee < self.policy.minimum_fee_shannons:
            self._refuse("fee-floor")
        if self._transferred_shannons + transfer > self.policy.maximum_transfer_shannons:
            self._refuse("transfer-limit")
        if self._fee_shannons + fee > self.policy.maximum_fee_shannons:
            self._refuse("fee-limit")
        return transaction, used, transfer, fee

    def sign_and_submit(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            transaction, inputs, transfer, fee = self._validate_transaction(request)
            self._verify_submission_chain()
            self._attempted_transactions += 1
            self._used_inputs.update(inputs)
            self._transferred_shannons += transfer
            self._fee_shannons += fee
            unsigned_core = {key: value for key, value in transaction.items() if key != "witnesses"}
            signing_failed = False
            try:
                raw_signed = self.key_holder.sign_transaction(deepcopy(transaction))
            except Exception:
                signing_failed = True
                raw_signed = None
            if signing_failed:
                raise SigningInfrastructureError("key-holder")
            try:
                signed = _canonical_object(raw_signed, "signed transaction")
            except Exception:
                raise SigningInfrastructureError("signed-transaction") from None
            signed_witnesses = signed.get("witnesses")
            if (
                set(signed) != set(transaction)
                or not isinstance(signed_witnesses, list)
                or len(signed_witnesses) != len(transaction["witnesses"])
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"0x(?:[0-9a-f]{2})*", value) is None
                    for value in signed_witnesses
                )
            ):
                raise SigningInfrastructureError("signed-transaction") from None
            signed_core = {key: value for key, value in signed.items() if key != "witnesses"}
            if signed_core != unsigned_core:
                raise SigningInfrastructureError("signed-transaction") from None
            submission_failed = False
            try:
                tx_hash = self.submit_rpc.call("send_transaction", [signed, "passthrough"])
            except Exception:
                submission_failed = True
                tx_hash = None
            if submission_failed:
                raise SigningInfrastructureError("submission")
            result_failed = False
            try:
                normalized_hash = _hash32(tx_hash, "submitted transaction hash")
            except Exception:
                result_failed = True
                normalized_hash = None
            if result_failed:
                raise SigningInfrastructureError("submission-result")
            return {"tx_hash": normalized_hash}


@dataclass(frozen=True)
class CellLease:
    lease_resource_id: str
    signer_handle: str
    lock_script: dict[str, Any]
    out_points: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _identifier(self.lease_resource_id, "cell lease ID")
        _identifier(self.signer_handle, "cell lease signer")
        lock_script = _script(self.lock_script, "cell lease lock script")
        if (
            not isinstance(self.out_points, tuple)
            or not self.out_points
            or len(self.out_points) > MAX_FUNDING_CELLS
        ):
            raise TestnetIntegrationError("cell lease needs a bounded immutable out-point set")
        checked = []
        for item in self.out_points:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TestnetIntegrationError("leased out-points must be immutable pairs")
            tx_hash, index = item
            checked.append((_hash32(tx_hash, "leased transaction hash"), index))
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise TestnetIntegrationError("leased output index is invalid")
        if tuple(checked) != tuple(sorted(set(checked))):
            raise TestnetIntegrationError("leased out-points must be unique and sorted")
        object.__setattr__(self, "lock_script", lock_script)


class FundingPreflightAdapter:
    """Inspect only pre-leased live cells; never discover, fund or replace them."""

    def __init__(
        self,
        rpc: RpcCallable,
        lease: CellLease,
        policy: SigningPolicy,
        chain: ChainIdentityObservation,
    ) -> None:
        if type(policy) is not SigningPolicy:
            raise TestnetIntegrationError("funding inspection needs a typed signing policy")
        if (
            lease.signer_handle != policy.signer_handle
            or lease.lock_script != policy.own_lock
            or lease.out_points != tuple(row.out_point for row in policy.leased_inputs)
            or policy.chain_identity_sha256 != chain.stable_identity_sha256
        ):
            raise TestnetIntegrationError("cell lease does not match the signing policy")
        self.rpc = rpc
        self.lease = lease
        self.policy = policy
        self.chain = chain

    def observe(self) -> FundingObservation:
        before = self.rpc.request_count
        rows = []
        expected_capacity = {
            row.out_point: row.capacity_shannons for row in self.policy.leased_inputs
        }
        for tx_hash, index in self.lease.out_points:
            out_point = {"tx_hash": tx_hash, "index": hex(index)}
            live = self.rpc.call("get_live_cell", [out_point, True])
            transaction = self.rpc.call("get_transaction", [tx_hash])
            if not isinstance(live, dict) or live.get("status") != "live":
                raise TestnetIntegrationError("a leased input is not live")
            cell = live.get("cell")
            output = cell.get("output") if isinstance(cell, dict) else None
            if not isinstance(output, dict) or output.get("lock") != self.lease.lock_script:
                raise TestnetIntegrationError("a leased input has the wrong lock")
            if output.get("type") is not None:
                raise TestnetIntegrationError("a leased input is not a plain capacity cell")
            data = cell.get("data") if isinstance(cell, dict) else None
            if not isinstance(data, dict) or data.get("content") != "0x":
                raise TestnetIntegrationError("a leased input is not a plain capacity cell")
            capacity = _hex_int(output.get("capacity"), "leased cell capacity")
            if capacity != expected_capacity[(tx_hash, index)]:
                raise TestnetIntegrationError("a leased input has unexpected capacity")
            status = transaction.get("tx_status") if isinstance(transaction, dict) else None
            if not isinstance(status, dict) or status.get("status") != "committed":
                raise TestnetIntegrationError("a leased input is not committed")
            block_number = _hex_int(status.get("block_number"), "leased cell block number")
            if block_number > self.chain.tip_number:
                raise TestnetIntegrationError("a leased input is ahead of the observed tip")
            confirmations = self.chain.tip_number - block_number + 1
            rows.append({
                "capacity_shannons": capacity,
                "confirmations": confirmations,
                "out_point": out_point,
                "plain_capacity": True,
            })
        minimum_confirmations = min(row["confirmations"] for row in rows)
        return FundingObservation(
            signer_handle=self.lease.signer_handle,
            lease_resource_id=self.lease.lease_resource_id,
            chain_identity_sha256=self.chain.stable_identity_sha256,
            spendable_capacity_shannons=sum(row["capacity_shannons"] for row in rows),
            cell_count=len(rows),
            minimum_confirmations=minimum_confirmations,
            cells_sha256=artifact_sha256({"cells": rows}),
            request_count=self.rpc.request_count - before,
        )


@dataclass(frozen=True)
class DeploymentRequirement:
    dependency_id: str
    out_point: tuple[str, int]
    expected_cell_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.dependency_id, "dependency ID")
        if not isinstance(self.out_point, tuple) or len(self.out_point) != 2:
            raise TestnetIntegrationError("dependency out-point must be an immutable pair")
        tx_hash, index = self.out_point
        _hash32(tx_hash, "dependency transaction hash")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise TestnetIntegrationError("dependency output index is invalid")
        _sha(self.expected_cell_sha256, "dependency cell digest")


class DependencyPreflightAdapter:
    def __init__(
        self,
        requirements: tuple[DeploymentRequirement, ...],
        *,
        rpc: RpcCallable | None,
        chain: ChainIdentityObservation | None,
    ) -> None:
        if not isinstance(requirements, tuple):
            raise TestnetIntegrationError("dependency requirements must be immutable")
        if not all(type(row) is DeploymentRequirement for row in requirements):
            raise TestnetIntegrationError("dependency requirements must be typed")
        identities = tuple(row.dependency_id for row in requirements)
        if identities != tuple(sorted(set(identities))):
            raise TestnetIntegrationError("dependency requirements must be unique and sorted")
        if (rpc is None) != (chain is None):
            raise TestnetIntegrationError("dependency RPC and chain must be present together")
        if rpc is None and requirements:
            raise TestnetIntegrationError(
                "local-hermetic tasks cannot declare deployed chain dependencies"
            )
        self.requirements = requirements
        self.rpc = rpc
        self.chain = chain

    def observe(self) -> DependencyObservation:
        if self.rpc is None:
            return DependencyObservation(
                dependencies=tuple(
                    (row.dependency_id, row.expected_cell_sha256) for row in self.requirements
                ),
                chain_identity_sha256=None,
                request_count=0,
            )
        before = self.rpc.request_count
        observed = []
        for requirement in self.requirements:
            tx_hash, index = requirement.out_point
            result = self.rpc.call("get_live_cell", [{
                "tx_hash": tx_hash,
                "index": hex(index),
            }, True])
            if not isinstance(result, dict) or result.get("status") != "live":
                raise TestnetIntegrationError("a deployed dependency is not live")
            cell = result.get("cell")
            if not isinstance(cell, dict):
                raise TestnetIntegrationError("a deployed dependency is malformed")
            actual = artifact_sha256({"cell": cell})
            observed.append((requirement.dependency_id, actual))
        return DependencyObservation(
            dependencies=tuple(sorted(observed)),
            chain_identity_sha256=self.chain.stable_identity_sha256,
            request_count=self.rpc.request_count - before,
        )


@dataclass(frozen=True)
class OutputTarget:
    resource_kind: str
    resource_id: str
    path: Path | None
    available: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        _identifier(self.resource_kind, "output resource kind")
        _identifier(self.resource_id, "output resource ID")
        if (self.path is None) == (self.available is None):
            raise TestnetIntegrationError("output target needs exactly one availability mechanism")
        if self.available is not None and not callable(self.available):
            raise TestnetIntegrationError("output availability check must be callable")


class OutputPreflightAdapter:
    def __init__(self, targets: tuple[OutputTarget, ...]) -> None:
        if not isinstance(targets, tuple) or not targets:
            raise TestnetIntegrationError("output targets must be a non-empty immutable sequence")
        if not all(type(row) is OutputTarget for row in targets):
            raise TestnetIntegrationError("output targets must be typed")
        identities = tuple((row.resource_kind, row.resource_id) for row in targets)
        if identities != tuple(sorted(set(identities))):
            raise TestnetIntegrationError("output targets must be unique and sorted")
        self.targets = targets

    def observe(self) -> OutputObservation:
        fresh = True
        symlinks = 0
        foreign = 0
        checks = 0
        for target in self.targets:
            checks += 1
            if target.path is not None:
                path = target.path
                if path.is_symlink():
                    symlinks += 1
                    fresh = False
                elif path.exists():
                    fresh = False
                parent = path.parent
                while parent != parent.parent:
                    if parent.is_symlink():
                        symlinks += 1
                        fresh = False
                        break
                    parent = parent.parent
            else:
                try:
                    available = target.available()
                except Exception:
                    available = False
                if type(available) is not bool or not available:
                    foreign += 1
                    fresh = False
        return OutputObservation(
            resources=tuple((row.resource_kind, row.resource_id) for row in self.targets),
            fresh=fresh,
            symlink_count=symlinks,
            foreign_owner_count=foreign,
            check_count=checks,
        )


@dataclass(frozen=True)
class IntegratedTaskProbe(TaskPreflightProbe):
    source_call: Callable[[float | None], SourceObservation]
    provider_call: Callable[[float | None], ProviderObservation]
    ckb_ai_call: Callable[[float | None], CkbAiObservation]
    rpc_call: Callable[[float | None], ChainIdentityObservation]
    signer_call: Callable[[float | None], SignerObservation]
    funding_call: Callable[[float | None], FundingObservation]
    dependencies_call: Callable[[float | None], DependencyObservation]
    outputs_call: Callable[[float | None], OutputObservation]

    def source(self, *, timeout_seconds: float | None) -> SourceObservation:
        return self.source_call(timeout_seconds)

    def provider(self, *, timeout_seconds: float | None) -> ProviderObservation:
        return self.provider_call(timeout_seconds)

    def ckb_ai(self, *, timeout_seconds: float | None) -> CkbAiObservation:
        return self.ckb_ai_call(timeout_seconds)

    def rpc(self, *, timeout_seconds: float | None) -> ChainIdentityObservation:
        return self.rpc_call(timeout_seconds)

    def signer(self, *, timeout_seconds: float | None) -> SignerObservation:
        return self.signer_call(timeout_seconds)

    def funding(self, *, timeout_seconds: float | None) -> FundingObservation:
        return self.funding_call(timeout_seconds)

    def dependencies(self, *, timeout_seconds: float | None) -> DependencyObservation:
        return self.dependencies_call(timeout_seconds)

    def outputs(self, *, timeout_seconds: float | None) -> OutputObservation:
        return self.outputs_call(timeout_seconds)
