"""Flat JSON results store and strict validator (ADR-0012, RECOMMENDATION §4/§7).

One JSON file per run under ``results/<suite_semver>/``. The validator is the mitigation for JSON
enforcing no invariants at rest: duplicate cell keys, invalid outcomes, frozen-suite drift, and
unknown chains all fail loud before aggregation or rendering.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from ckbbench.config import CHAIN_PROFILES
from ckbbench.run.devnet import DEVNET_CHAIN_ID, LIFECYCLE_POLICY
from ckbbench.run.mcp_surface import (
    PROFILE_BY_ARM,
    SURFACE_PROFILES,
    McpSurfaceError,
    profile_for_arm,
)
from ckbbench.run.metrics import (
    COMPLETE,
    INCOMPLETE,
    MULTIPLE_CATEGORIES,
    NOT_STARTED,
    PROVIDER_FAILURE_CATEGORY_SET,
    USAGE_STATUSES,
)
from ckbbench.run.model_profile import (
    RETRYABLE_PROVIDER_FAILURE_CATEGORIES,
    ModelProfileError,
    load_model_profile,
    report_profile,
)
from ckbbench.run.result import RESULT_SCHEMA_VERSION, RunResult, write_result

VALID_OUTCOMES: frozenset[str] = frozenset(
    {"pass", "agent_fail", "infra_fail", "protocol_violation"}
)
AGENT_LIMIT_FIELDS: frozenset[str] = frozenset(
    {"step_limit", "cost_limit", "wall_time_limit_seconds"}
)
# The headline claim is C - B, so those two arms must have been measured under one budget. A and D
# use the same production defaults but are not the compared pair, so they are outside this guard.
COMPARED_ARMS: frozenset[str] = frozenset({"B", "C"})
# Deliberately excludes seed and run_id: repeated trials are exactly where a silent budget change
# has to be caught rather than averaged away.
_METHODOLOGY_IDENTITY_FIELDS = (
    "suite_semver", "suite_freeze_hash", "mcp_server_version", "chain", "model",
)
_METRIC_COUNTS = (
    "model_calls", "provider_attempts", "provider_responses", "provider_retry_count",
    "provider_retry_delay_seconds", "history_compaction_count", "history_dropped_groups",
    "history_dropped_items", "history_max_prepared_bytes",
)
_METRIC_TOKENS = ("prompt_tokens", "completion_tokens", "total_tokens")
_METRIC_FIELDS = frozenset({"total_wall_seconds", "token_usage_status",
                            "provider_failure_category", "provider_failure_counts",
                            *_METRIC_COUNTS, *_METRIC_TOKENS})
# Outcomes whose correctness is scored. Schema 1.5 separates this from efficiency: a recovered
# provider attempt may be scored while its incomplete token denominator remains excluded.
_SCORED_OUTCOMES = frozenset({"pass", "agent_fail", "protocol_violation"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ResultsValidationError(ValueError):
    """Raised when a result set violates storage invariants (ADR-0012)."""


def cell_key(result: dict[str, Any]) -> tuple[str, str, str, str, int, str]:
    """Unique key for one run: suite, chain, arm, model, seed, run_id."""
    return (
        str(result["suite_semver"]),
        str(result["chain"]),
        str(result["arm"]),
        str(result["model"]),
        int(result["seed"]),
        str(result["run_id"]),
    )


def suite_results_dir(base_dir: Path | str, suite_semver: str) -> Path:
    """Path for one suite's flat JSON artifacts: ``<base>/results/<suite_semver>/``."""
    return Path(base_dir) / "results" / suite_semver


def persist_result(result: RunResult, base_dir: Path | str) -> Path:
    """Write one RunResult JSON under the suite's results directory."""
    dest = suite_results_dir(base_dir, result.suite_semver)
    return write_result(result, dest)


def load_results(results_dir: Path | str) -> list[dict[str, Any]]:
    """Load all ``*.json`` run artifacts from ``results_dir`` (sorted for determinism)."""
    root = Path(results_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"results directory not found: {root}")
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ResultsValidationError(f"{path.name}: expected JSON object")
        out.append(data)
    return out


