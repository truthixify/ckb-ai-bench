# Stateless integrity for on-chain Proofs

> **Validated by live spike (2026-06-12), `spikes/devnet-e2e/FINDINGS.md`.** All three checks were
> proven end to end against the devnet sidecar (valid Proof passes; stale, wrong-nonce, nonexistent,
> and borrow/replay Proofs all reject). The spike sharpened two points now folded into the Decision:
> the amount-as-nonce must be **high-entropy and verifier-private**, and the Verifier proves an
> **observable effect, not tx authorship** (the accepted boundary for effect-based On-chain Tasks).

## Context

On-chain Tasks pass when a transaction with the required effect lands on a CKB chain, and the agent
submits that transaction's ID as its Proof. On a public, shared chain (TestNet especially) a matching
transaction may already exist or be borrowed from another sender, so a naive "does a matching tx
exist?" check can be cheated.

## Decision

We defend Proof integrity **without any persistent uniqueness database**. Integrity comes from three
stateless checks the Verifier applies by direct CKB RPC:

1. **Freshness window.** An early Task records the chain's tip block at run-start. Every claimed
   transaction must have landed on or after that block — proving it is new to this run.
2. **Amount-as-nonce.** The exact CKB amount specified per Task acts as a nonce. The nonce **must be
   high-entropy and verifier-private** — a shannon-precise random amount the agent is never told is a
   nonce. The spike proved that a fixed or predictable amount only checks an *observable effect*, so a
   spectator or arranged transaction whose outputs coincidentally match could be borrowed by id. With a
   high-entropy verifier-private nonce the probability any unrelated tx pays exactly that many shannons
   to exactly this recipient is negligible, binding a matching committed tx to this run. An explicit
   nonce in the witness or data field is used additionally on Tasks where that is natural, but is not
   relied on everywhere.
3. **Structural assertions.** The Verifier asserts tight criteria on the transaction structure (locks,
   amounts, the specific contract involved, and the absence of unintended effects), so a transaction
   with the right outcome but the wrong structure fails.

We deliberately prefer Tasks built on a specific fixed smart contract (e.g. a Nervos DAO deposit, an
ACP transfer) because their on-chain structure is deterministic and therefore cleanly verifiable. DAO
*withdrawal* is out of scope for short runs because its minimum-delay lock cannot complete in one run.

## Consequences

This accepts a residual, near-zero risk that a pre-existing third-party transaction coincidentally
matches all of (newer than tip, exact amount, exact structure) instead of eliminating it with
infrastructure. We accept that risk to avoid operating and maintaining a stateful uniqueness service.

The Verifier proves the **observable effect**, not that the agent *constructed* the transaction (there
is no binding to the agent's keys or inputs). The spike confirmed this is acceptable for effect-based
On-chain Tasks given the high-entropy verifier-private nonce. A future task type that must bind
authorship would add an agent-key-signed marker (witness or data) the Verifier checks — tracked, not
required for v1.
