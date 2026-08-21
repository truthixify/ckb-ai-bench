"""LitellmModel plus a sanitized provider-usage ledger (ADR-0014).

Upstream core stays vendored unmodified; this is a subclass, like `ckb_agent.py`.

Two things the benchmark needs that upstream does not provide:

  * every raw provider attempt is counted, and a successful response is recorded BEFORE cost
    calculation or action parsing -- a response that later fails to parse still consumed tokens, so
    dropping it would understate the run;
  * the number of attempts per model turn is fixed by the caller rather than by a mutable
    environment default. Failed attempts and delayed recoveries remain visible, so correctness can
    be measured without pretending the token denominator is complete.

The ledger holds counts, the response's model identity, and three integers. It never holds request
messages, completion content, tool arguments, response IDs, headers, keys, raw bodies, or raw
exception text: a provider failure is recorded by its exception class name only.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from minisweagent.models import GLOBAL_MODEL_STATS
from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
from minisweagent.models.litellm_response_model import (
    LitellmResponseModel,
    LitellmResponseModelConfig,
)

# One identifier rule for the profile, the live probe and this runtime ledger. The harness package
# is importable wherever the factory builds this model.
from ckbbench.run.metrics import MULTIPLE_CATEGORIES, PROVIDER_FAILURE_CATEGORY_SET
from ckbbench.run.model_profile import RETRYABLE_PROVIDER_FAILURE_CATEGORIES, is_publishable

# The Responses API is the phase-one wire contract (ADR-0014). It reports usage under its own
# names; these are the ONLY place they are translated to the harness's long-standing public field
# names, so a result row keeps one vocabulary while the provider keeps its own.
NATIVE_USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
PUBLIC_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
# input -> prompt, output -> completion. One documented boundary, exercised by its own tests.
NATIVE_TO_PUBLIC = dict(zip(NATIVE_USAGE_FIELDS, PUBLIC_USAGE_FIELDS))


@dataclass(frozen=True)
class ProviderAttempt:
    """One raw provider call. `error` is an exception class name, never its message."""

    responded: bool
    model: str | None = None
    # One allowlisted category, never an exception class name or message fragment.
    failure_category: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    error: str | None = None

    @property
    def has_usage(self) -> bool:
        return None not in (self.prompt_tokens, self.completion_tokens, self.total_tokens)


def _valid_int(value: Any) -> int | None:
    """A usage field is an integer or it is absent. Never a bool, float, or numeric string."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_field(usage: Any, name: str) -> Any:
    raw = getattr(usage, name, None)
    if raw is None and isinstance(usage, dict):
        raw = usage.get(name)
    return raw


def _read_usage(response: Any) -> tuple[int, int, int] | None:
    """The three provider usage integers as (prompt, completion, total), or None.

    Reads the Responses-native `input_tokens`/`output_tokens`/`total_tokens` and maps input->prompt
    and output->completion here, at the single boundary where the provider vocabulary becomes the
    harness's public one.

    A missing `total_tokens` is not derived and a missing component is not replaced with zero: a
    guessed denominator is worse than an explicitly incomplete one.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None
    values: list[int] = []
    for native in NATIVE_USAGE_FIELDS:
        checked = _valid_int(_usage_field(usage, native))
        if checked is None:
            return None
        values.append(checked)
    prompt, completion, total = values
    if total != prompt + completion:
        return None
    return prompt, completion, total


class ProviderCallError(RuntimeError):
    """A sanitized provider/transport failure: a fixed message, no class name and no text.

    The ledger's fixed category, not this exception, is the provenance source.
    """

    def __init__(self, category: str) -> None:
        super().__init__("provider call failed")
        self.category = category


def _provider_exception_types() -> tuple[type[BaseException], ...]:
    """A POSITIVE list of what counts as provider/transport evidence.

    An inverse list would silently reclassify every new harness bug as provider health.
    """
    types: list[type[BaseException]] = [OSError, TimeoutError, json.JSONDecodeError]
    try:
        import litellm  # lazy: only present on the run-time path
    except Exception:  # pragma: no cover - the fork always has it at run time
        return tuple(types)
    for name in ("APIError", "APIConnectionError", "Timeout", "RateLimitError",
                 "ServiceUnavailableError", "InternalServerError", "BadRequestError",
                 "AuthenticationError", "NotFoundError", "PermissionDeniedError",
                 "UnsupportedParamsError", "ContextWindowExceededError"):
        candidate = getattr(litellm.exceptions, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            types.append(candidate)
    return tuple(types)


def is_provider_fault(exc: BaseException) -> bool:
    """Whether this failure is evidence about the endpoint rather than about this code."""
    return isinstance(exc, _provider_exception_types())


def _litellm_exceptions():
    """LiteLLM's exception namespace, or None when the fork is unavailable."""
    try:
        import litellm.exceptions as exc_mod
    except Exception:  # noqa: BLE001 - absence is normal outside the run-time path
        return None
    return exc_mod


