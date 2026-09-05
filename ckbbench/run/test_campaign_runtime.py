from __future__ import annotations

import hashlib
import io
import json
import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ckbbench.run.campaign import CampaignQualification
from ckbbench.run.campaign_operator import main as campaign_main
from ckbbench.run.campaign_runtime import (
    CampaignRuntimeError,
    MAX_SIGNER_POOL_BYTES,
    PrivateSignerEntry,
    PrivateSignerPool,
    ProductionCampaignRuntime,
    ProductionSourceObserver,
    ProductionTaskBackend,
    SubmissionIntentRpc,
    _KEY_HOLDER_SCRIPT,
    _agent_failure_exit_status,
    _output_path,
    _read_private_json,
    _resource_absent,
    _run_params,
    _verify_image,
    _verify_network,
    load_private_signer_pool,
    validate_private_signer_pool,
)
from ckbbench.run.model_profile import load_run_profile
from ckbbench.run.suite_release import (
    CampaignReleaseBinding,
    CampaignTrial,
    build_campaign_from_release,
    load_chain_profile,
    load_suite_release,
)
from ckbbench.run.single_task import AgentInfrastructureFailure
from ckbbench.run.task_preflight import ChainIdentityObservation
from ckbbench.run.task_attempt import artifact_sha256, canonical_json_bytes
from ckbbench.run.testnet_integration import (
    DirectChainProbe,
    LeasedSignerInput,
    PolicyConstrainedSigner,
    TestnetIntegrationError as SigningIntegrationError,
)
from ckbbench.run.test_suite_release import (
    CHAIN,
    _manifest,
    _qualification,
    _release,
    _surface,
    _trial,
)
from ckbbench.run.treatment_surface import TreatmentSurfaceProfile
from ckbbench.verify.codetask import BENCH_PASSWORD_ENV, CODE_CHALLENGE_ENV
from ckbbench.verify.onchain import TYPE_ID_CODE_HASH, TYPE_ID_HASH_TYPE, type_id_args


def test_key_holder_script_compares_lock_fields_instead_of_json_key_order():
    assert "sameScript(publicBinding.own_lock, payload.own_lock)" in _KEY_HOLDER_SCRIPT
    assert "JSON.stringify(publicBinding.own_lock)" not in _KEY_HOLDER_SCRIPT


def _runtime(tmp_path: Path):
    release = _release(tmp_path)
    control = _surface("B")
    treatment = _surface("C")
    manifest = build_campaign_from_release(
        release,
        campaign_id="campaign-" + "6" * 32,
        created_utc="2026-09-01T12:00:00Z",
        execution_plan_id="execution-plan-v1",
        repository_revision="7" * 40,
        source_tree_sha256="8" * 64,
        trials=(_trial(control, treatment),),
        chain_profiles=(CHAIN,),
        treatment_profiles=(control, treatment),
    )
    binding = CampaignReleaseBinding(
        release=release,
        chain_profiles=(CHAIN,),
        treatment_profiles=(control, treatment),
    )
    profile = replace(
        load_run_profile("gpt-5.6-sol"),
        profile_id="model-profile-synthetic-v1",
        requested_model="provider/model",
        probed_response_model="provider/model",
        reasoning_effort="high",
        sha256="4" * 64,
    )
    runtime = ProductionCampaignRuntime(
        binding,
        profile,
        repository_root=Path.cwd(),
        private_runtime_root=tmp_path / "private-runtime",
    )
    return binding, manifest, runtime


def _qualification_binding(record) -> CampaignQualification:
    return CampaignQualification(
        qualification_id=record.qualification_id,
        qualification_kind=record.qualification_kind,
        qualification_schema_version=record.schema_version,
        qualification_sha256=record.sha256,
        completed_utc=record.completed_utc,
        model_profile_id=record.profile_id,
        model_profile_sha256=record.profile_sha256,
        model_variant_id=record.model_variant_id,
    )


def _qualified_runtime(tmp_path: Path):
    release = _release(tmp_path, semver="5.0.0")
    control = _surface("B")
    treatment = _surface("C")
    profile = load_run_profile("gpt-5.6-luna")
    qualification = _qualification_binding(_qualification(profile))
    manifest = build_campaign_from_release(
        release,
        campaign_id="campaign-" + "a" * 32,
        created_utc="2026-09-01T12:00:00Z",
        execution_plan_id="execution-plan-qualified-v1",
        repository_revision="7" * 40,
        source_tree_sha256="8" * 64,
        trials=(_trial(control, treatment, profile=profile),),
        chain_profiles=(CHAIN,),
        treatment_profiles=(control, treatment),
        model_qualifications=(qualification,),
    )
    binding = CampaignReleaseBinding(
        release=release,
        chain_profiles=(CHAIN,),
        treatment_profiles=(control, treatment),
    )
    runtime = ProductionCampaignRuntime(
        binding,
        profile,
        model_qualification=qualification,
        repository_root=Path.cwd(),
        private_runtime_root=tmp_path / "private-runtime",
    )
    return manifest, runtime, qualification


def _chain() -> ChainIdentityObservation:
    return ChainIdentityObservation(
        chain_id=CHAIN.chain_id,
        genesis_hash=CHAIN.genesis_hash,
        tip_number=42,
        tip_hash="0x" + "2" * 64,
        request_count=4,
    )


def _catalog_tools() -> list[dict]:
    return [
        {
            "description": f"Public operation {name}",
            "inputSchema": {"properties": {}, "type": "object"},
            "name": name,
        }
        for name in (
            "dev_get_genesis_hash",
            "rpc_get_block_hash",
            "rpc_get_blockchain_info",
            "rpc_get_tip_block_number",
            "search_resources",
        )
    ]


def _testnet_surface(arm: str) -> TreatmentSurfaceProfile:
    return TreatmentSurfaceProfile.from_catalogs(
        profile_id=(
            "ckb-ai-control-testnet-v1"
            if arm == "B"
            else "ckb-ai-treatment-testnet-v1"
        ),
        server_name="ckb-ai-mcp",
        server_version="1.6.13",
        claims_live_chain=True,
        allowed_tools=() if arm == "B" else ("search_resources",),
        allowed_resource_prefixes=() if arm == "B" else ("ckb://docs/",),
        tools=_catalog_tools(),
        resources=[{
            "mimeType": "text/markdown",
            "name": "Reference",
            "uri": "ckb://docs/reference/transaction-structure",
        }],
    )