def validate_results(results: list[dict[str, Any]], *, profiles: tuple[Any, ...] | None = None) -> None:
    """Fail loud on invariant violations (ADR-0012).

    Checks:
    - no duplicate ``(suite, chain, arm, model, seed, run_id)`` keys;
    - every ``outcome`` is a known RunOutcome;
    - same ``suite_semver`` implies identical ``suite_freeze_hash`` and ``mcp_server_version``;
    - every ``chain`` is in ``CHAIN_PROFILES``;
    - ``schema_version`` is exactly the current schema: a legacy row cannot silently enter a
      current report, and no stored JSON is migrated in place;
    - ``mcp_surface_profile`` is present and is the fixed profile for that row's arm (ADR-0013);
    - the model profile is the reviewed one, the row's ``model`` is its requested model, and the
      token evidence is internally consistent for its status (ADR-0014);
    - ``agent_limits`` exists, is well-formed, and is concrete for any row that reached an agent;
    - every concrete B and C row of one methodology identity shares one budget tuple;
    - managed DevNet provenance, where present, is complete and shares one immutable chain identity
      (policy, genesis, config digest) -- prepared tips are expected to differ.
    """
    if not results:
        return

    seen: set[tuple[str, str, str, str, int, str]] = set()
    freeze_by_suite: dict[str, tuple[str, str]] = {}
    accepted_profiles = profiles or (report_profile(_reviewed_profile()),)
    profiles_by_key = {
        (profile.profile_id, profile.sha256): profile for profile in accepted_profiles
    }
    if len(profiles_by_key) != len(accepted_profiles):
        raise ResultsValidationError("report model profiles must have unique identity/digest pairs")

    _STRING_FIELDS = (
        "schema_version", "suite_semver", "chain", "arm", "model", "run_id",
        "suite_freeze_hash", "mcp_server_version", "outcome",
    )

    for i, row in enumerate(results):
        label = f"result[{i}]"
        if not isinstance(row, dict):
            raise ResultsValidationError(f"{label}: expected a JSON object")
        for field in (*_STRING_FIELDS, "seed"):
            if field not in row:
                raise ResultsValidationError(f"{label}: missing required field {field!r}")
        for required in ("agent_limits", "mcp_surface_profile"):
            if required not in row:
                raise ResultsValidationError(f"{label}: missing required field {required!r}")
        # Value-validity, not just presence: a null/blank string field or a bool/non-int seed
        # must fail loud (codex/grok-build), else cell_key's int()/str() would coerce silently or
        # crash with a bare ValueError outside this validator.
        for field in _STRING_FIELDS:
            val = row[field]
            if not isinstance(val, str) or not val.strip():
                raise ResultsValidationError(
                    f"{label}: field {field!r} must be a non-empty string, got {val!r}"
                )
        outcome = str(row["outcome"])
        if outcome not in VALID_OUTCOMES:
            raise ResultsValidationError(
                f"{label}: invalid outcome {outcome!r}; "
                f"expected one of {sorted(VALID_OUTCOMES)}"
            )
        seed = row["seed"]
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ResultsValidationError(f"{label}: seed must be an int, got {seed!r}")
        _validate_schema_version(label, str(row["schema_version"]))
        _validate_surface_profile(label, str(row["arm"]), row["mcp_surface_profile"])
        expected_profile = _profile_for_row(label, row, profiles_by_key)
        _validate_model_profile(label, row, expected_profile)
        _validate_usage(
            label,
            row,
            outcome=outcome,
            max_attempts_per_call=expected_profile.max_agent_query_attempts,
            retry_backoff_seconds=expected_profile.provider_retry_backoff_seconds,
            replay_max_bytes=expected_profile.replay_max_bytes,
        )
        _validate_agent_limits(label, row["agent_limits"], outcome=outcome)

        chain = str(row["chain"])
        if chain not in CHAIN_PROFILES:
            raise ResultsValidationError(
                f"{label}: unknown chain {chain!r}; expected one of {CHAIN_PROFILES}"
            )

        _validate_devnet_state(label, row.get("devnet_state"), outcome=outcome, chain=chain)

        key = cell_key(row)
        if key in seen:
            raise ResultsValidationError(
                f"{label}: duplicate cell key "
                f"(suite={key[0]}, chain={key[1]}, arm={key[2]}, "
                f"model={key[3]}, seed={key[4]}, run_id={key[5]})"
            )
        seen.add(key)

        suite = str(row["suite_semver"])
        freeze = (str(row["suite_freeze_hash"]), str(row["mcp_server_version"]))
        prior = freeze_by_suite.get(suite)
        if prior is not None and prior != freeze:
            raise ResultsValidationError(
                f"{label}: frozen-suite drift for {suite!r}: "
                f"expected suite_freeze_hash={prior[0]!r}, mcp_server_version={prior[1]!r}, "
                f"got suite_freeze_hash={freeze[0]!r}, mcp_server_version={freeze[1]!r}"
            )
        freeze_by_suite.setdefault(suite, freeze)

    _validate_comparison_budgets(results)
    _validate_model_methodology(results)
    _validate_devnet_identity(results)


