#!/usr/bin/env bash
# Unified test runner for the CKB AI Bench harness.
#
# Runs every test layer the harness has and fails loud (Rule 12) if any layer fails or is
# skipped. Layers are added as phases land:
#   - Python harness unit tests (pytest, with coverage)
#   - Node verifier-executable tests   (added in Phase 2)
#   - Rust hidden-suite tests          (added in Phase 2/6)
#
# Usage: scripts/test.sh            # all layers
#        scripts/test.sh --no-cov   # skip coverage (faster local loop)
set -euo pipefail
cd "$(dirname "$0")/.."

PY="agent/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "FAIL: $PY not found. Create it with: cd agent && uv venv --python 3.12 .venv" >&2
  exit 1
fi

COV="--cov=ckbbench --cov-report=term-missing"
for a in "$@"; do [ "$a" = "--no-cov" ] && COV=""; done

echo "== Python harness tests =="
"$PY" -m pytest $COV

# Node and Rust layers are wired in as their phases land; this script is the single entry point.
echo
echo "ALL TEST LAYERS PASSED"
