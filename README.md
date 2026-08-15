# CKB AI Bench

A benchmark suite for measuring whether the **CKB AI MCP server** (the "CKB AI" Model Context Protocol
server for Nervos CKB development) measurably improves an AI coding agent — and by how much.

Loosely inspired by [DeepSWE](https://deepswe.datacurve.ai), but organized as a **versioned leaderboard**:
each suite version freezes its tasks, prompts, and verifiers, and scores a matrix of
**model × chain × condition** with **Pass@1 + wall-time + tokens (cost)**.

## Status

**Production v1 harness built** (the `ckbbench/` package): the full pipeline runs a matrix cell
end to end and renders the static report. An earlier end-to-end run against the live MCP server was
verified by direct testnet RPC; that is **historical spike evidence**, not how phase one now runs.
The v1 suite ships **5 scored Tasks totalling 100 points** in `suites/ckb-v1/`, at manifest identity
`2.0.0`. Production wiring includes the matrix launch CLI (`scripts/run-matrix.sh`), proxy-log
violation reader, docker runner defaults, GitHub Actions CI, and the rust hidden-suite test layer.

**Phase one is DevNet-only.** Scored runs use a fresh local `ckb_dev` chain, and the pinned CKB AI
endpoint contributes only its **documentation surface**: C/D may call `search_resources` and read
`ckb://docs/` resources, nothing else (ADR-0013). Every arm reads chain state, signs, submits and
confirms through the selected `CKB_RPC_URL`. Operator launch prerequisites are therefore the LLM
proxy and the pinned agent/verifier images (`CKBBENCH_AGENT_IMAGE` / `CKBBENCH_VERIFIER_IMAGE`, which
default to the suite manifest's role pins); funded TestNet keys are **not** needed for a phase-one
DevNet run. Not yet a published benchmark run.

- **[docs/HARNESS.md](docs/HARNESS.md)**: the v1 application, how it fits together and how to run it. Start here for the harness.
- **[docs/RECOMMENDATION.md](docs/RECOMMENDATION.md)**: the architecture (v3) and the *why*.
- **[docs/adr/](docs/adr/)**: the 13 ADRs (the live decisions).
- **[docs/README.md](docs/README.md)** — research index (three rounds of cross-model research + adjudication).
- **[agent/README.md](agent/README.md)** — a fork of [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
  with a native MCP client added, **spike-proven end-to-end** against the live server. Upstream core is
  vendored unmodified; MCP is added in new files only.

## The core idea

The headline metric is a **condition ladder**, and the load-bearing result is the **`C − B`** delta:

| Arm | MCP | Web research | Measures |
|---|---|---|---|
| **A** | off | no (prompt) | innate model ability (floor) |
| **B** | off | yes | value of ordinary web research |
| **C** | `docs-only-v1` | yes | **CKB AI documentation value on top of web research** ← headline |
| **D** | `docs-only-v1` | no (prompt) | curated documentation vs stale/wrong web (diagnostic slice) |

Phase one runs on **DevNet** (deterministic) only, with the verifier always using **direct CKB RPC,
never the MCP server** under test. The design also allows TestNet scoring; that is not part of the
phase-one cut. The headline is scoped accordingly: *the marginal effect of the pinned CKB AI
documentation surface over ordinary web research on the frozen five-task DevNet suite* — not the
effect of the full hosted tool suite, its chain tools, its account, or its faucet (ADR-0013).

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
cd agent && uv venv --python 3.12.8 .venv \
  && uv pip install --python .venv/bin/python -r spike-requirements.txt \
  && uv pip install --python .venv/bin/python -e "..[dev]"
cd ..

# preferred operator path (./bench → scripts/ckbbench)
./bench setup
./bench test              # harness unit tests
./bench up                # proxy + devnet (+ image build)
./bench status
./bench smoke --model grok-composer-2.5-fast   # one live cell
./bench down              # stop services; DevNet chain state is retained
./bench reset             # down + remove the benchmark-owned DevNet chain state

scripts/test.sh --no-cov  # harness tests without the CLI
# ./bench test --docker   # also container integration proof
```

DevNet state lifecycle: `down` stops the stack and keeps the chain, `reset` also removes the
benchmark-owned `ckbbench-devnet-data` volume (a same-named foreign volume is never touched).
Neither is needed between cells — every Docker DevNet cell is prepared on a freshly generated
chain automatically. `--keep` / `CKBBENCH_KEEP=1` retains per-cell debugging leftovers but does
not preserve the chain: the next cell still starts fresh. See `docs/HARNESS.md` for the details.

Runtime config (RPC URLs, MCP endpoint, LLM proxy) is centralized in `ckbbench/config.py`;
copy `.env.example` to `.env` to retarget a run without editing code.

## License

The vendored `agent/minisweagent/` is MIT (upstream mini-swe-agent — see
`agent/MINISWEAGENT_LICENSE.md` and `agent/UPSTREAM_COMMIT.txt`).
