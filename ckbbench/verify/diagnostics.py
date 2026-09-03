"""Bounded diagnostic evidence produced by an independent verifier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DIAGNOSTIC_STATUSES = frozenset({"complete", "partial", "not_evaluated", "unavailable"})
MAX_DIAGNOSTIC_CRITERIA = 10_000


class VerificationDiagnosticError(ValueError):
    """A verifier diagnostic record is malformed or contradictory."""


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationDiagnosticError(f"{label} must be an integer")
    if value < 0 or value > MAX_DIAGNOSTIC_CRITERIA:
        raise VerificationDiagnosticError(
            f"{label} must be between 0 and {MAX_DIAGNOSTIC_CRITERIA}"
        )
    return value


@dataclass(frozen=True)
class VerificationDiagnostics:
    """Aggregate criterion progress that never contributes partial benchmark credit."""

    status: str
    criteria_passed: int
    criteria_failed: int
    criteria_not_evaluated: int
    criteria_total: int

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in DIAGNOSTIC_STATUSES:
            raise VerificationDiagnosticError("verification diagnostic status is unsupported")
        passed = _count(self.criteria_passed, "criteria_passed")
        failed = _count(self.criteria_failed, "criteria_failed")
        not_evaluated = _count(
            self.criteria_not_evaluated,
            "criteria_not_evaluated",
        )
        total = _count(self.criteria_total, "criteria_total")
        if passed + failed + not_evaluated != total:
            raise VerificationDiagnosticError("verification diagnostic counts do not sum to total")
        if self.status == "unavailable":
            if any((passed, failed, not_evaluated, total)):
                raise VerificationDiagnosticError(
                    "unavailable verification diagnostics must use structural zero counts"
                )
            return
        if total == 0:
            raise VerificationDiagnosticError("available verification diagnostics need criteria")
        if self.status == "complete" and not_evaluated != 0:
            raise VerificationDiagnosticError(
                "complete verification diagnostics cannot have unevaluated criteria"
            )
        if self.status == "partial" and (not_evaluated == 0 or failed == 0):
            raise VerificationDiagnosticError(
                "partial verification diagnostics need failed and unevaluated criteria"
            )
        if self.status == "not_evaluated" and (
            passed != 0 or failed != 0 or not_evaluated != total
        ):
            raise VerificationDiagnosticError(
                "not-evaluated verification diagnostics cannot carry evaluated criteria"
            )

    @classmethod
    def unavailable(cls) -> VerificationDiagnostics:
        return cls("unavailable", 0, 0, 0, 0)

    @classmethod
    def not_evaluated(cls, total: int) -> VerificationDiagnostics:
        return cls("not_evaluated", 0, 0, total, total)

    @classmethod
    def completed(cls, passed: int, failed: int) -> VerificationDiagnostics:
        return cls("complete", passed, failed, 0, passed + failed)

    @classmethod
    def stopped_at_failure(cls, passed: int, total: int) -> VerificationDiagnostics:
        if passed >= total:
            raise VerificationDiagnosticError("a failed criterion must remain inside the total")
        not_evaluated = total - passed - 1
        status = "partial" if not_evaluated else "complete"
        return cls(status, passed, 1, not_evaluated, total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "criteria_failed": self.criteria_failed,
            "criteria_not_evaluated": self.criteria_not_evaluated,
            "criteria_passed": self.criteria_passed,
            "criteria_total": self.criteria_total,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, document: Any) -> VerificationDiagnostics:
        keys = {
            "criteria_failed",
            "criteria_not_evaluated",
            "criteria_passed",
            "criteria_total",
            "status",
        }
        if not isinstance(document, dict) or set(document) != keys:
            raise VerificationDiagnosticError(
                "verification diagnostics must contain exactly the reviewed fields"
            )
        return cls(**document)
