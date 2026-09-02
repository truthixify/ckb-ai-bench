# ADR-0024: Model And Provider Qualification

## Status

Accepted.

## Context

A model profile describes the request contract, but its presence does not prove that the selected
endpoint currently accepts that contract or returns stable model and usage identities. Discovering
an incompatible route after a campaign begins wastes paid attempts and can leave a campaign with
evidence that cannot be compared.

## Decision

Every model variant selected for an accepted campaign must have a current qualification record.
Qualification sends three independent, production-shaped Responses requests through the exact
profile endpoint. Each request has a one-request transport with redirects and transport retries
disabled. The fixed bash tool call is validated and never executed.

All three responses must be complete, use the expected returned-model identity, contain exactly one
valid fixed tool call, and report non-negative native usage satisfying input plus output equals
total. The command stops at the first failure. It does not switch endpoints, models, providers,
routes, protocols, or thinking settings.

The canonical write-once record binds the exact profile digest, model variant, API style, thinking
level, request payload, request extensions, production retry policy, and usage contract. It retains
only fixed classifications, public identities, counters, timestamps, and usage integers. Generated
records live below `benchmark-output/model-qualifications/` and are not source files.

Qualification is deliberately stricter than an agent attempt. It does not use the agent's bounded
provider recovery policy, because recovery would hide instability during admission. The selected
campaign later binds the accepted qualification record's digest and uses the profile's normal retry
policy during execution.

## Consequences

- Three clean checks establish protocol compatibility and a short stability window, not future
  availability, model quality, price, or immutability of an alias.
- A failed check consumes its request and produces rejected evidence. Repeating it needs a new live
  authorization and a new destination.
- Profile, route, thinking, protocol, model-identity, or retry-policy drift invalidates the record.
- Campaign freezing must reject missing, stale, rejected, or mismatched qualification evidence.
