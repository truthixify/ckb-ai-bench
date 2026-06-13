# Adversarial review of the Tier-1 spikes (2026-06-12)

Three external reviewers were run in parallel (proving the multi-agent parallel-review capability)
against a fixed brief asking them to REFUTE each spike's claims. This records the roster, their
verdicts, my adjudication (I am the final call), and what was fixed.

## Roster (all reached the artifact and reviewed; no refusals)

- **grok-composer-2.5-fast** (grok CLI, x.ai), read-only.
- **grok-build** (grok CLI, x.ai), read-only. Sharpest reviewer.
- **codex** (codex-cli 0.139.0, `codex exec --sandbox read-only`), confirmed the exception_info fix
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

**Spike 1, ACCEPTED the central objection; FIXED.** The "hidden" suite was co-located with a
compile-time password `const`, so a hardcode cheat could pass without reading the lock args. Made
the password a verifier-private run param injected via `BENCH_PASSWORD`; proved the hardcode cheat
now FAILS against a fresh secret while the args-reading contract passes. (See code-task/FINDINGS.md.)
Residual, tracked: rejection tests should assert exit codes 5/6; harness must rebuild from agent
sources before grading; password-lock is intentionally a simple task.

**Spike 2, ACCEPTED unanimously; FIXED (biggest change).** The agent co-wrote `harness_meta.json`
and the verifier trusted it, no real anti-cheat. Split into three trust zones (`harness-prepare.js`
writes agent-visible `task.json` + verifier-private `secret.json`; agent writes only `tx_id.txt`;
verifier reads its own secret). Tightened STRUCTURE from `.find` to exactly-one-output. Added a real
borrow/replay negative (run 2 fed run 1's tx_id) that fails on both freshness and nonce. (See
devnet-e2e/FINDINGS.md.)

**Spike 3, ACCEPTED the caveats; FIXED two, surfaced one.** Added an OFF-arm model run (proves
0 MCP tools / used_mcp=False). Fixed the `mcp_call` JSON-with-spaces/quotes parsing bug and added
`agent/test_ckb_agent.py` (5 regression tests). Surfaced loudly: on LocalEnvironment the OFF arm
answered the tip task via host network (no MCP, but not MCP-gated), this is precisely the gap the
ADR-0006 egress proxy must close, now visible rather than hidden.

## Round 2 (verification re-review of the hardening)

The same two grok reviewers re-reviewed the hardened spikes (cold, with a skip-list of settled
points). They confirmed the round-1 fixes hold ("Trust zones are real", "OFF arm is wired
correctly", "All 5 regression tests pass", "the core hardening claim holds") and found two more
concrete bypasses, both now closed:

- **Spike 1 length-only cheat (grok-composer):** a `witness.len() == args.len()` cheat passed
  because the wrong-password test used a same-length witness. Fixed: test BOTH different-length and
  same-length wrong witnesses, assert specific exit codes (5/6) via `assert_rejected_with`, and
  PANIC if `BENCH_PASSWORD` is unset. Proven: the length cheat now FAILS, correct passes 4/4.
- **Spike 2 spectator-tx collision (both):** with a predictable nonce the verifier only checks an
  observable effect, so a third-party/arranged matching tx could be borrowed by id. Fixed:
  `harness-prepare.js` now uses a HIGH-ENTROPY random shannon nonce, making coincidental matches
  negligible. Added `run-spike.sh` (self-verifying, 5/5: valid + 4 negatives by exit code).

Both reviewers also noted the round-1 `assert_rejected_with` helper was dead code at review time;
it is now wired into the test bodies (resolved by the subsequent edit, proven above).

## Net outcome

All three spikes hold after two rounds of hardening. The reviews materially improved the proofs:
Spike 1 now defends the hidden-suite guarantee against hardcode AND length-only cheats with exit-code
assertions; Spike 2's anti-cheat is genuine (trust-zone split + high-entropy nonce + self-verifying
script); Spike 3 has a real OFF arm + a parsing regression suite. The most valuable cross-cutting
lesson: the egress proxy (ADR-0006) is load-bearing for OFF-arm data isolation, not optional polish.

Remaining design-level note (tracked, not spike-blocking): Spike 2's verifier proves the observable
effect, not tx authorship; a high-entropy verifier-private nonce makes this acceptable for
effect-based On-chain Tasks, and a future authorship-binding task would add an agent-key-signed
marker.

## Round 3 (final verification, CLEAN)

A third cold pass (grok-build, grok-composer) verified the round-2 fixes with both reviewers running
the cheat probes themselves (hardcode, length-only, borrow). Result:

- **grok-build:** Spike 1 SOUND, Spike 2 SOUND, Spike 3 SOUND, "No serious remaining issues."
- **grok-composer:** Spike 1 SOUND, Spikes 2 & 3 SOUND-with-(tracked-only)-caveats, "no serious
  remaining issues."

