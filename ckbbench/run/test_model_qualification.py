from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import ckbbench.run.model_qualification as qualification
from ckbbench.run.campaign import publish_document
from ckbbench.run.model_profile import load_run_profile
from ckbbench.run.provider_probe import (
    CompletionEvidence,
    ErrorStatusResponse,
    NonJsonResponse,
    ProbeError,
    ResponseFacts,
)


KEY = "sk-qualification-must-not-survive"
CANARY = "raw-provider-material-must-not-survive"
CREATED = "2026-09-02T10:00:00Z"
COMPLETED = "2026-09-02T10:00:03Z"
PROFILE = load_run_profile("gpt-5.6-luna")


class _Sender:
    def __init__(self) -> None:
        self.requests_sent = 0


def _evidence(**changes) -> CompletionEvidence:
    values = {
        "requests_sent": 1,
        "status_ok": True,
        "status_class": "2xx",
        "requested_model": PROFILE.requested_model,
        "returned_model": PROFILE.probed_response_model,
        "response_completed": True,
        "exactly_one_expected_tool_call": True,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "token_identity_holds": True,
    }
    values.update(changes)
    return CompletionEvidence(**values)


def _clock():
    values = iter((CREATED, COMPLETED))
    return lambda: next(values)


def _run(*, fail_at: int | None = None, evidence_changes=None, exception=None):
    senders: list[_Sender] = []

    def factory():
        sender = _Sender()
        senders.append(sender)
        return sender

    def probe(*, profile, api_key, transport):
        assert profile is PROFILE
        assert api_key == KEY
        transport.requests_sent += 1
        if fail_at == len(senders):
            if exception is not None:
                raise exception
            return _evidence(**(evidence_changes or {}))
        return _evidence()

    record = qualification.run_model_qualification(
        PROFILE,
        api_key=KEY,
        transport_factory=factory,
        probe=probe,
        clock=_clock(),
        qualification_id="qualification-0123456789abcdef0123456789abcdef",
    )
    return record, senders


def _document(**changes):
    record, _ = _run()
    document = record.to_dict()
    document.update(changes)
    return document


def test_three_independent_clean_requests_qualify_exact_profile_contract():
    record, senders = _run()
    assert record.outcome == "qualified"
    assert record.requests_sent == 3
    assert [sender.requests_sent for sender in senders] == [1, 1, 1]
    assert [sample.ordinal for sample in record.samples] == [1, 2, 3]
    assert all(sample.outcome == "passed" for sample in record.samples)
    assert record.profile_sha256 == PROFILE.sha256
    assert record.model_variant_id == PROFILE.model_variant_id
    assert record.api_base == PROFILE.api_base
    assert record.api_style == PROFILE.api_style
    assert record.thinking_level == PROFILE.thinking_level
    assert record.usage_contract == PROFILE.usage_contract
    assert len(record.request_payload_sha256) == 64
    assert len(record.request_extensions_sha256) == 64
    assert len(record.retry_policy_sha256) == 64


@pytest.mark.parametrize("ordinal", [1, 2, 3])
def test_first_failed_sample_stops_the_window(ordinal):
    record, senders = _run(
        fail_at=ordinal,
        evidence_changes={"returned_model": "different-model"},
    )
    assert record.outcome == "rejected"
    assert record.requests_sent == ordinal
    assert len(senders) == ordinal
    assert [sample.outcome for sample in record.samples] == [
        *("passed" for _ in range(ordinal - 1)),
        "failed",
    ]
    assert record.samples[-1].failure_kind == "model_drift"


