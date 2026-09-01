# Each Task attempt has one immutable evidence envelope

> **Status: accepted.** This decision implements the evidence boundary defined by ADR-0015. It does
> not change legacy `RunResult` schema `1.8.0` or wire the new format into the
> current matrix runner.

## Context

The legacy matrix stores one result for a complete multi-Task agent session. Campaign execution runs
Tasks independently, so its durable unit must represent one Task attempt, survive interruption,
attribute resources, and support one narrowly eligible infrastructure retry without replacing
history.

A single result JSON cannot prove what the supervisor intended before external activity, which
resources an interrupted process owns, or whether teardown completed after the result was sealed.
Those facts occur at different lifecycle boundaries and must remain independently immutable.

## Decision

One attempt is stored under an opaque 128-bit identifier:

```text
<attempt-root>/attempt-<32 lowercase hex>/
  intent.json
  journal/
    000000-<artifact sha256>.json
    ...
  result.json
  receipts/
    000000-<artifact sha256>.json
    ...
```

Every document uses exact-key validation and canonical ASCII JSON: sorted keys, compact separators,
no NaN, one trailing newline. Artifact digests cover the exact written bytes. The store publishes
with an exclusive hard link after flushing the candidate and never replaces an existing path.
Readers require a parsed document to serialize back to those exact bytes. Attempt directories are
single-assignment even if publication is interrupted.

### Intent

`ckbbench-task-attempt-intent-v1` is published before reservation or external activity. It binds:

- campaign manifest, batch, execution plan and trial;
- suite version and freeze, Task identity and Task-content digest;
- arm, treatment profile, chain track and chain profile;
- requested model, thinking level, canonical model-variant ID and exact model-profile digest;
- one Task-specific budget profile and its step, wall-time, provider-call and output-token limits;
- trial challenge, run-parameter derivation and exact prompt-safe parameter digest;
- the blinded verifier-private commitment scheme and digest;
- resource-equivalence and whole-Task retry policies; and
- repository revision, source tree, agent image, verifier image, toolchain and serialized-execution
  contract.

The commitment scheme requires canonical verifier-private bytes plus at least 256 bits of random
blinding material. Only the scheme and commitment enter public evidence.

### Ownership journal

`ckbbench-ownership-journal-entry-v1` is a contiguous, append-only SHA-256 chain. Sequence zero is a
reserve claim. Every later action binds the attempt, intent, predecessor entry, UTC, lifecycle phase,
resource kind and opaque public resource ID. A resource action is invalid before its claim, after a
final disposition, across an attempt boundary, or after a backwards phase or timestamp transition.

The journal records no private key, seed, credential, response body or verifier-private value. A
result seals one pre-teardown prefix. Entries after that prefix may only perform teardown. Once an
incomplete cleanup receipt exists, only reconciliation of an already claimed resource may extend
the chain; complete cleanup seals it permanently. Claims must precede teardown, cleanup actions are
limited to teardown or reconciliation, and a failed cleanup must be receipted before that resource
is tried again.

### Result

`ckbbench-task-attempt-result-v1` repeats the complete experimental identity instead of relying on a
directory name. It binds the intent digest and terminal pre-teardown journal entry and records:

- the preflight evidence identity and digest;
- actual initial-resource-equivalence digest;
- verifier status, raw verifier score, awarded score, bounded reason and public Proof;
- `pass`, `agent_fail`, `infra_fail`, or `protocol_violation` with correctness eligibility;
- sanitized failure stage/category and agent exit status;
- reservation, preflight, setup, agent and grading durations; and
- attempt-level provider calls, attempts, responses, retries, retry delay, exact or incomplete token
  usage, provider-reported cost status, sanitized failure counts and returned-model counts.

Every response must contribute to exactly one returned-model count. An infrastructure failure is
unscored. Agent failure and protocol violation remain scored zero outcomes, and a pass requires the
full Task score. Teardown state cannot rewrite an already sealed result.

### Cleanup and reconciliation receipts

`ckbbench-cleanup-receipt-v1` binds the intent, result, result journal prefix, current terminal
journal entry and prior receipt. It accounts exactly once for every claimed resource. A complete
receipt contains only released, retired, permanent or confirmed-absent dispositions. An incomplete
receipt names at least one failed disposition.

Reconciliation appends journal evidence and a new receipt linked to the failed predecessor. It
cannot reuse the same journal prefix or replace the earlier failure. An accepted attempt requires a
receipt chain ending in complete cleanup; earlier failed receipts remain health evidence.

### Whole-Task retry

Only retry ordinal one exists. Its intent links the predecessor intent, result and final cleanup
receipt digests. The store validates the predecessor's complete envelope before reserving the retry
directory and permits only one retry reservation to name that predecessor. The predecessor must be
an unscored `infra_fail` with complete cleanup.

Campaign, Task, arm, trial, chain, treatment, model variant, budget, challenge, policy and execution
source remain identical. The retry receives a new attempt ID, prompt-safe digest and blinded
verifier-private commitment. Its ownership journal cannot reuse a predecessor resource identity. A
retry cannot itself be retried. Readers revalidate the complete predecessor and these freshness
rules whenever they load a retry envelope.

## Store validation

Readers and writers refuse malformed or non-canonical JSON, duplicate keys, artifacts over 1 MiB,
symlinks, unexpected files, filename/digest drift, gaps, forks, reordering, cross-attempt bindings,
missing results or receipts, contradictory dispositions and incomplete cleanup when completeness
is required. The store serializes appends with an exclusive root-directory lock so two writers
cannot publish sibling entries for one sequence or reserve sibling retries for one predecessor.

Public artifacts apply a bounded-text and secret-shaped-value check. This is defense in depth; the
supervisor remains responsible for passing only allowlisted public fields and keeping raw secrets in
its private boundary.

## Historical and implementation boundary

The legacy matrix remains readable only as `RunResult` `1.8.0`. It is not migrated, rewritten or
mixed with Task-attempt evidence. The attempt schema and store are the foundation for live preflight,
resource acquisition, execution, grading, recovery, CLI resolution and reporting.

## Consequences

- One Task can fail or be retried without replacing another Task's evidence.
- Crash recovery can identify owned resources from durable history rather than process memory.
- Cleanup truth remains separate from correctness truth while both are cryptographically linked.
- Evidence volume increases because one attempt is a small immutable document set rather than one
  mutable lifecycle record.
- Accepted report resolution must validate complete envelopes and retry lineages, not scan for the
  most favorable result.
