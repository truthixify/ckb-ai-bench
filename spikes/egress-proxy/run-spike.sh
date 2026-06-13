#!/usr/bin/env bash
# Spike (NOT production): prove ADR-0006 egress control at the NETWORK layer.
#
# Claims proven, each by exit code (no output-grep that could mask a real result):
#   1. ALLOW+LOG : agent reaches the allowlisted chain RPC THROUGH the proxy, and the
#                  request is recorded in the proxy log.
#   2. BLOCK+LOG : agent's attempt to reach a NON-allowlisted host (web "research")
#                  through the proxy is REFUSED by the proxy, and the refusal is logged.
#   3. NO-BYPASS : agent attempting to reach an external host DIRECTLY (ignoring the
#                  proxy, i.e. a cheating model) FAILS at the network layer (no route).
#   4. NO-BYPASS-ALLOWED : even the allowlisted host is unreachable DIRECTLY; the only
#                  path to it is through the proxy. Proves the allow in (1) is the proxy's
#                  doing, not a leak in the network boundary.
set -euo pipefail
cd "$(dirname "$0")"

CHAIN_RPC_HOST="192.168.0.73"      # allowlisted (the TestNet archive node)
CHAIN_RPC_URL="http://192.168.0.73:18114"
BLOCKED_HOST="example.com"         # NOT allowlisted: stands in for web research
PROXY="http://proxy:8888"
# The proxy logs to stdout (container-native); we read it via `docker logs`.
proxylog () { docker logs ckb-egress-proxy 2>&1; }

fail=0
check () {  # check <want_exit> <label> <cmd...>
  local want="$1" label="$2"; shift 2
  local got=0
  "$@" >/dev/null 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then
    echo "PASS  $label (exit $got)"
  else
    echo "FAIL  $label (got exit $got, wanted $want)"
    fail=1
  fi
}

# curl inside the agent container. We DO NOT pass any *_proxy env so the agent's own
# choice of proxy is explicit per-test (a cheating model would skip the proxy).
ain () { docker exec ckb-egress-agent "$@"; }

echo "== bringing up proxy + agent (internal-only agent network) =="
docker compose down -v >/dev/null 2>&1 || true
docker compose up -d --build >/dev/null
# Tools are baked into both images; just wait for tinyproxy to bind its listen socket.
for i in $(seq 1 20); do
  if docker exec ckb-egress-proxy sh -c 'netstat -ltn 2>/dev/null | grep -q 8888' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo
echo "== claims =="

# (1) ALLOW + LOG: allowlisted chain RPC via the proxy -> succeeds (curl exit 0).
check 0 "allowlisted chain RPC reachable VIA proxy" \
  ain curl -fsS -m 15 -x "$PROXY" -X POST "$CHAIN_RPC_URL" \
      -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"get_tip_block_number","params":[]}'

# (2) BLOCK + LOG: non-allowlisted host via the proxy -> proxy refuses. tinyproxy
# returns an HTTP 403/forbidden page, so curl -f yields exit 22 (HTTP error >= 400).
check 22 "non-allowlisted host BLOCKED by proxy" \
  ain curl -fsS -m 15 -x "$PROXY" "http://$BLOCKED_HOST/"

# (3) NO-BYPASS: agent tries to reach the blocked host DIRECTLY (no proxy). On an
# internal-only network there is no route off-host, so curl fails to connect.
# curl connect/resolve failures are exit 6 (resolve) or 7 (connect) or 28 (timeout);
# we accept "nonzero and not a clean 0" by asserting it is NOT 0. Encode as: invert.
check 0 "direct (no-proxy) to blocked host FAILS at network layer" \
  sh -c 'docker exec ckb-egress-agent curl -fsS -m 8 http://example.com/ >/dev/null 2>&1; [ $? -ne 0 ]'

# (4) NO-BYPASS-ALLOWED: even the allowlisted host is NOT reachable directly; only the
# proxy can reach it. Proves the boundary, not a per-host leak.
check 0 "direct (no-proxy) to allowlisted host ALSO FAILS (only proxy bridges out)" \
  sh -c "docker exec ckb-egress-agent curl -fsS -m 8 $CHAIN_RPC_URL -X POST -H 'Content-Type: application/json' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"get_tip_block_number\",\"params\":[]}' >/dev/null 2>&1; [ \$? -ne 0 ]"

echo
echo "== machine-observed egress log (ADR-0006: not self-reported) =="
# (5) LOG completeness: the proxy log must contain BOTH the allowed connect to the chain
# RPC host AND the denied attempt to the blocked host.
check 0 "proxy logged the ALLOWED chain-RPC request" \
  sh -c "proxylog () { docker logs ckb-egress-proxy 2>&1; }; proxylog | grep -q '$CHAIN_RPC_HOST'"
check 0 "proxy logged the DENIED web-research attempt" \
  sh -c "proxylog () { docker logs ckb-egress-proxy 2>&1; }; proxylog | grep -qiE 'denied|filter|$BLOCKED_HOST'"

echo
echo "---- proxy log (tail) ----"
proxylog | tail -n 20 2>/dev/null || echo "(no log)"
echo "--------------------------"

echo
echo "== tearing down (only containers this spike started) =="
docker compose down -v >/dev/null 2>&1 || true

echo
if [ "$fail" -eq 0 ]; then
  echo "RESULT: ALL CHECKS PASSED"
  exit 0
else
  echo "RESULT: FAILURES PRESENT"
  exit 1
fi