@pytest.mark.parametrize(
    ("changes", "failure_kind"),
    [
        ({"requests_sent": 0}, "protocol_contract"),
        ({"status_ok": False}, "protocol_contract"),
        ({"status_class": "5xx"}, "protocol_contract"),
        ({"requested_model": "different-model"}, "protocol_contract"),
        ({"returned_model": "different-model"}, "model_drift"),
        ({"response_completed": False}, "incomplete_response"),
        ({"exactly_one_expected_tool_call": False}, "tool_contract"),
        ({"token_identity_holds": False}, "usage_contract"),
    ],
)
def test_response_contract_drift_is_rejected(changes, failure_kind):
    record, _ = _run(fail_at=1, evidence_changes=changes)
    assert record.outcome == "rejected"
    assert record.samples[0].failure_kind == failure_kind


def test_non_json_and_http_status_failures_keep_only_fixed_classification():
    facts = ResponseFacts(
        status_class="2xx",
        content_type="text/html",
        content_encoding="identity",
        byte_count=120,
        body_kind="html",
    )
    non_json, _ = _run(
        fail_at=1,
        exception=NonJsonResponse(
            facts,
            requests_sent=1,
            requested_model=PROFILE.requested_model,
            api_base=PROFILE.api_base,
        ),
    )
    assert non_json.samples[0].failure_kind == "non_json"
    assert non_json.samples[0].status_class == "2xx"

    error_facts = replace(facts, status_class="5xx", body_kind="plain_text")
    status, _ = _run(
        fail_at=1,
        exception=ErrorStatusResponse(
            error_facts,
            status=503,
            requests_sent=1,
            requested_model=PROFILE.requested_model,
            api_base=PROFILE.api_base,
        ),
    )
    assert status.samples[0].failure_kind == "http_status"
    assert status.samples[0].status_class == "5xx"


def test_unexpected_adapter_exception_is_sanitized_and_stops():
    record, senders = _run(fail_at=2, exception=RuntimeError(CANARY))
    rendered = json.dumps(record.to_dict())
    assert record.outcome == "rejected"
    assert record.samples[-1].failure_kind == "transport"
    assert len(senders) == 2
    assert CANARY not in rendered
    assert KEY not in rendered


def test_failure_before_a_send_is_valid_but_carries_no_response_evidence():
    senders: list[_Sender] = []

    def factory():
        sender = _Sender()
        senders.append(sender)
        return sender

    def probe(**_kwargs):
        raise ProbeError(CANARY)

    record = qualification.run_model_qualification(
        PROFILE,
        api_key=KEY,
        transport_factory=factory,
        probe=probe,
        clock=_clock(),
        qualification_id="qualification-0123456789abcdef0123456789abcdef",
    )
    assert record.requests_sent == 0
    assert record.samples[0].request_sent is False
    assert record.samples[0].status_class == "none"
    assert CANARY not in json.dumps(record.to_dict())


def test_transport_construction_failure_is_retained_as_an_unsent_rejection():
    def factory():
        raise RuntimeError(CANARY)

    record = qualification.run_model_qualification(
        PROFILE,
        api_key=KEY,
        transport_factory=factory,
        clock=_clock(),
        qualification_id="qualification-0123456789abcdef0123456789abcdef",
    )
    assert record.outcome == "rejected"
    assert record.requests_sent == 0
    assert record.samples[0].failure_kind == "transport"
    assert CANARY not in json.dumps(record.to_dict())


def test_record_round_trips_and_validates_for_the_exact_current_profile():
    record, _ = _run()
    restored = qualification.ModelQualification.from_dict(record.to_dict())
    assert restored == record
    restored.validate_for_profile(PROFILE, checked_utc="2026-09-03T10:00:00Z")


@pytest.mark.parametrize(
    "field",
    [
        "profile_id",
        "profile_sha256",
        "model_variant_id",
        "api_base",
        "api_style",
        "requested_model",
        "probed_response_model",
        "thinking_level",
        "request_payload_sha256",
        "request_extensions_sha256",
        "retry_policy_sha256",
        "usage_contract",
    ],
)
def test_any_profile_contract_drift_is_rejected(field):
    record, _ = _run()
    if field == "api_base":
        changed = replace(record, api_base="https://different.example/v1")
    elif field == "probed_response_model":
        samples = tuple(
            replace(sample, returned_model="different-probed-response-model")
            for sample in record.samples
        )
        changed = replace(
            record,
            probed_response_model="different-probed-response-model",
            samples=samples,
        )
    elif field.endswith("sha256"):
        changed = replace(record, **{field: "f" * 64})
    else:
        changed = replace(record, **{field: f"different-{field}"})
    with pytest.raises(qualification.ModelQualificationError, match="differs"):
        changed.validate_for_profile(PROFILE, checked_utc="2026-09-03T10:00:00Z")


