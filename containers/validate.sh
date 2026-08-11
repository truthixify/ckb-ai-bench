#!/usr/bin/env bash
# Integration proof for Phase 3 container topology (NOT part of pytest).
#
# (a) Builds agent + verifier images; asserts /tool-versions.txt shows pinned rust+clang+riscv.
# (b) Brings up devnet sidecar; asserts RPC get_tip_block_number works.
# (c) Asserts net-internal has no NAT (agent cannot curl a raw public IP directly - spike 4b).
#
# Tear-down targets ONLY ckbbench-* resources this script started.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
PY="${CKBBENCH_PYTHON:-$ROOT/agent/.venv/bin/python}"

AGENT_IMAGE="ckbbench-agent:validate"
VERIFIER_IMAGE="ckbbench-verifier:validate"
PROXY_IMAGE="ckbbench-proxy:validate"
COMPOSE="docker compose -f compose.yml"

fail=0
checks=0
passed=0

# The absence decision below is only durable if no other project operation can create state after
# it. Image builds take minutes, so take the shared lock BEFORE the inventory and hold it through
# teardown. This gate always owns its own lock -- it is never handed one -- so nothing outside this
# process can shorten the window it is protected for.
# shellcheck source=../scripts/lib/lock.sh
source "$ROOT/scripts/lib/lock.sh"
with_lock "validate"
echo "lock: acquired"

# The DevNet state volume is operator state unless THIS gate created it. Inventory it before doing
# anything: a pre-existing volume is borrowed and must never be reset merely to run validation.
DATA_VOLUME="ckbbench-devnet-data"
# Fail CLOSED: only an object-specific "no such volume" proves absence. A daemon, context or
# permission failure must not be read as permission to create and later delete state.
# The assignment must sit inside `if`: under `set -e` a failing command substitution in a bare
# assignment exits the script before $? can be read.
if volume_probe="$(docker volume inspect "$DATA_VOLUME" 2>&1 >/dev/null)"; then
  volume_rc=0
else
  volume_rc=$?
fi
if [ "$volume_rc" -eq 0 ]; then
  echo "BLOCKER: $DATA_VOLUME already exists; validation would disturb operator chain state."
  echo "  Stop the stack and run './bench reset' first, or remove it deliberately, then re-run."
  exit 1
# The name must appear as a WHOLE Docker-name token. Docker names use letters, digits, underscore,
# period and hyphen, so a bare substring match would let an error about `ckbbench-devnet-data-backup`
# prove that `ckbbench-devnet-data` is absent -- and the gate would then treat live operator state as
# something it created.
elif ! printf '%s' "$volume_probe" | grep -qi "no such volume" \
     || ! printf '%s' "$volume_probe" | grep -qE "(^|[^A-Za-z0-9_.-])$DATA_VOLUME([^A-Za-z0-9_.-]|\$)"; then
  echo "BLOCKER: cannot determine whether $DATA_VOLUME exists: $volume_probe"
  exit 1
fi
VOLUME_PREEXISTED=0

# Stopped benchmark services count too: the gate requires an absent stack, not just an idle one.
# The inventory must SUCCEED: `docker ps | grep || true` turns a daemon failure into an empty list,
# which is indistinguishable from "nothing exists" and would authorize teardown regardless.
benchmark_containers () {
  local out
  if ! out="$(docker ps -a --format '{{.Names}}')"; then
    echo "__DOCKER_PS_FAILED__"
    return 0
  fi
  printf '%s\n' "$out" | grep -E '^(ckbbench-|minisweagent-)' || true
}

existing_containers="$(benchmark_containers)"
if [ "$existing_containers" = "__DOCKER_PS_FAILED__" ]; then
  echo "BLOCKER: cannot inventory containers; refusing to run against an unproven stack."
  exit 1
fi
if [ -n "$existing_containers" ]; then
  echo "BLOCKER: benchmark containers exist (running or stopped):"
  echo "$existing_containers" | sed 's/^/  /'
  exit 1
fi

check () {
  local want="$1" label="$2"
  shift 2
  checks=$((checks + 1))
  local got=0
  "$@" >/dev/null 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then
    echo "PASS  $label (exit $got)"
    passed=$((passed + 1))
  else
    echo "FAIL  $label (got exit $got, wanted $want)"
    fail=1
  fi
}

teardown () {
  # `down` without -v: volume removal goes through the labelled, inspected lifecycle path below,
  # and only for a volume this gate created.
  $COMPOSE --profile agent down >/dev/null 2>&1 || true
  if [ "$VOLUME_PREEXISTED" -eq 0 ]; then
    # Re-check ownership immediately before removing, not just at creation time. A removal failure
    # must be visible in the exit status: "cleaned up" is a claim this gate has to earn.
    if ! "$PY" -m ckbbench.run.devnet --remove-data-volume >/dev/null 2>&1; then
      echo "FAIL  could not remove the disposable $DATA_VOLUME volume"
      fail=1
    fi
  fi
  leftovers="$(benchmark_containers)"
  if [ "$leftovers" = "__DOCKER_PS_FAILED__" ]; then
    echo "FAIL  could not inventory containers during teardown"
    fail=1
    leftovers=""
  fi
  if [ -n "$leftovers" ]; then
    echo "FAIL  benchmark containers remain after teardown:"
    echo "$leftovers" | sed 's/^/  /'
    fail=1
  fi
  if [ "$fail" -ne 0 ]; then
    echo "RESULT: CONTAINER CHECK FAILURES PRESENT (teardown)"
    release_lock
    exit 1
  fi
  docker rmi "$AGENT_IMAGE" "$VERIFIER_IMAGE" "$PROXY_IMAGE" >/dev/null 2>&1 || true
  rm -f "$ROOT/containers/proxy/allowlist.validate.built" 2>/dev/null || true
  release_lock
}
trap teardown EXIT

