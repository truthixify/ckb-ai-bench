# The phase-one MCP surface is CKB AI documentation only, on DevNet

## Context

The accepted five-task suite (`2.0.0`) is scored on a fresh local `ckb_dev` chain: every Docker
DevNet cell starts from an attested chain and the verifier grades that chain by direct RPC.

The pinned CKB AI endpoint is bound to public TestNet. Its catalog includes chain-bound tools —
`rpc_*` reads, `ckb_*` helpers, `dev_*` development and faucet actions, signing, deployment and
transaction submission — that answer about TestNet.

Until now C/D could call any advertised tool name and were steered to "prefer `mcp_call` for CKB
chain work". That is a wrong-chain path: the model could receive live state from a chain the
verifier never grades, and a `C - B` difference could then reflect a chain mismatch rather than the
product. Every arm already receives the selected chain identically through `CKB_RPC_URL`, so the
direct path was always available and equal.

## Decision

Phase one keeps the endpoint's **chain-neutral documentation surface** and removes its chain-bound
surface from the treatment. C and D run under one fixed profile, `docs-only-v1`:

```text
callable MCP tool:                       search_resources
callable MCP method (reserved action):   resources/read
allowed resource URI prefix:             ckb://docs/
live-chain MCP tools:                    none
```

A and B run under `off`: no MCP client, no MCP vocabulary, no interception. The ladder's only
intended treatment difference remains CKB AI availability and steering.

`ckbbench/run/mcp_surface.py` is the single source of truth for profile names, the allowed tool set,
the resource prefix and the arm→profile mapping. The same policy object governs **both** the
model-visible catalog and every dispatch, so a tool can never be hidden from the prompt while
staying callable, or callable while hidden. C/D prompts say MCP is for documentation and reference
lookup, and that live chain state, signing, submission and confirmation go through `CKB_RPC_URL`.

`mcp_surface_profile` is persisted in every result (schema `1.2.0`), including pre-agent
`infra_fail` rows, and validated before aggregation or rendering.

## Why an exact client-side allowlist

- **Exact names, not prefixes.** A prefix rule (`rpc_*`, `dev_*`) is a denylist of today's catalog
  wearing a pattern; an unknown future tool would default to *allowed*. `docs-only-v1` contains
  exactly `search_resources`, so anything new defaults to denied.
- **`search_tools` is not exposed.** It discovers the server's deferred live catalog, none of which
  is callable here. Advertising it would invite the model down a path the policy must reject and
  spend the shared step budget doing it.
- **`resources/read` is a reserved controller action, not a catalog entry**, so it is guarded
  separately by the exact `ckb://docs/` prefix.
- **Client-side, before any request.** A rejected action makes zero MCP requests and returns an
  ordinary failed observation, so the boundary holds regardless of what the hosted server would
  have answered.

Version pinning (ADR-0010) and surface pinning are separate invariants: preflight asserts the server
is the pinned build and advertises what the profile needs; this ADR decides what the model may
reach.

## Alternatives not selected

- **Deploy a DevNet-bound CKB AI server.** Would make the chain tools measurable, but it is vendor
  infrastructure work, changes what "the pinned endpoint" means, and is outside phase one.
- **Score on TestNet instead.** Reopens the accepted DevNet decision, loses per-cell determinism and
  the attested fresh chain, and depends on faucet and public-node availability.
- **Proxy or emulate the hosted RPC tools against DevNet.** Would benchmark harness glue while
  presenting it as CKB AI behavior.
- **Keep the full tool surface and caveat the result.** The confound would still be in the data; a
  caveat cannot remove it after the fact.

## Attribution contract

The headline may be described only as:

> the marginal effect of the pinned CKB AI documentation surface over ordinary web research on the
> frozen five-task DevNet suite

It must **not** be described as the effect of CKB AI's full hosted tool suite, its live-chain RPC
tools, its TestNet account, its faucet, or its transaction/deployment helpers.

| Frozen task | Attribution role | Reason |
| --- | --- | --- |
| `task-01-tip` | chain-execution control | The answer is run-bound current DevNet state. Both B and C must obtain it from the same direct RPC; no CKB AI documentation dependency is expected. Keep it in the suite, but do not cite its individual delta as direct product evidence. |
| `task-04-send-tx` | documentation-assisted engineering | CKB AI documentation may help construct/sign a valid transaction, but submission and confirmation use the selected DevNet directly. |
| `task-05-hashlock` | documentation-assisted engineering | CKB AI documentation may help author the CKB contract while grading remains hermetic and chain-independent. |
| `task-06-sudt-script` | direct resource sentinel | The canonical identity is present in `ckb://docs/reference/token-script-hashes`; this is the clearest direct test of the product surface. |
| `task-08-type-id-data-cell` | documentation-assisted engineering | CKB AI documentation may help with Type-ID rules and cell construction, while deployment and verification use the selected DevNet directly. |

The suite-level `C - B` correctness delta is eligible under the scoped headline. Task 01 acts as a
control and may dilute the aggregate; it does not create a confound, because both arms receive the
same chain path. Per-task correctness for Tasks 04, 05, 06 and 08 may be discussed as
product-sensitive. Per-task token/time attribution remains unavailable in the composed single-agent
run and must not be invented.

## Consequences

- The measured product surface is narrower than the hosted server's, and the report must say so.
- The chain tools, faucet, signing and deployment helpers are **not measured** by phase one. Whether
  they help is an open question this benchmark does not answer.
- `search_tools` remains an observed preflight capability, never a gate and never agent-visible.
- Legacy result rows carry no `mcp_surface_profile`, so they cannot build a current report. No
  accepted benchmark dataset exists yet, so there is nothing to migrate.
- Implementing this contract makes **no effectiveness claim**. A claim still requires accepted real
  B/C results under the later model/token and execution milestones.
