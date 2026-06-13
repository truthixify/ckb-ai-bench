# MCP as a means versus MCP as the answer

## Context

The headline result is the C minus B delta (MCP+web versus web-only). A skeptic will argue that if an
MCP tool can complete a Task by itself, arm C only proves "the product has a button," not that the
agent engineers CKB better. Some MCP tools (e.g. deploying a binary, funding, sending a marker tx)
overlap with what certain Tasks need.

## Decision

We distinguish two uses of the MCP and let task design, not prompt rules, enforce the line:

- **MCP as a means (allowed).** When the MCP performs plumbing in service of a Task whose real subject
  is something else (deploying a binary so a contract can be exercised, funding, sending a marker
  transaction), using it is legitimate and is part of the MCP's intended value: faster, higher success
  rate, fewer tokens.
- **MCP as the answer (disallowed).** When a Task's subject *is* the thing an MCP tool would do (e.g.
  "write a script that deploys X"), using that tool bypasses what is being measured.

The headline rests on **Code Tasks**: the Proof is the artifact the agent authored, graded by a Hidden
suite. No MCP convenience tool can author contract logic, so it cannot manufacture the Proof — the
anti-cheat is structural, baked into what the Proof *is*. On-chain convenience Tasks remain in the
suite and are kept honest by the stateless integrity checks (ADR-0001), but they are not load-bearing
for the headline.

## Consequences

Cases where an MCP tool overlaps a non-headline Task's subject are tolerated as minor noise rather than
eliminated, because the headline does not depend on those Tasks. Where it matters, we move the measured
subject into a Code Task so authorship — not deployment — is what is graded.
