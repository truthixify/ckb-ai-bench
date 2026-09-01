# Model profiles are provider-neutral protocol configurations

> **Status: accepted (2026-09-01) by project-operator authorization after recorded
> review; no independent review is claimed.** This ADR supersedes the provider-specific
> configuration shape in ADR-0014 for active profiles. Historical results keep the profile bytes and
> source revision that produced them.

## Context

The legacy matrix harness selected a reviewed endpoint and model, but its profile schema also encoded a
small provider enum. Python and shell code branched on that enum to choose credentials and construct
request routing. Adding another OpenAI-compatible endpoint could therefore require code changes even
when its protocol was already supported.

Provider names are deployment details, not benchmark methodology. The benchmark needs to bind the
wire contract, endpoint, model, inference settings and credentials without teaching the runtime
which company operates the endpoint.

## Decision

Active model profiles use schema version `9`. A selectable profile records:

- a model-oriented alias and `profile_id`;
- the exact safe API root, requested model and probed response model;
- the supported protocol, currently `openai-responses`;
- the exact generic credential locator `CKBBENCH_LLM_API_KEY`, never its value;
- qualification lineage: direct evidence, or the exact prior profile and finalized evidence
  digests supporting a semantic migration;
- a bounded `request_body_extensions` JSON object;
- reasoning, temperature, truncation, retry, timeout, replay, storage and usage settings; and
- qualification time plus the digest of the exact profile bytes.

The runtime has no provider enum and no provider-specific credential branch. `./bench` loads the
selected profile, exports its API root, and reads only the generic credential channel. The model
loader, production factory and qualification probe consume the same parsed profile object.

An empty `request_body_extensions` object means the endpoint accepts the base Responses request.
Non-empty extensions are data, not a runtime branch. The loader accepts only JSON values under
fixed limits of four nested levels, 64 aggregate collection items and 4,096 canonical UTF-8 bytes.
Keys and strings must be bounded publishable identifiers and cannot look credential-bearing.
Non-finite numbers and request-owned top-level fields are refused.

The request-owned fields include the model, input, tools, reasoning, stream, storage, temperature,
truncation, output limit, API key and extension container. The qualification probe and production
path each take a deep copy of the validated extension object. At the final HTTP boundary, the
production adapter verifies the exact profile URL and model and refuses any competing top-level
extension before inserting the reviewed fields.

The active profile filenames are model-oriented. A compatible new endpoint or routed model is added
by creating and qualifying a profile; no Python or shell provider branch is added. A different wire
protocol still requires an explicit reviewed implementation.

## Compatibility

Schema `9` is intentionally not backward-compatible with provider-specific profile shapes. Silent
dual parsing would let one claimed schema represent two different request contracts.

Existing result JSON and generated reports are not migrated or rewritten. Their historical profile
IDs and digests remain evidence under the source revision and profile bytes that produced them. New
runs use the active model-oriented aliases and schema `9` digests. Every migrated profile names its
schema-8 source digest and finalized qualification-evidence digest; it does not claim that the new
bytes themselves were sent during the earlier request. A profile first qualified under schema `9`
uses `direct-evidence-v1`; the finalized evidence binds the profile digest without creating a
circular evidence hash inside the profile.

## Consequences

- Switching between supported OpenAI-compatible deployments is a profile and environment change.
- Endpoint, model and request settings remain reviewable and digest-bound rather than ambient.
- All active scored profiles share one credential variable, so separate simultaneous credentials
  require separate operator environments rather than hidden fallback order.
- Provider-specific request fields can be represented without widening the trusted runtime surface.
- Unsupported protocols, unsafe extensions, unknown credentials and endpoint/model drift fail before
  an external request.
