"""Strict schema tests for bounded verifier diagnostic evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ckbbench.verify.diagnostics import (
    MAX_DIAGNOSTIC_CRITERIA,
    VerificationDiagnosticError,
    VerificationDiagnostics,
)


def test_diagnostic_constructors_cover_every_supported_state():
    assert VerificationDiagnostics.completed(5, 0) == VerificationDiagnostics(
        "complete", 5, 0, 0, 5
    )
    assert VerificationDiagnostics.completed(3, 2) == VerificationDiagnostics(
        "complete", 3, 2, 0, 5
    )
    assert VerificationDiagnostics.stopped_at_failure(2, 5) == VerificationDiagnostics(
        "partial", 2, 1, 2, 5
    )
    assert VerificationDiagnostics.stopped_at_failure(4, 5) == VerificationDiagnostics(
        "complete", 4, 1, 0, 5
    )
    assert VerificationDiagnostics.not_evaluated(5) == VerificationDiagnostics(
        "not_evaluated", 0, 0, 5, 5
    )
    assert VerificationDiagnostics.unavailable() == VerificationDiagnostics(
        "unavailable", 0, 0, 0, 0
    )


def test_diagnostics_round_trip_with_exact_fields():
    diagnostic = VerificationDiagnostics.stopped_at_failure(2, 5)
    assert VerificationDiagnostics.from_dict(diagnostic.to_dict()) == diagnostic
    assert set(diagnostic.to_dict()) == {
        "criteria_failed",
        "criteria_not_evaluated",
        "criteria_passed",
        "criteria_total",
        "status",
    }


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {},
        {
            "criteria_failed": 0,
            "criteria_not_evaluated": 0,
            "criteria_passed": 1,
            "criteria_total": 1,
        },
        {
            "criteria_failed": 0,
            "criteria_not_evaluated": 0,
            "criteria_passed": 1,
            "criteria_total": 1,
            "status": "complete",
            "extra": True,
        },
    ],
)
def test_diagnostics_reject_non_objects_and_inexact_fields(document):
    with pytest.raises(VerificationDiagnosticError, match="exactly"):
        VerificationDiagnostics.from_dict(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("criteria_passed", True),
        ("criteria_failed", -1),
        ("criteria_not_evaluated", 1.0),
        ("criteria_total", MAX_DIAGNOSTIC_CRITERIA + 1),
    ],
)
def test_diagnostics_reject_invalid_counts(field: str, value: object):
    diagnostic = VerificationDiagnostics.completed(1, 0)
    with pytest.raises(VerificationDiagnosticError):
        replace(diagnostic, **{field: value})


@pytest.mark.parametrize(
    "diagnostic",
    [
        ("complete", 1, 0, 1, 2),
        ("partial", 1, 0, 0, 1),
        ("partial", 0, 0, 1, 1),
        ("partial", 1, 0, 1, 2),
        ("not_evaluated", 1, 0, 0, 1),
        ("unavailable", 0, 0, 1, 1),
        ("unknown", 0, 0, 0, 0),
        ("complete", 1, 0, 0, 2),
    ],
)
def test_diagnostics_reject_contradictory_states(diagnostic):
    with pytest.raises(VerificationDiagnosticError):
        VerificationDiagnostics(*diagnostic)


@pytest.mark.parametrize(("passed", "total"), [(1, 1), (2, 1)])
def test_stopped_at_failure_requires_room_for_the_failed_criterion(passed: int, total: int):
    with pytest.raises(VerificationDiagnosticError, match="failed criterion"):
        VerificationDiagnostics.stopped_at_failure(passed, total)
