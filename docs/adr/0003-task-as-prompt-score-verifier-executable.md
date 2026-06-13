# A Task is a prompt, a score, and a verifier executable

## Context

The suite scores many kinds of Task (on-chain effects, authored contracts, factual answers) that are
checked in very different ways. We need one atomic unit the harness can run uniformly without special-
casing each kind.

## Decision

A **Task** is exactly three things:

1. **A prompt** — what to do, including the path where the Proof must be written so the Verifier can
   find it.
2. **A score amount** — the weight this Task contributes to the run's score.
3. **A verifier executable** — a self-contained program the harness runs automatically after the run.
   It reads the Proof at the known path and returns a pass/fail verdict.

The verifier executable's **language is per-Task**, chosen to fit the check: lightweight TypeScript/Node
for most (transaction validation, structural RPC checks) because it is simple and compatible across the
suite, and a Rust-based test suite for cases that need the CKB-VM (small-contract validation). The
harness is language-agnostic: it runs the executable and reads the verdict.

For **Code Tasks** the agent submits the compiled binary; the Rust verifier suite exercises it against
tight, predetermined criteria. Open-ended contracts with multiple valid answers are explicitly out of
scope for now and deferred until a later benchmark.

## Consequences

The runner, suite manifest, and scoring all build on this three-part unit, so changing it is expensive
later. Accepting compiled binaries (rather than source) means the verifier needs no compile toolchain,
but it forgoes "did it compile" as signal and admits a residual risk of an embedded precompiled cheat;
tight design criteria are relied on to make that impractical. The binary-versus-source choice is left
revisitable.
