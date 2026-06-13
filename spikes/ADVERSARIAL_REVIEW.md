# Adversarial review of the Tier-1 spikes (2026-06-12)

Three external reviewers were run in parallel (proving the multi-agent parallel-review capability)
against a fixed brief asking them to REFUTE each spike's claims. This records the roster, their
verdicts, my adjudication (I am the final call), and what was fixed.

## Roster (all reached the artifact and reviewed; no refusals)

- **grok-composer-2.5-fast** (grok CLI, x.ai) — read-only.
- **grok-build** (grok CLI, x.ai) — read-only. Sharpest reviewer.
- **codex** (codex-cli 0.139.0, `codex exec --sandbox read-only`) — confirmed the exception_info fix
  against the env contract; ran out of turn budget before emitting a full structured verdict, but
  its substantive conclusions (wiring-not-readiness; structure narrower than claimed) matched the
  groks.

Note: a fourth intended reviewer (`grok-4.3`) is not a valid grok-CLI model id (proxy-only alias);
the grok CLI exposes only `grok-build` and `grok-composer-2.5-fast`, so the two-grok roster is those.

## Verdicts

| Spike | grok-composer | grok-build | codex |
|---|---|---|---|
| 1 Code-Task grading | SOUND-WITH-CAVEATS | NOT-PROVEN | wiring, not readiness |
| 2 on-chain Proof + verifier | NOT-PROVEN | NOT-PROVEN | structure narrower than claimed |
| 3 real model loop | SOUND-WITH-CAVEATS | SOUND-WITH-CAVEATS | exception_info fix confirmed |

## Adjudication and fixes

**Spike 1 — ACCEPTED the central objection; FIXED.** The "hidden" suite was co-located with a
compile-time password `const`, so a hardcode cheat could pass without reading the lock args. Made
the password a verifier-private run param injected via `BENCH_PASSWORD`; proved the hardcode cheat
now FAILS against a fresh secret while the args-reading contract passes. (See code-task/FINDINGS.md.)
Residual, tracked: rejection tests should assert exit codes 5/6; harness must rebuild from agent
sources before grading; password-lock is intentionally a simple task.

**Spike 2 — ACCEPTED unanimously; FIXED (biggest change).** The agent co-wrote `harness_meta.json`
and the verifier trusted it — no real anti-cheat. Split into three trust zones (`harness-prepare.js`
writes agent-visible `task.json` + verifier-private `secret.json`; agent writes only `tx_id.txt`;
verifier reads its own secret). Tightened STRUCTURE from `.find` to exactly-one-output. Added a real
borrow/replay negative (run 2 fed run 1's tx_id) that fails on both freshness and nonce. (See
devnet-e2e/FINDINGS.md.)

**Spike 3 — ACCEPTED the caveats; FIXED two, surfaced one.** Added an OFF-arm model run (proves
0 MCP tools / used_mcp=False). Fixed the `mcp_call` JSON-with-spaces/quotes parsing bug and added
`agent/test_ckb_agent.py` (5 regression tests). Surfaced loudly: on LocalEnvironment the OFF arm
answered the tip task via host network (no MCP, but not MCP-gated) — this is precisely the gap the
ADR-0006 egress proxy must close, now visible rather than hidden.

## Net outcome

All three spikes hold after hardening. The reviews materially improved the proofs: Spike 2's
anti-cheat is now genuine, Spike 1 now defends the hidden-suite guarantee, and Spike 3 has a real
OFF arm + a parsing regression suite. The single most valuable cross-cutting lesson: the egress
proxy (ADR-0006) is load-bearing for OFF-arm integrity, not optional polish.