def _profile_for_row(
    label: str,
    row: dict[str, Any],
    profiles_by_key: dict[tuple[str, str], Any],
) -> Any:
    profile_id = row.get("model_profile_id")
    digest = row.get("model_profile_sha256")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ResultsValidationError(f"{label}: model_profile_id must be a non-empty string")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ResultsValidationError(
            f"{label}: model_profile_sha256 must be 64 lowercase hex characters"
        )
    expected = profiles_by_key.get((profile_id, digest))
    if expected is None:
        raise ResultsValidationError(
            f"{label}: model profile identity/digest is not in the report manifest"
        )
    return expected


_DEVNET_STATE_FIELDS = (
    "lifecycle_policy", "chain", "genesis_hash", "config_sha256",
    "prepared_tip_number", "prepared_tip_hash",
)


def _is_hex32(value: Any) -> bool:
    return (
        isinstance(value, str) and value.startswith("0x") and len(value) == 66
        and all(c in "0123456789abcdefABCDEF" for c in value[2:])
    )


def _validate_devnet_state(label: str, state: Any, *, outcome: str, chain: str) -> None:
    """Managed DevNet provenance, when present, must be complete and internally consistent.

    Absence is legal: TestNet cells, local runs, schema-1.0.0 rows, and a lifecycle failure that
    never established identity all carry none. A row that reached an agent on a managed DevNet
    carries the full object, so a malformed one must fail loud rather than reach the report.
    """
    if state is None:
        return
    if not isinstance(state, dict):
        raise ResultsValidationError(f"{label}: devnet_state must be an object, got {state!r}")
    for field in _DEVNET_STATE_FIELDS:
        if field not in state:
            raise ResultsValidationError(f"{label}: devnet_state missing {field!r}")
    if state["lifecycle_policy"] != LIFECYCLE_POLICY:
        raise ResultsValidationError(
            f"{label}: devnet_state.lifecycle_policy is {state['lifecycle_policy']!r}, "
            f"expected {LIFECYCLE_POLICY!r}"
        )
    if state["chain"] != DEVNET_CHAIN_ID:
        raise ResultsValidationError(
            f"{label}: devnet_state.chain is {state['chain']!r}, expected {DEVNET_CHAIN_ID!r}"
        )
    if not _is_hex32(state["genesis_hash"]):
        raise ResultsValidationError(
            f"{label}: devnet_state.genesis_hash is not a 32-byte hex hash: {state['genesis_hash']!r}"
        )
    if not _is_hex32(state["prepared_tip_hash"]):
        raise ResultsValidationError(
            f"{label}: devnet_state.prepared_tip_hash is not a 32-byte hex hash: "
            f"{state['prepared_tip_hash']!r}"
        )
    digest = state["config_sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(
        c not in "0123456789abcdef" for c in digest
    ):
        raise ResultsValidationError(
            f"{label}: devnet_state.config_sha256 is not a sha256 hex digest: {digest!r}"
        )
    tip = state["prepared_tip_number"]
    if isinstance(tip, bool) or not isinstance(tip, int) or tip < 0:
        raise ResultsValidationError(
            f"{label}: devnet_state.prepared_tip_number must be a non-negative int, got {tip!r}"
        )
    if chain != "devnet":
        raise ResultsValidationError(
            f"{label}: carries managed DevNet provenance but its chain is {chain!r}; a result "
            "cannot be graded on one chain and attested on another"
        )
    del outcome  # accepted for symmetry with the other validators; absence is legal in every state


def _validate_devnet_identity(results: list[dict[str, Any]]) -> None:
    """Managed DevNet rows of ONE SUITE must share one immutable chain identity.

    Policy, genesis and config digest are the reset contract; prepared tips are NOT compared,
    because the miner runs continuously and equal tips would be a fabricated claim (plan §9.1).
    Scoping by suite matches the existing freeze check: two different suites legitimately have
    different chain definitions, and validating a combined set must not reject them.
    """
    identity_by_suite: dict[str, tuple[str, str, str]] = {}
    for i, row in enumerate(results):
        state = row.get("devnet_state")
        if not isinstance(state, dict):
            continue
        suite = str(row["suite_semver"])
        current = (state["lifecycle_policy"], state["genesis_hash"], state["config_sha256"])
        prior = identity_by_suite.setdefault(suite, current)
        if current != prior:
            raise ResultsValidationError(
                f"result[{i}]: managed DevNet identity drift within suite {suite!r} "
                f"(policy/genesis/config {current} != {prior}); these rows did not run against "
                "the same deterministic chain definition"
            )


