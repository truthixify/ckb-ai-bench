#!/usr/bin/env bash
# Spike (NOT production): composed-prompt multi-task run (ADR-0008).
#
# Two parts, each asserted by exit code (no output-grep that could mask a result):
#   A. LOGIC (deterministic, no model): compose the prompt, verify known-good proofs
#      pass, a corrupted proof fails, and failure is ISOLATED to the one bad task.
#   B. MODEL (live grok via the proxy): drive the real agent through the composed
#      prompt; it must work all 3 independent tasks in one pass and all 3 Proofs must
#      grade PASS independently.
set -euo pipefail
cd "$(dirname "$0")"

PY="../../agent/.venv/bin/python"
export PYTHONPATH="$PWD/../../agent:$PWD"

fail=0
check () {  # check <want_exit> <label> <cmd...>
  local want="$1" label="$2"; shift 2
  local got=0
  "$@" >/dev/null 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then echo "PASS  $label (exit $got)"
  else echo "FAIL  $label (got $got, wanted $want)"; fail=1; fi
}

echo "== A. deterministic logic (composer + verifier + failure isolation) =="
check 0 "compose + verify known-good + corrupted-fails + isolation" \
  "$PY" test_logic.py

echo
echo "== B. live model loop (grok via proxy): all 3 tasks in one composed pass =="
# Model runs have inherent variance (a benchmark expects this: ADR-0011 mandates >=3
# runs/cell with CIs). We run the loop K times and require a strict majority (>= K-1),
# surfacing the pass rate as data. A single transient does not sink the spike; a broken
# mechanism fails every run. Each individual run still asserts: Submitted + used_mcp +
# all 3 proofs graded PASS independently.
check 0 "real model worked the composed suite across runs (>= K-1 of K)" \
  "$PY" run_model_arm.py

echo
if [ "$fail" -eq 0 ]; then echo "RESULT: ALL CHECKS PASSED"; exit 0
else echo "RESULT: FAILURES PRESENT"; exit 1; fi
