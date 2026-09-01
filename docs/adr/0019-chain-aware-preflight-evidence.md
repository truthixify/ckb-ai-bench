# Each Task attempt uses one chain-aware preflight evidence boundary

> **Status: accepted.** This decision implements the preflight contract in ADR-0015 over the
> Task-attempt intent and ownership journal from ADR-0018. It does not make live requests or change
> the legacy matrix runner.

## Context

A Task attempt can spend provider credit or mutate a public chain. It must not begin because a
generic health endpoint answered, because an RPC label says `testnet`, or because a key happens to
exist. Before generation, the controller needs evidence that the reviewed source, exact model route,
CKB AI treatment, chain, signer, funding, deployed dependencies and output namespace all agree with
the immutable attempt plan.

These checks cross trust boundaries. Provider, CKB AI and RPC responses are untrusted; signer and
funding adapters can accidentally target another network; filesystem checks can race or inspect an
unreviewed source tree. Raw responses, credentials and signing material cannot enter public evidence.

## Decision

`ckbbench-task-preflight-requirements-v1` is an exact-key canonical record bound to one immutable
Task-attempt intent. It freezes:

- a recent `bounded-generation-compatibility-v1` evidence digest and maximum age for the exact model
  profile;
- the `authenticated-non-generation-v1` readiness operation and its request ceiling;
- CKB AI server, catalog and Task surface identities, plus whether that surface claims a live chain;
- stable expected chain identity;
- opaque signer handle, public address and constrained signing-policy digest;
- maximum transfer, fee reserve, safety margin, minimum cell count and confirmations;
- deployed dependency identities;
- every reserved signer, input, workspace and runtime name; and
- the exact output resources that must still be fresh.

The schema imposes hard request and evidence-age ceilings in addition to campaign-selected limits.
On-chain requirements must reserve positive capacity, at least one input, the exact signer, a
workspace and a runtime name. Local-hermetic requirements carry no chain, signer or funding fields.

### Ordered checks

The engine validates the intent and contiguous reserve-only ownership journal before calling an
adapter. It then runs exactly:

```text
source -> provider -> CKB AI -> RPC -> signer -> funding -> dependencies -> outputs
```

Local-hermetic Tasks omit RPC, signer and funding. The first failure stops the sequence. Arm B does
not skip CKB AI readiness; matched B and C attempts differ later only in model-visible treatment.

The checks establish:

1. The current repository revision, canonical execution-source digest, role images and toolchain
   equal the intent, with no staged, tracked or untracked execution-input drift.
2. The exact model profile has recent generation-compatibility evidence. A separate authenticated
   readiness request sends no generation body, follows no redirect and is never called generation
   evidence.
3. CKB AI exposes the exact pinned server, catalog and Task surface. A chain-aware surface attests
   the expected stable network; a chain-neutral surface must not claim one.
4. Direct RPC returns the expected chain ID and genesis. The current tip is retained as observation
   provenance but stable network matching does not require a frozen tip.
5. The reserved signer is single-assignment, inaccessible to the agent, and enforces the exact
   policy. Signer, funding and dependency observations each bind the stable direct-RPC identity.
6. The reserved input lease has enough confirmed cells and spendable capacity for transfer, fees and
   safety margin. Preflight never generates a key, calls a faucet or refills capacity.
7. Dependencies match their pinned identities and every output resource is fresh, non-symlinked and
   unowned.

### Public evidence

`ckbbench-task-preflight-evidence-v2` records the requirements and intent digests, ordered check
digests, sanitized status, bounded controller request count, stable chain evidence, signer and funding
observation digests, and required and observed capacity. It produces the `PreflightBinding` stored in
the eventual Task result.

Controller requests are separate from model usage. Known observations have exact counts; an adapter
exception or malformed return makes the count `unknown` rather than inventing zero. Failure
stage/category pairs are allowlisted. The reader rejects missing checks, reordered stages, activity
after failure, contradictory count status, forged chain or funding fields and mismatched status.

Version 2 adds an explicit interruption outcome so recovery can seal an attempt that ended before
readiness checks without rerunning them or misreporting the plan as invalid.

Adapters return typed, allowlisted observations. Their exception messages, bodies, headers, URLs,
credentials, private keys and response content are discarded. The engine retains only canonical
public fields or their SHA-256 digest. A malformed output identifier is rejected before any adapter
call.

## Implementation boundary

This decision supplies the schemas and validation engine. The single-Task supervisor persists its
requirements and evidence, stops on failure, and binds the stored evidence into the result. Concrete
provider, CKB AI, RPC, signer, funding, deployment and filesystem adapters remain separate work. The
preflight module itself does not provision resources, execute an agent, grade a Task, clean up an
attempt or alter legacy matrix evidence.

## Consequences

- Paid generation cannot begin from a health check alone or against an unqualified model route.
- A readable chain label cannot substitute for matching genesis evidence.
- Funding and signer mistakes fail before model spend instead of becoming ambiguous Task failures.
- B receives the same treatment-readiness check as C without receiving the treatment itself.
- Adapter failures remain diagnosable by bounded category and stage without retaining provider or
  signer secrets.
- Real adapters must implement the typed observation contract and preserve the request ceilings.
