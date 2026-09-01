from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from ckbbench.run.task_attempt import artifact_sha256
from ckbbench.run.task_preflight import (
    ChainIdentityObservation,
    DependencyObservation,
    OutputObservation,
    run_task_preflight,
)
from ckbbench.run.testnet_integration import (
    CkbAiPreflightAdapter,
    CellLease,
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
    SignerPreflightAdapter,
    SigningPolicy,
    TestnetIntegrationError as IntegrationError,
)
from ckbbench.run.treatment_surface import TreatmentSurfaceProfile


GENESIS = "0x" + "1" * 64
TIP = "0x" + "2" * 64
INPUT_TX = "0x" + "3" * 64
DEP_TX = "0x" + "4" * 64
SUBMITTED_TX = "0x" + "5" * 64
OTHER_TX = "0x" + "6" * 64
CHAIN_ID = "ckb_testnet"


def _rpc_response(request: httpx.Request, result: Any, **extra: Any) -> httpx.Response:
    payload = json.loads(request.content)
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={"id": payload["id"], "jsonrpc": "2.0", "result": result, **extra},
    )


def _http_rpc(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    request_limit: int = 8,
) -> tuple[HttpJsonRpcClient, httpx.Client]:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    return (
        HttpJsonRpcClient(
            "https://testnet.example/rpc",
            request_limit=request_limit,
            client=client,
        ),
        client,
    )


def test_json_rpc_transport_sends_one_canonical_request_and_returns_only_result():
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.method == "POST"
        assert request.url == httpx.URL("https://testnet.example/rpc")
        assert request.headers["content-type"] == "application/json"
        return _rpc_response(request, {"chain": CHAIN_ID})

    rpc, client = _http_rpc(handler, request_limit=1)
    try:
        assert rpc.call("get_blockchain_info", []) == {"chain": CHAIN_ID}
        assert seen == [{
            "id": 1,
            "jsonrpc": "2.0",
            "method": "get_blockchain_info",
            "params": [],
        }]
        assert rpc.request_count == 1
        with pytest.raises(IntegrationError, match="ceiling"):
            rpc.call("get_tip_header", [])
        assert len(seen) == 1
    finally:
        client.close()


def test_json_rpc_preserves_the_configured_endpoint_path_exactly():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _rpc_response(request, {})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    rpc = HttpJsonRpcClient(
        "https://testnet.example/rpc/",
        request_limit=1,
        client=client,
    )
    try:
        rpc.call("get_blockchain_info", [])
        assert seen == ["https://testnet.example/rpc/"]
    finally:
        client.close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_json_rpc_redirects_are_never_followed(status: int):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(status, headers={"location": "https://other.example/rpc"})

    rpc, client = _http_rpc(handler)
    try:
        with pytest.raises(IntegrationError, match="redirect"):
            rpc.call("get_tip_header", [])
        assert requests == ["https://testnet.example/rpc"]
        assert rpc.request_count == 1
    finally:
        client.close()


def test_json_rpc_transport_fault_is_not_retried_or_echoed():
    calls = 0
    secret = "SENSITIVE-RPC-TRANSPORT-CONTENT"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(secret, request=request)

    rpc, client = _http_rpc(handler)
    try:
        with pytest.raises(IntegrationError) as excinfo:
            rpc.call("get_tip_header", [])
        assert calls == 1
        assert rpc.request_count == 1
        assert secret not in str(excinfo.value)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(200, headers={"content-type": "text/html"}, text="<html>"), "non-JSON"),
        (httpx.Response(500, headers={"content-type": "application/json"}, text="{}"), "status"),
        (httpx.Response(200, headers={"content-type": "application/json"}, text="not-json"), "valid JSON"),
        (httpx.Response(200, headers={"content-type": "application/json"}, json=[]), "envelope"),
        (httpx.Response(200, headers={"content-type": "application/json"}, json={"jsonrpc": "2.0", "id": 9, "result": {}}), "ID"),
        (httpx.Response(200, headers={"content-type": "application/json"}, json={"jsonrpc": "2.0", "id": 1, "result": {}, "extra": True}), "unexpected"),
        (httpx.Response(200, headers={"content-type": "application/json"}, content=b"x" * ((1 << 20) + 1)), "byte limit"),
    ],
    ids=("html", "error-status", "malformed-json", "array", "wrong-id", "extra-field", "oversized"),
)
def test_json_rpc_refuses_unusable_responses(response: httpx.Response, match: str):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return response

    rpc, client = _http_rpc(handler)
    try:
        with pytest.raises(IntegrationError, match=match):
            rpc.call("get_tip_header", [])
        assert rpc.request_count == 1
    finally:
        client.close()