def _category_rules() -> list[tuple[tuple[type[BaseException], ...], str]]:
    """Exception families mapped to fixed categories, SPECIFIC BEFORE GENERAL.

    Ordering is load-bearing: most LiteLLM errors inherit from a generic API error, so a broad rule
    placed first would swallow every specific one and publish `other_provider` for everything.
    """
    lit = _litellm_exceptions()
    rules: list[tuple[tuple[type[BaseException], ...], str]] = []

    def add(names: tuple[str, ...], category: str) -> None:
        if lit is None:
            return
        types = tuple(t for t in (getattr(lit, n, None) for n in names) if isinstance(t, type))
        if types:
            rules.append((types, category))

    add(("AuthenticationError",), "authentication")
    add(("PermissionDeniedError",), "authorization")
    add(("RateLimitError",), "rate_limit")
    add(("ContextWindowExceededError",), "context_window")
    add(("UnsupportedParamsError",), "unsupported")
    add(("Timeout", "APITimeoutError"), "timeout")
    rules.append(((TimeoutError,), "timeout"))
    add(("ServiceUnavailableError", "InternalServerError"), "server")
    add(("BadRequestError", "NotFoundError"), "request")
    rules.append(((json.JSONDecodeError,), "protocol"))
    add(("APIConnectionError",), "connection")
    # A non-timeout OSError is a transport failure. TimeoutError subclasses OSError, so the timeout
    # rule above must and does win first.
    rules.append(((OSError,), "connection"))
    return rules


def provider_failure_category(exc: BaseException) -> str | None:
    """The fixed category for a provider fault, or None for an internal harness error.

    Chosen purely by type. Nothing from the exception's message, response, URL, or class name can
    reach the result through this function.
    """
    if not is_provider_fault(exc):
        return None
    for types, category in _category_rules():
        if isinstance(exc, types):
            return category
    return "other_provider"


def _read_model(response: Any) -> str | None:
    """The response's model identity, or None when the server sent nothing publishable.

    This value is retained, serialized into the result and read back by the validator, so an
    unpublishable one is dropped rather than carried: absence makes the run incomplete, which is the
    honest outcome, while retaining it would put provider-controlled text into published provenance.
    """
    value = getattr(response, "model", None)
    if value is None and isinstance(response, dict):
        value = response.get("model")
    return value if is_publishable(value) else None


