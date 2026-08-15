# MCP server version is pinned and enforced at preflight

## Context

The MCP server is alpha and was reachable as a live shared endpoint, which would let it change
mid-suite and silently invalidate a "frozen" suite's headline result. We do, however, control the
server and can run a specific version.

## Decision

Scored suites run against an **MCP instance pinned to a specific version** we deploy, and the Suite
manifest records that version as `mcp_server_version`. The harness **enforces** the pin as a preflight:
the MCP handshake's `initialize` response already returns `serverInfo.version` (verified live: returns
`"1.6.12"`), and the harness asserts it equals the pinned version before scoring. A mismatch aborts or
flags the run rather than scoring against the wrong server.

This needs no special tool and no extra round-trip — `initialize()` is already called on every
MCP-enabled run.

**Version pinning and surface pinning are separate invariants.** This ADR fixes *which build* the
suite is scored against. What the model may reach on that build is decided client-side by the arm's
surface profile (ADR-0013): preflight asserts the server advertises the tools that profile needs and
records anything else it saw as an observation only. Neither invariant implies the other — a
correctly pinned server still exposes chain-bound tools that phase one does not measure.

## Consequences

Reproducibility of the C/D arms is enforced, not merely recorded. Note: the server uses deferred tool
loading (`search_tools`/`search_resources` are always on; other tools load on demand), so a
`mcp_tools_digest` pin over the full catalog is less straightforward than hashing a static list — the
version pin is the primary, reliable guard; a tools digest is a secondary, best-effort signal.
