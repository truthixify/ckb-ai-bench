#!/usr/bin/env bash
# Spike (NOT production): self-verifying end-to-end run of the ADR-0011 reporting
# pipeline on SYNTHETIC (clearly fake) data. Regenerates the synthetic dataset,
# runs the headline C-B / CI / chain-separation unit tests, renders the HTML
# ladder chart, and asserts the artifact structurally contains the headline
# C-B slope, CI bands, both chains, and all six models.
#
# Proof is by exit code (the `check` helper), not by eyeballing output. Pipes are
# avoided where they would mask a failing exit status.
#
# Usage: bash run-spike.sh    (exit 0 = the whole reporting pipeline works)
set -euo pipefail
cd "$(dirname "$0")"

pass=0
fail=0
# check <want_exit> <label> <cmd...>  : runs cmd, compares exit code to want.
check() {
  local want=$1; shift; local label=$1; shift
  local got=0
  "$@" >/tmp/ladder-spike-out 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then
    echo "  OK  $label (exit $got)"; pass=$((pass+1))
  else
    echo "  BAD $label (exit $got, wanted $want)"; fail=$((fail+1))
    sed 's/^/      /' /tmp/ladder-spike-out
  fi
}

HTML="ladder-chart.html"
DATA="synthetic-results.json"
MODELS=(Sonnet Opus Fable Grok-Build Grok-Compose GPT-5.5)

echo "[1] regenerate synthetic dataset"
rm -f "$DATA" "$HTML"
check 0 "generator runs"        node gen-synthetic-data.js
check 0 "dataset file exists"   test -f "$DATA"
check 0 "dataset marked _SYNTHETIC" node -e "process.exit(require('./$DATA')._SYNTHETIC===true?0:1)"
check 0 "48 cells (6x2x4)"      node -e "process.exit(require('./$DATA').cells.length===48?0:1)"

echo "[2] unit tests: C-B delta math, flat~0, negative<0, CI propagation, chain separation"
check 0 "node --test passes"    node --test

echo "[3] render the HTML ladder chart"
check 0 "renderer runs"         node render-ladder.js
check 0 "html artifact exists"  test -f "$HTML"

echo "[4] structural assertions on the rendered artifact"
# clearly-labeled synthetic marker present (cannot be mistaken for real)
check 0 "synthetic banner present" grep -q "SYNTHETIC DATA" "$HTML"
# both chains present as a real toggle (button + chart group each)
check 0 "devnet chain present"  grep -q 'data-chain="devnet"' "$HTML"
check 0 "testnet chain present" grep -q 'data-chain="testnet"' "$HTML"
check 0 "chain toggle buttons"  grep -q 'class="chain-btn' "$HTML"
# all six models named in the markup
for m in "${MODELS[@]}"; do
  check 0 "model line: $m"       grep -q "data-model=\"$m\"" "$HTML"
done
# the headline C-B slope is computed AND rendered: the bold B->C segment + the
# C-B delta badges (a positive, a flat ~0, and a negative one  -  the honesty proof)
check 0 "B->C headline segment rendered" grep -q 'class="bc-segment"' "$HTML"
check 0 "CI bands rendered"      grep -q 'class="ci-band"' "$HTML"
check 0 "CI whiskers rendered"   grep -q 'class="ci-whisker"' "$HTML"
# honest rendering: a strong positive, a ~flat, and a negative C-B all appear
check 0 "positive C-B (Opus +0.26) rendered" grep -q '+0.26' "$HTML"
check 0 "flat C-B (Grok-Build +0.01) rendered" grep -q '+0.01' "$HTML"
check 0 "negative C-B (GPT-5.5 -0.04) rendered" grep -q '\-0.04' "$HTML"
check 0 "a negative direction is shown honestly" grep -q 'dir-negative' "$HTML"

echo "[5] deep structural check: per-chain each group has 6 lines, 6 CI bands, 4 arms, 6 B->C segments"
check 0 "per-chain structure (6 models x 4 arms, both chains)" node -e '
  const fs=require("fs"); const html=fs.readFileSync("ladder-chart.html","utf8");
  const grp=(ch)=>{const m=html.match(new RegExp("<g class=\"chart\" data-chain=\""+ch+"\"[\\s\\S]*?</g>"));return m?m[0]:"";};
  let ok=true;
  for(const ch of ["devnet","testnet"]){
    const g=grp(ch);
    const lines=(g.match(/class="model-line"/g)||[]).length;
    const bands=(g.match(/class="ci-band"/g)||[]).length;
    const bc=(g.match(/class="bc-segment"/g)||[]).length;
    const ticks=(g.match(/class="tick"/g)||[]).length;
    if(lines!==6||bands!==6||bc!==6||ticks!==4){ console.error(ch,"bad:",{lines,bands,bc,ticks}); ok=false; }
    for(const m of ["Sonnet","Opus","Fable","Grok-Build","Grok-Compose","GPT-5.5"])
      if(!g.includes("class=\"model-line\" data-model=\""+m+"\"")){ console.error("missing",m,"in",ch); ok=false; }
  }
  process.exit(ok?0:1);
'
# guard: the two chains must NOT be merged onto one axis. Each chain group is a
# separate <g>; assert exactly two groups and that one model differs across them.
check 0 "chains kept separate (2 groups, distinct means)" node -e '
  const fs=require("fs"); const html=fs.readFileSync("ladder-chart.html","utf8");
  const groups=(html.match(/<g class="chart"/g)||[]).length;
  if(groups!==2){ console.error("expected 2 chart groups, got",groups); process.exit(1); }
  const ds=require("./synthetic-results.json");
  const dev=ds.cells.find(c=>c.chain==="devnet"&&c.model==="Opus"&&c.arm==="C").mean;
  const tn=ds.cells.find(c=>c.chain==="testnet"&&c.model==="Opus"&&c.arm==="C").mean;
  if(dev===tn){ console.error("devnet/testnet means identical => possibly merged"); process.exit(1); }
  process.exit(0);
'

echo
echo "RESULT: $pass passed, $fail failed"
rm -f /tmp/ladder-spike-out
if [ "$fail" -eq 0 ]; then
  echo "ALL CHECKS PASSED  -  ADR-0011 reporting pipeline proven end to end on synthetic data."
  echo "  artifact: $(pwd)/$HTML"
  echo "  data:     $(pwd)/$DATA"
  exit 0
else
  echo "SPIKE FAILED  -  $fail check(s) did not pass (see above). Reporting pipeline NOT proven."
  exit 1
fi