class UsageLedger:
    """In-memory record of this run's provider attempts. Sanitized by construction."""

    def __init__(self) -> None:
        self.attempts: list[ProviderAttempt] = []
        self.internal: list[str] = []
        self.retry_delays: list[int] = []
        self.turns = 0

    def record_turn(self) -> None:
        """One model turn requested by the agent, whatever the provider then does."""
        self.turns += 1

    def record_retry(self, delay_seconds: int) -> None:
        """Record one retry that actually began after its fixed wait completed."""
        self.retry_delays.append(delay_seconds)

    def record_failure(self, exc: BaseException) -> None:
        """A provider fault is an attempt; a harness bug is not.

        Counting a `TypeError` in this code as a failed provider attempt would charge the endpoint
        for our own defect and distort the health numbers a report publishes.
        """
        if is_provider_fault(exc):
            self.attempts.append(ProviderAttempt(
                responded=False,
                error=type(exc).__name__,
                failure_category=provider_failure_category(exc),
            ))
        else:
            self.internal.append(type(exc).__name__)

    @property
    def internal_errors(self) -> int:
        return len(self.internal)

    def record_response(self, response: Any) -> None:
        usage = _read_usage(response)
        prompt, completion, total = usage if usage is not None else (None, None, None)
        self.attempts.append(
            ProviderAttempt(
                responded=True,
                model=_read_model(response),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
            )
        )

    @property
    def turn_count(self) -> int:
        return self.turns

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def response_count(self) -> int:
        return sum(1 for a in self.attempts if a.responded)

    @property
    def response_models(self) -> set[str]:
        return {a.model for a in self.attempts if a.responded and a.model}

    def totals(self) -> tuple[int, int, int] | None:
        """Sums over responses that carried valid usage, or None when none did."""
        usable = [a for a in self.attempts if a.responded and a.has_usage]
        if not usable:
            return None
        return (
            sum(a.prompt_tokens for a in usable),
            sum(a.completion_tokens for a in usable),
            sum(a.total_tokens for a in usable),
        )

    @property
    def provider_failure_category(self) -> str | None:
        """One category for this run: None, the single category, or `multiple` when they disagree.

        Defensive rather than permissive: the accepted profile allows bounded transient recoveries,
        so a run may yield one concrete category or ``multiple``.
        """
        seen = {a.failure_category for a in self.attempts
                if not a.responded and a.failure_category in PROVIDER_FAILURE_CATEGORY_SET}
        if not seen:
            return None
        if len(seen) == 1:
            return next(iter(seen))
        return MULTIPLE_CATEGORIES

    @property
    def provider_failure_counts(self) -> dict[str, int]:
        """Sanitized failure counts, keyed only by the closed result vocabulary."""
        counts: dict[str, int] = {}
        for attempt in self.attempts:
            category = attempt.failure_category
            if not attempt.responded and category in PROVIDER_FAILURE_CATEGORY_SET:
                counts[category] = counts.get(category, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def retry_count(self) -> int:
        return len(self.retry_delays)

    @property
    def retry_delay_seconds(self) -> int:
        return sum(self.retry_delays)

    def last_provenance(self) -> dict[str, Any]:
        """Sanitized stand-in for the raw response: counts and identity only."""
        if not self.attempts:
            return {"usage": None, "model": None}
        last = self.attempts[-1]
        return {
            "model": last.model,
            "usage": None if not last.has_usage else {
                "prompt_tokens": last.prompt_tokens,
                "completion_tokens": last.completion_tokens,
                "total_tokens": last.total_tokens,
            },
        }

    def is_complete(self) -> bool:
        """Every attempt returned a response, every response carried usage, one model identity."""
        if not self.attempts:
            return False
        if any(not a.responded or not a.has_usage or not a.model for a in self.attempts):
            return False
        return len(self.response_models) == 1

    def is_correctness_complete(self) -> bool:
        """Every requested model turn eventually returned under one publishable identity.

        A failed attempt followed by a response makes token usage incomplete, but it does not erase
        the response or the agent actions produced from it. Correctness therefore follows turns and
        responses, while ``is_complete`` remains the stricter efficiency predicate.
        """
        if self.turns <= 0 or self.response_count != self.turns:
            return False
        responded = [attempt for attempt in self.attempts if attempt.responded]
        if any(not attempt.model for attempt in responded):
            return False
        return len(self.response_models) == 1


_SECRET_KEYS = ("api_key", "authorization", "auth", "key", "token", "password", "secret")


def _redacted(payload: Any) -> Any:
    """Strip credential-looking entries from anything this model renders or serializes."""
    if isinstance(payload, dict):
        return {
            k: ("(redacted)" if any(m in str(k).lower() for m in _SECRET_KEYS)
                else _redacted(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_redacted(v) for v in payload]
    return payload


class CkbLitellmModelConfig(LitellmModelConfig):
    max_query_attempts: int = 1
    """Provider attempts per model turn, supplied by the caller's reviewed profile."""
    retry_backoff_seconds: tuple[int, ...] = ()
    retryable_failure_categories: tuple[str, ...] = ()


class _SanitizedProviderCalls:
    """The Task 17 provider boundary, shared by every benchmark model.

    One policy, not two: the chat and Responses models differ only in wire contract, so the ledger,
    credential handling, attempt count and what may survive a turn are defined once here.
    """

    def _install_ledger(self, api_key: str) -> None:
        self.usage_ledger = UsageLedger()
        # Held outside the config the agent renders and serializes.
        self._call_secrets = {"api_key": api_key} if api_key else {}
        # Absent in every ordinary run. Only `./bench diagnose` attaches one.
        self.diagnostic = None
        self._diagnostic_seam = None
        self._attempt_index = 0

    def attach_diagnostic(self, session: Any, seam: Any) -> None:
        """Attach the diagnostic session and the transport seam accessor for this run.

        `seam` supplies `begin_attempt()`, `end_attempt()` and the current class attribute, so this
        boundary never imports httpx or decides what the seam is.
        """
        self.diagnostic = session
        self._diagnostic_seam = seam

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return _redacted(super().get_template_vars(**kwargs))

    def serialize(self) -> dict:
        return _redacted(super().serialize())

    def _query(self, messages: list[dict[str, str]], **kwargs):
        """The narrow provider-call boundary: every raw attempt passes through here.

        The API key is supplied here and only here, so it never enters the config the agent
        serializes. A provider failure is re-raised as a value-free typed error `from None`: the
        original message can carry a response body or a credential, and the default agent stores
        `str(e)` and a formatted traceback in its diagnostics.
        """
        diagnostic = self.diagnostic
        if diagnostic is not None:
            # Before transport: request `max_requests + 1` never reaches LiteLLM or HTTPX.
            diagnostic.reserve_request()
            try:
                self._diagnostic_seam.begin_attempt()
            except Exception:
                diagnostic.poison()
        provider_fault = False
        failure: BaseException | None = None
        try:
            response = super()._query(messages, **{**kwargs, **self._call_secrets})
        except BaseException as exc:
            failure = exc
            self.usage_ledger.record_failure(exc)
            if diagnostic is not None:
                self._record_diagnostic(messages, exc)
            if not is_provider_fault(exc):
                raise
            provider_fault = True
        if provider_fault:
            # Raised outside the handler so the original exception is not reachable through
            # __context__ either; `from None` alone only suppresses its display.
            # No class name: the agent stores `str(e)` and a traceback in its diagnostics. The
            # result ledger, not this message, is the provenance source.
            category = provider_failure_category(failure)
            raise ProviderCallError(category or "other_provider")
        # Recorded before cost calculation and action parsing: a response that later raises
        # FormatError still consumed tokens.
        self.usage_ledger.record_response(response)
        if diagnostic is not None:
            self._record_diagnostic(messages, None)
        del failure
        return response

    def _record_diagnostic(self, messages: list[dict[str, str]], exc: BaseException | None) -> None:
        """One record per attempt, from the prepared input, the exception TYPE and our own counters.

        The prepared list is the exact object handed to the provider call, so the shape describes
        what was attempted rather than what was reconstructed afterwards.

        The real turn index is passed through unclamped: clamping an out-of-range harness value would
        publish a defect as a healthy fact instead of failing closed. Any failure of the observer or
        the projection poisons the session for the same reason.
        """
        try:
            state = self._diagnostic_seam.end_attempt()
        except Exception:
            self.diagnostic.poison()
            return
        self.diagnostic.record(
            turn_index=self.usage_ledger.turn_count - 1,
            attempt_index=self._attempt_index,
            exc=exc,
            prepared=messages,
            transport_state=state,
        )

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        """Upstream's contract with the attempt count taken from the caller, not the environment."""
        self.usage_ledger.record_turn()
        attempts = max(1, int(self.config.max_query_attempts))
        backoffs = tuple(self.config.retry_backoff_seconds)
        retryable = frozenset(self.config.retryable_failure_categories)
        if len(backoffs) != attempts - 1:
            raise ValueError("provider retry schedule must define one delay per recovery attempt")
        if any(
            isinstance(delay, bool) or not isinstance(delay, int) or delay <= 0
            for delay in backoffs
        ):
            raise ValueError("provider retry delays must be positive integer seconds")
        approved = frozenset(RETRYABLE_PROVIDER_FAILURE_CATEGORIES)
        if not retryable.issubset(approved):
            raise ValueError("provider retry categories must use the transient-only vocabulary")
        aborts = tuple(self.abort_exceptions)
        for index in range(attempts):
            self._attempt_index = index
            try:
                prepared = self._prepare_messages_for_api(messages)
                if index > 0:
                    delay = backoffs[index - 1]
                    time.sleep(delay)
                    self.usage_ledger.record_retry(delay)
                response = self._query(prepared, **kwargs)
                break
            except aborts:
                raise
            except ProviderCallError as exc:
                if index == attempts - 1 or exc.category not in retryable:
                    raise
        # Post-response implementation failures are sanitized here, not at the boundary that
        # already recorded the response: the ledger keeps the attempt and its usage either way.
        cost_output = None
        try:
            cost_output = self._calculate_cost(response)
        except FormatError:
            raise
        except Exception:
            cost_output = None
        if cost_output is None:
            raise ResponseConversionError("provider cost accounting failed")
        GLOBAL_MODEL_STATS.add(cost_output["cost"])
        provenance = self.usage_ledger.last_provenance()
        try:
            actions = self._parse_actions(response)
        except FormatError as e:
            # Upstream persists the whole response here. This benchmark keeps only the sanitized
            # usage provenance: a raw body would carry content, arguments and response IDs into the
            # trajectory and every diagnostic built from it.
            e.messages[0].setdefault("extra", {})["response"] = provenance
            raise
        message = self._turn_message(response, actions)
        message["extra"] = {
            "actions": actions,
            "response": provenance,
            **cost_output,
            "timestamp": time.time(),
        }
        return message

    def _turn_message(self, response: Any, actions: list[dict]) -> dict:
        raise NotImplementedError


class CkbLitellmModel(_SanitizedProviderCalls, LitellmModel):
    """Chat-completions model with a fixed attempt count and a sanitized usage ledger.

    Retained for the development path. The accepted phase-one contract is Responses (ADR-0014).
    """

    def __init__(self, *, config_class: type = CkbLitellmModelConfig, api_key: str = "", **kwargs):
        super().__init__(config_class=config_class, **kwargs)
        self._install_ledger(api_key)

    def _turn_message(self, response: Any, actions: list[dict]) -> dict:
        return response.choices[0].message.model_dump()


class CkbLitellmResponseModelConfig(LitellmResponseModelConfig):
    max_query_attempts: int = 1
    """Provider attempts per model turn, supplied by the caller's reviewed profile."""
    retry_backoff_seconds: tuple[int, ...] = ()
    retryable_failure_categories: tuple[str, ...] = ()


class CkbLitellmResponseModel(_SanitizedProviderCalls, LitellmResponseModel):
    """The accepted phase-one model: the OpenAI Responses contract under the Task 17 boundary.

    Upstream's Responses model is correct about the protocol and wrong about retention: it puts the
    whole response into the returned message and into `FormatError`. Only the protocol is inherited.
    """

    def __init__(self, *, config_class: type = CkbLitellmResponseModelConfig, api_key: str = "",
                 **kwargs):
        super().__init__(config_class=config_class, **kwargs)
        self._install_ledger(api_key)

    def _turn_message(self, response: Any, actions: list[dict]) -> dict:
        """Every output item, in order: this IS the next stateless turn's input.

        GPT-5.6 persists reasoning across turns, and an application managing its own history must
        resend every response output item. Filtering to function calls silently dropped reasoning
        before turn two.

        Protocol history in the in-memory conversation is not published provenance: the response
        wrapper, response ID, headers, status and usage object stay out of it, and out of the
        ledger, result rows, evidence and diagnostics.
        """
        return {"object": "response", "output": _output_items(response)}

    def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
        """Flatten stateless history without replaying response-only status metadata.

        The CKBuilders HTTP Responses endpoint accepts the prior reasoning, message and function
        call items, but rejects their output-only ``status`` field as an unknown input parameter.
        Parsing already required every executable call to be completed, so removing that metadata
        changes no action semantics. All content, encrypted reasoning, IDs, arguments and ordering
        remain intact, and the stored in-memory history is never mutated.
        """
        prepared: list[dict] = []
        for message in messages:
            if message.get("object") == "response":
                for item in message.get("output", []):
                    prepared.append({
                        key: value for key, value in item.items()
                        if key not in {"extra", "status"}
                    })
            else:
                prepared.append({key: value for key, value in message.items() if key != "extra"})
        return prepared

    def _parse_actions(self, response: Any) -> list[dict]:
        """Executable actions only, with fixed-text failures.

        Upstream interpolates the returned tool name and parser text into `FormatError`, which the
        default agent stores as `str(e)` plus a formatted traceback. Every message here is a
        constant, so a provider-controlled value cannot reach a diagnostic through this path.
        """
        if _read_status(response) != "completed":
            raise FormatError(_fixed_format_error(UNFINISHED_RESPONSE))
        calls = [item for item in _output_items(response)
                 if item.get("type") == "function_call"]
        if not calls:
            raise FormatError(_fixed_format_error(NO_TOOL_CALL))
        actions: list[dict] = []
        seen: set[str] = set()
        for call in calls:
            actions.append(_executable_action(call, seen))
        return actions


UNFINISHED_RESPONSE = (
    "The response did not complete. Send exactly one completed bash tool call."
)
NO_TOOL_CALL = (
    "No tool call found in the response. Every response MUST include at least one bash tool call."
)
UNUSABLE_TOOL_CALL = (
    "A tool call was not usable. Every call must be a completed bash call with a unique call id "
    "and a string command."
)


def _fixed_format_error(text: str) -> dict[str, Any]:
    """A Responses-shaped FormatError message built from a constant, never from provider output."""
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
        "extra": {"interrupt_type": "FormatError"},
    }


def _executable_action(call: dict[str, Any], seen: set[str]) -> dict[str, Any]:
    """One action, or a fixed-text FormatError. Nothing provider-controlled is echoed.

    A call the harness cannot link to its result, or whose command is not a string, must not reach
    the execution environment: the agent would run it and only then find the turn unusable.
    """
    if call.get("status") != "completed":
        raise FormatError(_fixed_format_error(UNUSABLE_TOOL_CALL))
    call_id = call.get("call_id")
    if not isinstance(call_id, str) or not call_id.strip() or call_id in seen:
        raise FormatError(_fixed_format_error(UNUSABLE_TOOL_CALL))
    if call.get("name") != "bash":
        raise FormatError(_fixed_format_error(UNUSABLE_TOOL_CALL))
    raw = call.get("arguments")
    if not isinstance(raw, str):
        raise FormatError(_fixed_format_error(UNUSABLE_TOOL_CALL))
    arguments = _decoded(raw)
    if arguments is UNDECODABLE:
        # Raised OUTSIDE the parser's handler: a JSONDecodeError keeps the whole argument string in
        # `.doc`, and `from None` would leave it reachable through __context__.
        raise FormatError(_fixed_format_error(UNUSABLE_TOOL_CALL))
    command = arguments.get("command") if isinstance(arguments, dict) else None
    if not isinstance(command, str):
        raise FormatError(_fixed_format_error(UNUSABLE_TOOL_CALL))
    seen.add(call_id)
    return {"command": command, "tool_call_id": call_id}


UNDECODABLE = object()
"""Sentinel for arguments that are not JSON. Carries nothing from the failed parse."""


def _decoded(raw: str) -> Any:
    """Decode tool-call arguments, or report failure by value rather than by exception."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return UNDECODABLE


class ResponseConversionError(RuntimeError):
    """A fixed, value-free failure to serialize a provider item. Carries no provider text."""


def _read_status(response: Any) -> Any:
    status = getattr(response, "status", None)
    if status is None and isinstance(response, dict):
        status = response.get("status")
    return status


def _output_items(response: Any) -> list[dict[str, Any]]:
    """Every output item as a plain dict, in the provider's order.

    A hostile or broken item serializer must not put its exception text into the agent's diagnostics,
    so any failure here becomes one fixed error raised `from None`.
    """
    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output")
    if not isinstance(output, list):
        return []
    items: list[dict[str, Any]] = []
    for item in output:
        converted = _as_plain_item(item)
        if converted is None:
            # Raised OUTSIDE the handler: `from None` only suppresses display, leaving the original
            # exception reachable through __context__.
            raise ResponseConversionError("a provider output item could not be converted")
        items.append(converted)
    return items


def _as_plain_item(item: Any) -> dict[str, Any] | None:
    try:
        raw = item if isinstance(item, dict) else (
            item.model_dump() if hasattr(item, "model_dump") else dict(item)
        )
        return {key: value for key, value in raw.items() if key != "extra"}
    except Exception:
        return None
