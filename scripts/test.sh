#!/usr/bin/env bash
# Unified test runner for the CKB AI Bench harness. This is the project's test entry point
# (used locally and as the CI contract). It fails loud (Rule 12) if any wired layer fails, and
# reports honestly which layers ran vs are not yet wired, so an operator is never misled by a
# green "passed" that only ran one layer.
#
# Layers (added as phases land):
#   - python : harness unit tests (pytest + coverage)   [wired]
#   - docker : container integration (containers/validate.sh) [opt-in: CKBBENCH_DOCKER=1]
#   - node   : verifier-executable tests                 [Phase 2]
#   - rust   : hidden-suite tests                        [Phase 2/6]
#
# Usage: scripts/test.sh            # all wired layers, with coverage
#        scripts/test.sh --no-cov   # skip coverage (faster local loop)
set -euo pipefail
cd "$(dirname "$0")/.."

# Python interpreter: prefer an explicit override, else the agent venv (which carries the
# harness deps), else whatever python is on PATH. The harness package must be importable
# (editable install: cd agent && uv pip install --python .venv/bin/python -e "..[dev]").
PY="${CKBBENCH_PYTHON:-agent/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PY" ] || ! "$PY" -c 'import ckbbench, pytest' >/dev/null 2>&1; then
  echo "FAIL: no python with ckbbench + pytest importable." >&2
  echo "  Bootstrap: cd agent && uv venv --python 3.12 .venv \\" >&2
  echo "             && uv pip install --python .venv/bin/python -e \"..[dev]\"" >&2
  echo "  Or set CKBBENCH_PYTHON to a python that has them." >&2
  exit 1
fi

cov=(--cov=ckbbench --cov-report=term-missing)
for a in "$@"; do [ "$a" = "--no-cov" ] && cov=(); done

ran=()
skipped=()

echo "== python harness tests =="
"$PY" -m pytest "${cov[@]}"
ran+=("python:ok")

if [ "${CKBBENCH_DOCKER:-0}" = "1" ]; then
  echo
  echo "== docker container integration (CKBBENCH_DOCKER=1) =="
  bash containers/validate.sh
  ran+=("docker:ok")
else
  skipped+=("docker:opt-in-set-CKBBENCH_DOCKER=1")
fi

# Node and Rust layers are wired in as their phases land. Until then they are explicitly
# reported as not-run so the summary never overstates coverage.
echo
if [ "${#skipped[@]}" -gt 0 ]; then
  echo "LAYERS: ${ran[*]}  (${skipped[*]}; node: not-wired-yet; rust: not-wired-yet)"
else
  echo "LAYERS: ${ran[*]}  (node: not-wired-yet; rust: not-wired-yet)"
fi
echo "ALL WIRED TEST LAYERS PASSED"
