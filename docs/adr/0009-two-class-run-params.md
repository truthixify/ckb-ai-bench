# Run params are generated in a pre-step and split into two security classes

## Context

A Task's Verifier needs concrete values to check a Proof (recipient address, amount, sometimes a
private key or the full expected answer). Some Tasks require per-run-fresh values for integrity
(ADR-0001: unique recipient, amount-as-nonce). The agent must know *some* of these values to do the
task, but must not see others or it could cheat.

## Decision

A **pre-step runs before the agent wakes** and generates the run's values from each Task's parameter
schema: fresh addresses, nonce amounts, random values, and any private keys. The generated values
split into two classes:

- **Prompt-injected params** — the agent-safe subset it legitimately needs (recipient, amount). The
  prompt builder extracts these and renders them into the Composed prompt.
- **Verifier-private params** — secrets and answer values (private keys, expected results) that would
  let the agent shortcut. These are **held harness-side and never placed in the mounted folder during
  the agent's run.**

Both classes derive from the same generation step, so the agent and Verifier share identical
primitives wherever they must agree (same recipient, same amount), while secrets stay Verifier-only.

Verifier-private params are injected into the container **only at verify time, after the `done`
sentinel has ended the agent's run** — the same timing guarantee that protects the Hidden suite
(ADR-0002, ADR-0005). During the agent's run, only the prompt-injected subset exists anywhere reachable
by the agent.

## Consequences

A Task directory holds a parameter *schema* (which params are per-run-generated vs static), not the
concrete values, so it is not fully self-contained — concrete values are run-scoped. The placement
rule (secrets never in the mount during the run; injected only post-`done`) is what keeps per-run
integrity and anti-cheat consistent with the in-container Verifier.
