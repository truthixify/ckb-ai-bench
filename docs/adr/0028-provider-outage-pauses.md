# Provider outages pause campaign scheduling

> **Status: accepted.** This decision replaces the stopping behavior for newly frozen campaigns.
> Manifests that bind `serialized-evidence-stop-v1` retain their original meaning and bytes.

## Context

The original scheduler immediately used a slot's sole whole-Task infrastructure retry and then
continued to later slots when that retry also failed. During a provider outage, one batch command
could therefore consume both allowed attempts for several Tasks. Those attempts were honest
infrastructure evidence, but the scheduler had turned one shared outage into missing correctness
observations across the batch.

Provider availability and model performance are different measurements. A provider that cannot
answer a readiness request should not consume a Task attempt that has not started. Once an attempt
has started, however, its immutable evidence must remain visible and cannot be replaced because its
outcome is inconvenient.

## Decision

New campaigns bind `serialized-evidence-stop-v2`. Before reserving each original or retry attempt,
the operator prepares the exact slot inputs and performs a source-first provider gate:

- source identity, tracked state, role images, network and resource-name absence are checked first;
- the exact model profile and credential contract then make one authenticated, non-generation
  readiness request;
- an unavailable provider ends the command before an intent, resource claim or attempt directory is
  published; and
- a successful gate does not replace attempt preflight. The ordinary persisted preflight repeats
  source and provider checks immediately after reservation, closing the gap between availability and
  accepted execution as far as the serialized process permits.

Malformed gate observations, source drift, stale qualification evidence and readiness-contract drift
are hard errors rather than provider outages. Provider content and transport error text remain outside
operator output. The stopping policy fixes the gate request limit at one. A failed gate ends the
explicitly authorized command, so another check requires another operator invocation rather than an
automatic polling loop. Because no Task attempt exists, that request is campaign-operation overhead,
not Task acquisition usage.

Any retained `infra_fail`, whether it occurred during preflight or after agent execution began, ends
the current batch command after cleanup. The next operator invocation recomputes progress from the
manifest and immutable attempt store. If the failed original is eligible for the declared
whole-Task retry, the retry cooldown and a fresh provider gate precede that retry. If the sole retry
also fails, another invocation may continue with the next planned slot, but no third attempt is
allowed. Scored outcomes continue normally and are never retried.

The stopping-rule digest is part of the manifest. Historical manifests using
`serialized-evidence-stop-v1` keep the original continue-and-retry behavior so retained artifacts
remain readable and reproducible. Release-derived freezing selects version 2 for new campaigns.

## Consequences

- A provider outage detected before reservation spends one readiness request but no Task attempt,
  signer lease or generation budget.
- A mid-attempt outage remains visible in infrastructure health and acquisition evidence.
- One outage cannot automatically cascade through later slots in a single batch invocation.
- Resuming is explicit and requires two source/provider observations: the pre-attempt gate and the
  persisted Task preflight.
- The existing one-retry ceiling, append-only lineage, score-independent schedule and manual report
  resolution remain unchanged.