def _signed_runtime(tmp_path: Path, task_id: str = "task-04-send-tx"):
    release = load_suite_release(Path("suites/ckb-independent-v1"))
    chain = load_chain_profile(Path("configs/chains/ckb-testnet-pudge-v1.json"))
    control = _testnet_surface("B")
    treatment = _testnet_surface("C")
    profile = load_run_profile("gpt-5.6-sol")
    trial = CampaignTrial(
        batch_id="batch-signed",
        trial_id="trial-signed",
        task_id=task_id,
        control_slot_id="slot-signed-b",
        treatment_slot_id="slot-signed-c",
        requested_model=profile.requested_model,
        thinking_level=profile.thinking_level,
        model_profile_id=profile.profile_id,
        model_profile_sha256=profile.sha256,
        trial_challenge_id="challenge-signed",
        trial_challenge_sha256="3" * 64,
        control_profile_id=control.profile_id,
        control_profile_sha256=control.sha256,
        treatment_profile_id=treatment.profile_id,
        treatment_profile_sha256=treatment.sha256,
    )
    manifest = build_campaign_from_release(
        release,
        campaign_id="campaign-" + "2" * 32,
        created_utc="2026-09-01T12:00:00Z",
        execution_plan_id="execution-plan-signed-v1",
        repository_revision="7" * 40,
        source_tree_sha256="8" * 64,
        trials=(trial,),
        chain_profiles=(chain,),
        treatment_profiles=(control, treatment),
    )
    entries = []
    for index, (slot_id, ordinal) in enumerate(
        (slot.slot_id, ordinal)
        for slot in manifest.slots
        for ordinal in (0, 1)
    ):
        entries.append(PrivateSignerEntry(
            slot_id=slot_id,
            retry_ordinal=ordinal,
            signer_handle=f"signer-{index}",
            public_address=f"ckt1-synthetic-{index}",
            private_key="0x" + f"{index + 1:064x}",
            own_lock={
                "args": "0x" + f"{index + 1:040x}",
                "code_hash": "0x" + "1" * 64,
                "hash_type": "type",
            },
            lease_resource_id=f"lease-{index}",
            leased_inputs=(LeasedSignerInput(
                tx_hash="0x" + f"{index + 1:064x}",
                index=0,
                capacity_shannons=30_000_000_000,
            ),),
        ))
    pool = PrivateSignerPool(chain.profile_id, chain.sha256, tuple(sorted(
        entries,
        key=lambda row: (row.slot_id, row.retry_ordinal),
    )))
    binding = CampaignReleaseBinding(
        release=release,
        chain_profiles=(chain,),
        treatment_profiles=(control, treatment),
    )
    runtime = ProductionCampaignRuntime(
        binding,
        profile,
        repository_root=Path.cwd(),
        private_runtime_root=tmp_path / "private-runtime",
        signer_pool=pool,
    )
    return manifest, runtime


def _signer_pool_document(pool: PrivateSignerPool) -> dict:
    return {
        "chain_profile_id": pool.chain_profile_id,
        "chain_profile_sha256": pool.chain_profile_sha256,
        "entries": [
            {
                "lease_resource_id": entry.lease_resource_id,
                "leased_inputs": [row.to_dict() for row in entry.leased_inputs],
                "own_lock": entry.own_lock,
                "private_key": entry.private_key,
                "public_address": entry.public_address,
                "retry_ordinal": entry.retry_ordinal,
                "signer_handle": entry.signer_handle,
                "slot_id": entry.slot_id,
            }
            for entry in pool.entries
        ],
        "schema_version": "ckbbench-signer-pool-v1",
    }


def test_current_runtime_uses_the_manifest_qualification_for_preflight(tmp_path: Path):
    manifest, runtime, qualification = _qualified_runtime(tmp_path)
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)

    assert prepared.requirements.model_qualification_kind == qualification.qualification_kind
    assert (
        prepared.requirements.model_qualification_evidence_sha256
        == qualification.qualification_sha256
    )
    assert prepared.requirements.model_qualification_utc == qualification.completed_utc
    assert prepared.backend.model_qualification == qualification
    assert qualification.qualification_sha256 != (
        runtime.model_profile.qualification_source_evidence_sha256
        or runtime.model_profile.sha256
    )


@pytest.mark.parametrize("mutation", [None, "wrong-digest"])
def test_current_runtime_refuses_a_missing_or_different_manifest_qualification(
    tmp_path: Path,
    mutation: str | None,
):
    manifest, runtime, qualification = _qualified_runtime(tmp_path)
    runtime.model_qualification = (
        None
        if mutation is None
        else replace(qualification, qualification_sha256="9" * 64)
    )

    with pytest.raises(CampaignRuntimeError, match="qualification differs"):
        runtime.prepare(manifest, manifest.ordered_slots[0], None)


