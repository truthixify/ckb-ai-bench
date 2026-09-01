# TestNet treatment is task-scoped and signing is supervisor-controlled

> **Status: accepted.** This decision supplies concrete adapters for the network and treatment
> contracts in ADR-0015 and the preflight evidence boundary in ADR-0019. It does not authorize a
> public-chain request, transaction, model call or funded-key use.

## Context

Persistent TestNet cannot be reset between attempts. A benchmark must establish that CKB AI,
direct RPC, the signer, leased cells and deployed dependencies all refer to the intended network
before spending model tokens. Giving the agent a raw TestNet key, a faucet, an unrestricted remote
signer or the server's complete tool catalog would make the two treatment arms incomparable and
could mutate resources outside one attempt.

CKB AI discovery is also part of the measured treatment. Filtering only the rendered prompt is not
enough: a hidden tool can remain callable, a newly advertised tool can widen the treatment, and a
controller identity call can accidentally become model-visible.

## Decision

### Frozen treatment surface

Each task-scoped CKB AI profile is an exact-key canonical document. It binds the server name and
version, complete tool and resource catalog digests, model-visible tool names, resource URI
prefixes, whether the server claims a live chain, and the fixed controller-only identity tools.

Catalog order is normalized before hashing. Any membership, name, description or schema change
changes the digest. A live-chain profile must include all identity tools, but those tools cannot be
model-visible. Model-visible tools are rejected when their names or schemas expose faucets,
server-owned deployment, custody, key derivation, private signing material or transaction
submission.

One policy instance controls both discovery and dispatch. It filters the catalog shown to the
agent, validates the complete resource catalog, refuses tools and resource URIs outside the frozen
surface, and records local protocol violations. Arm B receives no MCP client. Arm C receives the
scoped client and the declared surface only.

### Independent network evidence

The direct CKB JSON-RPC transport sends canonical bounded requests over HTTP or HTTPS with no
redirects and no retries. Responses are streamed under a byte ceiling and must carry the exact
JSON-RPC success envelope. The direct probe binds the chain identifier and genesis, then verifies
that the reported tip hash is the block hash at the same tip height.

The CKB AI preflight independently initializes the server, validates both complete catalogs and
the pinned server identity, and invokes only the controller identity tools. Every method must
advance the client's request counter exactly once. A chain-aware observation binds the same chain
identifier and genesis as direct RPC and retains a coherent tip as provenance.

### Constrained signer and funding

A signer policy binds one opaque handle, public address, stable chain identity, exact leased
out-points and capacities, change and destination locks, permitted output types, cell and header
dependencies, and transaction, transfer, fee and output-data ceilings. The broker validates a
canonical unsigned transaction against that policy before asking a private key holder to sign it.
The signed transaction may change witnesses only. Submission returns only the transaction hash.

When a broker is bound, the agent factory blanks every recognized raw signer environment variable
and forwards none from the host. Both benchmark arms receive the same direct RPC and broker
capability. The private key, key path and unrestricted signer interface never enter the agent,
prompt, tool response or public evidence.

Broker-backed attempts require the isolated container environment. The host-local agent path is
refused because a shell running as the controller user is not a defensible boundary around signer
memory or host credentials. Local-hermetic tasks carry no signer and may still use the local path.

Funding preflight reads only the pre-leased out-points through direct RPC. The lease must match the
signer policy exactly. Each cell must be live, committed, sufficiently confirmed, locked to the
signer and have the capacity pinned by the policy. Preflight never generates a key, discovers a
different account, calls a faucet or refills funds.

Required TestNet deployments are read through direct RPC and hashed under the observed stable chain
identity. A local-hermetic task cannot declare a deployed chain dependency and executes no RPC,
signer or funding adapter.

### Runtime boundary

Constructing profiles, policies, adapters and an integrated preflight probe is offline. Campaign
listing, freezing and planning remain offline. Production execution continues to fail before an
attempt is created unless a complete reviewed runtime factory, model profile, treatment profile and
provisioned signer state are supplied together.

## Consequences

- Treatment drift fails before an agent or model starts.
- Controller chain checks do not silently widen the model-visible surface.
- A compromised or mistaken agent cannot sign arbitrary inputs, outputs, deployments, transfers or
  fees through the supported broker.
- B and C differ only in model-visible CKB AI access; readiness, RPC and signing capabilities stay
  matched.
- TestNet persistence is represented honestly through immutable leases and observations rather than
  a reset claim.
- Live compatibility, funding sufficiency and transaction behavior still require separately bounded
  authorization; offline tests establish adapter and refusal behavior only.
