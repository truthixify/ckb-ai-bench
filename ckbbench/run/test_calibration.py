from __future__ import annotations

from dataclasses import replace
import io
from pathlib import Path

import pytest

from ckbbench.run.attempt_store import AttemptStore
from ckbbench.run.calibration import (
    CALIBRATION_EVIDENCE_SCHEMA_VERSION,
    CalibrationError,
    PreparedCalibrationAttempt,
    load_calibration_evidence,
    run_calibration,
    validate_calibration_intent,
)
from ckbbench.run.campaign import CampaignError, validate_intent_for_slot
from ckbbench.run.campaign_operator import main
from ckbbench.run.suite_release import CampaignReleaseBinding
from ckbbench.run.task_attempt import TaskAttemptIntent, canonical_json_bytes
from ckbbench.run.task_preflight import ChainIdentityObservation, CkbAiObservation, DependencyObservation
from ckbbench.run.test_campaign import _intent
from ckbbench.run.test_campaign_operator import Backend, Probe, _utc_now
from ckbbench.run.test_suite_release import CHAIN, _contract, _manifest, _requirements


CALIBRATION_ID = "calibration-" + "c" * 32


class FakeTestnetProbe(Probe):
    def __init__(self, intent, requirements):
        super().__init__(intent, requirements)
        chain = ChainIdentityObservation(
            chain_id=CHAIN.chain_id,
            genesis_hash=CHAIN.genesis_hash,
            tip_number=100,
            tip_hash="0x" + "d" * 64,
            request_count=4,
        )
        self.rpc_value = chain
        self.ckb_ai_value = CkbAiObservation(
            surface_id=requirements.ckb_ai_surface_id,
            surface_sha256=requirements.ckb_ai_surface_sha256,
            server_version=requirements.ckb_ai_server_version,
            catalog_sha256=requirements.ckb_ai_catalog_sha256,
            ready=True,
            request_count=7,
            chain_identity=replace(chain, request_count=4),
        )
        self.dependencies_value = DependencyObservation(
            _contract().dependency_evidence,
            chain.stable_identity_sha256,
            1,
        )

    def rpc(self, *, timeout_seconds: float | None):
        return self._read("rpc")


class Runtime:
    def __init__(self, surfaces):
        self.surfaces = {
            (surface.profile_id, surface.sha256): surface for surface in surfaces
        }
        self.calls = 0

    def prepare_calibration(self, manifest, slot, calibration_id):
        self.calls += 1
        ordinary = _intent(manifest, slot)
        intent = replace(
            ordinary,
            created_utc=_utc_now(),
            identity=replace(ordinary.identity, campaign_id=calibration_id),
        )
        surface = self.surfaces[(slot.treatment_profile_id, slot.treatment_profile_sha256)]
        requirements = replace(
            _requirements(intent, surface),
            model_qualification_utc=intent.created_utc,
        )
        return PreparedCalibrationAttempt(
            intent,
            requirements,
            FakeTestnetProbe(intent, requirements),
            Backend("pass", slot.max_score, slot.requested_model),
            slot.max_score,
        )


def _inputs(tmp_path: Path):
    release, control, treatment, manifest = _manifest(tmp_path)
    binding = CampaignReleaseBinding(release, (CHAIN,), (control, treatment))
    runtime = Runtime((control, treatment))
    return manifest, binding, runtime


def test_one_calibration_runs_one_slot_and_is_never_campaign_eligible(tmp_path: Path):
    manifest, binding, runtime = _inputs(tmp_path)
    slot = manifest.ordered_slots[0]
    store = AttemptStore(tmp_path / "calibration-attempt")
    output = tmp_path / "calibration.json"
    evidence, envelope = run_calibration(
        manifest,
        slot.slot_id,
        CALIBRATION_ID,
        store,
        output,
        binding,
        runtime,
    )

    assert runtime.calls == 1
    assert evidence.schema_version == CALIBRATION_EVIDENCE_SCHEMA_VERSION
    assert evidence.accepted_campaign_eligible is False
    assert evidence.task_id == slot.task_id
    assert evidence.arm == "B"
    assert evidence.observed_steps == 1
    assert evidence.observed_provider_calls == 1
    assert evidence.usage_status == "complete"
    assert evidence.cleanup_complete is True
    assert evidence.result_sha256 == envelope.result.sha256
    assert load_calibration_evidence(output) == evidence
    with pytest.raises(CampaignError, match="does not match"):
        validate_intent_for_slot(manifest, slot, envelope.intent)


