#!/usr/bin/env bash
# Unified test runner for the CKB AI Bench harness. This is the project's test entry point
# (used locally and as the CI contract). It fails loud (Rule 12) if any wired layer fails, and
# reports honestly which layers ran vs are not yet wired, so an operator is never misled by a
# green "passed" that only ran one layer.
#
# Layers (added as phases land):
#   - python : harness unit tests (pytest + coverage)   [wired]
#   - docker : container integration (containers/validate.sh) [opt-in: CKBBENCH_DOCKER=1]
#   - rust   : hidden-suite tests                        [wired]
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
PINNED_PYTHON="$(awk '$1=="python"{print $2}' .tool-versions)"
[ -n "$PINNED_PYTHON" ] || { echo "FAIL: cannot read the python pin from .tool-versions" >&2; exit 1; }
if [ -z "$PY" ] || ! "$PY" -c 'import ckbbench, pytest' >/dev/null 2>&1; then
  echo "FAIL: no python with ckbbench + pytest importable." >&2
  echo "  Bootstrap: cd agent && uv venv --python $PINNED_PYTHON .venv \\" >&2
  echo "             && uv pip install --python .venv/bin/python -r spike-requirements.txt \\" >&2
  echo "             && uv pip install --python .venv/bin/python -e \"..[dev]\"" >&2
  echo "  Or set CKBBENCH_PYTHON to a python that has them." >&2
  exit 1
fi

_rust_toolchain_ok() {
  command -v cargo >/dev/null 2>&1 || return 1
  command -v rustc >/dev/null 2>&1 || return 1
  local ver req
  ver="$(rustc --version | awk '{print $2}')"
  req="1.95.0"
  [ "$(printf '%s\n' "$req" "$ver" | sort -V | head -1)" = "$req" ]
}

cov=(--cov=ckbbench --cov=containers --cov-report=term-missing)
for a in "$@"; do [ "$a" = "--no-cov" ] && cov=(); done
# `--no-cov` empties `cov`, and bash before 4.4 (stock macOS ships 3.2) treats "${cov[@]}" on an
# empty array as unset under `set -u`. Forward it the same way scripts/ckbbench forwards `extra`.

# The suite freeze records this interpreter as provenance. A release gate that runs under a
# different one would certify a runtime the benchmark never used.
RUNTIME_PYTHON="$("$PY" -c 'import platform;print(platform.python_version())')"
if [ "$RUNTIME_PYTHON" != "$PINNED_PYTHON" ]; then
  echo "FAIL: test runtime is python $RUNTIME_PYTHON, not the pinned $PINNED_PYTHON" >&2
  echo "  .tool-versions is the single source of truth; rebuild agent/.venv via ./bench setup." >&2
  exit 1
fi

ran=()
skipped=()

# CKBBENCH_DOCKER selects the integration LAYER for this runner. It is also the production switch
# that puts orchestration in docker mode, so leaving it set during the unit layer would run those
# tests against local Docker state. Capture the request, then run the unit layer with the
# production switch explicitly off. Production behaviour outside this runner is unchanged.
WANT_DOCKER_LAYER="${CKBBENCH_DOCKER:-0}"

echo "== python harness tests =="
CKBBENCH_DOCKER=0 "$PY" -m pytest "${cov[@]+"${cov[@]}"}"
ran+=("python:ok")

if [ "$WANT_DOCKER_LAYER" = "1" ]; then
  echo
  echo "== docker container integration (CKBBENCH_DOCKER=1) =="
  bash containers/validate.sh
  ran+=("docker:ok")
else
  skipped+=("docker:opt-in-set-CKBBENCH_DOCKER=1")
fi

echo
echo "== rust hidden-suite tests =="
if ! _rust_toolchain_ok; then
  skipped+=("rust:skipped-no-toolchain")
else
  "$PY" scripts/validate_hidden_suites.py \
    --cargo-target "${CKBBENCH_CARGO_TARGET_DIR:-/tmp/ckbbench-rust-target}" \
    --fixture-root "${CKBBENCH_RUST_FIXTURE_ROOT:-/tmp/ckbbench-rust-fixtures}"
  ran+=("rust:ok")
fi

echo
if [ "${#skipped[@]}" -gt 0 ]; then
  echo "LAYERS: ${ran[*]}  (${skipped[*]})"
else
  echo "LAYERS: ${ran[*]}"
fi
echo "ALL WIRED TEST LAYERS PASSED"
