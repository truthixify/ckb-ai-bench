"""Spike (NOT production): deterministic logic test for the composed-suite spike.

No model. Proves the parts that must hold regardless of any agent's behavior:
  1. The composer assembles preamble + ordered fragments + postamble and a thin pointer.
  2. The verifier grades each Proof independently by DIRECT RPC.
  3. A corrupted Proof FAILS, and that failure is ISOLATED: the other two tasks still
     PASS (no cascade), which is the strict-independence guarantee (ADR-0008).
Exit 0 only if every assertion holds; nonzero (via AssertionError) otherwise.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import compose as composer
from verify import verify_all, _rpc

REGISTRY = Path(__file__).parent / "registry"


def main() -> int:
    composed, metas = composer.compose(REGISTRY)
    assert len(metas) == 3, "expected 3 tasks from the registry"
    # Composed prompt is preamble + each fragment in manifest order + postamble.
    assert "numbered list of INDEPENDENT" in composed
    assert composed.index("Task 1") < composed.index("Task 2") < composed.index("Task 3"), \
        "fragments must appear in manifest order"

    # Pointer is thin: it references the file, it is NOT the wall of text.
    mount = Path(tempfile.mkdtemp(prefix="ct-logic-"))
    inst, digest = composer.write_instructions(composed, mount)
    pointer = composer.pointer_prompt(inst)
    assert inst.name in pointer and "Task 1" not in pointer, "pointer must not inline the tasks"
    assert len(digest) == 64, "composed prompt must be sha256-hashed (freeze)"

    # Known-good proofs (fetched by direct RPC) all PASS.
    tip = _rpc("get_tip_block_number", [])
    epoch = _rpc("get_current_epoch", [])["number"]
    bh = _rpc("get_block_hash", ["0x1"])
    (mount / "proof_tip.txt").write_text(tip)
    (mount / "proof_epoch.txt").write_text(epoch)
    (mount / "proof_blockhash.txt").write_text(bh)
    good = {v["id"]: v for v in verify_all(metas, mount, "unused")}
    assert all(v["pass"] for v in good.values()), f"known-good proofs must all pass: {good}"

    # FAILURE ISOLATION: corrupt ONLY the epoch proof. epoch FAILS, the other two PASS.
    (mount / "proof_epoch.txt").write_text("0xdeadbeef")
    iso = {v["id"]: v for v in verify_all(metas, mount, "unused")}
    assert iso["task-02-epoch"]["pass"] is False, "corrupted epoch proof must fail"
    assert iso["task-01-tip"]["pass"] is True, "tip must still pass (no cascade)"
    assert iso["task-03-blockhash"]["pass"] is True, "blockhash must still pass (no cascade)"

    # MISSING proof is graded fail, not a crash (one task missing must not break others).
    (mount / "proof_tip.txt").unlink()
    miss = {v["id"]: v for v in verify_all(metas, mount, "unused")}
    assert miss["task-01-tip"]["pass"] is False and "missing" in miss["task-01-tip"]["reason"]
    assert miss["task-03-blockhash"]["pass"] is True, "missing tip must not affect blockhash"

    print("logic OK: compose+pointer+hash, independent grading, failure isolation, missing-proof handling")
    return 0


if __name__ == "__main__":
    sys.exit(main())