@pytest.mark.parametrize(
    "checked",
    ["2026-09-02T09:59:59Z", "2026-10-04T10:00:04Z"],
)
def test_future_or_stale_evidence_is_rejected(checked):
    record, _ = _run()
    with pytest.raises(qualification.ModelQualificationError, match="stale"):
        record.validate_for_profile(PROFILE, checked_utc=checked)


def test_rejected_evidence_is_never_accepted_for_a_campaign():
    record, _ = _run(fail_at=1, evidence_changes={"response_completed": False})
    with pytest.raises(qualification.ModelQualificationError, match="no accepted"):
        record.validate_for_profile(PROFILE, checked_utc="2026-09-03T10:00:00Z")


@pytest.mark.parametrize(
    "change",
    [
        {"qualification_id": "human-readable-name"},
        {"request_limit": 4},
        {"requests_sent": 2},
        {"outcome": "maybe"},
        {"schema_version": "old"},
        {"qualification_kind": "catalog-only"},
        {"completed_utc": "2026-09-02T09:59:59Z"},
    ],
)
def test_malformed_top_level_records_are_rejected(change):
    with pytest.raises(qualification.ModelQualificationError):
        qualification.ModelQualification.from_dict(_document(**change))


def test_partial_success_cannot_claim_qualification():
    document = _document()
    document["samples"] = document["samples"][:2]
    document["requests_sent"] = 2
    with pytest.raises(qualification.ModelQualificationError, match="three stable"):
        qualification.ModelQualification.from_dict(document)


def test_rejected_record_cannot_continue_after_its_first_failure():
    document = _document(outcome="rejected")
    document["samples"][1]["outcome"] = "failed"
    document["samples"][1]["failure_kind"] = "model_drift"
    with pytest.raises(qualification.ModelQualificationError, match="first failed"):
        qualification.ModelQualification.from_dict(document)


def test_exact_sample_schema_refuses_extra_fields_and_invalid_unsent_evidence():
    document = _document()
    document["samples"][0]["extra"] = True
    with pytest.raises(qualification.ModelQualificationError, match="sample"):
        qualification.ModelQualification.from_dict(document)

    sample = {
        "exactly_one_expected_tool_call": False,
        "failure_kind": "transport",
        "input_tokens": 1,
        "ordinal": 1,
        "outcome": "failed",
        "output_tokens": None,
        "request_sent": False,
        "response_completed": False,
        "returned_model": None,
        "status_class": "none",
        "token_identity_holds": False,
        "total_tokens": None,
    }
    with pytest.raises(qualification.ModelQualificationError, match="unsent"):
        qualification.QualificationSample.from_dict(sample)


def test_canonical_evidence_is_write_once_and_digest_stable(tmp_path):
    record, _ = _run()
    path = tmp_path / "qualification.json"
    publish_document(path, record.to_dict(), "model qualification evidence")
    assert qualification.load_model_qualification(path) == record
    assert qualification.qualification_sha256(path) == record.sha256
    with pytest.raises(Exception, match="already exists"):
        publish_document(path, record.to_dict(), "model qualification evidence")


@pytest.mark.parametrize("payload", [b"{}", b'{"a":1,"a":2}\n', b"not-json\n"])
def test_noncanonical_duplicate_or_malformed_files_are_rejected(tmp_path, payload):
    path = tmp_path / "qualification.json"
    path.write_bytes(payload)
    with pytest.raises(qualification.ModelQualificationError):
        qualification.load_model_qualification(path)