def test_json_rpc_refuses_a_client_that_follows_redirects():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
        follow_redirects=True,
    )
    try:
        with pytest.raises(IntegrationError, match="must not follow"):
            HttpJsonRpcClient("https://testnet.example", request_limit=1, client=client)
    finally:
        client.close()


class _Rpc:
    def __init__(self, handler: Callable[[str, list[Any]], Any]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, list[Any]]] = []

    @property
    def request_count(self) -> int:
        return len(self.calls)

    def call(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, deepcopy(params)))
        return self.handler(method, params)


def _chain() -> ChainIdentityObservation:
    return ChainIdentityObservation(
        chain_id=CHAIN_ID,
        genesis_hash=GENESIS,
        tip_number=16,
        tip_hash=TIP,
        request_count=4,
    )


def test_direct_chain_probe_binds_chain_genesis_and_one_coherent_tip():
    responses = {
        "get_blockchain_info": {"chain": CHAIN_ID},
        "get_tip_header": {"number": "0x10", "hash": TIP},
    }

    def handler(method: str, params: list[Any]) -> Any:
        if method == "get_block_hash":
            return GENESIS if params == ["0x0"] else TIP
        return responses[method]

    rpc = _Rpc(handler)
    observation = DirectChainProbe(rpc).observe()
    assert observation == _chain()
    assert [method for method, _params in rpc.calls] == [
        "get_blockchain_info",
        "get_block_hash",
        "get_tip_header",
        "get_block_hash",
    ]


def test_direct_chain_probe_rejects_an_incoherent_tip():
    def handler(method: str, params: list[Any]) -> Any:
        if method == "get_blockchain_info":
            return {"chain": CHAIN_ID}
        if method == "get_tip_header":
            return {"number": "0x10", "hash": TIP}
        if params == ["0x0"]:
            return GENESIS
        return OTHER_TX

    with pytest.raises(IntegrationError, match="incoherent"):
        DirectChainProbe(_Rpc(handler)).observe()


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "search_resources",
            "description": "Search CKB resources",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
        *[
            {
                "name": name,
                "description": "Controller chain identity read",
                "inputSchema": {"type": "object", "properties": {}},
            }
            for name in (
                "dev_get_genesis_hash",
                "rpc_get_block_hash",
                "rpc_get_blockchain_info",
                "rpc_get_tip_block_number",
            )
        ],
        {
            "name": "dev_request_testnet_funds",
            "description": "Privileged faucet",
            "inputSchema": {"type": "object", "properties": {"address": {"type": "string"}}},
        },
    ]


def _resources() -> list[dict[str, Any]]:
    return [{"uri": "ckb://docs/reference/transactions", "name": "Transactions"}]


def _surface(*, live: bool = True) -> TreatmentSurfaceProfile:
    return TreatmentSurfaceProfile.from_catalogs(
        profile_id="ckb-ai-testnet-transaction-v1" if live else "ckb-ai-local-code-v1",
        server_name="ckb-ai-mcp",
        server_version="1.7.0",
        claims_live_chain=live,
        allowed_tools=("search_resources",),
        allowed_resource_prefixes=("ckb://docs/",),
        tools=_tools(),
        resources=_resources(),
    )


class _CkbAi:
    def __init__(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        version: str = "1.7.0",
        count_correctly: bool = True,
    ) -> None:
        self.tools = deepcopy(_tools() if tools is None else tools)
        self.version = version
        self.count_correctly = count_correctly
        self.calls: list[tuple[str, Any]] = []

    @property
    def request_count(self) -> int:
        return len(self.calls)

    def _record(self, name: str, value: Any) -> Any:
        if self.count_correctly:
            self.calls.append((name, None))
        return deepcopy(value)

    def initialize(self) -> dict[str, Any]:
        return self._record(
            "initialize",
            {"serverInfo": {"name": "ckb-ai-mcp", "version": self.version}},
        )

    def list_tools(self) -> list[dict[str, Any]]:
        return self._record("tools/list", self.tools)

    def list_resources(self) -> list[dict[str, Any]]:
        return self._record("resources/list", _resources())

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        values = {
            "rpc_get_blockchain_info": json.dumps({"chain": CHAIN_ID}),
            "dev_get_genesis_hash": GENESIS,
            "rpc_get_tip_block_number": "0x10",
            "rpc_get_block_hash": TIP,
        }
        if self.count_correctly:
            self.calls.append((name, deepcopy(arguments)))
        return {"content": [{"type": "text", "text": values[name]}]}


