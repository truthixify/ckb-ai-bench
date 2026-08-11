"""Flat JSON results store and strict validator (ADR-0012, RECOMMENDATION §4/§7).

One JSON file per run under ``results/<suite_semver>/``. The validator is the mitigation for JSON
enforcing no invariants at rest: duplicate cell keys, invalid outcomes, frozen-suite drift, and
unknown chains all fail loud before aggregation or rendering.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from ckbbench.config import CHAIN_PROFILES
from ckbbench.run.devnet import DEVNET_CHAIN_ID, LIFECYCLE_POLICY
from ckbbench.run.result import RunResult, write_result

VALID_OUTCOMES: frozenset[str] = frozenset(
    {"pass", "agent_fail", "infra_fail", "protocol_violation"}
)
AGENT_LIMIT_FIELDS: frozenset[str] = frozenset(
    {"step_limit", "cost_limit", "wall_time_limit_seconds"}
)


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


def validate_results(results: list[dict[str, Any]]) -> None:
    """Fail loud on invariant violations (ADR-0012).

    Checks:
    - no duplicate ``(suite, chain, arm, model, seed, run_id)`` keys;
    - every ``outcome`` is a known RunOutcome;
    - same ``suite_semver`` implies identical ``suite_freeze_hash`` and ``mcp_server_version``;
    - every ``chain`` is in ``CHAIN_PROFILES``;
    - ``agent_limits`` exists, is well-formed, and is concrete for any row that reached an agent;
    - managed DevNet provenance, where present, is complete and shares one immutable chain identity
      (policy, genesis, config digest) -- prepared tips are expected to differ.
    """
    if not results:
        return

    seen: set[tuple[str, str, str, str, int, str]] = set()
    freeze_by_suite: dict[str, tuple[str, str]] = {}

    _STRING_FIELDS = (
        "suite_semver", "chain", "arm", "model", "run_id",
        "suite_freeze_hash", "mcp_server_version", "outcome",
    )

    for i, row in enumerate(results):
        label = f"result[{i}]"
        if not isinstance(row, dict):
            raise ResultsValidationError(f"{label}: expected a JSON object")
        for field in (*_STRING_FIELDS, "seed"):
            if field not in row:
                raise ResultsValidationError(f"{label}: missing required field {field!r}")
        if "agent_limits" not in row:
            raise ResultsValidationError(f"{label}: missing required field 'agent_limits'")
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

    _validate_devnet_identity(results)


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
    if outcome != "infra_fail":
        for name, value in (
            ("step_limit", step),
            ("cost_limit", cost),
            ("wall_time_limit_seconds", wall),
        ):
            if value is None:
                raise ResultsValidationError(
                    f"{label}: agent_limits.{name} must be present for outcome {outcome!r}"
                )


def outcome_is_valid(outcome: str) -> bool:
    """Type-narrowing helper: True when ``outcome`` is a valid RunOutcome string."""
    return outcome in VALID_OUTCOMES
