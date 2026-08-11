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
RUST_SPIKE_REFERENCE="spikes/code-task/ws/build/release/hashlock"
RUST_TASK_DIR="suites/ckb-v1/task-05-hashlock"

# Generated Rust content must land outside the repository: the freeze hashes authored files under a
# task directory, and `target` is excluded from that hash, so an in-repo Cargo target would mutate
# the suite invisibly.
resolve_external_dir() {
  local label="$1" raw="$2" abs repo
  if [ -z "${raw//[[:space:]]/}" ]; then
    echo "FAIL: $label resolved to an empty path" >&2
    return 1
  fi
  # Resolve without creating: a rejected value must leave no directory behind, and a relative value
  # must stay correct after the test subshell changes directory.
  abs="$("$PY" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$raw")" || return 1
  repo="$("$PY" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' .)" || return 1
  if [ "$abs" = "/" ]; then
    echo "FAIL: $label must not be the filesystem root" >&2
    return 1
  fi
  if [ "$abs" = "$repo" ] || [ "${repo#"$abs"/}" != "$repo" ]; then
    echo "FAIL: $label must not be the repository or one of its ancestors: $abs" >&2
    return 1
  fi
  if [ "${abs#"$repo"/}" != "$abs" ]; then
    echo "FAIL: $label must be outside the repository: $abs" >&2
    return 1
  fi
  printf '%s\n' "$abs"
}

# Create one directory component, refusing a pre-existing symlink: `mkdir -p` follows an existing
# parent symlink, which would place the fixture wherever that link points.
make_child_dir() {
  local path="$1/$2"
  if [ -L "$path" ] || { [ -e "$path" ] && [ ! -d "$path" ]; }; then
    echo "FAIL: refusing to use a symlink or non-directory at $path" >&2
    return 1
  fi
  if ! mkdir -p "$path"; then
    echo "FAIL: cannot create $path" >&2
    return 1
  fi
}

# The fixture must match a checked-out reference on every run: the external root is reusable, so a
# stale binary left there would otherwise grade the hidden suite. A canonical reference that exists
# in an invalid form is worktree damage, not a reason to grade the spike binary instead.
stage_rust_fixture() {
  local dest="$1" src=""
  if [ -e "$RUST_REFERENCE" ] || [ -L "$RUST_REFERENCE" ]; then
    if [ -L "$RUST_REFERENCE" ] || [ ! -f "$RUST_REFERENCE" ]; then
      echo "FAIL: canonical reference $RUST_REFERENCE is not a regular file" >&2
      return 1
    fi
    src="$RUST_REFERENCE"
  elif [ -e "$RUST_SPIKE_REFERENCE" ] || [ -L "$RUST_SPIKE_REFERENCE" ]; then
    if [ -L "$RUST_SPIKE_REFERENCE" ] || [ ! -f "$RUST_SPIKE_REFERENCE" ]; then
      echo "FAIL: spike reference $RUST_SPIKE_REFERENCE is not a regular file" >&2
      return 1
    fi
    src="$RUST_SPIKE_REFERENCE"
  else
    echo "FAIL: rust hidden-suite needs a reference binary at $RUST_REFERENCE (or the spike build)" >&2
    return 1
  fi
  if [ -L "$dest" ] || { [ -e "$dest" ] && [ ! -f "$dest" ]; }; then
    echo "FAIL: refusing to replace a symlink or non-regular fixture at $dest" >&2
    return 1
  fi
  if [ -f "$dest" ]; then
    if ! cmp -s "$src" "$dest"; then
      echo "FAIL: fixture at $dest differs from $src; remove it deliberately to restage" >&2
      return 1
    fi
    return 0
  fi
  cp "$src" "$dest"
}

# Production digest, used to prove the rust layer authored nothing under the task directory.
task05_digest() {
  "$PY" -c 'import sys; from pathlib import Path
from ckbbench.suite.freeze import hash_task_dir
print(hash_task_dir(Path(sys.argv[1])))' "$RUST_TASK_DIR"
}

echo
echo "== rust hidden-suite tests =="
if ! _rust_toolchain_ok; then
  skipped+=("rust:skipped-no-toolchain")
else
  if ! digest_before="$(task05_digest)" || [ -z "$digest_before" ]; then
    echo "FAIL: could not compute the $RUST_TASK_DIR directory digest" >&2
    exit 1
  fi

  if ! RUST_CARGO_TARGET="$(resolve_external_dir CKBBENCH_CARGO_TARGET_DIR \
      "${CKBBENCH_CARGO_TARGET_DIR:-/tmp/ckbbench-rust-target}")"; then
    exit 1
  fi
  if ! RUST_FIXTURE_ROOT="$(resolve_external_dir CKBBENCH_RUST_FIXTURE_ROOT \
      "${CKBBENCH_RUST_FIXTURE_ROOT:-$RUST_CARGO_TARGET/fixture}")"; then
    exit 1
  fi
  if ! mkdir -p "$RUST_CARGO_TARGET" \
    || ! make_child_dir "$RUST_FIXTURE_ROOT" build \
    || ! make_child_dir "$RUST_FIXTURE_ROOT/build" release; then
    exit 1
  fi
  # The completed parent must still be exactly beneath the accepted root.
  RUST_FIXTURE_DIR="$("$PY" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' \
    "$RUST_FIXTURE_ROOT/build/release")" || exit 1
  if [ "$RUST_FIXTURE_DIR" != "$RUST_FIXTURE_ROOT/build/release" ]; then
    echo "FAIL: fixture directory resolves outside its root: $RUST_FIXTURE_DIR" >&2
    exit 1
  fi
  if ! stage_rust_fixture "$RUST_FIXTURE_DIR/hashlock"; then
    exit 1
  fi
  (
    cd "$RUST_DIR"
    export BENCH_PASSWORD=test-secret-for-ci
    export CARGO_TARGET_DIR="$RUST_CARGO_TARGET"
    export TOP="$RUST_FIXTURE_ROOT"
    cargo test
  )

  if ! digest_after="$(task05_digest)" || [ -z "$digest_after" ]; then
    echo "FAIL: could not re-compute the $RUST_TASK_DIR directory digest" >&2
    exit 1
  fi
  if [ "$digest_before" != "$digest_after" ]; then
    echo "FAIL: the rust layer modified $RUST_TASK_DIR (directory digest changed)" >&2
    exit 1
  fi
  ran+=("rust:ok")
fi

echo
if [ "${#skipped[@]}" -gt 0 ]; then
  echo "LAYERS: ${ran[*]}  (${skipped[*]})"
else
  echo "LAYERS: ${ran[*]}"
fi
echo "ALL WIRED TEST LAYERS PASSED"