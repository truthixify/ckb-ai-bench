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
  $COMPOSE --profile agent down -v >/dev/null 2>&1 || true
  docker rmi "$AGENT_IMAGE" "$VERIFIER_IMAGE" "$PROXY_IMAGE" >/dev/null 2>&1 || true
  rm -f "$ROOT/containers/proxy/allowlist.validate.built" 2>/dev/null || true
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

echo "== (a) build agent + verifier images =="
docker build -f agent.Dockerfile -t "$AGENT_IMAGE" "$ROOT" >/tmp/ckbbench-validate-agent.log 2>&1
docker build -f verifier.Dockerfile -t "$VERIFIER_IMAGE" . >/tmp/ckbbench-validate-verifier.log 2>&1
assert_tool_versions "$AGENT_IMAGE" "agent image"
assert_tool_versions "$VERIFIER_IMAGE" "verifier image"

echo "== (b) devnet sidecar RPC =="
# Block-mode allowlist for validate (devnet node + proxy only).
"$PY" "$ROOT/containers/build_allowlist.py" \
  --arm A --chain-rpc http://ckbbench-devnet-node:8114 \
  -o "$ROOT/containers/proxy/allowlist.validate.built"
export CKBBENCH_ALLOWLIST_FILE="$ROOT/containers/proxy/allowlist.validate.built"

$COMPOSE down -v >/dev/null 2>&1 || true
$COMPOSE up -d ckbbench-devnet-node ckbbench-devnet-miner ckbbench-proxy >/dev/null

for i in $(seq 1 60); do
  if docker run --rm --network ckbbench-net-internal curlimages/curl:8.12.1 \
      -fsS -m 5 -X POST http://ckbbench-devnet-node:8114 \
      -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"get_tip_block_number","params":[]}' \
      | grep -q result; then
    break
  fi
  sleep 2
done

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