# CKB AI Bench

A benchmark suite for measuring whether the **CKB AI MCP server** (the "CKB AI" Model Context Protocol
server for Nervos CKB development) measurably improves an AI coding agent — and by how much.

Loosely inspired by [DeepSWE](https://deepswe.datacurve.ai), but organized as a **versioned leaderboard**:
each suite version freezes its tasks, prompts, and verifiers, and scores a matrix of
**model × chain × condition** with **Pass@1 + wall-time + tokens (cost)**.

## Status

The current release is the immutable `5.0.1` registry in `suites/ckb-core-v2/`: eight scored CKB
development Tasks totalling 100 points. Every Task runs independently through setup, execution,
grading, immutable evidence publication and teardown. Task contracts select either TestNet or a
local-hermetic chain, set their own model and harness limits, and bind exact agent/verifier image,
toolchain, treatment, chain and verifier identities.

The campaign operator freezes paired B/C slots before execution, supports one declared whole-Task
infrastructure retry, and builds reports only from an explicit accepted resolution after execution.
The implementation is ready for controlled campaigns; retained diagnostic runs are not presented as
publication evidence until the campaign is complete and resolved.

- **[docs/HARNESS.md](docs/HARNESS.md)**: how the harness fits together and how to operate it.
- **[docs/SIGNER_POOL.md](docs/SIGNER_POOL.md)**: preparing and validating private TestNet signer leases.
- **[docs/RECOMMENDATION.md](docs/RECOMMENDATION.md)**: the architecture (v3) and the *why*.
- **[docs/adr/](docs/adr/)**: the architecture decision records (the live decisions).
- **[docs/README.md](docs/README.md)** — research index (three rounds of cross-model research + adjudication).
- **[agent/README.md](agent/README.md)** — the maintained [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
  fork and native MCP integration.

## The core idea

The primary result is the **`C - B`** delta for each exact model and thinking level. B receives
ordinary web research and no model-visible CKB AI. C receives the same environment plus the exact
Task-declared CKB AI surface. Reports keep correctness, complete-usage tokens, agent wall time,
infrastructure health, retries, raw attempts and sample counts separate. Reaching a declared Task
budget is a scored outcome; infrastructure failures are retained but not converted into scores.

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
| **C** | Task-declared | yes | **CKB AI value on top of web research** ← headline |
| **D** | `docs-only-v1` | no (prompt) | curated documentation vs stale/wrong web (diagnostic slice) |

The current release's treatment is exactly `search_resources` plus `ckb://docs/` resource reads.
Direct RPC and constrained signing are symmetric harness capabilities, not CKB AI treatment. The
verifier always uses independent direct RPC or a hermetic hidden suite, never the server under test.

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

# operator entry point (./bench -> scripts/ckbbench)
./bench setup
./bench test              # complete offline harness and agent test suite
./bench models            # list supported model profiles
./bench campaign tasks --suite suites/ckb-core-v2

# move the exact frozen images between compatible Docker hosts
./bench images export --suite suites/ckb-core-v2 --output /safe/path/ckbbench-images
./bench images verify --suite suites/ckb-core-v2 --bundle /safe/path/ckbbench-images
./bench images import --suite suites/ckb-core-v2 --bundle /safe/path/ckbbench-images

scripts/test.sh --no-cov  # harness tests without the CLI
# ./bench test --docker   # also container integration proof
```

Campaign intents, results, receipts, resolutions and generated reports belong under
`benchmark-output/`. They remain local unless an operator deliberately publishes the accepted
evidence bundle.

Runtime config (RPC URLs, MCP endpoint and provider credentials) is centralized in
`ckbbench/config.py`. Copy `.env.example` to `.env`, set `CKBBENCH_LLM_API_KEY`, then select any
entry from `./bench models`. The chosen profile supplies its endpoint, exact model, protocol
settings and bounded request extensions without a code change.

## License

The vendored `agent/minisweagent/` is MIT (upstream mini-swe-agent — see
`agent/MINISWEAGENT_LICENSE.md` and `agent/UPSTREAM_COMMIT.txt`).
