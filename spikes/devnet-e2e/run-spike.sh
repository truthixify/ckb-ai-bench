#!/usr/bin/env bash
# Spike (NOT production): self-verifying end-to-end run of the on-chain Proof +
# verifier with the trust-zone split and the high-entropy nonce. Asserts the
# verifier PASSES the valid Proof and REJECTS every negative (by exit code, not by
# grepping output). Requires the devnet sidecar up (docker compose up -d).
#
# Usage: ./run-spike.sh    (exit 0 = all cases behaved correctly)
set -u
cd "$(dirname "$0")"

pass=0
fail=0
check() { # check <want_exit> <label> <cmd...>
  local want=$1; shift; local label=$1; shift
  "$@" >/dev/null 2>&1
  local got=$?
  if [ "$got" -eq "$want" ]; then echo "  OK  $label (exit $got)"; pass=$((pass+1))
  else echo "  BAD $label (exit $got, wanted $want)"; fail=$((fail+1)); fi
}

rm -rf run1 secret1 run2 secret2 borrow secret_stale secret_wrong

echo "[run 1] harness prepare -> agent send -> verify (expect PASS)"
node harness-prepare.js ./run1 ./secret1
node send-proof.js ./run1
echo "  waiting for commitment..."; sleep 5
check 0 "valid proof passes" node verify-proof.js ./run1 ./secret1

echo "[negatives]"
# stale: baseline forced above the tx block
node -e "const s=require('./secret1/secret.json'); s.harness_tip=9999999; require('fs').mkdirSync('secret_stale',{recursive:true}); require('fs').writeFileSync('secret_stale/secret.json',JSON.stringify(s))"
check 1 "stale tx rejected" node verify-proof.js ./run1 ./secret_stale

# wrong nonce: verifier-private amount differs from the tx
node -e "const s=require('./secret1/secret.json'); s.nonce_amount_shannons='99999999999'; require('fs').mkdirSync('secret_wrong',{recursive:true}); require('fs').writeFileSync('secret_wrong/secret.json',JSON.stringify(s))"
check 1 "wrong-nonce rejected" node verify-proof.js ./run1 ./secret_wrong

# nonexistent tx
mkdir -p borrow
echo "0xdeadbeef00000000000000000000000000000000000000000000000000000000" > borrow/tx_id.txt
check 1 "nonexistent tx rejected" node verify-proof.js ./borrow ./secret1

# borrow/replay: run 2 (new tip + new random nonce), fed run 1's committed tx_id
node harness-prepare.js ./run2 ./secret2
cp run1/tx_id.txt run2/tx_id.txt   # the cheat: borrow instead of send
check 1 "borrowed tx rejected" node verify-proof.js ./run2 ./secret2

echo
echo "RESULT: $pass passed, $fail failed"
rm -rf run1 secret1 run2 secret2 borrow secret_stale secret_wrong
[ "$fail" -eq 0 ]
