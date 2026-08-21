"""Provider-boundary integration, import order, and accepted-path non-regression.

No Docker, socket, provider, MCP or RPC path is reached: the model's transport is faked and the
transport seam is exercised through the observer's own wrapper.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

import httpx
import pytest
from litellm import exceptions as litellm_exceptions

from ckbbench.run.diagnostic import (
    MAX_PROVIDER_REQUESTS,
    DiagnosticLimitReached,
    DiagnosticSession,
)

CANARY = "SK-LIVE-CANARY https://user:sk@proxy.example/v1 CMD-CANARY"


class _Seam:
    """A minimal stand-in for the worker's seam controller."""

    def __init__(self, state: str = "response_seen") -> None:
        self.state = state
        self.begins = 0
        self.ends = 0

    def begin_attempt(self) -> None:
        self.begins += 1

    def end_attempt(self) -> str:
        self.ends += 1
        return self.state


def _model(responses=None, errors=None):
    """A benchmark Responses model whose provider call is replaced by a local script."""
    from ckb_model import CkbLitellmResponseModel

    model = CkbLitellmResponseModel(model_name="openai/gpt-x", model_kwargs={},
                                    cost_tracking="ignore_errors")
    script = list(errors or [])
    payloads = list(responses or [])

    def fake_query(self, messages, **kwargs):
        if script:
            raise script.pop(0)
        return payloads.pop(0) if payloads else _usable_response()

    from minisweagent.models.litellm_response_model import LitellmResponseModel

    LitellmResponseModel._query = fake_query
    return model


def _usable_response():
    import types as _t

    return _t.SimpleNamespace(
        model="gpt-x",
        status="completed",
        output=[{"type": "function_call", "call_id": "c1", "name": "bash",
                 "arguments": '{"command": "ls"}', "status": "completed"}],
        usage=_t.SimpleNamespace(input_tokens=5, output_tokens=2, total_tokens=7),
    )


@pytest.fixture
def restore_query():
    from minisweagent.models.litellm_response_model import LitellmResponseModel

    original = LitellmResponseModel._query
    yield
    LitellmResponseModel._query = original


def test_an_ordinary_model_has_no_diagnostic_attached(restore_query):
    model = _model()
    assert model.diagnostic is None


def test_a_failed_attempt_is_recorded_with_its_family_and_transport(restore_query):
    from ckb_model import ProviderCallError

    exc = litellm_exceptions.BadRequestError(message=CANARY, model="m", llm_provider="p")
    model = _model(errors=[exc])
    session = DiagnosticSession()
    seam = _Seam("handler_entered_no_response")
    model.attach_diagnostic(session, seam)

    with pytest.raises(ProviderCallError):
        model.query([{"role": "user", "content": "x"}])

    assert len(session.records) == 1
    record = session.records[0]
    assert record["outcome"] == "bad_request"
    assert record["transport_state"] == "handler_entered_no_response"
    assert seam.begins == 1 and seam.ends == 1


def test_a_successful_attempt_is_recorded_as_responded(restore_query):
    model = _model()
    session = DiagnosticSession()
    model.attach_diagnostic(session, _Seam("response_seen"))
    model.query([{"role": "user", "content": "x"}])
    assert [r["outcome"] for r in session.records] == ["responded"]
    assert session.records[0]["transport_state"] == "response_seen"


def test_the_projection_describes_the_prepared_input(restore_query):
    model = _model()
    session = DiagnosticSession()
    model.attach_diagnostic(session, _Seam())
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"object": "response", "output": [
            {"type": "reasoning", "id": "r"},
            {"type": "function_call", "call_id": "c1", "name": "bash",
             "arguments": "{}", "status": "completed"},
        ]},
    ]
    model.query(history)
    shape = session.records[0]["input_shape"]
    # The response wrapper is flattened before the call, so the projection sees its output items.
    assert shape["type_sequence"] == ["system", "user", "reasoning", "function_call"]
    assert shape["pairing"]["call_items"] == 1


def test_request_seventeen_never_reaches_the_provider(restore_query):
    calls = {"n": 0}
    model = _model()

    def counting(self, messages, **kwargs):
        calls["n"] += 1
        return _usable_response()

    from minisweagent.models.litellm_response_model import LitellmResponseModel

    # Installed AFTER the model is built: `_model()` installs its own replacement.
    LitellmResponseModel._query = counting
    session = DiagnosticSession()
    model.attach_diagnostic(session, _Seam())
    for _ in range(MAX_PROVIDER_REQUESTS):
        model.query([{"role": "user", "content": "x"}])
    assert calls["n"] == MAX_PROVIDER_REQUESTS

    with pytest.raises(DiagnosticLimitReached):
        model.query([{"role": "user", "content": "x"}])
    assert calls["n"] == MAX_PROVIDER_REQUESTS, "request 17 reached the provider"