def test_multi_model_manifest_accepts_only_slots_for_the_runtime_profile(tmp_path: Path):
    release = _release(tmp_path, semver="5.0.0")
    control = _surface("B")
    treatment = _surface("C")
    luna = load_run_profile("gpt-5.6-luna")
    sol = load_run_profile("gpt-5.6-sol")
    luna_trial = _trial(control, treatment, profile=luna)
    sol_trial = replace(
        _trial(control, treatment, profile=sol),
        batch_id="batch-sol",
        trial_id="trial-sol",
        control_slot_id="slot-sol-b",
        treatment_slot_id="slot-sol-c",
        trial_challenge_id="challenge-sol",
        trial_challenge_sha256="6" * 64,
    )
    qualifications = tuple(sorted(
        (
            _qualification_binding(_qualification(luna, ordinal=1)),
            _qualification_binding(_qualification(sol, ordinal=2)),
        ),
        key=lambda row: row.profile_key,
    ))
    manifest = build_campaign_from_release(
        release,
        campaign_id="campaign-" + "b" * 32,
        created_utc="2026-09-01T12:00:00Z",
        execution_plan_id="execution-plan-multi-model-v1",
        repository_revision="7" * 40,
        source_tree_sha256="8" * 64,
        trials=(luna_trial, sol_trial),
        chain_profiles=(CHAIN,),
        treatment_profiles=(control, treatment),
        model_qualifications=qualifications,
    )
    binding = CampaignReleaseBinding(release, (CHAIN,), (control, treatment))
    luna_qualification = manifest.qualification_for_profile(luna.profile_id, luna.sha256)
    runtime = ProductionCampaignRuntime(
        binding,
        luna,
        model_qualification=luna_qualification,
        repository_root=Path.cwd(),
        private_runtime_root=tmp_path / "private-runtime",
    )
    luna_slot = next(slot for slot in manifest.slots if slot.model_profile_id == luna.profile_id)
    sol_slot = next(slot for slot in manifest.slots if slot.model_profile_id == sol.profile_id)

    assert runtime.prepare(manifest, luna_slot, None).intent.identity.model_profile_id == luna.profile_id
    with pytest.raises(CampaignRuntimeError, match="selected slot"):
        runtime.prepare(manifest, sol_slot, None)


def test_code_task_run_params_use_matching_generic_and_legacy_challenges():
    release = load_suite_release(Path("suites/ckb-core-v2"))
    task = next(task for task in release.suite.tasks if task.id == "task-09-since-lock")
    slot = SimpleNamespace(
        run_params_derivation="seeded-sha256-v1",
        task_id=task.id,
        trial_challenge_sha256="3" * 64,
    )
    params = _run_params(task, slot, 0)
    generic = params.verifier_private[CODE_CHALLENGE_ENV]
    assert generic == params.verifier_private[BENCH_PASSWORD_ENV]
    assert len(generic) == 64


def _write_private_pool(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document), encoding="ascii")
    path.chmod(0o600)


def test_prepare_is_inert_and_derives_matched_arm_neutral_parameters(tmp_path: Path):
    binding, manifest, runtime = _runtime(tmp_path)
    control = next(slot for slot in manifest.slots if slot.arm == "B")
    treatment = next(slot for slot in manifest.slots if slot.arm == "C")

    prepared_b = runtime.prepare(manifest, control, None)
    prepared_c = runtime.prepare(manifest, treatment, None)

    assert not runtime.private_runtime_root.exists()
    assert prepared_b.intent.identity.prompt_params_sha256 == prepared_c.intent.identity.prompt_params_sha256
    assert prepared_b.intent.identity.verifier_private_commitment_sha256 != (
        prepared_c.intent.identity.verifier_private_commitment_sha256
    )
    assert prepared_b.requirements.required_resource_claims != (
        prepared_c.requirements.required_resource_claims
    )
    binding.validate_preflight(
        manifest,
        control,
        prepared_b.intent,
        prepared_b.requirements,
    )
    assert prepared_b.requirements.ckb_ai_request_limit == 7


def test_setup_releases_only_one_task_and_keeps_private_values_outside_workspace(
    tmp_path: Path,
):
    _binding, manifest, runtime = _runtime(tmp_path)
    slot = manifest.ordered_slots[0]
    prepared = runtime.prepare(manifest, slot, None)
    backend = prepared.backend
    assert isinstance(backend, ProductionTaskBackend)
    backend.preflight.direct_chain = _chain()

    setup = backend.setup(
        prepared.intent,
        prepared.requirements,
        timeout_seconds=30,
    )

    workspace = backend.material.workspace
    assert setup.initial_resource_equivalence_sha256
    assert (workspace / "INSTRUCTIONS.md").is_file()
    assert (workspace / f"{slot.task_id}.json").is_file()
    assert {path.name for path in workspace.iterdir()} == {
        "INSTRUCTIONS.md",
        f"{slot.task_id}.json",
    }
    assert (backend.material.private_dir / "verifier-private.json").is_file()
    assert "harness_tip" not in (workspace / f"{slot.task_id}.json").read_text()

    workspace_claim = next(
        claim for claim in prepared.requirements.required_resource_claims if claim[0] == "workspace"
    )
    assert backend.cleanup_resource(
        prepared.intent,
        *workspace_claim,
        timeout_seconds=30,
    ) == "released"
    assert not backend.material.runtime_dir.exists()


def test_production_agent_refuses_local_execution_before_constructing_an_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _binding, manifest, runtime = _runtime(tmp_path)
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    assert isinstance(backend, ProductionTaskBackend)
    backend.preflight.direct_chain = _chain()
    backend.setup(prepared.intent, prepared.requirements, timeout_seconds=30)
    monkeypatch.delenv("CKBBENCH_DOCKER", raising=False)

    with pytest.raises(CampaignRuntimeError, match="isolated Docker"):
        backend.start_agent(prepared.intent, timeout_seconds=30)
    assert backend._agent is None


def test_production_agent_scopes_proxy_evidence_before_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _binding, manifest, runtime = _runtime(tmp_path)
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    assert isinstance(backend, ProductionTaskBackend)
    backend.preflight.direct_chain = _chain()
    backend.setup(prepared.intent, prepared.requirements, timeout_seconds=30)
    monkeypatch.setenv("CKBBENCH_DOCKER", "1")
    monkeypatch.setattr("ckbbench.run.campaign_runtime.time.time", lambda: 123.5)
    observed = {}
    proxy_check = lambda _arm, _workspace: False

    def fake_violation_check(**kwargs):
        observed["violation"] = kwargs
        return proxy_check

    agent = SimpleNamespace(protocol_violation_count=0)

    def fake_agent_factory(**kwargs):
        observed["factory_options"] = kwargs

        def construct(**construction):
            observed["construction"] = construction
            return agent

        return construct

    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime.make_violation_check",
        fake_violation_check,
    )
    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime.make_agent_factory",
        fake_agent_factory,
    )

    assert backend.start_agent(prepared.intent, timeout_seconds=30) is agent
    assert observed["violation"] == {
        "arm": prepared.intent.identity.arm,
        "chain": backend.material.chain.chain_track,
        "log_since": 123.5,
        "mcp_url": backend.mcp_endpoint,
    }
    assert backend._proxy_violation_check is proxy_check