def test_calibration_refuses_an_ordinary_campaign_intent(tmp_path: Path):
    manifest, _binding, _runtime = _inputs(tmp_path)
    slot = manifest.ordered_slots[0]
    with pytest.raises(CalibrationError, match="pilot identity"):
        validate_calibration_intent(manifest, slot, CALIBRATION_ID, _intent(manifest, slot))


def test_calibration_refuses_existing_storage_and_does_not_prepare(tmp_path: Path):
    manifest, binding, runtime = _inputs(tmp_path)
    root = tmp_path / "calibration-attempt"
    root.mkdir()
    with pytest.raises(CalibrationError, match="must be absent"):
        run_calibration(
            manifest,
            manifest.ordered_slots[0].slot_id,
            CALIBRATION_ID,
            AttemptStore(root),
            tmp_path / "calibration.json",
            binding,
            runtime,
        )
    assert runtime.calls == 0


def test_calibration_refuses_existing_summary_before_runtime_or_attempt_storage(tmp_path: Path):
    manifest, binding, runtime = _inputs(tmp_path)
    root = tmp_path / "calibration-attempt"
    output = tmp_path / "calibration.json"
    output.write_text("{}\n", encoding="ascii")

    with pytest.raises(CalibrationError, match="summary must be absent"):
        run_calibration(
            manifest,
            manifest.ordered_slots[0].slot_id,
            CALIBRATION_ID,
            AttemptStore(root),
            output,
            binding,
            runtime,
        )

    assert runtime.calls == 0
    assert not root.exists()


def test_calibration_evidence_loader_refuses_noncanonical_and_campaign_eligible_bytes(
    tmp_path: Path,
):
    manifest, binding, runtime = _inputs(tmp_path)
    output = tmp_path / "calibration.json"
    evidence, _envelope = run_calibration(
        manifest,
        manifest.ordered_slots[0].slot_id,
        CALIBRATION_ID,
        AttemptStore(tmp_path / "calibration-attempt"),
        output,
        binding,
        runtime,
    )
    output.write_text(str(evidence.to_dict()), encoding="ascii")
    with pytest.raises(CalibrationError, match="invalid"):
        load_calibration_evidence(output)

    document = evidence.to_dict()
    document["accepted_campaign_eligible"] = True
    with pytest.raises(CalibrationError, match="never be campaign-eligible"):
        type(evidence).from_dict(document)

    document = evidence.to_dict()
    document["recorded_utc"] = "not-a-timeZ"
    with pytest.raises(CalibrationError, match="recorded time"):
        type(evidence).from_dict(document)


def test_cli_requires_explicit_authorization_before_runtime_or_storage(tmp_path: Path):
    manifest, binding, runtime = _inputs(tmp_path)
    manifest_path = tmp_path / "campaign.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
    attempt_root = tmp_path / "calibration-attempt"
    output = tmp_path / "calibration.json"
    command = [
        "calibrate",
        "--manifest",
        str(manifest_path),
        "--slot",
        manifest.ordered_slots[0].slot_id,
        "--calibration-id",
        CALIBRATION_ID,
        "--attempt-root",
        str(attempt_root),
        "--output",
        str(output),
    ]
    stderr = io.StringIO()
    assert main(
        command,
        calibration_runtime=runtime,
        release_binding=binding,
        stderr=stderr,
    ) == 1
    assert "explicit live authorization" in stderr.getvalue()
    assert runtime.calls == 0
    assert not attempt_root.exists()
    assert not output.exists()

    stdout = io.StringIO()
    stderr = io.StringIO()
    assert main(
        [*command, "--authorized-by-user"],
        calibration_runtime=runtime,
        release_binding=binding,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert runtime.calls == 1
    assert CALIBRATION_ID in stdout.getvalue()
    assert stderr.getvalue() == ""
    assert load_calibration_evidence(output).accepted_campaign_eligible is False