def test_no_canary_reaches_the_artifact_or_any_sanitized_surface(restore_query):
    from ckb_model import ProviderCallError

    exc = litellm_exceptions.BadRequestError(message=CANARY, model="m", llm_provider="p")
    model = _model(errors=[exc])
    session = DiagnosticSession()
    model.attach_diagnostic(session, _Seam("not_started"))

    history = [{"role": "user", "content": "CMD-" + "CANARY"}]
    with pytest.raises(ProviderCallError) as raised:
        model.query(history)

    surfaces = (
        session.to_bytes("2.0.0-devnet-B-diagnostic-s1-1").decode()
        + str(raised.value) + repr(raised.value)
        + repr(raised.value.__cause__) + repr(raised.value.__context__)
        + "".join(traceback.format_exception(type(raised.value), raised.value,
                                             raised.value.__traceback__))
        + repr(model.serialize()) + repr(model.get_template_vars())
    )
    for canary in ("SK-LIVE-CANARY", "sk@proxy.example", "CMD-CANARY", "BadRequestError"):
        assert canary not in surfaces


def test_the_diagnostic_never_changes_the_wire_payload(restore_query):
    """Diagnostic-on and diagnostic-off must hand the provider deeply identical input."""
    seen: list[list[dict]] = []

    def capture(self, messages, **kwargs):
        seen.append(json.loads(json.dumps(messages)))
        return _usable_response()

    from minisweagent.models.litellm_response_model import LitellmResponseModel

    history = [
        {"role": "system", "content": "s"},
        {"object": "response", "output": [{"type": "reasoning", "id": "r"}],
         "extra": {"actions": []}},
    ]

    off = _model()
    LitellmResponseModel._query = capture
    off.query(history)

    on = _model()
    LitellmResponseModel._query = capture
    on.attach_diagnostic(DiagnosticSession(), _Seam())
    on.query(history)

    assert seen[0] == seen[1]


def test_normal_mode_never_patches_httpx(restore_query):
    before = httpx.HTTPTransport.handle_request
    model = _model()
    model.query([{"role": "user", "content": "x"}])
    assert httpx.HTTPTransport.handle_request is before


# --- import order --------------------------------------------------------------------------------


def test_importing_ckbbench_pins_the_local_cost_map_even_from_an_ambient_false():
    """`import litellm` otherwise fetches its cost map over HTTPS at import time."""
    script = (
        "import os, sys, json\n"
        "import ckbbench\n"
        "print(json.dumps({'pinned': os.environ.get('LITELLM_LOCAL_MODEL_COST_MAP')}))\n"
    )
    env = {
        **os.environ,
        "LITELLM_LOCAL_MODEL_COST_MAP": "False",
        "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[2])]),
    }
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=60, env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip())["pinned"] == "True"


