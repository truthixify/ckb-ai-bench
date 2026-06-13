# Spike: MCP version pin enforced at preflight - FINDINGS (2026-06-12)

Goal (ADR-0010): prove the harness can, at preflight, call the live MCP server's JSON-RPC
`initialize`, read `result.serverInfo.version`, compare it to a PINNED version, and HARD-FAIL
(nonzero exit, clear message) on mismatch. Also confirm the server's deferred-tool-loading signal
(`search_tools` / `search_resources` are the always-on discovery tools). Run LIVE against the real
shared endpoint. Dependency-free node 22 (`global fetch`), no new package.json deps.

## Endpoint and transport (verified live)

- Endpoint: `https://mcp.ckbdev.com/ckbai`, HTTP JSON-RPC, no auth, Streamable HTTP transport.
- The transport answers `text/event-stream`. The request MUST send
  `Accept: application/json, text/event-stream`, without it the server returns **HTTP 406**
  (verified). The checker always sends this header.
- The response is SSE-framed: the JSON-RPC payload arrives on `data:` line(s). The checker strips
  the `data:` prefix and reassembles per-event before `JSON.parse`. A bare JSON body is also handled.
- The server is **stateless**: no `mcp-session-id` header is issued and `tools/list` answers without
  a prior session. We still send `notifications/initialized` after `initialize` to stay spec-correct;
  it is best-effort and not required by this server.

## What was done (live)

1. `mcp-preflight.mjs` POSTs `initialize` (`protocolVersion: 2024-11-05`), parses the SSE frame, and
   extracts `result.serverInfo.version`.
2. Hard gate: if `version !== PINNED` it prints a clear refusal and exits **2**. On match it exits 0.
3. Tool-surface: POSTs `tools/list`, confirms `search_tools` and `search_resources` are present, and
   reports the total tool count. It also checks the `initialize` response `instructions` field, which
   documents the deferred-loading contract.
4. `run-spike.sh` asserts every case by EXIT CODE captured directly via `$?` (no pipes, avoids the
   known `tail`/pipe exit-code-masking bug), and ends with `[ "$fail" -eq 0 ]`.

## Live observations

- `serverInfo.name`: **`ckb-ai-mcp`**
- `serverInfo.version`: **`1.6.12`** (the pinned value)
- Negotiated `protocolVersion` in the response: `2025-06-18` (we request `2024-11-05`; the server
  upshifts. The handshake still succeeds and `serverInfo.version` is unaffected.)
- `tools/list` returns **51 tools**, last two being `search_tools` and `search_resources`.
- `initialize.instructions` literally states: *"This server uses deferred loading. The 'search_tools'
  and 'search_resources' tools are always available ... Other tools are loaded on-demand when invoked."*

### How deferred-tool-loading actually manifests (important nuance)

The raw `tools/list` RPC returns the **full 51-tool catalog**, including the two always-on tools, it
does NOT hide the deferred ones. "Deferred loading" here is a **client-facing contract declared in the
`initialize` instructions**, not a truncation of `tools/list`: an MCP client is told to surface only
`search_tools` / `search_resources` up front and load the other ~49 on demand when invoked. So the
reliable deferred-loading signals are (a) `search_tools` + `search_resources` present in the catalog,
and (b) the deferred-loading statement in `initialize.instructions`. The checker verifies both. This
matches ADR-0010's note that a full-catalog `mcp_tools_digest` is less straightforward; the version pin
is the primary guard.

## Results (live, `bash run-spike.sh`)

| # | Case | Pin / target | Expected exit | Got |
|---|---|---|---|---|
| 1 | matching version passes | `1.6.12` | 0 | 0 OK |
| 2 | mismatched version refuses (ADR-0010) | `9.9.9` | 2 | 2 OK |
| 3 | deferred-loading tools present | (reports 51, both present) | 0 | 0 OK |
| 4 | unreachable endpoint refuses | bad host | 3 | 3 OK |
| 5 | env-configured preflight passes | `MCP_URL`/`MCP_PINNED_VERSION` | 0 | 0 OK |

`RESULT: 5/5 passed, 0 failed`, script exits 0.

Checker exit codes are distinct on purpose so callers assert the REASON, not merely nonzero:
**0** match, **2** version mismatch (the ADR-0010 refusal), **3** transport/handshake failure,
**4** bad usage (missing URL or pin).

## The decisive result

The FAIL case fails for the RIGHT reason: with pin `9.9.9` the server still reports `1.6.12`, the
checker prints `MCP version mismatch: server reports "1.6.12", suite pins "9.9.9". Refusing to score
against the wrong server.` and exits 2. It is not an accidental failure, case 1 with pin `1.6.12`
passes against the same live server. Sabotaging a check (forcing an impossible expected code) makes
`run-spike.sh` exit nonzero and report `4/5`, so the "all passed" assertion is real, not decorative.

## Conclusions

1. `initialize` alone gives `serverInfo.version`; the pin is enforceable with **zero extra round-trips**
   on the path the harness already runs for MCP-enabled runs (ADR-0010).
2. The same env-configurable code serves real preflight (`1.6.12`) and the negative test (`9.9.9`),
   so the guard is exercised, not just written.
3. Deferred loading is a declared client contract, not a `tools/list` truncation; pin the **version**,
   treat any tools digest as a secondary best-effort signal.

## Residual / tracked caveats (brutal honesty)

- **Shared endpoint, not the deployed pinned instance.** This proves the mechanism against the live
  shared `mcp.ckbdev.com/ckbai`, which today happens to report `1.6.12`. The ADR's real guarantee is
  that scored suites run against an instance WE deploy and pin; the harness must point `MCP_URL` at
  that pinned instance (the env hooks make this trivial). Not provable here without that instance.
- **Protocol-version upshift to `2025-06-18`** is the server's choice; we did not pin the protocol
  version, only `serverInfo.version`. If a future server drops the `2024-11-05` request it could
  change handshake behavior. Out of scope for ADR-0010 but worth tracking.
- **Tools digest not pinned.** Per the nuance above, only the version is enforced; a full-catalog
  digest is left as a secondary signal (matches the ADR consequence).
- **Stateless server today.** The checker tolerates a session-ful server (it sends
  `notifications/initialized`) but only the stateless behavior was observed live.

## Reproduce

```
cd spikes/mcp-preflight
bash run-spike.sh                                   # live, exit 0 = 5/5

# direct checker (argv: URL, pinned version):
node mcp-preflight.mjs https://mcp.ckbdev.com/ckbai 1.6.12   # exit 0
node mcp-preflight.mjs https://mcp.ckbdev.com/ckbai 9.9.9    # exit 2 (mismatch)

# or via env (same code path the real harness uses):
MCP_URL=https://mcp.ckbdev.com/ckbai MCP_PINNED_VERSION=1.6.12 node mcp-preflight.mjs
```