assert_tool_versions () {
  local image="$1" label="$2"
  local txt
  txt="$(docker run --rm "$image" cat /tool-versions.txt)"
  echo "$txt" | grep -q "rustc 1.95" || { echo "FAIL  $label missing rustc 1.95"; fail=1; return; }
  echo "$txt" | grep -qi "clang" || { echo "FAIL  $label missing clang"; fail=1; return; }
  echo "$txt" | grep -q "riscv64imac-unknown-none-elf" || { echo "FAIL  $label missing riscv target"; fail=1; return; }
  echo "PASS  $label tool-versions.txt pins rust+clang+riscv"
  passed=$((passed + 1))
  checks=$((checks + 1))
}

echo "== (a) build agent + verifier images (repo-root context for cargo bake) =="
docker build -f agent.Dockerfile -t "$AGENT_IMAGE" "$ROOT" >/tmp/ckbbench-validate-agent.log 2>&1
# Verifier bake needs suites/; context must be repo root (not containers/ only).
docker build -f verifier.Dockerfile -t "$VERIFIER_IMAGE" "$ROOT" >/tmp/ckbbench-validate-verifier.log 2>&1
assert_tool_versions "$AGENT_IMAGE" "agent image"
assert_tool_versions "$VERIFIER_IMAGE" "verifier image"
# Structural bake gates (image-local cargo + /work seed); full offline smoke is bake-time.
check 0 "agent image has /work sticky seed" \
  docker run --rm --user 1000:1000 "$AGENT_IMAGE" sh -c 'test -d /work && test -w /work'
check 0 "verifier image has image-local CARGO_HOME" \
  docker run --rm "$VERIFIER_IMAGE" sh -c 'test -d /opt/ckbbench-cargo && grep -q CARGO_HOME= /tool-versions.txt'
# Agent image must never contain hidden suite sources.
check 0 "agent image has no hidden suite tree" \
  sh -c 'docker run --rm "$0" sh -c "test ! -e /tmp/verifier-bake && test ! -d /suite/src"' "$AGENT_IMAGE"
# The pinned transaction SDK must import from an arbitrary fresh workspace with NO network: a
# graded run cannot download packages, and Node's ESM resolver only walks parent directories.
check 0 "agent image imports pinned CKB SDK offline from a fresh workspace" \
  sh -c 'docker run --rm --network none --user 1000:1000 -w /work "$0" \
    sh -c "mkdir -p /work/fresh-\$\$ && cd /work/fresh-\$\$ \
      && node --input-type=module -e \"import { SignerCkbPrivateKey } from \\\"@ckb-ccc/core\\\"; if (typeof SignerCkbPrivateKey !== \\\"function\\\") process.exit(1)\""' \
  "$AGENT_IMAGE"
check 0 "agent image records the pinned CKB SDK version" \
  sh -c 'docker run --rm "$0" grep -q "@ckb-ccc/core: 1.12.5" /tool-versions.txt' "$AGENT_IMAGE"

echo "== (b) devnet sidecar RPC =="
# Block-mode allowlist for validate (devnet node + proxy only).
"$PY" "$ROOT/containers/build_allowlist.py" \
  --arm A --chain-rpc http://ckbbench-devnet-node:8114 \
  -o "$ROOT/containers/proxy/allowlist.validate.built"
export CKBBENCH_ALLOWLIST_FILE="$ROOT/containers/proxy/allowlist.validate.built"

$COMPOSE down >/dev/null 2>&1 || true
$COMPOSE up -d ckbbench-proxy >/dev/null

# Bring DevNet up through the production lifecycle controller, not a bare `compose up`: it creates
# the labelled state volume, hands it to the node user, and proves chain identity, miner progress
# and indexer readiness. Validating the real path is the point of this gate.
checks=$((checks + 1))
if "$PY" -c 'from ckbbench.run.devnet import prepare_devnet; s = prepare_devnet(); \
print(f"prepared {s.chain} tip={s.prepared_tip_number} genesis={s.genesis_hash[:18]}...")'; then
  echo "PASS  devnet prepared through the production lifecycle controller"
  passed=$((passed + 1))
else
  echo "FAIL  devnet lifecycle preparation"
  fail=1
fi

check 0 "devnet get_tip_block_number via RPC" \
  sh -c 'docker run --rm --network ckbbench-net-internal curlimages/curl:8.12.1 \
    -fsS -m 10 -X POST http://ckbbench-devnet-node:8114 \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"get_tip_block_number\",\"params\":[]}" \
    | grep -q result'

echo "== (c) internal network has no NAT (spike 4b) =="
$COMPOSE --profile agent up -d ckbbench-agent >/dev/null

check 0 "agent direct curl to raw public IP fails at L3 (6/7/28)" \
  sh -c 'docker exec ckbbench-agent curl -fsS -m 8 http://1.1.1.1/ >/dev/null 2>&1; ec=$?; case "$ec" in 6|7|28) exit 0;; *) echo "got curl exit $ec, wanted 6/7/28"; exit 1;; esac'

echo
echo "SUMMARY: $passed/$checks checks passed"
if [ "$fail" -eq 0 ]; then
  echo "RESULT: ALL CONTAINER CHECKS PASSED"
  exit 0
fi
echo "RESULT: CONTAINER CHECK FAILURES PRESENT"
exit 1