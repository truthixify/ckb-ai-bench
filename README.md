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
`3.0.0`. Production wiring includes the matrix launch CLI (`scripts/run-matrix.sh`), proxy-log
violation reader, docker runner defaults, GitHub Actions CI, and the rust hidden-suite test layer.
The controller releases those Tasks one at a time in the frozen order while keeping one continuous
agent session, so a model cannot spend the shared budget on a later Task before earlier proofs exist.

**Phase one is DevNet-only.** Scored runs use a fresh local `ckb_dev` chain, and the pinned CKB AI
endpoint contributes only its **documentation surface**: C/D may call `search_resources` and read
`ckb://docs/` resources, nothing else (ADR-0013). Every arm reads chain state, signs, submits and
confirms through the selected `CKB_RPC_URL`. Operator launch prerequisites are therefore the LLM
proxy and the pinned agent/verifier images (`CKBBENCH_AGENT_IMAGE` / `CKBBENCH_VERIFIER_IMAGE`, which
default to the suite manifest's role pins); funded TestNet keys are **not** needed for a phase-one
DevNet run. Not yet a published benchmark run.

- **[docs/HARNESS.md](docs/HARNESS.md)**: the v1 application, how it fits together and how to run it. Start here for the harness.
- **[docs/RECOMMENDATION.md](docs/RECOMMENDATION.md)**: the architecture (v3) and the *why*.
- **[docs/adr/](docs/adr/)**: the architecture decision records (the live decisions).
- **[docs/README.md](docs/README.md)** — research index (three rounds of cross-model research + adjudication).
- **[agent/README.md](agent/README.md)** — a fork of [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
  with a native MCP client added, **spike-proven end-to-end** against the live server. Upstream core is
  vendored unmodified; MCP is added in new files only.

## The core idea

The headline metric is a **condition ladder**, and the load-bearing result is the **`C − B`** delta.
The report keeps suite-perfect Pass@1 and also shows weighted and per-task effectiveness,
complete-usage tokens, agent wall time, infrastructure health, raw values and sample counts. A
chart or leaderboard headline requires the declared three scored runs per arm, equal counts,
matching seed sets and no infrastructure-excluded run. Reaching the shared step or wall-time limit
is a scored outcome: verified work remains in the comparison and the report records the stop.
Prompt-visible randomized task values are deterministically derived from each seed. The matrix runs
one seed block at a time and alternates B/C order between blocks, while every cell keeps its own
fresh DevNet and private verifier material.

Cross-model rows are descriptive rather than a controlled model ranking. All current profiles use
high reasoning, but model-supported settings may still differ in temperature, truncation and
request extensions. Within a model, B and C use the same exact profile, so that model's C minus B
comparison remains the scoped CKB AI treatment contrast.

The self-contained HTML report is generated from validated flat-JSON rows. Its `Results through`
time comes from the newest canonical run ID, so it shows a real UTC data vintage while identical
inputs still rebuild byte-for-byte. The report renders only chains backed by retained rows.

| Arm | MCP | Web research | Measures |
|---|---|---|---|
| **A** | off | no (prompt) | innate model ability (floor) |
| **B** | off | yes | value of ordinary web research |
| **C** | `docs-only-v1` | yes | **CKB AI documentation value on top of web research** ← headline |
| **D** | `docs-only-v1` | no (prompt) | curated documentation vs stale/wrong web (diagnostic slice) |

Phase one runs on **DevNet** (deterministic), with the verifier always using **direct CKB RPC,
never the MCP server** under test. The headline is scoped accordingly: *the marginal effect of the pinned CKB AI
documentation surface over ordinary web research on the frozen five-task DevNet suite* — not the
effect of the full hosted tool suite, its chain tools, its account, or its faucet (ADR-0013).

The independent-attempt campaign path uses the immutable `5.0.1` registry in
`suites/ckb-core-v2/`. It executes and grades one of eight Tasks per isolated attempt, applies a
model-neutral budget and harness deadlines per Task, and selects TestNet or local-hermetic execution
from that Task's frozen contract. Earlier shared-session and five-task releases remain unchanged.

## Layout

```
docs/        design recommendation (v3) + ADRs + the research trail that produced it
agent/       the mini-swe-agent fork + native MCP client + the passing spike
ckbbench/    the production harness package (suite / verify / run / matrix)
suites/      versioned historical and independent-Task Suite registries
containers/  agent image, verifier image, devnet sidecar, egress proxy
benchmark-output/  ignored local results, reports, smoke output, and diagnostics
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
./bench models            # list supported model profiles
./bench smoke --profile gpt-5.6-luna           # one live cell
./bench run --profile gpt-5.6-luna --arms B,C --seeds 1,2,3
./bench down              # stop services; DevNet chain state is retained
./bench reset             # down + remove the benchmark-owned DevNet chain state

scripts/test.sh --no-cov  # harness tests without the CLI
# ./bench test --docker   # also container integration proof
```

Production rows are written under `benchmark-output/results/<suite-semver>/` and the generated
report under `benchmark-output/site/`. The entire output root is local and gitignored.

DevNet state lifecycle: `down` stops the stack and keeps the chain, `reset` also removes the
benchmark-owned `ckbbench-devnet-data` volume (a same-named foreign volume is never touched).
Neither is needed between cells — every Docker DevNet cell is prepared on a freshly generated
chain automatically. `--keep` / `CKBBENCH_KEEP=1` retains per-cell debugging leftovers but does
not preserve the chain: the next cell still starts fresh. See `docs/HARNESS.md` for the details.

Runtime config (RPC URLs, MCP endpoint and provider credentials) is centralized in
`ckbbench/config.py`. Copy `.env.example` to `.env`, set `CKBBENCH_LLM_API_KEY`, then select any
entry from `./bench models`. The chosen profile supplies its endpoint, exact model, protocol
settings and bounded request extensions without a code change.

## License

The vendored `agent/minisweagent/` is MIT (upstream mini-swe-agent — see
`agent/MINISWEAGENT_LICENSE.md` and `agent/UPSTREAM_COMMIT.txt`).
