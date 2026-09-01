# Accepted execution follows one immutable campaign schedule

> **Status: accepted.** This decision supplies the campaign, scheduler and evidence resolution
> boundary required by ADR-0015. It composes the attempt store from ADR-0018, the preflight contract
> from ADR-0019 and the isolated supervisor without changing legacy matrix execution.

## Context

Independent Task attempts are useful only if an operator cannot choose which completed outcomes to
keep, skip an unfavorable planned slot, run overlapping accepted campaigns, or replace an
infrastructure failure with an undeclared rerun. A report-directory scan is also an inclusion choice:
it cannot establish which attempts were planned before their outcomes were known.

## Decision

An accepted campaign begins with one canonical `ckbbench-campaign-manifest-v1` document published
atomically under a new opaque campaign ID. It fixes the suite and freeze, execution source, complete
batch and slot order, B/C trial pairs, Task and challenge identities, chain and treatment profiles,
model variants, per-Task budgets, resource-equivalence policy, and retry and stopping rules. Matching
B/C slots are adjacent and identical outside their treatment profile. Their order alternates across
pairs.

The executable policies are canonical records, not unverified labels:

- `whole-task-infrastructure-retry-v2` permits exactly one fresh retry after an unscored,
  allowlisted `infra_fail` whose cleanup is complete and a 30-second cooldown has elapsed. The
  allowlist is part of the policy bytes. Configuration drift, wrong-network observations, stale
  qualification, insufficient funding, dependency mismatch, malformed adapter output, scored
  outcomes and budget exhaustion are terminal. A retry of a retry is also terminally ineligible.
  The policy digest is
  `04e149ec29671adf8bcf61e70b39f612bf18cc5043d2dc88ad7cbcc7919bb56c`.
- `serialized-evidence-stop-v1` continues after scored outcomes and an exhausted infrastructure
  retry, never adapts to scores, and pauses on active, corrupt or incompletely cleaned evidence. Its
  digest is `768e9459edee96e2cdea5ba2f3fff9cfeb632cfe6ca5066f9efde10d57f6ac4e`.

The scheduler takes one non-blocking lock in a private per-user host runtime directory for the entire
command. The production location is shared across checkouts rather than relative to one repository.
It derives the next slot from the immutable manifest and validated attempt-store contents; it writes
no mutable cursor. `run-task` can execute only that next slot. `run-batch` executes the declared batch
in order, continues after a scored failure, performs the one eligible infrastructure retry, and stops
when cleanup or evidence integrity is unresolved. Recovery seals and cleans an interrupted attempt;
it does not resume its agent or grading process.

No execution command generates a report. A separate command emits
`ckbbench-report-resolution-v1` only when every planned slot is terminal. For each original and retry
attempt it names the exact intent, preflight requirements, ordered ownership-journal entries,
preflight evidence, result, and ordered cleanup or reconciliation receipts. The resolver validates
complete envelopes and retry lineage and makes no outcome-dependent inclusion choice. A directory
scan emits only `ckbbench-exploratory-preview-v1`, which cannot parse as accepted evidence.

The campaign command surface is available through `./bench campaign`. Listing, freezing and planning
are offline. Execution uses an injected runtime factory. Until concrete provider, CKB AI, RPC,
signer, funding and container adapters are supplied, the production CLI refuses before reserving an
attempt or contacting an external service.

## Consequences

- A process restart recomputes the same next slot from retained evidence rather than a mutable state
  file.
- A completed infrastructure predecessor remains visible beside its single retry.
- Two accepted campaigns cannot overlap through the supported host operator path.
- Report inputs are reproducible from an explicit pre-outcome plan and exact artifact digests.
- Report-builder source identity, aggregation and rendering remain report-layer work; they must bind
  this resolution rather than rescan the attempt directory.
- Concrete live adapters and TestNet treatment integration remain separate work and require their own
  authorization and validation.