def test_ckb_ai_preflight_validates_catalog_server_and_testnet_identity():
    client = _CkbAi()
    profile = _surface()
    observation = CkbAiPreflightAdapter(client, profile).observe()

    assert observation.ready
    assert observation.surface_sha256 == profile.sha256
    assert observation.catalog_sha256 == profile.catalog_sha256
    assert observation.chain_identity == replace(_chain(), request_count=4)
    assert observation.request_count == 7
    assert [name for name, _args in client.calls] == [
        "initialize",
        "tools/list",
        "resources/list",
        "rpc_get_blockchain_info",
        "dev_get_genesis_hash",
        "rpc_get_tip_block_number",
        "rpc_get_block_hash",
    ]


@pytest.mark.parametrize("drift", ["version", "catalog"])
def test_ckb_ai_preflight_refuses_server_or_catalog_drift_before_identity_calls(drift: str):
    tools = _tools()
    version = "1.7.0"
    if drift == "version":
        version = "1.7.1"
    else:
        tools[0]["description"] = "Changed"
    client = _CkbAi(tools=tools, version=version)
    observation = CkbAiPreflightAdapter(client, _surface()).observe()
    assert not observation.ready
    assert observation.chain_identity is None
    assert observation.request_count == 3
    assert len(client.calls) == 3


def test_ckb_ai_preflight_requires_exact_request_accounting():
    with pytest.raises(IntegrationError, match="accounting"):
        CkbAiPreflightAdapter(_CkbAi(count_correctly=False), _surface()).observe()


def _script(marker: str) -> dict[str, str]:
    return {"args": "0x" + marker * 40, "code_hash": "0x" + marker * 64, "hash_type": "type"}


OWN_LOCK = _script("a")
DESTINATION_LOCK = _script("b")
OTHER_LOCK = _script("c")
CELL_DEP = {"dep_type": "code", "out_point": {"index": "0x0", "tx_hash": DEP_TX}}


def _policy(
    *,
    maximum_transfer_shannons: int = 30_000,
    maximum_output_data_bytes: int = 0,
) -> SigningPolicy:
    destinations = (DESTINATION_LOCK,) if maximum_transfer_shannons else ()
    return SigningPolicy(
        policy_id="transaction-signer-v1",
        signer_handle="signer-attempt-a",
        public_address="ckt1-public-address",
        chain_identity_sha256=_chain().stable_identity_sha256,
        leased_inputs=(LeasedSignerInput(INPUT_TX, 0, 100_000),),
        own_lock=OWN_LOCK,
        permitted_destination_locks=destinations,
        permitted_output_types=(None,),
        cell_deps=(CELL_DEP,),
        header_deps=(),
        maximum_transfer_shannons=maximum_transfer_shannons,
        maximum_fee_shannons=1_000,
        maximum_transactions=1,
        maximum_output_data_bytes=maximum_output_data_bytes,
    )


def _transaction_request() -> dict[str, Any]:
    return {
        "transaction": {
            "cell_deps": [deepcopy(CELL_DEP)],
            "header_deps": [],
            "inputs": [{
                "previous_output": {"index": "0x0", "tx_hash": INPUT_TX},
                "since": "0x0",
            }],
            "outputs": [
                {"capacity": hex(30_000), "lock": deepcopy(DESTINATION_LOCK), "type": None},
                {"capacity": hex(69_500), "lock": deepcopy(OWN_LOCK), "type": None},
            ],
            "outputs_data": ["0x", "0x"],
            "version": "0x0",
            "witnesses": ["0x"],
        }
    }