def test_the_cost_map_pin_precedes_the_first_litellm_import():
    """Assignment, not setdefault: the value must be True by the time litellm's module body runs."""
    import ckbbench

    source = Path(ckbbench.__file__).read_text()
    code_lines = [ln for ln in source.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert 'os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"' in code_lines
    assert not any("setdefault" in ln for ln in code_lines)
    assert source.index("LITELLM_LOCAL_MODEL_COST_MAP") < source.index("_pkg_version")


# --- accepted-path non-regression -----------------------------------------------------------------


def test_the_accepted_result_schema_is_unchanged():
    from ckbbench.run.result import RESULT_SCHEMA_VERSION

    assert RESULT_SCHEMA_VERSION == "1.5.0"


def test_no_diagnostic_field_entered_the_accepted_metrics_key_set():
    from ckbbench.matrix.store import _METRIC_FIELDS

    assert _METRIC_FIELDS == frozenset({
        "total_wall_seconds", "token_usage_status", "provider_failure_category",
        "model_calls", "provider_attempts", "provider_responses",
        "prompt_tokens", "completion_tokens", "total_tokens",
    })


def test_the_report_never_reads_a_diagnostic_artifact():
    from ckbbench.matrix import build_site, render, store

    for module in (build_site, render, store):
        source = Path(module.__file__).read_text()
        assert ".diag.json" not in source
        assert "diagnose" not in source
        assert "ckbbench.run.diagnostic" not in source


def test_an_ambient_worker_flag_cannot_arm_a_normal_command(monkeypatch):
    from ckbbench.run.diagnose import worker_requested

    monkeypatch.setenv("CKBBENCH_DIAGNOSTIC_WORKER", "1")
    assert worker_requested() is True
    monkeypatch.delenv("CKBBENCH_DIAGNOSTIC_WORKER")
    assert worker_requested() is False


def test_the_worker_refuses_to_run_unconfigured(monkeypatch):
    from ckbbench.run import diagnose_worker

    monkeypatch.delenv("CKBBENCH_DIAGNOSTIC_WORKER", raising=False)
    assert diagnose_worker.main() == 2


def test_ordinary_docker_environments_keep_automatic_cleanup():
    from minisweagent.environments.docker import DockerEnvironmentConfig

    config = DockerEnvironmentConfig(image="x", cwd="/tmp")
    assert config.auto_cleanup is True
    assert config.run_args == ["--rm"]
    assert config.container_name == ""
    assert config.labels == []


def test_a_parent_owned_environment_removes_nothing_but_keeps_its_id():
    """Suppressing `__del__` by discarding the id would also destroy the only immutable selector."""
    from minisweagent.environments.docker import DockerEnvironment

    env = DockerEnvironment.__new__(DockerEnvironment)
    env.config = type("C", (), {"auto_cleanup": False, "executable": "docker"})()
    env.container_id = "abc123"
    env.cleanup()
    assert env.container_id == "abc123"


# --- review-revision-1 reversals -------------------------------------------------------------------


def test_the_child_environment_is_an_allowlist_not_a_copy():
    """Copying os.environ forwarded unrelated operator secrets into the worker."""
    from ckbbench.run.diagnose import DiagnosticIdentity
    from ckbbench.run.diagnose_cli import child_environment

    identity = DiagnosticIdentity.create(
        run_id="2.0.0-devnet-B-m-s1-1", artifact_root=Path("/tmp/x"), run_dir=Path("/tmp/x/run"),
        execution_id="0" * 32,
    )
    source = {
        "PATH": "/usr/bin",
        "CKBBENCH_LLM_API_KEY": "sk-production-value",
        "AWS_SECRET_ACCESS_KEY": "UNRELATED-SECRET-CANARY",
        "GITHUB_TOKEN": "UNRELATED-TOKEN-CANARY",
        "MY_PRIVATE_THING": "UNRELATED-OTHER-CANARY",
    }
    env = child_environment(identity, source)

    assert env["PATH"] == "/usr/bin"
    assert env["CKBBENCH_LLM_API_KEY"] == "sk-production-value"  # the established channel
    blob = " ".join(f"{k}={v}" for k, v in env.items())
    for canary in ("UNRELATED-SECRET-CANARY", "UNRELATED-TOKEN-CANARY", "UNRELATED-OTHER-CANARY"):
        assert canary not in blob
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["CKBBENCH_DIAGNOSTIC_WORKER"] == "1"


def test_the_run_id_uses_the_reviewed_model_identity():
    from ckbbench.run.diagnose_cli import run_id_for

    assert run_id_for("gpt-5.6-sol", 1786900000).startswith("2.0.0-devnet-B-gpt-5.6-sol-s1-")


def test_an_out_of_range_turn_index_poisons_instead_of_clamping(restore_query):
    """Clamping published a harness defect as a healthy fact."""
    from ckb_model import ProviderCallError

    model = _model(errors=[litellm_exceptions.BadRequestError(
        message=CANARY, model="m", llm_provider="p")])
    session = DiagnosticSession()
    model.attach_diagnostic(session, _Seam())
    model.usage_ledger.turns = 500  # far beyond the accepted step limit

    with pytest.raises(ProviderCallError):
        model.query([{"role": "user", "content": "x"}])

    assert session.instrumentation_ok is False
    document = json.loads(session.to_bytes("2.0.0-devnet-B-m-s1-1"))
    assert document["instrumentation_ok"] is False
    assert document["records"] == []


def test_an_observer_failure_poisons_the_session(restore_query):
    from ckb_model import ProviderCallError

    class Exploding(_Seam):
        def end_attempt(self):
            raise RuntimeError("observer failed")

    model = _model(errors=[litellm_exceptions.BadRequestError(
        message=CANARY, model="m", llm_provider="p")])
    session = DiagnosticSession()
    model.attach_diagnostic(session, Exploding())

    with pytest.raises(ProviderCallError):
        model.query([{"role": "user", "content": "x"}])

    assert session.instrumentation_ok is False
    assert json.loads(session.to_bytes("2.0.0-devnet-B-m-s1-1"))["instrumentation_ok"] is False


def test_a_begin_attempt_failure_poisons_but_does_not_change_provider_behaviour(restore_query):
    class Exploding(_Seam):
        def begin_attempt(self):
            raise RuntimeError("observer failed")

    model = _model()
    session = DiagnosticSession()
    model.attach_diagnostic(session, Exploding())
    model.query([{"role": "user", "content": "x"}])  # the provider call still succeeds
    assert session.instrumentation_ok is False


def test_poisoning_is_terminal_and_drops_earlier_records(restore_query):
    session = DiagnosticSession()
    session.record(turn_index=0, attempt_index=0, exc=None, prepared=[],
                   transport_state="not_started")
    assert len(session.records) == 1
    session.poison()
    assert session.records == [] and session.dropped == 0
    session.record(turn_index=0, attempt_index=0, exc=None, prepared=[],
                   transport_state="not_started")
    assert session.records == [], "a poisoned session accepted a later record"