@pytest.mark.parametrize(
    ("exception_name", "expected_status"),
    (
        ("SignerActionError", "SignerActionError:submission"),
        ("ProfiledProviderError", "ProfiledProviderError"),
        ("ProviderCallError", "ProviderCallError"),
        ("ResponseConversionError", "ResponseConversionError"),
        ("ResponseHistoryError", "ResponseHistoryError"),
        ("SpoofedResponseHistoryError", "AgentRuntimeError"),
        ("UnexpectedAdapterFailure", "AgentRuntimeError"),
    ),
)
def test_production_agent_failure_retains_only_an_allowlisted_exception_type(
    tmp_path: Path,
    exception_name: str,
    expected_status: str,
):
    _binding, manifest, runtime = _runtime(tmp_path)
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    assert isinstance(backend, ProductionTaskBackend)
    secret = "sk-live-must-not-survive https://provider.invalid/private raw-body"
    if exception_name == "SignerActionError":
        from ckb_agent import SignerActionError

        failure = SignerActionError("submission")
    elif exception_name == "UnexpectedAdapterFailure":
        failure_type = type(exception_name, (RuntimeError,), {})
        failure = failure_type(secret)
    elif exception_name == "SpoofedResponseHistoryError":
        import ckb_model

        failure_type = type("ResponseHistoryError", (ckb_model.ResponseHistoryError,), {})
        failure = failure_type(secret)
    else:
        import ckb_model

        failure_type = getattr(ckb_model, exception_name)
        failure = failure_type(secret)

    class Agent:
        model = SimpleNamespace(usage_ledger=SimpleNamespace(
            attempt_count=0,
            attempts=(),
            provider_failure_category=None,
            provider_failure_counts={},
            response_count=0,
            turn_count=0,
        ))

        def run(self, _pointer):
            raise failure

    agent = Agent()
    backend._agent = agent
    budget = prepared.intent.identity.budget

    with pytest.raises(AgentInfrastructureFailure) as caught:
        backend.run_agent(
            agent,
            step_limit=budget.step_limit,
            wall_time_limit_seconds=budget.wall_time_limit_seconds,
            provider_call_limit=budget.provider_call_limit,
            output_token_limit=budget.output_token_limit,
        )

    assert caught.value.observation.exit_status == expected_status
    retained = repr(caught.value.observation) + str(caught.value)
    assert secret not in retained
    assert "provider.invalid" not in retained
    assert "raw-body" not in retained


def test_signer_failure_status_accepts_only_exact_allowlisted_categories():
    from ckb_agent import SignerActionError

    for category in (
        "chain-check",
        "key-holder",
        "signed-transaction",
        "submission",
        "submission-result",
        "unknown",
    ):
        assert _agent_failure_exit_status(SignerActionError(category)) == (
            f"SignerActionError:{category}"
        )

    tampered = SignerActionError("submission")
    tampered.category = "submission:PRIVATE-CONTENT"
    assert _agent_failure_exit_status(tampered) == "AgentRuntimeError"

    spoofed = type("SignerActionError", (SignerActionError,), {})
    assert _agent_failure_exit_status(spoofed("submission")) == "AgentRuntimeError"


def test_production_output_preflight_detects_a_reserved_path_collision(tmp_path: Path):
    _binding, manifest, runtime = _runtime(tmp_path)
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    assert isinstance(backend, ProductionTaskBackend)
    kind, _resource_id = backend.material.output_resources[0]
    collision = _output_path(backend.material, kind)
    collision.mkdir(parents=True)

    observed = backend.observe_outputs(timeout_seconds=30)

    assert observed.fresh is False
    assert observed.check_count == 1
    assert observed.symlink_count == 0
    assert observed.foreign_owner_count == 0


def test_recovery_backend_accepts_reloaded_intent_and_refuses_foreign_cleanup(tmp_path: Path):
    _binding, manifest, runtime = _runtime(tmp_path)
    slot = manifest.ordered_slots[0]
    prepared = runtime.prepare(manifest, slot, None)
    backend = prepared.backend
    assert isinstance(backend, ProductionTaskBackend)
    backend.preflight.direct_chain = _chain()
    backend.setup(prepared.intent, prepared.requirements, timeout_seconds=30)

    reloaded = type(prepared.intent).from_dict(prepared.intent.to_dict())
    workspace_claim = next(
        claim for claim in prepared.requirements.required_resource_claims if claim[0] == "workspace"
    )
    assert backend.cleanup_resource(
        reloaded,
        *workspace_claim,
        timeout_seconds=30,
    ) == "released"
    with pytest.raises(CampaignRuntimeError, match="undeclared"):
        backend.cleanup_resource(
            reloaded,
            "workspace",
            "foreign-workspace",
            timeout_seconds=30,
        )