class _KeyHolder:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        change_core: bool = False,
        malformed_witnesses: bool = False,
    ) -> None:
        self.error = error
        self.change_core = change_core
        self.malformed_witnesses = malformed_witnesses
        self.calls: list[dict[str, Any]] = []

    def sign_transaction(self, transaction: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(deepcopy(transaction))
        if self.error is not None:
            raise self.error
        signed = deepcopy(transaction)
        signed["witnesses"][0] = "0x00"
        if self.change_core:
            signed["outputs"][0]["capacity"] = "0x1"
        if self.malformed_witnesses:
            signed["witnesses"] = [None]
        return signed


def _submit_rpc(*, error: Exception | None = None) -> _Rpc:
    def handler(method: str, params: list[Any]) -> Any:
        if method == "get_blockchain_info":
            return {"chain": CHAIN_ID}
        if method == "get_block_hash":
            return GENESIS if params == ["0x0"] else TIP
        if method == "get_tip_header":
            return {"number": "0x10", "hash": TIP}
        assert method == "send_transaction"
        assert params[1] == "passthrough"
        if error is not None:
            raise error
        return SUBMITTED_TX

    return _Rpc(handler)


def test_constrained_signer_accepts_one_exact_transaction_and_returns_only_its_hash():
    key_holder = _KeyHolder()
    rpc = _submit_rpc()
    signer = PolicyConstrainedSigner(_policy(), key_holder, rpc)

    assert signer.sign_and_submit(_transaction_request()) == {"tx_hash": SUBMITTED_TX}
    assert len(key_holder.calls) == 1
    assert [method for method, _params in rpc.calls] == [
        "get_blockchain_info",
        "get_block_hash",
        "get_tip_header",
        "get_block_hash",
        "send_transaction",
    ]
    assert rpc.calls[-1][1][0]["witnesses"] == ["0x00"]
    inspection = SignerPreflightAdapter(signer).observe()
    assert inspection.signer_handle == "signer-attempt-a"
    assert inspection.signing_policy_sha256 == signer.policy.sha256
    assert inspection.single_assignment
    assert not inspection.agent_accessible


def _invalid_signing_request(case: str) -> dict[str, Any]:
    request = _transaction_request()
    transaction = request["transaction"]
    if case == "extra-request-key":
        request["extra"] = True
    elif case == "wrong-input":
        transaction["inputs"][0]["previous_output"]["tx_hash"] = OTHER_TX
    elif case == "nonzero-since":
        transaction["inputs"][0]["since"] = "0x1"
    elif case == "cell-dependency":
        transaction["cell_deps"] = []
    elif case == "header-dependency":
        transaction["header_deps"] = [OTHER_TX]
    elif case == "destination":
        transaction["outputs"][0]["lock"] = deepcopy(OTHER_LOCK)
    elif case == "output-type":
        transaction["outputs"][0]["type"] = deepcopy(OTHER_LOCK)
    elif case == "transfer":
        transaction["outputs"][0]["capacity"] = hex(30_001)
        transaction["outputs"][1]["capacity"] = hex(69_499)
    elif case == "fee":
        transaction["outputs"][1]["capacity"] = hex(68_000)
    elif case == "data":
        transaction["outputs_data"][0] = "0x00"
    elif case == "version":
        transaction["version"] = "0x1"
    elif case == "zero-output":
        transaction["outputs"][0]["capacity"] = "0x0"
    elif case == "witness":
        transaction["witnesses"][0] = "not-hex"
    else:
        raise AssertionError(case)
    return request


@pytest.mark.parametrize(
    "case",
    [
        "extra-request-key",
        "wrong-input",
        "nonzero-since",
        "cell-dependency",
        "header-dependency",
        "destination",
        "output-type",
        "transfer",
        "fee",
        "data",
        "version",
        "zero-output",
        "witness",
    ],
)
def test_constrained_signer_refuses_every_policy_escape_before_signing(case: str):
    key_holder = _KeyHolder()
    rpc = _submit_rpc()
    signer = PolicyConstrainedSigner(_policy(), key_holder, rpc)
    with pytest.raises(IntegrationError, match="attempt policy"):
        signer.sign_and_submit(_invalid_signing_request(case))
    assert key_holder.calls == []
    assert rpc.calls == []
    assert signer.protocol_violation_count == 1


def test_constrained_signer_is_single_assignment():
    signer = PolicyConstrainedSigner(_policy(), _KeyHolder(), _submit_rpc())
    signer.sign_and_submit(_transaction_request())
    with pytest.raises(IntegrationError, match="attempt policy"):
        signer.sign_and_submit(_transaction_request())
    assert signer.protocol_violation_count == 1


@pytest.mark.parametrize("boundary", ["key-holder", "submit-rpc"])
def test_signer_dependency_failures_do_not_retain_private_content(boundary: str):
    secret = "SENSITIVE-SIGNER-CONTENT"
    key_holder = _KeyHolder(error=RuntimeError(secret)) if boundary == "key-holder" else _KeyHolder()
    rpc = _submit_rpc(error=RuntimeError(secret)) if boundary == "submit-rpc" else _submit_rpc()
    signer = PolicyConstrainedSigner(_policy(), key_holder, rpc)
    with pytest.raises(IntegrationError) as excinfo:
        signer.sign_and_submit(_transaction_request())
    assert secret not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_key_holder_cannot_change_the_signed_transaction_intent():
    rpc = _submit_rpc()
    signer = PolicyConstrainedSigner(_policy(), _KeyHolder(change_core=True), rpc)
    with pytest.raises(IntegrationError, match="changed transaction intent"):
        signer.sign_and_submit(_transaction_request())
    assert all(method != "send_transaction" for method, _params in rpc.calls)


def test_key_holder_must_return_canonical_witnesses():
    rpc = _submit_rpc()
    signer = PolicyConstrainedSigner(
        _policy(),
        _KeyHolder(malformed_witnesses=True),
        rpc,
    )
    with pytest.raises(IntegrationError, match="malformed witnesses"):
        signer.sign_and_submit(_transaction_request())
    assert all(method != "send_transaction" for method, _params in rpc.calls)


def test_signer_refuses_a_submission_rpc_bound_to_another_chain_before_signing():
    key_holder = _KeyHolder()

    def handler(method: str, params: list[Any]) -> Any:
        if method == "get_blockchain_info":
            return {"chain": "ckb"}
        if method == "get_block_hash":
            return GENESIS if params == ["0x0"] else TIP
        if method == "get_tip_header":
            return {"number": "0x10", "hash": TIP}
        raise AssertionError(method)

    rpc = _Rpc(handler)
    signer = PolicyConstrainedSigner(_policy(), key_holder, rpc)
    with pytest.raises(IntegrationError, match="does not match"):
        signer.sign_and_submit(_transaction_request())
    assert key_holder.calls == []
    assert all(method != "send_transaction" for method, _params in rpc.calls)


def test_signing_policy_supports_deployment_without_transfer_or_output_data():
    policy = _policy(maximum_transfer_shannons=0, maximum_output_data_bytes=0)
    assert policy.permitted_destination_locks == ()
    assert policy.maximum_transfer_shannons == 0


def test_signing_policy_rejects_secret_shaped_public_fields():
    with pytest.raises(IntegrationError, match="secret-shaped"):
        replace(_policy(), policy_id="sk-live-private-policy")


def test_signing_policy_requires_immutable_destination_locks():
    with pytest.raises(IntegrationError, match="destination locks must be immutable"):
        replace(_policy(), permitted_destination_locks=[DESTINATION_LOCK])


def _lease() -> CellLease:
    return CellLease(
        lease_resource_id="lease-attempt-a",
        signer_handle="signer-attempt-a",
        lock_script=OWN_LOCK,
        out_points=((INPUT_TX, 0),),
    )


def _funding_rpc(*, change: str | None = None) -> _Rpc:
    def handler(method: str, params: list[Any]) -> Any:
        if method == "get_live_cell":
            output = {"capacity": hex(100_000), "lock": deepcopy(OWN_LOCK), "type": None}
            cell = {
                "data": {"content": "0x", "hash": OTHER_TX},
                "output": output,
            }
            result: dict[str, Any] = {"status": "live", "cell": cell}
            if change == "not-live":
                result["status"] = "dead"
            elif change == "lock":
                output["lock"] = deepcopy(OTHER_LOCK)
            elif change == "capacity":
                output["capacity"] = hex(99_999)
            elif change == "typed":
                output["type"] = deepcopy(OTHER_LOCK)
            elif change == "data":
                cell["data"]["content"] = "0x00"
            return result
        status: dict[str, Any] = {"status": "committed", "block_number": "0xe"}
        if change == "pending":
            status = {"status": "pending"}
        elif change == "ahead":
            status["block_number"] = "0x11"
        return {"tx_status": status}

    return _Rpc(handler)


def test_funding_preflight_inspects_only_the_exact_policy_lease():
    rpc = _funding_rpc()
    observation = FundingPreflightAdapter(rpc, _lease(), _policy(), _chain()).observe()
    assert observation.signer_handle == "signer-attempt-a"
    assert observation.lease_resource_id == "lease-attempt-a"
    assert observation.chain_identity_sha256 == _chain().stable_identity_sha256
    assert observation.spendable_capacity_shannons == 100_000
    assert observation.cell_count == 1
    assert observation.minimum_confirmations == 3
    assert observation.request_count == 2
    assert [method for method, _params in rpc.calls] == ["get_live_cell", "get_transaction"]


@pytest.mark.parametrize(
    "change",
    ["not-live", "lock", "capacity", "typed", "data", "pending", "ahead"],
)
def test_funding_preflight_refuses_unspendable_or_unconfirmed_cells(change: str):
    with pytest.raises(IntegrationError):
        FundingPreflightAdapter(
            _funding_rpc(change=change),
            _lease(),
            _policy(),
            _chain(),
        ).observe()


def test_funding_preflight_refuses_a_lease_different_from_the_signer_policy():
    different = replace(_lease(), out_points=((OTHER_TX, 0),))
    with pytest.raises(IntegrationError, match="does not match"):
        FundingPreflightAdapter(_funding_rpc(), different, _policy(), _chain())


def test_deployed_dependency_is_bound_to_the_observed_chain():
    cell = {
        "data": {"content": "0x00", "hash": OTHER_TX},
        "output": {"capacity": "0x64", "lock": deepcopy(OWN_LOCK), "type": None},
    }
    requirement = DeploymentRequirement(
        dependency_id="secp256k1-deployment",
        out_point=(DEP_TX, 0),
        expected_cell_sha256=artifact_sha256({"cell": cell}),
    )
    rpc = _Rpc(lambda method, _params: {"status": "live", "cell": cell})
    observation = DependencyPreflightAdapter(
        (requirement,),
        rpc=rpc,
        chain=_chain(),
    ).observe()
    assert observation == DependencyObservation(
        dependencies=((requirement.dependency_id, requirement.expected_cell_sha256),),
        chain_identity_sha256=_chain().stable_identity_sha256,
        request_count=1,
    )
    assert rpc.calls[0][0] == "get_live_cell"


def test_local_dependency_preflight_performs_no_rpc_and_refuses_chain_deployments():
    observation = DependencyPreflightAdapter((), rpc=None, chain=None).observe()
    assert observation == DependencyObservation((), None, 0)
    requirement = DeploymentRequirement("deployment", (DEP_TX, 0), "a" * 64)
    with pytest.raises(IntegrationError, match="local-hermetic"):
        DependencyPreflightAdapter((requirement,), rpc=None, chain=None)


def test_output_preflight_requires_fresh_non_symlinked_resources(tmp_path: Path):
    workspace = tmp_path / "workspace"
    runtime_free = True
    targets = (
        OutputTarget("runtime-name", "runtime-attempt-a", None, lambda: runtime_free),
        OutputTarget("workspace", "workspace-attempt-a", workspace),
    )
    observation = OutputPreflightAdapter(targets).observe()
    assert observation == OutputObservation(
        resources=(("runtime-name", "runtime-attempt-a"), ("workspace", "workspace-attempt-a")),
        fresh=True,
        symlink_count=0,
        foreign_owner_count=0,
        check_count=2,
    )

    workspace.mkdir()
    assert not OutputPreflightAdapter(targets).observe().fresh


def test_output_preflight_requires_typed_targets():
    with pytest.raises(IntegrationError, match="must be typed"):
        OutputPreflightAdapter((object(),))


def test_integrated_local_probe_never_calls_rpc_signer_or_funding_adapters():
    from ckbbench.run.test_task_preflight import _fixture

    intent, journal, requirements, fixture = _fixture(local=True)
    requirements = replace(requirements, required_dependencies=())
    forbidden_calls: list[str] = []

    def forbidden(name: str) -> Any:
        forbidden_calls.append(name)
        raise AssertionError(name)

    probe = IntegratedTaskProbe(
        source_call=lambda: fixture.source_value,
        provider_call=lambda: fixture.provider_value,
        ckb_ai_call=lambda: fixture.ckb_ai_value,
        rpc_call=lambda: forbidden("rpc"),
        signer_call=lambda: forbidden("signer"),
        funding_call=lambda: forbidden("funding"),
        dependencies_call=lambda: DependencyPreflightAdapter(
            (), rpc=None, chain=None
        ).observe(),
        outputs_call=lambda: fixture.outputs_value,
    )
    evidence = run_task_preflight(
        intent,
        journal,
        requirements,
        probe,
        checked_utc="2026-09-01T00:10:00Z",
        evidence_id="preflight-" + "a" * 32,
    )
    assert evidence.status == "passed"
    assert tuple(check.name for check in evidence.checks) == (
        "source",
        "provider",
        "ckb_ai",
        "dependencies",
        "outputs",
    )
    assert forbidden_calls == []