This was a fix-free round (no artifact edits), so it is the clean exit round. Trivial, non-blocking
observations noted and accepted as-is: `tests/src/lib.rs` carries an unused template-generated
Loader (not ours to prune; Context's default search is what runs), and the exit-code assertion
matches the rendered `error code N ` string (render-dependent but correct against the live format).
No serious issue survived three rounds across two independent models. Spikes certified sound.

---

# Adversarial review of the Tier-2 spikes (2026-06-13)

Same method, five new spikes (egress-proxy, container-verifier, composed-suite, mcp-preflight,
ladder-chart). Three reviewers run in parallel each round (grok-build, grok-composer via the
grok CLI; codex via `codex exec --sandbox read-only`). I adjudicate; I am the final call.

## Roster note

The two grok reviewers returned complete structured verdicts every round. codex, given a broad
round-1 brief, spent its turn budget reading files and returned no verdict (same limit seen in
the Tier-1 review); given a TIGHTER, scoped brief in round 2 it returned a sharp verdict and
caught a real issue the groks missed. Lesson applied: scope codex's brief tightly.

## Round 1 verdicts

| Spike | grok-build | grok-composer | codex |
|---|---|---|---|
| 1 egress-proxy | NOT-PROVEN | SOUND-WITH-CAVEATS | (no verdict: budget) |
| 2 container-verifier | SOUND-WITH-CAVEATS | SOUND-WITH-CAVEATS | (no verdict) |
| 3 composed-suite | NOT-PROVEN | NOT-PROVEN | (no verdict) |
| 4 mcp-preflight | SOUND | SOUND-WITH-CAVEATS | (no verdict) |
| 5 ladder-chart | SOUND-WITH-CAVEATS | SOUND-WITH-CAVEATS | (no verdict) |

Both groks' single most serious issue CONVERGED: **Spike #3 proof-without-work** -- the verifier
graded only proof VALUES, so an agent could fetch them by direct curl (or hardcode block 1's
public hash) and pass while making only a token MCP call.

## Adjudication and fixes (round 1)

- **Spike #3 -- ACCEPTED (the convergent finding); FIXED.** Added an MCP PROVENANCE GATE: each
  task passes only if its value is correct AND the agent invoked that task's specific `rpc_`
  tool over MCP (from the trajectory's `extra.mcp_tool` tags). `test_logic.py` proves it
  deterministically (no-MCP -> all fail; one-tool -> only that task passes). The live model
  genuinely invokes all three tools, so it still passes 3/3. (composed-suite/FINDINGS.md.)
- **Spike #1 -- ACCEPTED (grok-build); FIXED.** Isolation was exercised only over
  curl-to-hostname, and the logging claim overstated L3-blocked coverage. Added a raw-IP
  direct-egress test and a raw-IP-via-proxy allowlist test, and scoped the logging claim.
- **Spikes #2/#4/#5 -- SOUND-WITH-CAVEATS, only tracked caveats; no now-worthy fix.** The
  grep-heuristic isolation (#2), no-browser-render (#5), tool-count-not-asserted (#4) are
  documented tracked caveats, not now-worthy.

## Round 2 (verification of the fixes)

Both groks: **Fix A (Spike #3 provenance) HOLDS, Fix B (Spike #1 raw-IP) HOLDS, "no serious
remaining issues."** codex (tight brief) agreed Fix A HOLDS but rated Fix B DOES-NOT-HOLD:

- **codex -- ACCEPTED; FIXED.** The raw-IP checks asserted only "nonzero" / "exit 22", which
  would not distinguish a network/proxy denial from a reachable-host HTTP error. Closed: check
  4b now asserts the SPECIFIC L3-failure curl codes (6/7/28) so an HTTP error (22) fails it;
  check 4c adds a proxy-LOG assertion that the 403 is tinyproxy's filter refusal of 1.1.1.1,
  not an origin error. Now 9/9. This is the value of the three-reviewer panel: the two grok
  reviewers passed 4b/4c as written; codex caught the assertion-precision gap.

## Round 3 (final verification - UNANIMOUS CLEAN)

A third cold pass on the tightened raw-IP assertions. All THREE reviewers returned a verdict:

- **grok-build:** raw-IP fix HOLDS - "no serious remaining issues."
- **grok-composer:** raw-IP fix HOLDS - "no serious remaining issues." (flagged one trivial
  doc-drift: the Reproduce line still said 6/6 while the table said 9/9; fixed.)
- **codex:** raw-IP fix HOLDS - "no serious remaining issues." (4b cannot false-pass on an
  HTTP outcome; 4c's log assertion removes the origin-error ambiguity; FINDINGS scopes
  logging correctly.)

This was a fix-free round (the only edit was the trivial 6/6 -> 9/9 doc-drift, not a
finding-driven code change), so it is the clean exit round. No serious issue survived three
rounds across three independent models (two grok + codex).

## Net outcome (Tier-2)

All five Tier-2 spikes hold after the review. The reviews materially improved two proofs:
Spike #3 now binds each task PASS to genuine per-task MCP invocation (proof-without-work
closed); Spike #1 now proves L3 routing isolation with SPECIFIC exit codes and proves the
allowlist refusal is the proxy's filter via the log (assertion-precision gap closed). The most
valuable cross-cutting lesson: a three-reviewer panel with at least one tightly-scoped reviewer
catches assertion-precision gaps that broad reviewers pass over.

Tracked, not spike-blocking (design-level): Spike #3's value+provenance gate proves the agent
invoked the MCP tool, not that the proof was sourced FROM the MCP output (a direct-curl value
plus a per-task noop mcp_call would still pass) - acceptable for the delivery-mechanism claim;
binding proof-to-MCP-output is future product hardening. Spike #2 one-image-vs-two and the
grep-based isolation heuristic; Spike #1 IPv6/ICMP/tcpdump (routeless anyway). These are
documented in each FINDINGS.md.
