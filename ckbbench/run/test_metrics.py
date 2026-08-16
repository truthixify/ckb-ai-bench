"""Metrics v1 tests: run wall-time plus provider token provenance (ADR-0014).

The status is the point: a run whose usage cannot be established completely must say so rather than
report a number that looks like a full billable total.
"""

from __future__ import annotations

import pytest

from ckbbench.run.metrics import (
    COMPLETE,
    INCOMPLETE,
    NOT_STARTED,
    RunMetrics,
    collect_metrics_from_agent,
    response_model_identity,
)


class _Ledger:
    """Stands in for the fork's ledger with the same read surface."""

    def __init__(self, *, turns, attempts, responses, totals, complete, models=("gpt-x",)):
        self.turn_count = turns
        self.attempt_count = attempts
        self.response_count = responses
        self._totals = totals
        self._complete = complete
        self.response_models = set(models)

    def totals(self):
        return self._totals

    def is_complete(self):
        return self._complete


class _Model:
    def __init__(self, ledger):
        self.usage_ledger = ledger


class _Agent:
    def __init__(self, ledger=None):
        if ledger is not None:
            self.model = _Model(ledger)


def _metrics(ledger, wall=2.0):
    return collect_metrics_from_agent(_Agent(ledger), wall_seconds=wall)


def test_an_agent_with_no_ledger_reports_not_started():
    """Absence of evidence is recorded as absence, never as a zero-token run."""
    m = collect_metrics_from_agent(_Agent(), wall_seconds=1.5)
    assert m.token_usage_status == NOT_STARTED
    assert (m.model_calls, m.provider_attempts, m.provider_responses) == (0, 0, 0)
    assert (m.prompt_tokens, m.completion_tokens, m.total_tokens) == (None, None, None)
    assert m.total_wall_seconds == 1.5
    assert m.efficiency_eligible is False


def test_an_untouched_ledger_reports_not_started():
    m = _metrics(_Ledger(turns=0, attempts=0, responses=0, totals=None, complete=False))
    assert m.token_usage_status == NOT_STARTED
    assert m.total_tokens is None


def test_a_fully_observed_run_is_complete_and_sums_exactly():
    m = _metrics(_Ledger(turns=3, attempts=3, responses=3, totals=(120, 45, 165), complete=True))
    assert m.token_usage_status == COMPLETE
    assert (m.prompt_tokens, m.completion_tokens, m.total_tokens) == (120, 45, 165)
    assert (m.model_calls, m.provider_attempts, m.provider_responses) == (3, 3, 3)
    assert m.efficiency_eligible is True


def test_an_unequal_count_is_incomplete_even_when_the_ledger_says_otherwise():
    """`complete` requires model_calls == attempts == responses, not just valid usage."""
    m = _metrics(_Ledger(turns=3, attempts=4, responses=3, totals=(1, 1, 2), complete=True))
    assert m.token_usage_status == INCOMPLETE
    assert m.efficiency_eligible is False


def test_known_tokens_survive_a_later_failure_as_an_explicit_lower_bound():
    """The sums are kept as evidence, but never labelled a full total."""
    m = _metrics(_Ledger(turns=2, attempts=2, responses=1, totals=(10, 5, 15), complete=False))
    assert m.token_usage_status == INCOMPLETE
    assert (m.prompt_tokens, m.completion_tokens, m.total_tokens) == (10, 5, 15)
    assert m.efficiency_eligible is False


def test_incomplete_with_no_usable_usage_keeps_the_three_fields_null():
    m = _metrics(_Ledger(turns=1, attempts=1, responses=0, totals=None, complete=False))
    assert m.token_usage_status == INCOMPLETE
    assert (m.prompt_tokens, m.completion_tokens, m.total_tokens) == (None, None, None)
    assert m.provider_responses == 0


@pytest.mark.parametrize("models,expected", [
    (("gpt-x",), "gpt-x"),
    ((), None),
    (("gpt-x", "gpt-y"), None),
])
def test_the_returned_model_identity_is_never_guessed(models, expected):
    ledger = _Ledger(turns=1, attempts=1, responses=1, totals=(1, 1, 2), complete=True,
                     models=models)
    assert response_model_identity(_Agent(ledger)) is expected


def test_a_missing_ledger_has_no_model_identity():
    assert response_model_identity(_Agent()) is None


def test_run_metrics_defaults_are_the_not_started_shape():
    m = RunMetrics(total_wall_seconds=2.0)
    assert m.token_usage_status == NOT_STARTED
    assert m.total_tokens is None and m.prompt_tokens is None and m.completion_tokens is None
    assert (m.model_calls, m.provider_attempts, m.provider_responses) == (0, 0, 0)
