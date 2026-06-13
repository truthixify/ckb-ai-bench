#!/usr/bin/env bash
# Spike (NOT production): prove the Code-Task hidden-suite grading guarantee holds across
# the real CONTAINER trust boundary (ADR-0004 fat pinned toolchain image, ADR-0005 hermetic
# verifier fed by a mounted folder, ADR-0002 hide-proof Proof, ADR-0009 verifier-private
# params injected post-`done`).
#
# The Tier-1 code-task spike proved hidden-suite grading NATIVELY on the host. This proves
# the SAME guarantee when the agent build and the grade run in SEPARATE containers, where
# content isolation + injection timing are enforced by WHAT IS MOUNTED into each stage, not
# by trust.
#
# Topology (one pinned toolchain image, TWO distinct `docker run` stages, DIFFERENT mounts):
#   AGENT stage    : mounts agent-ws RW (contract sources only, NO tests/, NO password),
#                    runs `make build` -> build/release/hashlock into the mount.
#   VERIFIER stage : a SECOND, separate run that mounts the hidden suite RW + the agent's
#                    binary READ-ONLY + injects BENCH_PASSWORD (a fresh per-run secret) only
#                    now. It compiles and runs the Rust ckb-testtool suite against the agent
#                    binary off-chain. The suite's process exit code is the grade.
#
# Each claim is asserted by EXIT CODE via the `check` helper; we capture $? directly and
# never mask an exit code through a pipe.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"

IMAGE="ckb-toolchain:spike"
# ONE shared cargo cache (crates.io index + downloaded crates), pre-warmed ONCE before the
# timed checks. All stages run as the same host uid, so there is no root-owned-subdir race;
# a shared warm cache means each stage does NOT re-fetch the crates.io index (a slow, flaky
# cold step that, with three separate caches, tripled the transient-failure surface).
# Each build runs in its OWN subdir of a separate WORK volume (NOT a host bind mount), which
# avoids the overlay bind-mount write race on target/ that intermittently corrupted builds.
CARGO_VOL="ckb-cv-cargo"                 # shared, warm crate cache (read-mostly during builds)
WORK_VOL="ckb-cv-work"                   # scratch build trees (one subdir per stage)
SRC="$HERE/../code-task/ws"              # the proven-on-host Code-Task sources we grade
HOST_UID="$(id -u)"; HOST_GID="$(id -g)" # run containers as host user so artifacts are ours

# Staging dirs this spike creates and tears down.
# *_SRC are source-only workspaces (contract sources, NO tests/, NO password); the build
# mounts them READ-ONLY and writes the binary into the ART_* artifact dirs.
AGENT_WS_SRC="$HERE/agent-ws"
CHEAT_WS_SRC="$HERE/cheat-ws"
VERIFIER_WS="$HERE/verifier-ws"
ART_OK="$HERE/artifact-correct"          # agent's CORRECT binary, mounted :ro at verify
ART_CHEAT="$HERE/artifact-cheat"         # agent's CHEAT  binary, mounted :ro at verify

fail=0
checks=0
passed=0
check () {  # check <want_exit> <label> <cmd...>
  local want="$1" label="$2"; shift 2
  checks=$((checks+1))
  local got=0
  "$@" >/dev/null 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then
    echo "PASS  $label (exit $got)"
    passed=$((passed+1))
  else
    echo "FAIL  $label (got exit $got, wanted $want)"
    fail=1
  fi
}

# Run as host user so build artifacts in the mount are host-owned (no root-owned droppings).
run_tc () { docker run --rm --user "$HOST_UID:$HOST_GID" "$@"; }

# Create a FRESH, host-owned volume (idempotent: remove any stale one first, so a prior
# interrupted run cannot leave a root-owned volume that breaks this run).
fresh_vol () {  # fresh_vol <volume-name> <mount-path>
  docker volume rm "$1" >/dev/null 2>&1 || true
  docker volume create "$1" >/dev/null
  docker run --rm -v "$1":"$2" "$IMAGE" chown -R "$HOST_UID:$HOST_GID" "$2"
}

