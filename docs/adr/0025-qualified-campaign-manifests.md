# ADR-0025: Qualified Campaign Manifests

## Status

Accepted.

## Context

Model qualification is useful only if an accepted campaign identifies the exact evidence it relied
on. Reading a profile's older migration evidence at runtime would allow a newly frozen campaign to
claim qualification without binding the three-request admission record that was actually reviewed.
Selecting evidence by directory scan or modification time would also make the campaign depend on
mutable local state.

Existing campaign manifests are immutable historical artifacts. Their bytes and digests must remain
valid, so adding qualification provenance requires a new manifest schema rather than changing the
legacy representation.

## Decision

Campaign manifest version 2 carries one qualification binding for every exact model-profile ID and
digest used by its slots. Each binding records the qualification ID, schema and kind, canonical
artifact digest, completion time, profile identity, and model-variant identity. Bindings use
canonical profile order and must exactly cover the manifest's slot profiles.

Freezing a current suite campaign requires explicit paths for every selected model profile and
qualification artifact. It does not search for evidence. The freezer loads canonical write-once
records, validates each accepted qualification against its exact current profile and the campaign
creation time, and refuses missing, duplicate, surplus, stale, rejected, or mismatched inputs.

During execution, a model-specific runtime resolves its exact binding from the manifest. Provider
preflight records that binding's digest and completion time, not historical evidence embedded in the
profile. The live command reopens the explicitly supplied canonical qualification record, validates
it against the current profile and time, and requires it to reproduce the manifest binding before it
constructs live adapters. A runtime can execute only slots for its selected profile. Freshness is
checked again at every Task preflight.

Legacy suite campaigns retain manifest version 1 and cannot carry qualification bindings. Current
suite campaigns require version 2 and cannot downgrade to the legacy behavior.

## Consequences

- Historical campaign bytes and digests remain unchanged.
- A multi-model campaign can be frozen once and executed as serial model-specific batches.
- Changing a profile or qualification record requires a new campaign manifest.
- A qualification that expires during a long campaign stops later Task attempts at preflight rather
  than silently extending its validity.
