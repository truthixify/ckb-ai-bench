"""Metrics v1 tests: total wall + tokens, best-effort when usage absent (RECOMMENDATION §5)."""

from __future__ import annotations

from ckbbench.run.metrics import RunMetrics, _usage_total_tokens, collect_metrics_from_agent


class FakeAgent:
    def __init__(self, messages: list) -> None:
        self.messages = messages


def test_collect_metrics_wall_and_tokens():
    agent = FakeAgent(
        [
            {"extra": {"response": {"usage": {"total_tokens": 100}}}},
            {"extra": {"response": {"usage": {"prompt_tokens": 40, "completion_tokens": 10}}}},
        ]
    )
    metrics = collect_metrics_from_agent(agent, wall_seconds=12.5)
    assert metrics.total_wall_seconds == 12.5
    assert metrics.total_tokens == 150


def test_collect_metrics_tokens_none_when_usage_absent():
    agent = FakeAgent([{"role": "assistant"}, {"extra": {"response": {}}}])
    metrics = collect_metrics_from_agent(agent, wall_seconds=1.0)
    assert metrics.total_tokens is None


def test_collect_metrics_ignores_non_dict_messages():
    agent = FakeAgent(
        [
            "not-a-dict",
            {"extra": "bad"},
            {"extra": {"response": "bad"}},
            {"extra": {"response": {"usage": {}}}},
        ]
    )
    metrics = collect_metrics_from_agent(agent, wall_seconds=0.5)
    assert metrics.total_tokens is None


def test_usage_total_tokens_helpers():
    assert _usage_total_tokens({"total_tokens": 7}) == 7
    assert _usage_total_tokens({"prompt_tokens": 3, "completion_tokens": 4}) == 7
    assert _usage_total_tokens({}) is None


def test_run_metrics_dataclass():
    m = RunMetrics(total_wall_seconds=2.0, total_tokens=None)
    assert m.total_wall_seconds == 2.0
    assert m.total_tokens is None