def _validate_schema_version(label: str, version: str) -> None:
    """A current report is built from current rows only.

    Legacy rows predate ``mcp_surface_profile``, so their treatment is unknown. Accepting them
    would mean inferring provenance that was never recorded; they are refused rather than migrated.
    """
    if version != RESULT_SCHEMA_VERSION:
        raise ResultsValidationError(
            f"{label}: schema_version {version!r} cannot build a current report; this harness "
            f"reports schema {RESULT_SCHEMA_VERSION!r}"
        )


def _validate_surface_profile(label: str, arm: str, profile: Any) -> None:
    """The row's MCP surface must be the fixed profile for its arm (ADR-0013).

    Checked per row, so the verdict cannot depend on which trial is loaded first, and a missing
    profile is never inferred from the arm.
    """
    if not isinstance(profile, str) or not profile.strip():
        raise ResultsValidationError(
            f"{label}: mcp_surface_profile must be a non-empty string, got {profile!r}"
        )
    if profile not in SURFACE_PROFILES:
        raise ResultsValidationError(
            f"{label}: unknown mcp_surface_profile {profile!r} for arm {arm!r}; expected one of "
            f"{sorted(SURFACE_PROFILES)}"
        )
    try:
        expected = profile_for_arm(arm)
    except McpSurfaceError as exc:
        raise ResultsValidationError(
            f"{label}: unknown arm {arm!r}; expected one of {sorted(PROFILE_BY_ARM)}"
        ) from exc
    if profile != expected:
        raise ResultsValidationError(
            f"{label}: arm {arm!r} must run under mcp_surface_profile {expected!r}, got "
            f"{profile!r}"
        )


def _reviewed_profile():
    """The tracked phase-one profile. Its absence is a refusal, never an unpinned mode.

    Falling back to "accept any well-formed digest" would let an arbitrary model path pass as the
    approved one, which is exactly what the profile exists to prevent. Tests inject a synthetic
    reviewed profile instead of relying on the real file.
    """
    try:
        return load_model_profile()
    except ModelProfileError as exc:
        raise ResultsValidationError(
            f"a current phase-one report needs the tracked model profile: {exc}"
        ) from None


def _validate_model_profile(label: str, row: dict[str, Any], expected) -> None:
    """Model provenance must name the reviewed profile and agree with the row's model."""
    if expected is None:  # pragma: no cover - _reviewed_profile now raises instead
        raise ResultsValidationError(f"{label}: no reviewed model profile is available")
    profile_id = row.get("model_profile_id")
    digest = row.get("model_profile_sha256")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ResultsValidationError(
            f"{label}: model_profile_id must be a non-empty string, got {profile_id!r}"
        )
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ResultsValidationError(
            f"{label}: model_profile_sha256 must be 64 lowercase hex characters"
        )
    if profile_id != expected.profile_id:
        raise ResultsValidationError(
            f"{label}: model_profile_id {profile_id!r} is not the reviewed "
            f"{expected.profile_id!r}"
        )
    if digest != expected.sha256:
        raise ResultsValidationError(
            f"{label}: model_profile_sha256 does not match the tracked phase-one profile"
        )
    # Diagnostics name the expected value only. The got value comes from a result file whose model
    # fields are provider- or operator-controlled, so echoing one would publish it here.
    if str(row["model"]) != expected.requested_model:
        raise ResultsValidationError(
            f"{label}: model is not the profile's requested {expected.requested_model!r}"
        )
    # A moving alias can resolve to a different model between profile qualification and a run; a
    # matched B/C pair on the new identity would otherwise pass unnoticed.
    response_model = row.get("model_response_id")
    if response_model is not None and response_model != expected.probed_response_model:
        raise ResultsValidationError(
            f"{label}: returned model is not the profile's probed "
            f"{expected.probed_response_model!r}"
        )


