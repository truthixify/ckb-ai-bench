# Thinking level is part of model-variant identity

> **Status: accepted.** This decision refines ADR-0015 section 10 without changing historical
> matrix result bytes.

## Context

The profile already pinned `reasoning_effort`, but reporting and aggregation primarily keyed rows by
the requested model string. Two profiles for the same model could therefore be rejected as drift or,
outside strict validation, collapse into one series. A reader also could not see the thinking level
in charts, filters or run provenance.

Model names do not identify one inference treatment. Thinking level, temperature, truncation,
context replay, routing, timeout and retry settings can all change behavior. B and C are comparable
only when all of those settings are identical.

## Decision

`reasoning_effort` remains the stored profile field because it is the provider-facing contract.
The benchmark exposes that exact value as `thinking_level`. Accepted values include explicit effort
levels plus two honest absence states:

- `provider-default`: the endpoint supports reasoning but the provider chooses the level;
- `unsupported`: the endpoint does not accept a reasoning-level parameter.

For either absence state, production and the compatibility probe omit the `reasoning` request field.
They never translate the state into an invented effort. Explicit effort levels continue to send the
exact `{"effort": <level>}` object.

One canonical `model_variant_id` is the SHA-256 digest of canonical JSON containing:

- schema marker `model-variant-v1`;
- requested model;
- thinking level;
- profile ID; and
- SHA-256 of the exact reviewed profile bytes.

The profile digest binds every other inference, protocol, routing and retry setting. The variant ID
therefore changes when the requested model, thinking level, logical profile or any profile byte
changes. Human-facing labels show `<requested model> · thinking <level> · variant <fingerprint>` so
two profiles with the same model and thinking level remain distinguishable. The full variant ID
stays available in provenance.

New run IDs include a filename-safe thinking label plus a short display prefix of the variant ID.
The result artifact still carries the full profile ID and digest. Run-ID prefixes are navigation
aids, not the authoritative identity.

Validation, duplicate detection, budget checks, aggregation and B/C pairing include the exact
profile ID and digest. Multiple variants of one requested model may coexist in one validated report.
A B row from one variant and a C row from another remain two incomplete series and never form a
comparison. Returned-model and budget parity are enforced inside each exact variant.

Reports resolve thinking level and full variant ID from the accepted profile bound by every row.
Charts, leaderboards, model details, Task tables, run filters and provenance use the variant identity,
so two thinking levels appear side by side without sharing routes or interactive state.

## Historical schema boundary

Matrix result schema `1.8.0` is not rewritten. Its rows already bind exact profile bytes through
`model_profile_id` and `model_profile_sha256`; the report resolves the explicit thinking level and
variant ID from that accepted profile. Direct `thinking_level` and `model_variant_id` fields belong
to the independent task-attempt schema.

This bridge keeps historical evidence readable without pretending an old row had fields it did not
store. A raw matrix row without its accepted profile is insufficient to reconstruct its display
metadata and cannot enter an accepted report.

## Consequences

- The same model at different thinking levels is reportable but never pooled.
- Reformatting a profile creates a new variant because exact reviewed bytes are evidence.
- `provider-default` is observable provenance, not equivalent to any explicit effort.
- `unsupported` describes a protocol limitation rather than a low or disabled thinking level.
- Existing tracked profiles remain byte-identical and retain their qualification evidence.
- Independent task-attempt artifacts can reuse this identity unchanged.