cleanup () {
  # Tear down ONLY what this spike created. Never touch other containers/volumes.
  # The build writes target/ into the WORK volume (not the source ws or a host bind mount),
  # so the source ws and artifacts are plain host-owned dirs we can remove directly.
  rm -rf "$AGENT_WS_SRC" "$CHEAT_WS_SRC" "$VERIFIER_WS" "$ART_OK" "$ART_CHEAT" 2>/dev/null || true
  docker volume rm "$CARGO_VOL" "$WORK_VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== sanity: proven Code-Task sources present =="
test -f "$SRC/contracts/hashlock/src/main.rs" || { echo "missing contract source"; exit 1; }
test -f "$SRC/tests/src/tests.rs"             || { echo "missing hidden suite";    exit 1; }

echo "== build the pinned toolchain image (cached after first run) =="
docker build -f toolchain.Dockerfile -t "$IMAGE" . >/dev/null
echo "   image: $IMAGE"
echo "   toolchain provenance (ADR-0004 manifest):"
docker run --rm "$IMAGE" cat /tool-versions.txt | sed 's/^/     /'

echo "== prepare host-owned cargo + work volumes =="
fresh_vol "$CARGO_VOL" /cargo
fresh_vol "$WORK_VOL"  /work
# Pre-warm the shared cargo cache ONCE (fetch the crates.io index + all crates) by building
# the correct contract before the timed checks. This removes the slow, flaky cold index
# fetch from inside the checks: every stage after this reuses the warm cache. We build into
# a throwaway work subdir; the real per-stage builds below repeat with their own outputs.
echo "   warming cargo cache (one cold fetch + compile; subsequent stages are warm)..."
# The cold compile (full crate download + build) is the one network/IO-heavy step and is
# occasionally cut short by a transient (a slow crates.io index fetch, an overlay write
# blip). It is fully idempotent (throwaway /work/warm into a fresh cache), so we retry it
# up to 3 times: a transient succeeds on a later try, a GENUINE build error fails all three
# (the tail is printed and the spike aborts). This is a deterministic-transform retry, not
# masking a real failure.
warm_cache () {
  docker run --rm --user "$HOST_UID:$HOST_GID" \
    -v "$SRC":/src:ro -v "$CARGO_VOL":/cargo -v "$WORK_VOL":/work -e CARGO_HOME=/cargo \
    "$IMAGE" sh -c '
      set -e
      rm -rf /work/warm && mkdir -p /work/warm
      cp -r /src/contracts /src/scripts /src/Makefile /src/Cargo.lock /work/warm/
      printf "[workspace]\nresolver=\"2\"\nmembers=[\"contracts/hashlock\"]\n[profile.release]\noverflow-checks=true\nstrip=false\ncodegen-units=1\ndebug=true\n" > /work/warm/Cargo.toml
      cd /work/warm && make build
    ' >/tmp/cv-warm.log 2>&1
}
warmed=0
for attempt in 1 2 3; do
  if warm_cache; then warmed=1; break; fi
  echo "   warm-up attempt $attempt failed (transient?); retrying..."
done
[ "$warmed" -eq 1 ] || { echo "FATAL: cache warm-up build failed 3x; tail:"; tail -15 /tmp/cv-warm.log; exit 1; }
echo "   cache warm."

# ---------------------------------------------------------------------------------------
# Stage the AGENT workspace: ONLY contract sources. NO tests/. NO password anywhere.
# The agent-only Cargo.toml lists ONLY the contract (not `tests`), so the workspace builds
# without the hidden suite present -- the suite is genuinely out of the agent's reach.
# ---------------------------------------------------------------------------------------
stage_agent_ws () {  # stage_agent_ws <dest> <contract-main.rs>
  local dest="$1" main="$2"
  rm -rf "$dest"; mkdir -p "$dest"
  cp -r "$SRC/contracts" "$dest/"
  cp -r "$SRC/scripts"   "$dest/"
  cp    "$SRC/Makefile"  "$dest/"
  cp    "$SRC/Cargo.lock" "$dest/"
  cp    "$main" "$dest/contracts/hashlock/src/main.rs"
  cat > "$dest/Cargo.toml" <<'EOF'
[workspace]
resolver = "2"

members = [
  # @@INSERTION_POINT@@
  "contracts/hashlock",
]

[profile.release]
overflow-checks = true
strip = false
codegen-units = 1
debug = true
EOF
}

echo "== stage AGENT workspace (correct contract) and CHEAT workspace =="
stage_agent_ws "$AGENT_WS_SRC" "$SRC/contracts/hashlock/src/main.rs"   # correct (reads lock args)
stage_agent_ws "$CHEAT_WS_SRC" "$HERE/fixtures/cheat-main.rs"          # cheat (hardcoded guess)

# ---------------------------------------------------------------------------------------
# Stage the VERIFIER workspace: the HIDDEN suite + a workspace listing ONLY `tests`.
# This is kept OUT of the agent's reach and is mounted only at verify time.
# ---------------------------------------------------------------------------------------
echo "== stage VERIFIER workspace (the hidden suite, withheld from the agent) =="
rm -rf "$VERIFIER_WS"; mkdir -p "$VERIFIER_WS"
cp -r "$SRC/tests"     "$VERIFIER_WS/"
cp    "$SRC/Cargo.lock" "$VERIFIER_WS/"
cat > "$VERIFIER_WS/Cargo.toml" <<'EOF'
[workspace]
resolver = "2"

members = [
  "tests",
]
EOF

# The agent build. The source ws is mounted READ-ONLY (the agent build cannot write back
# to its own sources -- stronger isolation); the build runs in a per-stage subdir of the
# WORK volume (NOT a host bind mount) to avoid the overlay bind-mount write race that
# intermittently corrupted target/ (cargo creates files faster than the overlay bind
# settles). It reuses the shared, pre-warmed cargo cache, so no slow/flaky cold index fetch
# happens inside the check. Only the final binary is copied to the host artifact dir -- a
# single write, no race -- placed at <out>/build/release/<name> so <out> IS the read-only
# artifact mounted at verify time (the harness grades the binary built from agent sources).
agent_build () {  # agent_build <ws-dir> <work-name> <out-dir>
  local log; log="/tmp/cv-build-$(basename "$3").log"
  # The build is idempotent (its own work subdir, fresh out dir each attempt). Retry up to
  # 3 times to absorb a transient (overlay write blip), the same deterministic-transform
  # retry as the warm-up; a real compile error fails all three and the tail is surfaced.
  local attempt ec
  for attempt in 1 2 3; do
    rm -rf "$3"; mkdir -p "$3/build/release"; chmod -R 777 "$3"
    ec=0
    run_tc -v "$1":/ws:ro -v "$3":/out \
           -v "$CARGO_VOL":/cargo -v "$WORK_VOL":/work -e CARGO_HOME=/cargo \
           -e WN="$2" "$IMAGE" sh -c '
      set -e
      rm -rf "/work/$WN" && cp -r /ws "/work/$WN" && cd "/work/$WN"
      make build
      cp build/release/hashlock /out/build/release/hashlock
    ' >"$log" 2>&1 || ec=$?
    if [ "$ec" -eq 0 ] && [ -f "$3/build/release/hashlock" ]; then
      return 0
    fi
  done
  # Fail loud: the binary MUST exist on success, else a later grade would run on nothing.
  { echo "    agent_build FAILED after 3 attempts (last exit $ec); tail of $log:"; tail -10 "$log"; } >&2
  return 1
}

# The verifier grade command: cwd is the suite ws; the agent binary is mounted :ro and the
# suite finds it via TOP=/artifact -> /artifact/build/release/<name>; BENCH_PASSWORD only now.
verify_grade () {  # verify_grade <artifact-dir> <bench_password>
  run_tc -v "$VERIFIER_WS":/vws -w /vws \
         -v "$1":/artifact:ro \
         -v "$CARGO_VOL":/cargo -e CARGO_HOME=/cargo \
         -e TOP=/artifact -e MODE=release \
         -e BENCH_PASSWORD="$2" \
         "$IMAGE" cargo test --release
}

echo
echo "================================ CLAIMS (by exit code) ================================"

# --- BUILD: agent container builds the correct contract -> binary lands in the artifact. ---
# agent_build writes <out>/build/release/hashlock, so ART_OK IS the read-only verify mount.
check 0 "BUILD: agent container builds correct contract" \
  agent_build "$AGENT_WS_SRC" "agent" "$ART_OK"
check 0 "BUILD: built binary is a RISC-V CKB-VM ELF in the artifact" \
  sh -c "test -f '$ART_OK/build/release/hashlock' && file '$ART_OK/build/release/hashlock' | grep -q 'RISC-V'"

# --- ISOLATION-SUITE: the hidden suite is ABSENT from the agent stage. ---
# (a) mount-level: tests/ directory is not in the agent source workspace at all.
check 1 "ISOLATION-SUITE: tests/ dir absent from agent workspace mount" \
  run_tc -v "$AGENT_WS_SRC":/ws "$IMAGE" test -e /ws/tests
# (b) content-level: the hidden grader's signature is nowhere in the agent's reach
#     (the mounted source ws AND the agent's cargo crate cache); grep exit 1 == no match.
check 1 "ISOLATION-SUITE: hidden grader signature absent from agent ws + cargo cache" \
  run_tc -v "$AGENT_WS_SRC":/ws -v "$CARGO_VOL":/cargo "$IMAGE" \
    grep -rql "assert_rejected_with\|correct_password_unlocks\|wrong_password_same_length" /ws /cargo

# --- ISOLATION-SECRET: BENCH_PASSWORD is absent from the agent env AND the agent ws. ---
check 0 "ISOLATION-SECRET: BENCH_PASSWORD unset in agent container env" \
  run_tc -v "$AGENT_WS_SRC":/ws "$IMAGE" sh -c '[ -z "${BENCH_PASSWORD:-}" ]'
check 1 "ISOLATION-SECRET: no BENCH_PASSWORD literal anywhere in agent workspace" \
  run_tc -v "$AGENT_WS_SRC":/ws "$IMAGE" grep -rqi "BENCH_PASSWORD" /ws

# --- A FRESH per-run secret. Generated NOW from /dev/urandom; a TEST value, never a real
#     credential. Neither the correct contract nor the cheat ever saw it. ---
FRESH_PW="spike-test-pw-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
echo
echo "fresh per-run verifier-private secret (TEST value, generated now): $FRESH_PW"
echo

# --- GRADE-PASS: hermetic verifier grades the CORRECT contract as PASS with the fresh
#     secret it never saw (it reads the lock args at runtime, so any password works). ---
check 0 "GRADE-PASS: verifier grades CORRECT contract as PASS (fresh secret)" \
  verify_grade "$ART_OK" "$FRESH_PW"

# --- READ-ONLY: the verifier mounts the agent artifact read-only; a write must be refused,
#     proving the verifier cannot tamper with what it grades. `sh`'s redirection onto a
#     read-only mount fails before the command runs, which `sh -c` reports as exit 2. ---
check 2 "READ-ONLY: write into the :ro agent-artifact mount is refused" \
  run_tc -v "$ART_OK":/artifact:ro "$IMAGE" \
    sh -c 'echo tamper > /artifact/build/release/hashlock'

# --- GRADE-FAIL: the agent stage REBUILDS the CHEAT from cheat sources (harness rule:
#     grade the binary built from submitted sources, never a stale one), then the SAME
#     hermetic verifier grades it with the SAME fresh secret the cheat never saw. It must
#     FAIL (nonzero) -- specifically `cargo test` exits 101 on a failing suite. ---
check 0 "BUILD: agent container rebuilds CHEAT contract from its sources" \
  agent_build "$CHEAT_WS_SRC" "cheat" "$ART_CHEAT"
# Soundness guard: GRADE-FAIL is only meaningful if the cheat binary actually built. A
# missing binary would make the suite fail for the WRONG reason (no cell to deploy), which
# would falsely "pass" this negative check. Assert a real RISC-V binary exists first.
check 0 "PRECHECK: cheat binary actually built (so GRADE-FAIL is meaningful, not vacuous)" \
  sh -c "test -f '$ART_CHEAT/build/release/hashlock' && file '$ART_CHEAT/build/release/hashlock' | grep -q 'RISC-V'"
check 101 "GRADE-FAIL: verifier grades CHEAT contract as FAIL (same fresh secret)" \
  verify_grade "$ART_CHEAT" "$FRESH_PW"

echo
echo "======================================================================================"
echo "SUMMARY: $passed/$checks checks passed"
if [ "$fail" -eq 0 ]; then
  echo "RESULT: ALL CHECKS PASSED"
  exit 0
else
  echo "RESULT: FAILURES PRESENT"
  exit 1
fi
