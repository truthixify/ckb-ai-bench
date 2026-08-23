"""Profile loader tests: the reviewed model path must be exact and safe to publish (ADR-0014).

A profile that parses loosely would let two rows claim one configuration while running another, and
a profile that carries a credential would publish it.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from pathlib import Path

import pytest

from ckbbench.run import model_profile as model_profile_mod
from ckbbench.run.model_profile import (
    PROVIDER_REQUEST_TIMEOUT_SECONDS,
    ModelProfile,
    ModelProfileError,
    available_run_profiles,
    load_model_profile,
    load_report_profile,
    load_run_profile,
    parse_model_profile,
)

VALID = {
    "api_base": "https://proxy.example/v1",
    "api_style": "openai-responses",
    "drop_unsupported_params": True,
    "evidence_utc": "2026-08-15T09:30:00Z",
    "litellm_num_retries": 0,
    "max_agent_query_attempts": 4,
    "model_stability": "moving_alias",
    "probed_response_model": "openai/gpt-5-mini",
    "observation_max_bytes": 32768,
    "profile_id": "phase1-model-openrouter-gpt-5-mini-v1",
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
    "requested_model": "openai/gpt-5-mini",
    "retryable_provider_failure_categories": [
        "rate_limit", "timeout", "connection", "server", "protocol", "other_provider",
    ],
    "schema_version": "8",
    "temperature": None,
    "truncation": "disabled",
    "usage_contract": "openai-responses-usage-v1",
}
CKBUILDERS_VALID = {
    **VALID,
    "api_base": "https://share-ai.ckbdev.com",
    "provider": "ckbuilders",
    "provider_allow_fallbacks": False,
    "provider_order": [],
    "provider_require_parameters": False,
    "probed_response_model": "gpt-5.6-sol",
    "requested_model": "gpt-5.6-sol",
    "reasoning_effort": "high",
    "temperature": 0,
    "truncation": "omitted",
}
DEEPSEEK_VALID = {
    **VALID,
    "probed_response_model": "deepseek/deepseek-v4-flash-0731",
    "provider_order": ["open-inference/fp4"],
    "reasoning_effort": "high",
    "requested_model": "deepseek/deepseek-v4-flash-0731",
}
ROUTED_VALID = {
    **VALID,
    "probed_response_model": "google/gemini-3.7-flash",
    "provider_order": ["google-vertex/global"],
    "reasoning_effort": "high",
    "requested_model": "google/gemini-3.7-flash",
}
CANARIES = ("sk-live-do-not-log", "tok-abc123", "raw-server-body")


def _write(tmp_path: Path, doc: dict, *, sort_keys: bool = True, indent: int | None = 2) -> Path:
    path = tmp_path / "phase1-model.json"
    path.write_text(json.dumps(doc, sort_keys=sort_keys, indent=indent) + "\n")
    return path


def test_a_valid_profile_loads_as_an_immutable_value_bound_to_the_file_bytes(tmp_path: Path):
    path = _write(tmp_path, VALID)
    profile = load_model_profile(path)
    assert profile.requested_model == "openai/gpt-5-mini"
    assert profile.litellm_model_name == "openai/openai/gpt-5-mini"
    assert profile.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(Exception):
        profile.requested_model = "other"  # type: ignore[misc]


def test_reformatting_changes_the_digest_but_not_the_parsed_semantics(tmp_path: Path):
    """The digest binds the exact tracked bytes, so a reformatted file is a different profile."""
    a = load_model_profile(_write(tmp_path, VALID))
    other = tmp_path / "other"
    other.mkdir()
    b = load_model_profile(_write(other, VALID, sort_keys=False, indent=None))
    assert a.sha256 != b.sha256
    assert (a.requested_model, a.api_base, a.temperature) == (
        b.requested_model, b.api_base, b.temperature
    )


def test_the_internal_name_keeps_the_litellm_provider_and_openrouter_catalog_namespaces():
    profile = parse_model_profile(VALID, sha256="a" * 64)
    assert profile.litellm_model_name == "openai/openai/gpt-5-mini"


def test_deepseek_uses_its_pinned_openrouter_route_and_reasoning_contract():
    profile = parse_model_profile(DEEPSEEK_VALID, sha256="d" * 64)
    assert profile.litellm_model_name == "openai/deepseek/deepseek-v4-flash-0731"
    assert profile.reasoning() == {"effort": "high"}
    assert profile.provider_extra_body() == {
        "provider": {
            "order": ["open-inference/fp4"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }


def test_a_routed_profile_uses_its_provider_and_pinned_high_reasoning():
    profile = parse_model_profile(ROUTED_VALID, sha256="e" * 64)
    assert profile.litellm_model_name == "openai/google/gemini-3.7-flash"
    assert profile.reasoning() == {"effort": "high"}
    assert profile.provider_extra_body() == {
        "provider": {
            "order": ["google-vertex/global"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }


def test_route_and_reasoning_are_profile_data_instead_of_model_specific_code():
    profile = parse_model_profile(
        {**ROUTED_VALID, "provider_order": ["another-route"], "reasoning_effort": "max"},
        sha256="e" * 64,
    )
    assert profile.provider_order == ("another-route",)
    assert profile.reasoning_effort == "max"


def test_an_openrouter_profile_still_requires_one_exact_route():
    for route in ([], ["openai", "another-route"]):
        with pytest.raises(ModelProfileError):
            parse_model_profile({**ROUTED_VALID, "provider_order": route}, sha256="e" * 64)


def test_a_ckbuilders_profile_uses_the_direct_responses_path_without_openrouter_routing():
    profile = parse_model_profile(CKBUILDERS_VALID, sha256="c" * 64)
    assert profile.litellm_model_name == "openai/gpt-5.6-sol"
    assert profile.provider == "ckbuilders"
    assert profile.provider_extra_body() is None
    assert profile.model_kwargs()["temperature"] == 0
    assert "extra_body" not in profile.model_kwargs()
    assert "truncation" not in profile.model_kwargs()
    assert "provider route: direct" in profile.summary_lines()


@pytest.mark.parametrize("field,value", [
    ("provider_order", ["openai"]),
    ("provider_allow_fallbacks", True),
    ("provider_require_parameters", True),
    ("requested_model", "openai/gpt-5.6-sol"),
])
def test_a_ckbuilders_profile_refuses_openrouter_only_settings(field, value):
    with pytest.raises(ModelProfileError):
        parse_model_profile({**CKBUILDERS_VALID, field: value}, sha256="c" * 64)


def test_an_unsupported_provider_fails_before_any_request_can_exist():
    with pytest.raises(ModelProfileError, match="provider must be one of"):
        parse_model_profile({**VALID, "provider": "unknown"}, sha256="c" * 64)


def test_a_missing_key_fails():
    doc = {k: v for k, v in VALID.items() if k != "temperature"}
    with pytest.raises(ModelProfileError, match="needs exactly"):
        parse_model_profile(doc, sha256="a" * 64)


def test_a_missing_provider_timeout_fails():
    doc = {k: v for k, v in VALID.items() if k != "provider_request_timeout_seconds"}
    with pytest.raises(ModelProfileError, match="needs exactly"):
        parse_model_profile(doc, sha256="a" * 64)


def test_an_extra_key_fails():
    with pytest.raises(ModelProfileError, match="unexpected"):
        parse_model_profile({**VALID, "cost_limit": 1}, sha256="a" * 64)


@pytest.mark.parametrize("field,value", [
    ("profile_id", "phase1-gpt-v1"),
    ("schema_version", 1),
    ("provider", "ckbuilders"),
    ("provider_order", ["openai", "azure"]),
    ("provider_allow_fallbacks", True),
    ("provider_require_parameters", False),
    ("api_style", "openai-chat-completions"),
    ("usage_contract", "openai-usage-v2"),
    ("drop_unsupported_params", False),
    ("litellm_num_retries", 1),
    ("max_agent_query_attempts", 1),
    ("provider_request_timeout_seconds", 299),
    ("provider_retry_backoff_seconds", [4, 8, 15]),
    ("retryable_provider_failure_categories", ["timeout"]),
    ("reasoning_context", "all_turns"),
    ("replay_policy", "all-turns"),
    ("replay_max_bytes", 65536),
    ("observation_max_bytes", 16384),
    ("truncation", "auto"),
    ("model_stability", "stable"),
])
def test_a_wrong_constant_or_enum_fails(field, value):
    with pytest.raises(ModelProfileError):
        parse_model_profile({**VALID, field: value}, sha256="a" * 64)


@pytest.mark.parametrize("field", [
    "temperature", "litellm_num_retries", "max_agent_query_attempts",
    "provider_request_timeout_seconds", "observation_max_bytes",
])
def test_a_boolean_where_an_integer_is_required_fails(field):
    """`True == 1` in Python; a bool must not satisfy an integer contract."""
    with pytest.raises(ModelProfileError):
        parse_model_profile({**VALID, field: True}, sha256="a" * 64)


@pytest.mark.parametrize("value", [0, -1, 1, 299, 301, 300.0, "300", None, CANARIES[0]])
def test_the_provider_timeout_is_exact_and_never_echoed(value):
    with pytest.raises(ModelProfileError) as exc:
        parse_model_profile(
            {**VALID, "provider_request_timeout_seconds": value}, sha256="a" * 64
        )
    assert CANARIES[0] not in str(exc.value)


@pytest.mark.parametrize("model", [
    "gpt-5.5", "openai/openai/gpt-5.5", "", "   ", " openai/gpt-5.5",
    "openai/gpt-5.5 ", "openai/gpt\t5.5", "openai/", "openai/gpt/5.5", 7, None,
])
def test_a_malformed_requested_model_fails(model):
    with pytest.raises(ModelProfileError):
        parse_model_profile({**VALID, "requested_model": model}, sha256="a" * 64)


@pytest.mark.parametrize("base", [
    "https://user:pass@proxy.example/v1",
    "https://proxy.example/v1?x=1",
    "https://proxy.example/v1#frag",
    "ftp://proxy.example/v1",
    "proxy.example/v1",
    "https:///v1",
    "https://proxy.example/v1/models",
    "https://proxy.example/v1/chat/completions",
    "https://proxy.example/v1\nmore",
    "",
])
def test_an_unsafe_api_base_fails(base):
    with pytest.raises(ModelProfileError):
        parse_model_profile({**VALID, "api_base": base}, sha256="a" * 64)


def test_only_a_trailing_slash_is_normalized():
    normalized = parse_model_profile({**VALID, "api_base": "https://proxy.example/v1/"},
                                     sha256="a" * 64)
    assert normalized.api_base == "https://proxy.example/v1"
    deeper = parse_model_profile({**VALID, "api_base": "https://proxy.example/openai/v1"},
                                 sha256="a" * 64)
    assert deeper.api_base == "https://proxy.example/openai/v1"


@pytest.mark.parametrize("value", [
    "2026-08-15", "2026-08-15T09:30:00", "2026-08-15T09:30:00+02:00", "not-a-time", "",
])
def test_a_bad_evidence_timestamp_fails(value):
    with pytest.raises(ModelProfileError):
        parse_model_profile({**VALID, "evidence_utc": value}, sha256="a" * 64)


@pytest.mark.parametrize("field", ["api_base", "requested_model", "probed_response_model"])
def test_a_credential_bearing_value_is_refused(field):
    doc = {**VALID, field: f"https://proxy.example/v1?token={CANARIES[1]}"}
    with pytest.raises(ModelProfileError, match="credential-bearing"):
        parse_model_profile(doc, sha256="a" * 64)


def test_no_canary_reaches_an_error_string_or_traceback():
    doc = {**VALID, "probed_response_model": CANARIES[0]}
    with pytest.raises(ModelProfileError) as exc:
        parse_model_profile(doc, sha256="a" * 64)
    rendered = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    for canary in CANARIES:
        assert canary not in str(exc.value)
        assert canary not in rendered


def test_an_absent_or_malformed_file_fails_without_echoing_it(tmp_path: Path):
    with pytest.raises(ModelProfileError, match="no model profile"):
        load_model_profile(tmp_path / "missing.json")
    broken = tmp_path / "phase1-model.json"
    broken.write_text('{"api_base": ' + CANARIES[0])
    with pytest.raises(ModelProfileError) as exc:
        load_model_profile(broken)
    assert CANARIES[0] not in str(exc.value)


def test_loading_opens_no_socket_and_reads_no_api_key(tmp_path: Path, monkeypatch):
    import socket

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("the profile loader performed an external action")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setenv("CKBBENCH_LLM_API_KEY", CANARIES[0])
    profile = load_model_profile(_write(tmp_path, VALID))
    rendered = "\n".join(profile.summary_lines())
    for canary in CANARIES:
        assert canary not in rendered
    assert profile.profile_id == VALID["profile_id"]


def test_model_kwargs_carry_the_reviewed_settings_and_no_credential():
    profile = parse_model_profile(VALID, sha256="a" * 64)
    kwargs = profile.model_kwargs()
    assert "temperature" not in kwargs
    assert kwargs["drop_params"] is True
    assert kwargs["num_retries"] == 0
    assert kwargs["timeout"] == PROVIDER_REQUEST_TIMEOUT_SECONDS == 300
    assert kwargs["store"] is False
    assert kwargs["api_base"] == "https://proxy.example/v1"
    assert kwargs["reasoning"] == {"effort": "medium"}
    assert kwargs["truncation"] == "disabled"
    assert kwargs["extra_body"] == {
        "provider": {
            "order": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }
    # The credential never enters the rendered config: the client receives it separately.
    assert "api_key" not in kwargs
    assert "sk-" not in json.dumps(
        {f.name: getattr(profile, f.name) for f in ModelProfile.__dataclass_fields__.values()}
    )


def test_the_summary_names_provenance_without_a_credential():
    profile = parse_model_profile(VALID, sha256="b" * 64)
    lines = "\n".join(profile.summary_lines())
    assert "phase1-model-openrouter-gpt-5-mini-v1" in lines
    assert "openai/gpt-5-mini" in lines
    assert "moving_alias" in lines
    assert "https://proxy.example/v1" in lines
    assert "litellm=0" in lines and "agent_attempts=4" in lines
    assert "retry backoff: 4s,8s,16s" in lines
    assert "retryable failures: rate_limit,timeout,connection,server,protocol,other_provider" in lines
    assert "provider request timeout: 300s" in lines
    assert "provider route: openai fallbacks=false require_parameters=true" in lines
    assert "temperature=omitted" in lines
    assert "store=false" in lines
    assert (
        "replay: prefix-tail-groups-v1 max_bytes=131072 observation_max_bytes=32768 "
        "provider_truncation=disabled" in lines
    )
    assert "sk-" not in lines and "Authorization" not in lines


def test_legacy_profile_namespace_is_refused():
    with pytest.raises(ModelProfileError, match="current provider-neutral namespace"):
        parse_model_profile({**VALID, "profile_id": "phase1-gpt-v12"}, sha256="b" * 64)


def test_the_ckbuilders_run_profile_is_reportable_by_exact_bytes():
    path = model_profile_mod.MODEL_PROFILE_DIR / "ckbuilders-gpt-5.6-sol.json"
    profile = load_report_profile(path)
    assert profile.profile_id == "phase1-model-ckbuilders-gpt-5-6-sol-v1"
    assert profile.sha256 == "be96fc5e42ea2e42b43c2b29687568fc13b9e891226fa71e22177b4cbd77db47"
    assert profile.requested_model == profile.probed_response_model == "gpt-5.6-sol"
    assert profile.model_stability == "moving_alias"
    assert profile.max_agent_query_attempts == 4
    assert profile.provider_retry_backoff_seconds == (4, 8, 16)
    assert profile.replay_max_bytes == 131072


def test_the_deepseek_run_profile_is_reportable_by_exact_bytes():
    path = model_profile_mod.MODEL_PROFILE_DIR / "openrouter-deepseek-v4-flash.json"
    profile = load_report_profile(path)
    assert profile.profile_id == "phase1-model-openrouter-deepseek-v4-flash-v2"
    assert profile.sha256 == "55fb155665da7a8e4034e6f6a5b105dc56027f2d5794bc0c97f52ec461475fc2"
    assert profile.requested_model == profile.probed_response_model == (
        "deepseek/deepseek-v4-flash-0731"
    )
    assert profile.model_stability == "dated_snapshot"
    assert profile.max_agent_query_attempts == 4
    assert profile.provider_retry_backoff_seconds == (4, 8, 16)
    assert profile.replay_max_bytes == 131072


def test_the_luna_run_profile_is_reportable_by_exact_bytes():
    path = model_profile_mod.MODEL_PROFILE_DIR / "ckbuilders-gpt-5.6-luna.json"
    profile = load_report_profile(path)
    assert profile.profile_id == "phase1-model-ckbuilders-gpt-5-6-luna-v1"
    assert profile.sha256 == "eb56b9b4a70c70afdbc5062bf41a70ea1ae88d76c82d2f1267bc6cc974782f3c"
    assert profile.requested_model == profile.probed_response_model == "gpt-5.6-luna"
    assert profile.model_stability == "moving_alias"
    assert profile.max_agent_query_attempts == 4
    assert profile.provider_retry_backoff_seconds == (4, 8, 16)
    assert profile.replay_max_bytes == 131072
    runtime = load_run_profile("ckbuilders-gpt-5.6-luna")
    assert runtime.provider == "ckbuilders" and runtime.provider_order == ()


def test_the_supported_catalog_is_named_unique_and_excludes_retired_models():
    profiles = available_run_profiles()
    aliases = {alias for alias, _profile in profiles}
    provider_models = {(profile.provider, profile.requested_model) for _alias, profile in profiles}

    assert aliases == {
        "ckbuilders-gpt-5.6-sol",
        "ckbuilders-gpt-5.6-luna",
        "ckbuilders-gpt-5.6-terra",
        "openrouter-deepseek-v4-flash",
        "openrouter-deepseek-v4-pro",
        "openrouter-gemini-3.7-flash",
        "openrouter-ox-alpha",
    }
    assert len(provider_models) == len(profiles)
    assert all(profile.requested_model != "openai/gpt-5-mini" for _alias, profile in profiles)
    assert all(profile.reasoning_effort == "high" for _alias, profile in profiles)


@pytest.mark.parametrize(
    ("alias", "model", "route", "digest"),
    [
        (
            "ckbuilders-gpt-5.6-luna",
            "gpt-5.6-luna",
            (),
            "eb56b9b4a70c70afdbc5062bf41a70ea1ae88d76c82d2f1267bc6cc974782f3c",
        ),
        (
            "ckbuilders-gpt-5.6-terra",
            "gpt-5.6-terra",
            (),
            "7d2820b0196f834580d8c7d0ed8354504a952ee2faf3105759ef65da192f6343",
        ),
        (
            "openrouter-deepseek-v4-pro",
            "deepseek/deepseek-v4-pro-0813",
            ("alibaba",),
            "d20d5b6e3e935adf9eb850adf19ad24107f4bd1d35da1afcb72ffddbbd12e8ad",
        ),
        (
            "openrouter-gemini-3.7-flash",
            "google/gemini-3.7-flash",
            ("google-vertex/global",),
            "01a4b8422754f12a4e23e71b4ed8abf9fc9811cb98f6c508a2ce175f3b385eb8",
        ),
        (
            "openrouter-ox-alpha",
            "stealth/ox-alpha",
            ("stealth",),
            "2c5174ba2e030a303a07ff15e0a418253e529db423a08e2fcc16b63af594b139",
        ),
    ],
)
def test_new_supported_profiles_are_bound_to_qualified_bytes(alias, model, route, digest):
    profile = load_run_profile(alias)
    assert profile.requested_model == profile.probed_response_model == model
    assert profile.provider_order == route
    assert profile.sha256 == digest


def test_a_report_profile_outside_the_selectable_catalog_is_refused(tmp_path: Path):
    candidate = tmp_path / "profile.json"
    candidate.write_text(json.dumps(VALID), encoding="utf-8")
    with pytest.raises(ModelProfileError, match="configs/models"):
        load_report_profile(candidate)
