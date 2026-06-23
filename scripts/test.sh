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
if [ -z "$PY" ] || ! "$PY" -c 'import ckbbench, pytest' >/dev/null 2>&1; then
  echo "FAIL: no python with ckbbench + pytest importable." >&2
  echo "  Bootstrap: cd agent && uv venv --python 3.12 .venv \\" >&2
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

RUST_DIR="suites/ckb-v1/task-05-hashlock/hidden"
RUST_REFERENCE="suites/ckb-v1/task-05-hashlock/reference/hashlock"
RUST_FIXTURE="suites/ckb-v1/task-05-hashlock/build/release/hashlock"
echo
echo "== rust hidden-suite tests =="
if ! _rust_toolchain_ok; then
  skipped+=("rust:skipped-no-toolchain")
else
  if [ ! -f "$RUST_FIXTURE" ]; then
    mkdir -p "$(dirname "$RUST_FIXTURE")"
    if [ -f "$RUST_REFERENCE" ]; then
      cp "$RUST_REFERENCE" "$RUST_FIXTURE"
    elif [ -f "spikes/code-task/ws/build/release/hashlock" ]; then
      cp "spikes/code-task/ws/build/release/hashlock" "$RUST_FIXTURE"
    fi
  fi
  if [ ! -f "$RUST_FIXTURE" ]; then
    echo "FAIL: rust hidden-suite needs reference binary at $RUST_REFERENCE (or spike build)" >&2
    exit 1
  fi
  (
    cd "$RUST_DIR"
    export BENCH_PASSWORD=test-secret-for-ci
    export CARGO_TARGET_DIR="${CKBBENCH_CARGO_TARGET_DIR:-/tmp/ckbbench-rust-target}"
    cargo test
  )
  ran+=("rust:ok")
fi

echo
if [ "${#skipped[@]}" -gt 0 ]; then
  echo "LAYERS: ${ran[*]}  (${skipped[*]})"
else
  echo "LAYERS: ${ran[*]}"
fi
echo "ALL WIRED TEST LAYERS PASSED"