def test_symlink_and_oversized_files_are_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_text("{}\n")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(qualification.ModelQualificationError, match="regular file"):
        qualification.load_model_qualification(link)
    target.write_bytes(b"x" * (qualification.MAX_QUALIFICATION_BYTES + 1))
    with pytest.raises(qualification.ModelQualificationError, match="size limit"):
        qualification.load_model_qualification(target)


def test_cli_refuses_before_live_runner_without_explicit_authorization(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "benchmark-output" / "model-qualifications"
    monkeypatch.setattr(qualification, "QUALIFICATION_ROOT", root)
    monkeypatch.setattr(qualification, "load_run_profile", lambda _selection: PROFILE)
    called = False

    def runner(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("runner must stay offline")

    destination = root / "evidence.json"
    rc = qualification.main(
        ["--profile", "p", "--output", str(destination)],
        runner=runner,
    )
    assert rc == 2
    assert called is False
    assert not destination.exists()
    assert "refused" in capsys.readouterr().err


def test_cli_publishes_success_once_without_retaining_the_key(tmp_path, monkeypatch, capsys):
    root = tmp_path / "benchmark-output" / "model-qualifications"
    destination = root / "evidence.json"
    record, _ = _run()
    monkeypatch.setattr(qualification, "QUALIFICATION_ROOT", root)
    monkeypatch.setattr(qualification, "load_run_profile", lambda _selection: PROFILE)
    monkeypatch.setattr(qualification, "resolve_llm_api_key", lambda _name: KEY)

    def runner(profile, *, api_key):
        assert profile is PROFILE and api_key == KEY
        return record

    argv = [
        "--profile",
        "p",
        "--output",
        str(destination),
        "--authorized-by-user",
    ]
    assert qualification.main(argv, runner=runner) == 0
    output = capsys.readouterr()
    assert record.qualification_id in output.out
    assert KEY not in output.out + output.err + destination.read_text()
    assert qualification.main(argv, runner=runner) == 2


def test_cli_publishes_a_rejected_live_outcome_and_returns_one(tmp_path, monkeypatch):
    root = tmp_path / "benchmark-output" / "model-qualifications"
    destination = root / "rejected.json"
    record, _ = _run(fail_at=2, exception=ProbeError(CANARY))
    monkeypatch.setattr(qualification, "QUALIFICATION_ROOT", root)
    monkeypatch.setattr(qualification, "load_run_profile", lambda _selection: PROFILE)
    monkeypatch.setattr(qualification, "resolve_llm_api_key", lambda _name: KEY)

    assert qualification.main(
        [
            "--profile",
            "p",
            "--output",
            str(destination),
            "--authorized-by-user",
        ],
        runner=lambda _profile, *, api_key: record,
    ) == 1
    restored = qualification.load_model_qualification(destination)
    assert restored.outcome == "rejected"
    assert restored.requests_sent == 2
    assert CANARY not in destination.read_text()


def test_cli_rejects_destinations_outside_the_generated_evidence_root(
    tmp_path, monkeypatch
):
    root = tmp_path / "benchmark-output" / "model-qualifications"
    monkeypatch.setattr(qualification, "QUALIFICATION_ROOT", root)
    monkeypatch.setattr(qualification, "load_run_profile", lambda _selection: PROFILE)
    assert qualification.main([
        "--profile",
        "p",
        "--output",
        str(tmp_path / "elsewhere.json"),
        "--authorized-by-user",
    ]) == 2


def test_generated_record_never_contains_tool_arguments_response_ids_or_raw_material():
    record, _ = _run()
    rendered = json.dumps(record.to_dict(), sort_keys=True)
    forbidden = (
        KEY,
        CANARY,
        "Authorization",
        "Bearer",
        "call_id",
        "arguments",
        "response_id",
        "echo ckbbench-probe",
    )
    assert all(value not in rendered for value in forbidden)
