"""Flat JSON results store and strict validator (ADR-0012, RECOMMENDATION §4/§7).

One JSON file per run under ``results/<suite_semver>/``. The validator is the mitigation for JSON
enforcing no invariants at rest: duplicate cell keys, invalid outcomes, frozen-suite drift, and
unknown chains all fail loud before aggregation or rendering.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ckbbench.config import CHAIN_PROFILES
from ckbbench.run.result import RunResult, write_result

VALID_OUTCOMES: frozenset[str] = frozenset(
    {"pass", "agent_fail", "infra_fail", "protocol_violation"}
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
    - every ``chain`` is in ``CHAIN_PROFILES``.
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
        # Value-validity, not just presence: a null/blank string field or a bool/non-int seed
        # must fail loud (codex/grok-build), else cell_key's int()/str() would coerce silently or
        # crash with a bare ValueError outside this validator.
        for field in _STRING_FIELDS:
            val = row[field]
            if not isinstance(val, str) or not val.strip():
                raise ResultsValidationError(
                    f"{label}: field {field!r} must be a non-empty string, got {val!r}"
                )
        seed = row["seed"]
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ResultsValidationError(f"{label}: seed must be an int, got {seed!r}")

        outcome = str(row["outcome"])
        if outcome not in VALID_OUTCOMES:
            raise ResultsValidationError(
                f"{label}: invalid outcome {outcome!r}; "
                f"expected one of {sorted(VALID_OUTCOMES)}"
            )

        chain = str(row["chain"])
        if chain not in CHAIN_PROFILES:
            raise ResultsValidationError(
                f"{label}: unknown chain {chain!r}; expected one of {CHAIN_PROFILES}"
            )

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


def outcome_is_valid(outcome: str) -> bool:
    """Type-narrowing helper: True when ``outcome`` is a valid RunOutcome string."""
    return outcome in VALID_OUTCOMES