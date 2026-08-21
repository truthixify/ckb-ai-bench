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

from ckbbench.run.model_profile import (
    PROFILE_ID,
    PROVIDER_REQUEST_TIMEOUT_SECONDS,
    ModelProfile,
    ModelProfileError,
    load_model_profile,
    parse_model_profile,
)

VALID = {
    "api_base": "https://proxy.example/v1",
    "api_style": "openai-responses",
    "drop_unsupported_params": True,
    "evidence_utc": "2026-08-15T09:30:00Z",
    "litellm_num_retries": 0,
    "max_agent_query_attempts": 4,
    "model_stability": "dated_snapshot",
    "probed_response_model": "gpt-5.5-2026-02-11",
    "profile_id": "phase1-gpt-v6",
    "provider": "ckbuilders",
    "provider_request_timeout_seconds": 300,
    "provider_retry_backoff_seconds": [4, 8, 16],
    "reasoning_context": "all_turns",
    "reasoning_effort": "medium",
    "store": False,
    "requested_model": "gpt-5.5-2026-02-11",
    "retryable_provider_failure_categories": [
        "rate_limit", "timeout", "connection", "server", "protocol", "other_provider",
    ],
    "schema_version": "5",
    "temperature": 0,
    "usage_contract": "openai-responses-usage-v1",
}
CANARIES = ("sk-live-do-not-log", "tok-abc123", "raw-server-body")


def _write(tmp_path: Path, doc: dict, *, sort_keys: bool = True, indent: int | None = 2) -> Path:
    path = tmp_path / "phase1-gpt.json"
    path.write_text(json.dumps(doc, sort_keys=sort_keys, indent=indent) + "\n")
    return path


def test_a_valid_profile_loads_as_an_immutable_value_bound_to_the_file_bytes(tmp_path: Path):
    path = _write(tmp_path, VALID)
    profile = load_model_profile(path)
    assert profile.requested_model == "gpt-5.5-2026-02-11"
    assert profile.litellm_model_name == "openai/gpt-5.5-2026-02-11"
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


def test_the_internal_name_has_exactly_one_prefix():
    profile = parse_model_profile(VALID, sha256="a" * 64)
    assert profile.litellm_model_name.count("openai/") == 1


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
    ("provider", "openai"),
    ("api_style", "openai-chat-completions"),
    ("usage_contract", "openai-usage-v2"),
    ("temperature", 0.0001),
    ("temperature", 1),
    ("drop_unsupported_params", False),
    ("litellm_num_retries", 1),
    ("max_agent_query_attempts", 1),
    ("provider_request_timeout_seconds", 299),
    ("provider_retry_backoff_seconds", [4, 8, 15]),
    ("retryable_provider_failure_categories", ["timeout"]),
    ("model_stability", "stable"),
])
def test_a_wrong_constant_or_enum_fails(field, value):
    with pytest.raises(ModelProfileError):
        parse_model_profile({**VALID, field: value}, sha256="a" * 64)


@pytest.mark.parametrize("field", [
    "temperature", "litellm_num_retries", "max_agent_query_attempts",
    "provider_request_timeout_seconds",
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
    "openai/gpt-5.5", "", "   ", " gpt-5.5", "gpt-5.5 ", "gpt\t5.5", "vendor/gpt-5.5", 7, None,
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
    broken = tmp_path / "phase1-gpt.json"
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
    assert profile.profile_id == PROFILE_ID


def test_model_kwargs_carry_the_reviewed_settings_and_no_credential():
    profile = parse_model_profile(VALID, sha256="a" * 64)
    kwargs = profile.model_kwargs()
    assert kwargs["temperature"] == 0
    assert kwargs["drop_params"] is True
    assert kwargs["num_retries"] == 0
    assert kwargs["timeout"] == PROVIDER_REQUEST_TIMEOUT_SECONDS == 300
    assert kwargs["store"] is False
    assert kwargs["api_base"] == "https://proxy.example/v1"
    # The credential never enters the rendered config: the client receives it separately.
    assert "api_key" not in kwargs
    assert "sk-" not in json.dumps(
        {f.name: getattr(profile, f.name) for f in ModelProfile.__dataclass_fields__.values()}
    )


def test_the_summary_names_provenance_without_a_credential():
    profile = parse_model_profile(VALID, sha256="b" * 64)
    lines = "\n".join(profile.summary_lines())
    assert "phase1-gpt-v6" in lines
    assert "gpt-5.5-2026-02-11" in lines
    assert "dated_snapshot" in lines
    assert "https://proxy.example/v1" in lines
    assert "litellm=0" in lines and "agent_attempts=4" in lines
    assert "retry backoff: 4s,8s,16s" in lines
    assert "retryable failures: rate_limit,timeout,connection,server,protocol,other_provider" in lines
    assert "provider request timeout: 300s" in lines
    assert "store=false" in lines
    assert "sk-" not in lines and "Authorization" not in lines