def test_signed_recovery_uses_stored_plan_without_reloading_private_keys(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    slot = manifest.ordered_slots[0]
    prepared = runtime.prepare(manifest, slot, None)
    backend = prepared.backend
    backend.preflight.direct_chain = _chain()
    backend.setup(prepared.intent, prepared.requirements, timeout_seconds=30)
    runtime.signer_pool = None

    requirements, recovered, max_score = runtime.prepare_recovery(
        manifest,
        slot,
        SimpleNamespace(
            intent=prepared.intent,
            preflight_requirements=prepared.requirements,
        ),
    )

    assert requirements == prepared.requirements
    assert max_score == slot.max_score
    spendable = next(
        claim
        for claim in requirements.required_resource_claims
        if claim[0] == "spendable-input"
    )
    assert recovered.cleanup_resource(
        prepared.intent, *spendable, timeout_seconds=30
    ) == "released"
    workspace = next(
        claim for claim in requirements.required_resource_claims if claim[0] == "workspace"
    )
    assert recovered.cleanup_resource(
        prepared.intent, *workspace, timeout_seconds=30
    ) == "released"


def test_profile_drift_is_refused_before_attempt_material_or_external_work(tmp_path: Path):
    _binding, manifest, runtime = _runtime(tmp_path)
    runtime.model_profile = replace(runtime.model_profile, sha256="5" * 64)

    with pytest.raises(CampaignRuntimeError, match="model profile differs"):
        runtime.prepare(manifest, manifest.ordered_slots[0], None)

    assert not runtime.private_runtime_root.exists()


def test_private_signer_pool_uses_one_owner_private_bounded_file(tmp_path: Path):
    _manifest_value, runtime = _signed_runtime(tmp_path)
    assert runtime.signer_pool is not None
    pool_path = tmp_path / "private-pool.json"
    _write_private_pool(pool_path, _signer_pool_document(runtime.signer_pool))

    loaded = load_private_signer_pool(pool_path, repository_root=Path.cwd())

    assert loaded == runtime.signer_pool
    pool_path.chmod(0o644)
    with pytest.raises(CampaignRuntimeError, match="mode 0600"):
        load_private_signer_pool(pool_path, repository_root=Path.cwd())


def test_private_signer_pool_refuses_a_file_changed_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _manifest_value, runtime = _signed_runtime(tmp_path)
    assert runtime.signer_pool is not None
    pool_path = tmp_path / "private-pool.json"
    _write_private_pool(pool_path, _signer_pool_document(runtime.signer_pool))
    real_fstat = os.fstat
    calls = 0

    def changed_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        observed = real_fstat(descriptor)
        if calls == 1:
            return observed
        return SimpleNamespace(
            st_dev=observed.st_dev,
            st_ino=observed.st_ino,
            st_mode=observed.st_mode,
            st_uid=observed.st_uid,
            st_size=observed.st_size,
            st_mtime_ns=observed.st_mtime_ns + 1,
        )

    monkeypatch.setattr("ckbbench.run.campaign_runtime.os.fstat", changed_fstat)
    with pytest.raises(CampaignRuntimeError, match="changed while it was being read"):
        load_private_signer_pool(pool_path, repository_root=Path.cwd())


def test_signer_pool_cli_validates_exact_campaign_coverage_without_exposing_keys(
    tmp_path: Path,
):
    manifest, runtime = _signed_runtime(tmp_path)
    assert runtime.signer_pool is not None
    pool_path = tmp_path / "private-pool.json"
    _write_private_pool(pool_path, _signer_pool_document(runtime.signer_pool))
    manifest_path = tmp_path / "campaign.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    stdout = io.StringIO()
    stderr = io.StringIO()

    assert campaign_main(
        [
            "validate-signer-pool",
            "--manifest", str(manifest_path),
            "--signer-pool", str(pool_path),
            "--repository-root", str(Path.cwd()),
        ],
        release_binding=runtime.release_binding,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert f"entries={len(runtime.signer_pool.entries)}" in stdout.getvalue()
    assert runtime.signer_pool.entries[0].private_key not in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_signer_pool_validator_rejects_incomplete_campaign_coverage(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    assert runtime.signer_pool is not None
    incomplete = replace(runtime.signer_pool, entries=runtime.signer_pool.entries[:-1])

    with pytest.raises(CampaignRuntimeError, match="exactly cover"):
        validate_private_signer_pool(manifest, runtime.release_binding, incomplete)


def test_signer_pool_json_schema_tracks_the_runtime_field_contract():
    schema = json.loads(
        (Path("docs") / "signer-pool.schema.json").read_text(encoding="utf-8")
    )
    entry = schema["properties"]["entries"]["items"]
    leased_input = entry["properties"]["leased_inputs"]["items"]
    own_lock = entry["properties"]["own_lock"]

    assert set(schema["required"]) == {
        "chain_profile_id", "chain_profile_sha256", "entries", "schema_version",
    }
    assert set(entry["required"]) == {
        "lease_resource_id", "leased_inputs", "own_lock", "private_key",
        "public_address", "retry_ordinal", "signer_handle", "slot_id",
    }
    assert set(leased_input["required"]) == {"capacity_shannons", "index", "tx_hash"}
    assert set(own_lock["required"]) == {"args", "code_hash", "hash_type"}
    assert schema["properties"]["schema_version"]["const"] == "ckbbench-signer-pool-v1"


def test_private_document_reader_refuses_permissions_symlinks_and_noncanonical_bytes(
    tmp_path: Path,
):
    document = tmp_path / "private.json"
    document.write_bytes(b'{"value":1}\n')
    document.chmod(0o600)
    assert _read_private_json(document, "private test") == {"value": 1}

    document.chmod(0o644)
    with pytest.raises(CampaignRuntimeError, match="private-file boundary"):
        _read_private_json(document, "private test")
    document.chmod(0o600)
    document.write_bytes(b'{ "value": 1 }\n')
    with pytest.raises(CampaignRuntimeError, match="canonical"):
        _read_private_json(document, "private test")

    target = tmp_path / "target.json"
    target.write_bytes(b'{"value":1}\n')
    target.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(CampaignRuntimeError, match="could not be read safely"):
        _read_private_json(link, "private test")


def test_private_signer_pool_refuses_symlinks_duplicates_oversize_and_repository_files(
    tmp_path: Path,
):
    _manifest_value, runtime = _signed_runtime(tmp_path)
    assert runtime.signer_pool is not None
    document = _signer_pool_document(runtime.signer_pool)
    pool_path = tmp_path / "private-pool.json"
    _write_private_pool(pool_path, document)

    link = tmp_path / "pool-link.json"
    link.symlink_to(pool_path)
    with pytest.raises(CampaignRuntimeError):
        load_private_signer_pool(link, repository_root=Path.cwd())

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":"ckbbench-signer-pool-v1",'
        '"schema_version":"ckbbench-signer-pool-v1"}',
        encoding="ascii",
    )
    duplicate.chmod(0o600)
    with pytest.raises(CampaignRuntimeError, match="duplicate"):
        load_private_signer_pool(duplicate, repository_root=Path.cwd())

    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_SIGNER_POOL_BYTES + 1)
    oversized.chmod(0o600)
    with pytest.raises(CampaignRuntimeError, match="size"):
        load_private_signer_pool(oversized, repository_root=Path.cwd())

    with pytest.raises(CampaignRuntimeError, match="outside"):
        load_private_signer_pool(pool_path, repository_root=tmp_path)


@pytest.mark.parametrize("dimension", ["key", "lease", "outpoint"])
def test_signer_pool_refuses_reused_private_resources(tmp_path: Path, dimension: str):
    manifest, runtime = _signed_runtime(tmp_path)
    assert runtime.signer_pool is not None
    entries = list(runtime.signer_pool.entries)
    first, second = entries[:2]
    if dimension == "key":
        entries[1] = replace(second, private_key=first.private_key)
    elif dimension == "lease":
        entries[1] = replace(second, lease_resource_id=first.lease_resource_id)
    else:
        entries[1] = replace(second, leased_inputs=first.leased_inputs)
    runtime.signer_pool = replace(runtime.signer_pool, entries=tuple(entries))

    with pytest.raises(CampaignRuntimeError, match="reuses"):
        runtime.prepare(manifest, manifest.ordered_slots[0], None)


def test_signer_pool_refuses_unequal_matched_arm_capacity(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    assert runtime.signer_pool is not None
    entries = list(runtime.signer_pool.entries)
    b_slot = next(slot for slot in manifest.slots if slot.arm == "B")
    c_slot = next(slot for slot in manifest.slots if slot.arm == "C")
    b_entry = runtime.signer_pool.entry_for(b_slot.slot_id, 0)
    c_index = next(
        index
        for index, entry in enumerate(entries)
        if (entry.slot_id, entry.retry_ordinal) == (c_slot.slot_id, 0)
    )
    c_entry = entries[c_index]
    entries[c_index] = replace(
        c_entry,
        leased_inputs=(replace(
            c_entry.leased_inputs[0],
            capacity_shannons=b_entry.leased_inputs[0].capacity_shannons + 1,
        ),),
    )
    runtime.signer_pool = replace(runtime.signer_pool, entries=tuple(entries))

    with pytest.raises(CampaignRuntimeError, match="unequal capacity"):
        runtime.prepare(manifest, manifest.ordered_slots[0], None)


def test_signer_pool_is_revalidated_for_every_new_attempt(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    runtime.prepare(manifest, manifest.ordered_slots[0], None)
    assert runtime.signer_pool is not None
    entries = list(runtime.signer_pool.entries)
    entries[1] = replace(entries[1], private_key=entries[0].private_key)
    runtime.signer_pool = replace(runtime.signer_pool, entries=tuple(entries))

    with pytest.raises(CampaignRuntimeError, match="reuses"):
        runtime.prepare(manifest, manifest.ordered_slots[0], None)


def test_private_runtime_root_cannot_overlap_execution_inputs(tmp_path: Path):
    release, control, treatment, _manifest_value = _manifest(tmp_path)
    binding = CampaignReleaseBinding(
        release=release,
        chain_profiles=(CHAIN,),
        treatment_profiles=(control, treatment),
    )
    profile = replace(
        load_run_profile("gpt-5.6-sol"),
        profile_id="model-profile-synthetic-v1",
        requested_model="provider/model",
        probed_response_model="provider/model",
        reasoning_effort="high",
        sha256="4" * 64,
    )

    with pytest.raises(CampaignRuntimeError, match="under benchmark-output"):
        ProductionCampaignRuntime(
            binding,
            profile,
            repository_root=Path.cwd(),
            private_runtime_root=Path.cwd() / "ckbbench" / "private-runtime",
        )


def test_source_observer_recomputes_revision_tree_and_execution_input_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    binding, manifest, _runtime_value = _runtime(tmp_path)
    source = manifest.execution_source
    tree = b"synthetic canonical git tree\0"
    verified: list[tuple[str, str]] = []

    def run_checked(argv, **_kwargs):
        if tuple(argv) == ("git", "rev-parse", "HEAD"):
            return "a" * 40 + "\n"
        if tuple(argv) == ("git", "ls-tree", "-r", "--full-tree", "-z", "HEAD"):
            return tree
        raise AssertionError(argv)

    def git_names(_root, *args):
        if args == ("ls-files", "--others", "--exclude-standard"):
            return ("research/local-note.md", ".DS_Store", "ckbbench/new-runtime.py")
        return ()

    monkeypatch.setattr("ckbbench.run.campaign_runtime._run_checked", run_checked)
    monkeypatch.setattr("ckbbench.run.campaign_runtime._git_names", git_names)
    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime.resolve_agent_image",
        lambda **_kwargs: source.agent_image_digest,
    )
    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime.resolve_verifier_image",
        lambda **_kwargs: source.verifier_image_digest,
    )
    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime.resolve_agent_network",
        lambda: "ckbbench-net-internal",
    )
    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime._verify_image",
        lambda _root, image, *, role: verified.append((role, image)),
    )
    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime._verify_network",
        lambda _root, network: verified.append(("network", network)),
    )
    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime._resource_absent",
        lambda *_args: True,
    )

    observation = ProductionSourceObserver(
        Path.cwd(), source, binding.release.suite
    ).observe("ckbbench-attempt-synthetic")

    assert observation.execution_source.repository_revision == "a" * 40
    assert observation.execution_source.source_tree_sha256 == hashlib.sha256(tree).hexdigest()
    assert observation.staged_change_count == 0
    assert observation.tracked_change_count == 0
    assert observation.untracked_execution_input_count == 1
    assert observation.untracked_execution_inputs_sha256 == artifact_sha256({
        "execution_inputs": ["ckbbench/new-runtime.py"],
    })
    assert verified == [
        ("agent", source.agent_image_digest),
        ("verifier", source.verifier_image_digest),
        ("network", "ckbbench-net-internal"),
    ]