def _wall_seconds(label: str, metrics: dict[str, Any]) -> float:
    value = metrics["total_wall_seconds"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultsValidationError(
            f"{label}: metrics.total_wall_seconds must be a real number, got "
            f"{type(value).__name__}"
        )
    if not math.isfinite(value) or value < 0:
        raise ResultsValidationError(
            f"{label}: metrics.total_wall_seconds must be finite and non-negative"
        )
    return float(value)


def _count(label: str, metrics: dict[str, Any], field: str) -> int:
    value = metrics[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResultsValidationError(
            f"{label}: metrics.{field} must be a non-negative int, got {value!r}"
        )
    return value


def _token(label: str, metrics: dict[str, Any], field: str) -> int | None:
    value = metrics[field]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResultsValidationError(
            f"{label}: metrics.{field} must be a non-negative int or null, got {value!r}"
        )
    return value


def _validate_provider_failure_category(
    label: str, metrics: dict[str, Any], *, outcome: str
) -> None:
    """The category must be an allowlisted token AND consistent with the counts it explains.

    Diagnostics name the field and the allowed literals only. A rejected value is provider- or
    file-controlled and could carry a secret, so it is never echoed.
    """
    category = metrics.get("provider_failure_category")
    if category is not None and (
        not isinstance(category, str) or category not in PROVIDER_FAILURE_CATEGORY_SET
    ):
        raise ResultsValidationError(
            f"{label}: metrics.provider_failure_category must be null or one of "
            f"{sorted(PROVIDER_FAILURE_CATEGORY_SET)}"
        )

    status = metrics.get("token_usage_status")
    attempts = metrics.get("provider_attempts")
    responses = metrics.get("provider_responses")
    if not isinstance(attempts, int) or not isinstance(responses, int):
        return  # the count validator below reports malformed counts

    unanswered = attempts - responses
    if unanswered > 0:
        if category is None:
            raise ResultsValidationError(
                f"{label}: {unanswered} unanswered provider attempt(s) require a "
                "metrics.provider_failure_category"
            )
        if status != INCOMPLETE:
            raise ResultsValidationError(
                f"{label}: a provider failure category requires token_usage_status "
                f"{INCOMPLETE!r}"
            )
        if category == MULTIPLE_CATEGORIES and unanswered < 2:
            raise ResultsValidationError(
                f"{label}: category {MULTIPLE_CATEGORIES!r} needs at least two unanswered attempts"
            )
    elif category is not None:
        raise ResultsValidationError(
            f"{label}: metrics.provider_failure_category must be null when every provider "
            "attempt was answered"
        )


def _validate_provider_failure_counts(label: str, metrics: dict[str, Any]) -> None:
    """Failure counts exactly explain unanswered attempts without retaining raw errors."""
    counts = metrics.get("provider_failure_counts")
    if not isinstance(counts, dict):
        raise ResultsValidationError(
            f"{label}: metrics.provider_failure_counts must be an object"
        )
    allowed = PROVIDER_FAILURE_CATEGORY_SET - {MULTIPLE_CATEGORIES}
    total = 0
    for category, count in counts.items():
        if not isinstance(category, str) or category not in allowed:
            raise ResultsValidationError(
                f"{label}: metrics.provider_failure_counts keys must be in {sorted(allowed)}"
            )
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ResultsValidationError(
                f"{label}: metrics.provider_failure_counts values must be positive integers"
            )
        total += count

    unanswered = metrics["provider_attempts"] - metrics["provider_responses"]
    if total != unanswered:
        raise ResultsValidationError(
            f"{label}: provider failure counts total {total} does not match {unanswered} "
            "unanswered attempt(s)"
        )
    category = metrics.get("provider_failure_category")
    expected = None
    if len(counts) == 1:
        expected = next(iter(counts))
    elif len(counts) > 1:
        expected = MULTIPLE_CATEGORIES
    if category != expected:
        raise ResultsValidationError(
            f"{label}: metrics.provider_failure_category does not summarize "
            "metrics.provider_failure_counts"
        )


def _possible_retry_delay_totals(
    calls: int, retries: int, backoffs: tuple[int, ...]
) -> set[int]:
    """All scheduled-delay totals for distributing retries across model turns."""
    prefixes = [0]
    for delay in backoffs:
        prefixes.append(prefixes[-1] + delay)
    states = {(0, 0)}
    for _ in range(calls):
        states = {
            (used + count, total + prefixes[count])
            for used, total in states
            for count in range(len(prefixes))
            if used + count <= retries
        }
    return {total for used, total in states if used == retries}


def _validate_retry_telemetry(
    label: str,
    metrics: dict[str, Any],
    *,
    calls: int,
    attempts: int,
    retries: int,
    retry_delay: int,
    retry_backoff_seconds: tuple[int, ...],
) -> None:
    _validate_provider_failure_counts(label, metrics)
    failed_attempts = attempts - metrics["provider_responses"]
    unresolved_calls = calls - metrics["provider_responses"]
    unretried_failures = failed_attempts - retries
    if unretried_failures < 0 or unretried_failures > unresolved_calls:
        raise ResultsValidationError(
            f"{label}: metrics.provider_retry_count is inconsistent with failed provider attempts "
            "and unresolved model calls"
        )
    counts = metrics["provider_failure_counts"]
    retryable_failures = sum(
        count for category, count in counts.items()
        if category in RETRYABLE_PROVIDER_FAILURE_CATEGORIES
    )
    if retries > retryable_failures:
        raise ResultsValidationError(
            f"{label}: metrics.provider_retry_count exceeds retryable provider failures"
        )
    possible_delays = _possible_retry_delay_totals(calls, retries, retry_backoff_seconds)
    if retry_delay not in possible_delays:
        raise ResultsValidationError(
            f"{label}: metrics.provider_retry_delay_seconds is inconsistent with the reviewed "
            "retry schedule"
        )


def _validate_replay_telemetry(
    label: str,
    *,
    calls: int,
    compactions: int,
    dropped_groups: int,
    dropped_items: int,
    max_prepared_bytes: int,
    replay_max_bytes: int,
) -> None:
    if compactions > calls:
        raise ResultsValidationError(
            f"{label}: metrics.history_compaction_count exceeds model_calls"
        )
    if compactions == 0 and (dropped_groups != 0 or dropped_items != 0):
        raise ResultsValidationError(
            f"{label}: history cannot be dropped without a recorded compaction"
        )
    if compactions > 0 and (
        dropped_groups < compactions or dropped_items < dropped_groups
    ):
        raise ResultsValidationError(
            f"{label}: history compaction totals are internally inconsistent"
        )
    if max_prepared_bytes > replay_max_bytes:
        raise ResultsValidationError(
            f"{label}: metrics.history_max_prepared_bytes exceeds the reviewed replay ceiling"
        )
    if max_prepared_bytes == 0 and (compactions or dropped_groups or dropped_items):
        raise ResultsValidationError(
            f"{label}: history compaction needs a non-zero prepared-byte observation"
        )


def _validate_usage(
    label: str,
    row: dict[str, Any],
    *,
    outcome: str,
    max_attempts_per_call: int,
    retry_backoff_seconds: tuple[int, ...],
    replay_max_bytes: int,
) -> None:
    """Token evidence must be internally consistent and honest about what it is (ADR-0014)."""
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        raise ResultsValidationError(f"{label}: metrics must be an object")
    if set(metrics) != _METRIC_FIELDS:
        raise ResultsValidationError(
            f"{label}: metrics keys must be {sorted(_METRIC_FIELDS)}, got {sorted(metrics)}"
        )
    _wall_seconds(label, metrics)
    status = metrics["token_usage_status"]
    # `in` on an unhashable value raises TypeError; a malformed status must be a validation error.
    if not isinstance(status, str) or status not in USAGE_STATUSES:
        raise ResultsValidationError(
            f"{label}: metrics.token_usage_status must be one of {sorted(USAGE_STATUSES)}, "
            f"got {status!r}"
        )
    calls, attempts, responses, retries, retry_delay = (
        _count(label, metrics, f) for f in _METRIC_COUNTS[:5]
    )
    compactions, dropped_groups, dropped_items, max_prepared_bytes = (
        _count(label, metrics, f) for f in _METRIC_COUNTS[5:]
    )
    prompt, completion, total = (_token(label, metrics, f) for f in _METRIC_TOKENS)
    present = [t for t in (prompt, completion, total) if t is not None]
    if present and len(present) != 3:
        raise ResultsValidationError(
            f"{label}: metrics token fields must be all present or all null"
        )
    if present and total != prompt + completion:
        raise ResultsValidationError(
            f"{label}: metrics tokens break the identity total = prompt + completion"
        )
    response_model = row.get("model_response_id")
    if response_model is not None and (
        not isinstance(response_model, str) or not response_model.strip()
    ):
        raise ResultsValidationError(
            f"{label}: model_response_id must be a non-empty string or null"
        )
    _validate_replay_telemetry(
        label,
        calls=calls,
        compactions=compactions,
        dropped_groups=dropped_groups,
        dropped_items=dropped_items,
        max_prepared_bytes=max_prepared_bytes,
        replay_max_bytes=replay_max_bytes,
    )

    # Every model call has one first attempt and at most the reviewed bounded recovery count. A
    # response closes one model call, so retries may raise attempts above calls but never responses.
    if attempts < calls:
        raise ResultsValidationError(
            f"{label}: every model call needs at least one provider attempt, got {calls} call(s) "
            f"and {attempts} attempt(s)"
        )
    if attempts > calls * max_attempts_per_call:
        raise ResultsValidationError(
            f"{label}: {attempts} provider attempt(s) exceed the reviewed ceiling of "
            f"{max_attempts_per_call} per {calls} model call(s)"
        )
    if responses > attempts:
        raise ResultsValidationError(
            f"{label}: metrics report {responses} response(s) for {attempts} attempt(s)"
        )
    if responses > calls:
        raise ResultsValidationError(
            f"{label}: metrics report {responses} response(s) for {calls} model call(s)"
        )
    if responses == 0 and (present or response_model is not None):
        raise ResultsValidationError(
            f"{label}: no provider response can carry tokens or a returned model identity"
        )
    if status == NOT_STARTED:
        if (calls, attempts, responses) != (0, 0, 0) or present or response_model is not None:
            raise ResultsValidationError(
                f"{label}: 'not_started' usage cannot carry calls, attempts, responses, tokens "
                "or a returned model"
            )
        if outcome != "infra_fail":
            raise ResultsValidationError(
                f"{label}: outcome {outcome!r} cannot carry 'not_started' usage; a correctness "
                "row needs complete token evidence"
            )
        _validate_provider_failure_category(label, metrics, outcome=outcome)
        _validate_retry_telemetry(
            label, metrics, calls=calls, attempts=attempts, retries=retries,
            retry_delay=retry_delay, retry_backoff_seconds=retry_backoff_seconds,
        )
        return
    if status == COMPLETE:
        if attempts == 0 or not (calls == attempts == responses):
            raise ResultsValidationError(
                f"{label}: 'complete' usage needs at least one attempt and "
                f"model_calls == provider_attempts == provider_responses, got "
                f"({calls}, {attempts}, {responses})"
            )
        if not present:
            raise ResultsValidationError(f"{label}: 'complete' usage cannot have null tokens")
        if response_model is None:
            raise ResultsValidationError(
                f"{label}: 'complete' usage needs one returned model identity"
            )
        _validate_provider_failure_category(label, metrics, outcome=outcome)
        _validate_retry_telemetry(
            label, metrics, calls=calls, attempts=attempts, retries=retries,
            retry_delay=retry_delay, retry_backoff_seconds=retry_backoff_seconds,
        )
        return
    # incomplete
    if attempts == 0:
        raise ResultsValidationError(
            f"{label}: 'incomplete' usage needs at least one provider attempt"
        )
    if outcome in _SCORED_OUTCOMES:
        if calls == 0 or responses != calls:
            raise ResultsValidationError(
                f"{label}: outcome {outcome!r} with incomplete usage needs every model call to "
                f"eventually receive a response, got {calls} call(s) and {responses} response(s)"
            )
        if response_model is None:
            raise ResultsValidationError(
                f"{label}: outcome {outcome!r} with incomplete usage needs one returned model "
                "identity"
            )
    _validate_provider_failure_category(label, metrics, outcome=outcome)
    _validate_retry_telemetry(
        label, metrics, calls=calls, attempts=attempts, retries=retries,
        retry_delay=retry_delay, retry_backoff_seconds=retry_backoff_seconds,
    )


def _validate_model_methodology(results: list[dict[str, Any]]) -> None:
    """B and C of one identity must share the model path, and a run has one returned identity.

    Order-independent: rows are grouped, then rendered in sorted order.
    """
    by_identity: dict[tuple[str, ...], dict[tuple[str, str, str], set[str]]] = {}
    for row in results:
        arm = str(row["arm"])
        if arm not in COMPARED_ARMS:
            continue
        response_model = row.get("model_response_id")
        if response_model is None:
            continue
        identity = tuple(str(row[f]) for f in _METHODOLOGY_IDENTITY_FIELDS)
        key = (str(row["model_profile_id"]), str(row["model_profile_sha256"]),
               str(row["model"]), str(response_model))
        by_identity.setdefault(identity, {}).setdefault(key, set()).add(arm)

    for identity in sorted(by_identity):
        paths = by_identity[identity]
        if len(paths) == 1:
            continue
        found = "; ".join(
            f"arm(s) {','.join(sorted(arms))} on profile {key[0]} ({key[1][:12]}) "
            f"requesting {key[2]} returning {key[3]}"
            for key, arms in sorted(paths.items())
        )
        raise ResultsValidationError(
            "mixed B/C model methodology for "
            f"(suite={identity[0]}, freeze={identity[1]}, mcp={identity[2]}, chain={identity[3]}): "
            f"{found}; these rows are not one comparable model path"
        )


def _budget_tuple(limits: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        limits["step_limit"],
        limits["cost_limit"],
        limits["wall_time_limit_seconds"],
    )


def _validate_comparison_budgets(results: list[dict[str, Any]]) -> None:
    """B and C rows of one methodology identity must share one concrete budget (RD2).

    A ``C - B`` difference measured under different step, cost or wall-time ceilings is causally
    ambiguous: it can reflect the product, the budget, or both. The identity excludes seed and
    run_id on purpose, so drift between two trials of the SAME arm fails here rather than being
    aggregated. Rows that never reached an agent carry all-null limits and are skipped; a partially
    null object was already rejected upstream.

    Both the verdict and the message are order-independent: the same set of rows in any order
    produces the same diagnostic.
    """
    by_identity: dict[tuple[str, ...], dict[tuple[Any, Any, Any], set[str]]] = {}
    for row in results:
        arm = str(row["arm"])
        if arm not in COMPARED_ARMS:
            continue
        limits = row["agent_limits"]
        if all(limits[name] is None for name in AGENT_LIMIT_FIELDS):
            continue
        identity = tuple(str(row[f]) for f in _METHODOLOGY_IDENTITY_FIELDS)
        by_identity.setdefault(identity, {}).setdefault(_budget_tuple(limits), set()).add(arm)

    for identity in sorted(by_identity):
        budgets = by_identity[identity]
        if len(budgets) == 1:
            continue
        found = "; ".join(
            f"arm(s) {','.join(sorted(arms))} at "
            f"(step={budget[0]}, cost={budget[1]}, wall={budget[2]})"
            for budget, arms in sorted(budgets.items(), key=lambda kv: repr(kv[0]))
        )
        raise ResultsValidationError(
            "mixed B/C agent budgets for "
            f"(suite={identity[0]}, freeze={identity[1]}, mcp={identity[2]}, "
            f"chain={identity[3]}, model={identity[4]}): {found}; "
            "these rows are not one comparable methodology"
        )


def _validate_agent_limits(label: str, limits: Any, *, outcome: str) -> None:
    """Agent budgets are part of result provenance; malformed values must fail loud."""
    if not isinstance(limits, dict):
        raise ResultsValidationError(f"{label}: agent_limits must be an object")
    keys = set(limits)
    if keys != AGENT_LIMIT_FIELDS:
        raise ResultsValidationError(
            f"{label}: agent_limits keys must be {sorted(AGENT_LIMIT_FIELDS)}, "
            f"got {sorted(keys)}"
        )
    step = limits["step_limit"]
    wall = limits["wall_time_limit_seconds"]
    cost = limits["cost_limit"]
    for name, value in (("step_limit", step), ("wall_time_limit_seconds", wall)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ResultsValidationError(f"{label}: agent_limits.{name} must be a non-negative int or null")
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ResultsValidationError(
            f"{label}: agent_limits.cost_limit must be a finite non-negative number or null"
        )
    named = (("step_limit", step), ("cost_limit", cost), ("wall_time_limit_seconds", wall))
    if outcome != "infra_fail":
        for name, value in named:
            if value is None:
                raise ResultsValidationError(
                    f"{label}: agent_limits.{name} must be present for outcome {outcome!r}"
                )
        return
    # A pre-agent infra_fail legitimately has no budget at all. Half of one is not provenance: it
    # cannot say which ceiling the row ran under, so it must not reach the comparison guard.
    missing = [name for name, value in named if value is None]
    if missing and len(missing) != len(named):
        raise ResultsValidationError(
            f"{label}: agent_limits must be all-null for a pre-agent {outcome!r} or fully "
            f"concrete; got null {sorted(missing)}"
        )


def outcome_is_valid(outcome: str) -> bool:
    """Type-narrowing helper: True when ``outcome`` is a valid RunOutcome string."""
    return outcome in VALID_OUTCOMES
