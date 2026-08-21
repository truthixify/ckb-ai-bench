"""Matrix tests validate against an injected synthetic reviewed profile.

`validate_results()` refuses to run without the tracked phase-one profile, and that refusal is the
point: a report must be pinned to the reviewed model path. Tests must not depend on the real file
existing, and the real file must never be able to make this suite pass by accident, so every matrix
test runs against an explicit synthetic profile it controls.
"""

from __future__ import annotations

import pytest

from ckbbench.matrix import store
from ckbbench.matrix.test_fixtures import (
    SYNTHETIC_MODEL,
    SYNTHETIC_PROFILE_SHA256,
    SYNTHETIC_RESPONSE_MODEL,
)
from ckbbench.run.model_profile import parse_model_profile

SYNTHETIC_PROFILE_DOC = {
    "api_base": "https://synthetic.invalid/v1",
    "api_style": "openai-responses",
    "drop_unsupported_params": True,
    "evidence_utc": "2026-08-15T09:30:00Z",
    "litellm_num_retries": 0,
    "max_agent_query_attempts": 4,
    "model_stability": "moving_alias",
    "probed_response_model": SYNTHETIC_RESPONSE_MODEL,
    "profile_id": "phase1-gpt-v8",
    "provider": "openrouter",
    "provider_allow_fallbacks": False,
    "provider_order": ["openai"],
    "provider_require_parameters": True,
    "provider_request_timeout_seconds": 300,
    "provider_retry_backoff_seconds": [4, 8, 16],
    "reasoning_context": "prefix_tail_groups",
    "reasoning_effort": "medium",
    "replay_max_bytes": 131072,
    "replay_policy": "prefix-tail-groups-v1",
    "store": False,
    "requested_model": SYNTHETIC_MODEL,
    "retryable_provider_failure_categories": [
        "rate_limit", "timeout", "connection", "server", "protocol", "other_provider",
    ],
    "schema_version": "7",
    "temperature": None,
    "truncation": "disabled",
    "usage_contract": "openai-responses-usage-v1",
}


def synthetic_profile(**overrides):
    return parse_model_profile(
        {**SYNTHETIC_PROFILE_DOC, **overrides}, sha256=SYNTHETIC_PROFILE_SHA256
    )


@pytest.fixture
def reviewed_profile(monkeypatch):
    """Set the reviewed profile these results are validated against."""

    def use(**overrides):
        profile = synthetic_profile(**overrides)
        monkeypatch.setattr(store, "_reviewed_profile", lambda: profile)
        return profile

    use()
    return use


@pytest.fixture(autouse=True)
def _synthetic_reviewed_profile(reviewed_profile):
    """Every matrix test validates against the synthetic profile unless it sets another."""
    return reviewed_profile