@pytest.mark.parametrize(
    ("kind", "output", "expected"),
    [
        ("container", "Error: No such container: exact-agent", True),
        ("container", "Error: No such container: exact-agent-backup", False),
        ("container", "Error: No such volume: exact-agent", False),
        ("volume", "Error: No such volume: exact-work", True),
        ("volume", "Error: No such volume: old-exact-work", False),
        ("volume", "permission denied: exact-work", False),
    ],
)
def test_resource_absence_requires_the_exact_name_and_kind(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    output: str,
    expected: bool,
):
    class Completed:
        returncode = 1
        stdout = ""
        stderr = output

    monkeypatch.setattr("ckbbench.run.campaign_runtime.subprocess.run", lambda *a, **k: Completed())

    name = "exact-agent" if kind == "container" else "exact-work"
    assert _resource_absent(Path.cwd(), kind, name) is expected


@pytest.mark.parametrize(
    "mutation",
    ["id", "platform", "role", "release-family"],
)
def test_frozen_image_inspection_refuses_identity_and_role_drift(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
):
    image = "sha256:" + "1" * 64
    row = {
        "Architecture": "arm64",
        "Config": {
            "Labels": {
                "org.ckbbench.release-family": "independent-task-suite-v1",
                "org.ckbbench.role": "agent",
            },
        },
        "Id": image,
        "Os": "linux",
    }
    if mutation == "id":
        row["Id"] = "sha256:" + "2" * 64
    elif mutation == "platform":
        row["Architecture"] = "amd64"
    elif mutation == "role":
        row["Config"]["Labels"]["org.ckbbench.role"] = "verifier"
    else:
        row["Config"]["Labels"]["org.ckbbench.release-family"] = "other"
    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime._docker_json",
        lambda *_args: [row],
    )

    with pytest.raises(CampaignRuntimeError, match="identity, platform or role"):
        _verify_image(Path.cwd(), image, role="agent")


