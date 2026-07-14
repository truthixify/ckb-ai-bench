# CKB AI Bench

A benchmark suite for measuring whether the **CKB AI MCP server** (the "CKB AI" Model Context Protocol
server for Nervos CKB development) measurably improves an AI coding agent — and by how much.

Loosely inspired by [DeepSWE](https://deepswe.datacurve.ai), but organized as a **versioned leaderboard**:
each suite version freezes its tasks, prompts, and verifiers, and scores a matrix of
**model × chain × condition** with **Pass@1 + wall-time + tokens (cost)**.

## Status

**Production v1 harness built** (the `ckbbench/` package): the full pipeline runs a matrix cell
end to end and renders the static report, and has been **proven live** with a real model over the
LLM proxy + live MCP server, verified by direct testnet RPC (the production agent factory is
`ckbbench.run.agent_factory`). The v1 suite ships **7 scored Tasks** in `suites/ckb-v1/`.
Production wiring includes the matrix launch CLI (`scripts/run-matrix.sh`), proxy-log violation
reader, docker runner defaults, GitHub Actions CI, and the rust hidden-suite test layer.
Operator launch prerequisites: funded TestNet keys (`CKBBENCH_TESTNET_SENDER_PRIVKEY`), pinned
agent/verifier images (`CKBBENCH_AGENT_IMAGE` / `CKBBENCH_VERIFIER_IMAGE`), and a full matrix run.
Not yet a published benchmark run.

- **[docs/HARNESS.md](docs/HARNESS.md)**: the v1 application, how it fits together and how to run it. Start here for the harness.
- **[docs/RECOMMENDATION.md](docs/RECOMMENDATION.md)**: the architecture (v3) and the *why*.
- **[docs/adr/](docs/adr/)**: the 12 ADRs (the live decisions).
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
docs/        design recommendation (v3) + ADRs + the research trail that produced it
agent/       the mini-swe-agent fork + native MCP client + the passing spike
ckbbench/    the production harness package (suite / verify / run / matrix)
suites/      versioned Suite registries (the v1 task set)
containers/  agent image, verifier image, devnet sidecar, egress proxy (Phase 3)
site/        the static reporting surface (ladder chart + leaderboard)
spikes/      the proven de-risking spikes the harness is built from
```

## Develop

The harness is a Python package; tests run via one entry point.

```bash
# one-time bootstrap (creates the venv the harness + agent fork share)
cd agent && uv venv --python 3.12 .venv \
  && uv pip install --python .venv/bin/python -r spike-requirements.txt \
  && uv pip install --python .venv/bin/python -e "..[dev]"
cd ..

# preferred operator path (root entrypoint → scripts/ckbbench)
./ckb-bench setup
./ckb-bench test              # harness unit tests
./ckb-bench up                # proxy + devnet (+ image build)
./ckb-bench status
./ckb-bench smoke --model grok-composer-2.5-fast   # one live cell
./ckb-bench reset             # tear down to pristine runtime

scripts/test.sh --no-cov      # harness tests without the CLI
# ./ckb-bench test --docker   # also container integration proof
```

Runtime config (RPC URLs, MCP endpoint, LLM proxy) is centralized in `ckbbench/config.py`;
copy `.env.example` to `.env` to retarget a run without editing code.

## License

The vendored `agent/minisweagent/` is MIT (upstream mini-swe-agent — see
`agent/MINISWEAGENT_LICENSE.md` and `agent/UPSTREAM_COMMIT.txt`).
