#!/usr/bin/env bash
# Spike (written as the real preflight): self-verifying proof of ADR-0010 run LIVE
# against the pinned MCP server. The harness, at preflight, calls the MCP
# `initialize` handshake, reads serverInfo.version, and HARD-FAILS if it does not
# equal the pinned version. We assert by EXIT CODE (not by grepping output):
#
#   PASS case:        pin = real live version (1.6.12)  -> checker exits 0
#   FAIL case:        pin = wrong version (9.9.9)        -> checker exits 2 (refusal)
#   tool-surface:     search_tools/search_resources present, report tool count
#   unreachable:      bad host                           -> checker exits 3
#
# Exit codes are captured DIRECTLY via $? (no `| tail`, no pipe) so a pipeline
# cannot swallow the real status. Output is sent to /dev/null when we only want
# the code; the tool-surface case prints its line so the count is visible.
#
# Usage: ./run-spike.sh   (exit 0 = every case behaved correctly)
set -euo pipefail
cd "$(dirname "$0")"

URL="${MCP_URL:-https://mcp.ckbdev.com/ckbai}"
REAL_VERSION="${MCP_PINNED_VERSION:-1.6.12}"   # the version we pin / deploy
WRONG_VERSION="9.9.9"                           # deliberately not the live version
CHECKER="node mcp-preflight.mjs"

pass=0
fail=0
check() { # check <want_exit> <label> <cmd...>
  local want=$1; shift; local label=$1; shift
  set +e
  "$@" >/dev/null 2>&1
  local got=$?
  set -e
  if [ "$got" -eq "$want" ]; then
    echo "  OK  $label (exit $got)"; pass=$((pass+1))
  else
    echo "  BAD $label (exit $got, wanted $want)"; fail=$((fail+1))
  fi
}

echo "[preflight spike] endpoint: $URL"
echo

echo "[1] PASS case: pin = real live version ($REAL_VERSION) -> expect exit 0"
check 0 "matching version passes preflight" $CHECKER "$URL" "$REAL_VERSION"

echo "[2] FAIL case: pin = wrong version ($WRONG_VERSION) -> expect exit 2 (refusal)"
check 2 "mismatched version refuses (ADR-0010)" $CHECKER "$URL" "$WRONG_VERSION"

echo "[3] tool-surface case: deferred-loading discovery tools present"
# Run once, capture exit DIRECTLY, then print the tool/deferred lines for evidence.
set +e
SURFACE_OUT="$($CHECKER "$URL" "$REAL_VERSION" 2>&1)"
SURFACE_RC=$?
set -e
echo "$SURFACE_OUT" | grep -E "^(tools|deferred-loading)" | sed 's/^/      /'
if [ "$SURFACE_RC" -eq 0 ] \
   && echo "$SURFACE_OUT" | grep -q "search_tools: true" \
   && echo "$SURFACE_OUT" | grep -q "search_resources: true"; then
  echo "  OK  search_tools + search_resources present (deferred loading) (exit $SURFACE_RC)"
  pass=$((pass+1))
else
  echo "  BAD deferred-loading discovery tools NOT confirmed (exit $SURFACE_RC)"
  fail=$((fail+1))
fi

echo "[4] unreachable case: bad host -> expect exit 3 (transport failure)"
check 3 "unreachable endpoint refuses" $CHECKER "https://mcp.ckbdev.com.invalid-nope/ckbai" "$REAL_VERSION"

echo "[5] config case: pin/url via ENV (same code path as real preflight) -> expect exit 0"
check 0 "env-configured preflight passes" env MCP_URL="$URL" MCP_PINNED_VERSION="$REAL_VERSION" node mcp-preflight.mjs

total=$((pass+fail))
echo
echo "RESULT: $pass/$total passed, $fail failed"
[ "$fail" -eq 0 ]