@pytest.mark.parametrize("mutation", ["name", "external", "proxy"])
def test_agent_network_inspection_refuses_boundary_drift(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
):
    row = {
        "Containers": {"1": {"Name": "ckbbench-proxy"}},
        "Internal": True,
        "Name": "ckbbench-net-internal",
    }
    if mutation == "name":
        row["Name"] = "other-network"
    elif mutation == "external":
        row["Internal"] = False
    else:
        row["Containers"] = {"1": {"Name": "other-proxy"}}
    monkeypatch.setattr(
        "ckbbench.run.campaign_runtime._docker_json",
        lambda *_args: [row],
    )

    with pytest.raises(CampaignRuntimeError, match="network|proxy"):
        _verify_network(Path.cwd(), "ckbbench-net-internal")


def test_submission_intent_is_persisted_before_the_rpc_call():
    events: list[str] = []

    class Rpc:
        def call(self, method, params):
            events.append(f"rpc:{method}")
            return "0x" + "1" * 64

    rpc = SubmissionIntentRpc(Rpc(), lambda: events.append("intent"))

    assert rpc.call("send_transaction", [{}, "passthrough"]) == "0x" + "1" * 64
    assert events == ["intent", "rpc:send_transaction"]


def test_submission_intent_survives_an_rpc_failure():
    events: list[str] = []

    class Rpc:
        def call(self, method, params):
            events.append(f"rpc:{method}")
            raise OSError("synthetic transport failure")

    rpc = SubmissionIntentRpc(Rpc(), lambda: events.append("intent"))

    with pytest.raises(OSError, match="synthetic transport failure"):
        rpc.call("send_transaction", [{}, "passthrough"])
    assert events == ["intent", "rpc:send_transaction"]


def test_non_submission_rpc_does_not_retire_the_lease():
    events: list[str] = []

    class Rpc:
        def call(self, method, params):
            events.append(f"rpc:{method}")
            return {"chain": "ckb_testnet"}

    rpc = SubmissionIntentRpc(Rpc(), lambda: events.append("intent"))

    assert rpc.call("get_blockchain_info", []) == {"chain": "ckb_testnet"}
    assert events == ["rpc:get_blockchain_info"]


def test_submission_intent_rpc_preserves_chain_probe_request_accounting():
    class Rpc:
        def __init__(self):
            self.request_count = 0

        def call(self, method, params):
            self.request_count += 1
            if method == "get_blockchain_info":
                return {"chain": "ckb_testnet"}
            if method == "get_block_hash":
                return "0x" + ("1" if params == ["0x0"] else "2") * 64
            if method == "get_tip_header":
                return {"number": "0x10", "hash": "0x" + "2" * 64}
            raise AssertionError(method)

    inner = Rpc()
    rpc = SubmissionIntentRpc(inner, lambda: None)
    observed = DirectChainProbe(rpc).observe()

    assert observed.chain_id == "ckb_testnet"
    assert observed.request_count == 4
    assert rpc.request_count == 4


def test_signed_release_claims_the_same_lease_set_that_funding_preflight_observes(
    tmp_path: Path,
):
    manifest, runtime = _signed_runtime(tmp_path)

    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    entry = prepared.backend.material.signer_entry
    policy = prepared.backend.material.signing_policy

    assert entry is not None
    assert policy is not None
    assert policy.minimum_fee_shannons == 100_000
    assert ("spendable-input", entry.lease_resource_id) in (
        prepared.requirements.required_resource_claims
    )
    assert not any(
        resource_id.startswith(entry.lease_resource_id + "-")
        for kind, resource_id in prepared.requirements.required_resource_claims
        if kind == "spendable-input"
    )
    assert prepared.requirements.funding is not None
    assert not runtime.private_runtime_root.exists()


