"""Spike (NOT production): run the composed-suite model loop K times and assert the
MECHANISM works on a majority, surfacing model variance as data (not hiding it).

A benchmark expects per-cell variance (ADR-0011 mandates >=3 runs with CIs). A spike
must prove the composed-prompt delivery + independent grading MECHANISM is sound, while
being honest that a single stochastic run can miss. So we run K times, report the pass
rate, and require a strict majority. A systematically broken mechanism fails every run;
a transient does not sink the spike.

Exit 0 iff passes >= ceil(K/2)+ (we require >= K-1 of K, i.e. at most one transient).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
K = int(os.getenv("BENCH_RUNS", "3"))


def main() -> int:
    passes = 0
    results = []
    for i in range(1, K + 1):
        proc = subprocess.run(
            [sys.executable, str(HERE / "spike_composed_suite.py")],
            capture_output=True, text=True,
        )
        ok = "RESULT: PASS" in proc.stdout
        score = ""
        for line in proc.stdout.splitlines():
            if line.startswith("score:"):
                score = line.strip()
        results.append((i, ok, score))
        if ok:
            passes += 1
        print(f"  run {i}/{K}: {'PASS' if ok else 'FAIL'}  {score}")

    # Require at most one transient miss out of K (>= K-1). With K=3 that is >=2/3.
    threshold = max(1, K - 1)
    rate = f"{passes}/{K}"
    print(f"model-loop pass rate: {rate} (threshold >= {threshold}/{K})")
    if passes >= threshold:
        print(f"PASS: composed-suite mechanism sound across runs ({rate})")
        return 0
    print(f"FAIL: mechanism not reliably sound ({rate} < {threshold}/{K})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
