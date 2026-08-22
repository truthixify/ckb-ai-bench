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
    correctness_evidence_complete,
    response_model_identity,
)


class _Ledger:
    """Stands in for the fork's ledger with the same read surface."""

    def __init__(self, *, turns, attempts, responses, totals, complete, models=("gpt-x",),
                 correctness_complete=None):
        self.turn_count = turns
        self.attempt_count = attempts
        self.response_count = responses
        self._totals = totals
        self._complete = complete
        self.response_models = set(models)
        self._correctness_complete = (
            responses == turns and turns > 0 and len(self.response_models) == 1
            if correctness_complete is None else correctness_complete
        )

    def totals(self):
        return self._totals

    def is_complete(self):
        return self._complete

    def is_correctness_complete(self):
        return self._correctness_complete


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


def test_history_compaction_evidence_is_collected_without_content():
    ledger = _Ledger(turns=3, attempts=3, responses=3, totals=(120, 45, 165), complete=True)
    ledger.history_compaction_count = 2
    ledger.history_dropped_groups = 5
    ledger.history_dropped_items = 12
    ledger.history_max_prepared_bytes = 131000
    metrics = _metrics(ledger)
    assert (
        metrics.history_compaction_count,
        metrics.history_dropped_groups,
        metrics.history_dropped_items,
        metrics.history_max_prepared_bytes,
    ) == (2, 5, 12, 131000)
    assert not hasattr(metrics, "history") and not hasattr(metrics, "messages")


def test_an_unequal_count_is_incomplete_even_when_the_ledger_says_otherwise():
    """`complete` requires model_calls == attempts == responses, not just valid usage."""
    m = _metrics(_Ledger(turns=3, attempts=4, responses=3, totals=(1, 1, 2), complete=True))
    assert m.token_usage_status == INCOMPLETE
    assert m.efficiency_eligible is False


def test_a_recovered_attempt_is_correctness_complete_but_efficiency_incomplete():
    ledger = _Ledger(
        turns=1, attempts=2, responses=1, totals=(10, 5, 15), complete=False,
        correctness_complete=True,
    )
    agent = _Agent(ledger)
    metrics = collect_metrics_from_agent(agent, wall_seconds=1.0)
    assert correctness_evidence_complete(agent) is True
    assert metrics.token_usage_status == INCOMPLETE
    assert metrics.efficiency_eligible is False


def test_correctness_completeness_fails_closed_without_the_ledger_method():
    ledger = _Ledger(turns=1, attempts=1, responses=1, totals=(1, 1, 2), complete=True)
    ledger.is_correctness_complete = None
    assert correctness_evidence_complete(_Agent(ledger)) is False


@pytest.mark.parametrize("result", [1, "yes", None])
def test_correctness_completeness_requires_an_exact_boolean_true(result):
    ledger = _Ledger(turns=1, attempts=1, responses=1, totals=(1, 1, 2), complete=True)
    ledger.is_correctness_complete = lambda: result
    assert correctness_evidence_complete(_Agent(ledger)) is False


def test_correctness_completeness_fails_closed_when_the_ledger_check_raises():
    ledger = _Ledger(turns=1, attempts=1, responses=1, totals=(1, 1, 2), complete=True)
    ledger.is_correctness_complete = lambda: (_ for _ in ()).throw(RuntimeError("broken"))
    assert correctness_evidence_complete(_Agent(ledger)) is False


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


# --- one failed attempt survives from the ledger to a validated result row -------------------------

class _FailedLedger:
    """The read surface `collect_metrics_from_agent` uses, after a provider attempt went unanswered."""

    turn_count = 2
    attempt_count = 2
    response_count = 1
    response_models = {"gpt-5.6-sol"}
    internal_errors = 0
    provider_failure_category = "connection"
    provider_failure_counts = {"connection": 1}
    retry_count = 0
    retry_delay_seconds = 0

    def totals(self):
        return (10, 5, 15)

    def is_complete(self):
        return False

    def is_correctness_complete(self):
        return False


class _FailedAgent:
    def __init__(self, ledger):
        self.model = type("M", (), {"usage_ledger": ledger})()


def test_the_failure_category_is_collected_from_the_ledger():
    metrics = collect_metrics_from_agent(_FailedAgent(_FailedLedger()), wall_seconds=1.0)
    assert metrics.provider_failure_category == "connection"
    assert metrics.provider_failure_counts == {"connection": 1}
    assert metrics.token_usage_status == "incomplete"
    assert metrics.provider_attempts == 2 and metrics.provider_responses == 1


@pytest.mark.parametrize("value", [
    "OSError", "", " ", "CONNECTION", "unknown", True, 7, 1.5, None,
    ["connection"], ("connection",), {"connection"}, {"category": "connection"},
])
def test_only_an_allowlisted_category_is_accepted_from_the_ledger(value):
    """An unhashable value must reduce to None, not raise: `in` alone would TypeError."""
    ledger = _FailedLedger()
    ledger.provider_failure_category = value
    metrics = collect_metrics_from_agent(_FailedAgent(ledger), wall_seconds=1.0)
    expected = value if value in ("connection",) else None
    assert metrics.provider_failure_category == expected


def test_a_ledgerless_agent_reports_no_category():
    metrics = collect_metrics_from_agent(object(), wall_seconds=0.0)
    assert metrics.provider_failure_category is None
    assert metrics.token_usage_status == "not_started"


def test_the_category_survives_serialization_and_store_validation(tmp_path, monkeypatch):
    """Ledger -> RunMetrics -> RunResult -> JSON -> load -> validate, as one path."""
    import json

    import ckbbench.matrix.store as store
    from ckbbench.matrix.conftest import synthetic_profile
    from ckbbench.matrix.store import (
        ResultSuiteContract,
        ResultTaskContract,
        load_results,
        validate_results,
    )
    from ckbbench.matrix.test_fixtures import (
        SYNTHETIC_MCP_VERSION,
        SYNTHETIC_SUITE_FREEZE,
        SYNTHETIC_SUITE_SEMVER,
        SYNTHETIC_TASK_ID,
        synthetic_run_dict,
    )

    # The matrix package's autouse reviewed-profile fixture does not reach this module, so the
    # synthetic profile is injected explicitly rather than validating against the committed one.
    monkeypatch.setattr(store, "_reviewed_profile", lambda: synthetic_profile())

    metrics = collect_metrics_from_agent(_FailedAgent(_FailedLedger()), wall_seconds=1.0)
    row = synthetic_run_dict(arm="B", outcome="infra_fail", run_id="b1", metrics=metrics)
    assert row["metrics"]["provider_failure_category"] == "connection"

    results = tmp_path / "2.0.0"
    results.mkdir()
    (results / "b1.json").write_text(json.dumps(row))
    loaded = load_results(results)
    assert loaded[0]["metrics"]["provider_failure_category"] == "connection"
    validate_results(
        loaded,
        suite_contracts=(
            ResultSuiteContract(
                suite_semver=SYNTHETIC_SUITE_SEMVER,
                suite_freeze_hash=SYNTHETIC_SUITE_FREEZE,
                mcp_server_version=SYNTHETIC_MCP_VERSION,
                tasks=(ResultTaskContract(SYNTHETIC_TASK_ID, 10, True),),
                max_score=10,
            ),
        ),
    )


def test_complete_and_not_started_metrics_serialize_a_null_category():
    from ckbbench.matrix.test_fixtures import synthetic_run_dict

    for outcome in ("pass", "infra_fail"):
        row = synthetic_run_dict(arm="B", outcome=outcome, run_id=f"{outcome}-x")
        assert row["metrics"]["provider_failure_category"] is None
