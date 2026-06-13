# Spike: composed-prompt multi-task run (ADR-0008) - FINDINGS (2026-06-12)

Goal: prove the Suite DELIVERY path end to end, beyond the single-task Tier-1 model loop.
A Suite stores Tasks as a registry of directories but delivers them as ONE composed
prompt written to the mount, reached by a thin pointer; the agent works ALL tasks in one
pass to `done`, writing N independent Proofs, each graded INDEPENDENTLY. This spike also
surfaces the two things ADR-0008/0009 call out: strict Task independence, and per-task
token/time attribution loss in a single composed pass.

## What was built

- `registry/` models ADR-0008 STORAGE: three task dirs (`task-01-tip`, `task-02-epoch`,
  `task-03-blockhash`), each with a `prompt.txt` fragment + `meta.json` (proof file,
  score, check), indexed by `manifest.json` (ordered task list + suite pins).
- `compose.py` models DELIVERY: assembles preamble + fragments (in manifest order) +
  postamble into a single composed prompt, writes it as `INSTRUCTIONS.md` on the mount,
  sha256-hashes it (the freeze), and returns a THIN POINTER ("read INSTRUCTIONS.md ...")
  as the prompt actually injected. The wall of text is never injected directly.
- `spike_composed_suite.py` drives the real agent (grok-composer-2.5-fast via the
  localhost:18321 OpenAI-compatible proxy) over the pointer, in one LocalEnvironment pass.
- `verify.py` is the independent Verifier: it grades each Proof by DIRECT CKB RPC (the
  testnet archive node), never the MCP server. tip = freshness window; epoch = exact
  match to current epoch; blockhash = exact match to block 1's hash.

## The three independent tasks (each a different MCP read, no cross-references)

| Task | MCP tool | Proof file | Verifier check (direct RPC) |
|---|---|---|---|
| task-01-tip | rpc_get_tip_block_number | proof_tip.txt | proof tip within a freshness window of the verify-time tip |
| task-02-epoch | rpc_get_current_epoch | proof_epoch.txt | proof epoch == current epoch number |
| task-03-blockhash | rpc_get_block_hash {block_number:1} | proof_blockhash.txt | proof == block 1 hash (deterministic) |

## The decisive results

A typical live run:

```
agent exit=Submitted calls=5 used_mcp=True elapsed=12.1s
  task-01-tip        -> PASS  (tip within freshness window)
  task-02-epoch      -> PASS  (epoch matches current epoch)
  task-03-blockhash  -> PASS  (hash matches block 1)
score: 30 (3/3 tasks pass)
independence: OK (no fragment names another task proof)
```

The agent read the pointer, opened INSTRUCTIONS.md, and worked all three independent
tasks in ONE pass to `done`, each via MCP, writing three distinct Proofs that each graded
PASS independently.

`run-spike.sh` asserts this in two parts, by exit code:
- **A. Deterministic logic** (`test_logic.py`, no model): the composer assembles in
  manifest order and the pointer does NOT inline the tasks; known-good proofs all pass; a
  corrupted proof FAILS while the other two still PASS (failure isolation); a missing
  proof grades fail without crashing the others.
- **B. Live model loop** (`run_model_arm.py`): runs the loop K=3 times and requires a
  strict majority (>= K-1), printing the pass rate. Each individual run still asserts
  Submitted + used_mcp + all 3 proofs verified.

## Model variance (surfaced, not hidden)

During characterization the live model loop passed 6/6 standalone runs, but one earlier
run failed transiently (a single off-run, not a logic fault: the deterministic part A
always passes, and re-runs returned 3/3). This is exactly the per-cell variance a
benchmark expects, which is why ADR-0011 mandates >= 3 runs per cell with CIs shown.
The spike encodes that reality: part B runs K times and reports the rate rather than
betting the proof on one stochastic run. A systematically broken mechanism would fail
every run; a transient does not sink the spike.

## MCP provenance gate (round-2 hardening, closes the adversarial "proof-without-work" finding)

Both round-1 adversarial reviewers (grok-build, grok-composer) raised the same top issue: the
verifier graded only the proof VALUE, and a correct value is not proof of work. Block 1's hash
is a fixed public constant; the tip sits in a freshness window; the epoch is the current one.
A cheating agent could fetch all three by direct RPC/curl (or hardcode the blockhash) and pass
while making only a token MCP call, since success required just `used_mcp = any(...)`.

Fixed: each task now also requires PROVENANCE. The agent's trajectory records every `mcp_call`
with its tool name; `apply_provenance_gate` (verify.py) passes a task only if its value is
correct AND the agent invoked THAT task's specific `rpc_` tool over MCP. So a value-only cheat
fails even with the right answer. `test_logic.py` proves this deterministically: with correct
values but (a) all tools invoked -> all pass, (b) NO MCP invocation -> all fail
("proof-without-work"), (c) only one task's tool invoked -> only that task passes (per-task, not
global). The live model genuinely invokes all three tools, so it still passes 3/3.

## Strict independence (ADR-0008)

No task fragment references another task's Proof file; `test_logic.py` asserts this
mechanically, and the failure-isolation test proves one task failing does not change
another's grade. Intra-run dependencies are deliberately out of scope for v1.

## Per-task attribution loss (ADR-0009, confirmed)

The whole suite is one composed pass to a single `done`. The loop emits no per-task
"complete" signal, so tokens and wall-time CANNOT be split per task: only a run TOTAL is
available (here 5 calls / ~12 s). The spike writes this conclusion to `attribution.json`
rather than pretending a per-task number exists. Per-task metrics remain a deferred
enhancement that would require a per-Task complete tool-call signal.

## Residual (tracked, design-level, not spike-blocking)

- The tasks here are MCP reads (deterministic, reliable) to isolate the DELIVERY
  mechanism. A real suite mixes Code Tasks and On-chain Tasks; their verifiers are the
  Tier-1 hidden-suite and on-chain-proof spikes, already proven. This spike does not
  re-prove those; it proves they can be DELIVERED together and graded independently.
- The OFF arm is not re-run here (Tier-1 Spike 3 + the egress-proxy spike cover MCP
  isolation); this spike is about multi-task delivery on a single arm.
- The verifier reads the public testnet node; in production it targets the run's chain
  profile by URL (DevNet sidecar or TestNet), symmetric per ADR-0005.

## Reproduce

```
cd spikes/composed-suite
bash run-spike.sh        # part A deterministic; part B = K live model runs, majority
# single live run:
PYTHONPATH="$PWD/../../agent:$PWD" ../../agent/.venv/bin/python spike_composed_suite.py
```
