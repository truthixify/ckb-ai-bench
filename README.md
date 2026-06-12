# CKB AI Bench

A benchmark suite for measuring whether the **CKB AI MCP server** (the "CKB AI" Model Context Protocol
server for Nervos CKB development) measurably improves an AI coding agent — and by how much.

Loosely inspired by [DeepSWE](https://deepswe.datacurve.ai), but organized as a **versioned leaderboard**:
each suite version freezes its tasks, prompts, and verifiers, and scores a matrix of
**model × chain × condition** with **Pass@1 + wall-time + tokens (cost)**.

## Status

Design + a working agent spike. Not yet a full benchmark run.

- **[docs/RECOMMENDATION.md](docs/RECOMMENDATION.md)** — the current architecture (v3). Start here.
- **[docs/README.md](docs/README.md)** — research index (three rounds of cross-model research + adjudication).
- **[agent/README.md](agent/README.md)** — a fork of [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
  with a native MCP client added, **spike-proven end-to-end** against the live server. Upstream core is
  vendored unmodified; MCP is added in new files only.

## The core idea

The headline metric is a **condition ladder**, and the load-bearing result is the **`C − B`** delta:

| Arm | MCP | Web research | Measures |
|---|---|---|---|
| **A** | no | no (prompt) | innate model ability (floor) |
| **B** | no | yes | value of ordinary web research |
| **C** | yes | yes | **MCP value on top of web research** ← headline |
| **D** | yes | no (prompt) | curated MCP vs stale/wrong web (diagnostic slice) |

Run on both **DevNet** (deterministic) and **TestNet** (live ops), across multiple models, with the
verifier always using **direct CKB RPC, never the MCP server** under test.

## Layout

```
docs/        design recommendation (v3) + the research trail that produced it
agent/       the mini-swe-agent fork + native MCP client + the passing spike
```

## License

The vendored `agent/minisweagent/` is MIT (upstream mini-swe-agent — see
`agent/MINISWEAGENT_LICENSE.md` and `agent/UPSTREAM_COMMIT.txt`).