def test_type_id_release_builds_a_bounded_policy_from_the_exact_leased_input(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path, "task-08-type-id-data-cell")

    for slot in manifest.ordered_slots:
        prepared = runtime.prepare(manifest, slot, None)
        material = prepared.backend.material
        entry = material.signer_entry
        policy = material.signing_policy

        assert entry is not None
        assert policy is not None
        assert len(entry.leased_inputs) == 1
        leased = entry.leased_inputs[0]
        expected_args = type_id_args({
            "previous_output": {"index": hex(leased.index), "tx_hash": leased.tx_hash},
            "since": "0x0",
        }, 0)
        expected_type = {
            "args": "0x" + expected_args.hex(),
            "code_hash": TYPE_ID_CODE_HASH,
            "hash_type": TYPE_ID_HASH_TYPE,
        }
        assert policy.permitted_output_types == (None,)
        assert policy.required_type_id_output is not None
        assert policy.required_type_id_output.to_dict() == {
            "code_hash": TYPE_ID_CODE_HASH,
            "hash_type": TYPE_ID_HASH_TYPE,
            "output_index": 0,
        }
        assert expected_args.hex() not in json.dumps(policy.to_dict(), sort_keys=True)
        assert policy.maximum_transfer_shannons == 20_000_000_000
        assert policy.minimum_fee_shannons == 100_000
        assert policy.maximum_output_data_bytes == 32
        assert policy.maximum_transactions == 1
        assert policy.permitted_destination_locks[0]["args"] == "0x470dcdc5e44064909650113a274b3b36aecb6dc7"
        assert prepared.requirements.signing_policy_sha256 == policy.sha256
        fee = 100_000
        change = leased.capacity_shannons - policy.maximum_transfer_shannons - fee
        transaction = deepcopy(
            policy.to_dict()["request_format"]["unsigned_transaction_template"]
        )
        transaction["outputs"] = [
            {
                "capacity": hex(policy.maximum_transfer_shannons),
                "lock": policy.permitted_destination_locks[0],
                "type": expected_type,
            },
            {"capacity": hex(change), "lock": entry.own_lock, "type": None},
        ]
        transaction["outputs_data"] = [
            material.params.prompt_injected["payload_hex"],
            "0x",
        ]
        constrained = PolicyConstrainedSigner(
            policy,
            SimpleNamespace(),
            SimpleNamespace(),
        )
        _transaction, used, transferred, charged_fee = constrained._validate_transaction(
            {"transaction": transaction}
        )
        assert used == {leased.out_point}
        assert transferred == policy.maximum_transfer_shannons
        assert charged_fee == fee

        wrong_args = deepcopy(transaction)
        wrong_args["outputs"][0]["type"]["args"] = "0x" + "00" * 32
        missing_type = deepcopy(transaction)
        missing_type["outputs"][0]["type"] = None
        extra_type = deepcopy(transaction)
        extra_type["outputs"][1]["type"] = expected_type
        for refused in (wrong_args, missing_type, extra_type):
            with pytest.raises(SigningIntegrationError, match="violates"):
                PolicyConstrainedSigner(
                    policy,
                    SimpleNamespace(),
                    SimpleNamespace(),
                )._validate_transaction({"transaction": refused})
        assert not runtime.private_runtime_root.exists()


def test_uncertain_submission_retires_inputs_and_confirmed_submission_is_permanent(
    tmp_path: Path,
):
    manifest, runtime = _signed_runtime(tmp_path / "uncertain")
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    backend.preflight.direct_chain = _chain()
    backend.setup(prepared.intent, prepared.requirements, timeout_seconds=30)
    backend._record_submission_attempt()
    spendable = next(
        claim
        for claim in prepared.requirements.required_resource_claims
        if claim[0] == "spendable-input"
    )
    transaction = next(
        claim
        for claim in prepared.requirements.required_resource_claims
        if claim[0] == "transaction"
    )
    assert backend.cleanup_resource(
        prepared.intent, *spendable, timeout_seconds=30
    ) == "retired"
    assert backend.cleanup_resource(
        prepared.intent, *transaction, timeout_seconds=30
    ) == "retired"

    manifest, runtime = _signed_runtime(tmp_path / "confirmed")
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    backend.preflight.direct_chain = _chain()
    backend.setup(prepared.intent, prepared.requirements, timeout_seconds=30)
    backend._record_submission_attempt()
    backend._record_submission("0x" + "9" * 64)
    spendable = next(
        claim
        for claim in prepared.requirements.required_resource_claims
        if claim[0] == "spendable-input"
    )
    assert backend.cleanup_resource(
        prepared.intent, *spendable, timeout_seconds=30
    ) == "permanent"


def test_cleanup_refuses_a_symlinked_submission_marker(tmp_path: Path):
    manifest, runtime = _signed_runtime(tmp_path)
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    backend.preflight.direct_chain = _chain()
    backend.setup(prepared.intent, prepared.requirements, timeout_seconds=30)
    outside = tmp_path / "foreign-marker.json"
    outside.write_bytes(b'{"state":"submission-attempted"}\n')
    outside.chmod(0o600)
    (backend.material.private_dir / "submission-intent.marker").symlink_to(outside)
    spendable = next(
        claim
        for claim in prepared.requirements.required_resource_claims
        if claim[0] == "spendable-input"
    )

    with pytest.raises(CampaignRuntimeError, match="could not be read safely"):
        backend.cleanup_resource(prepared.intent, *spendable, timeout_seconds=30)
    assert outside.read_bytes() == b'{"state":"submission-attempted"}\n'


def test_protocol_decision_includes_agent_local_signer_refusals(tmp_path: Path):
    _binding, manifest, runtime = _runtime(tmp_path)
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    assert isinstance(backend, ProductionTaskBackend)
    backend._proxy_violation_check = lambda _arm, _workspace: False
    backend._agent = SimpleNamespace(protocol_violation_count=1)

    assert backend.protocol_violated(
        prepared.intent,
        timeout_seconds=30,
    ) is True

    backend._agent = SimpleNamespace(protocol_violation_count=0)
    assert backend.protocol_violated(
        prepared.intent,
        timeout_seconds=30,
    ) is False


def test_protocol_decision_includes_attempt_scoped_proxy_evidence(tmp_path: Path):
    _binding, manifest, runtime = _runtime(tmp_path)
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    assert isinstance(backend, ProductionTaskBackend)
    backend._agent = SimpleNamespace(protocol_violation_count=0)
    observed = []
    backend._proxy_violation_check = lambda arm, workspace: observed.append(
        (arm, workspace)
    ) or True

    assert backend.protocol_violated(prepared.intent, timeout_seconds=30) is True
    assert observed == [(prepared.intent.identity.arm, backend.material.workspace)]


def test_protocol_decision_fails_closed_without_proxy_evidence(tmp_path: Path):
    _binding, manifest, runtime = _runtime(tmp_path)
    prepared = runtime.prepare(manifest, manifest.ordered_slots[0], None)
    backend = prepared.backend
    assert isinstance(backend, ProductionTaskBackend)
    backend._agent = SimpleNamespace(protocol_violation_count=0)

    with pytest.raises(CampaignRuntimeError, match="proxy evidence boundary"):
        backend.protocol_violated(prepared.intent, timeout_seconds=30)
