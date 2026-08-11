# CKB AI Bench Harness (v1)

The production harness that runs the benchmark and renders its results. This is the "how it fits
together and how to run it" guide; the *why* lives in `docs/RECOMMENDATION.md` and `docs/adr/`.

## What it does

For each matrix cell `(suite, chain, arm, model, seed)` the harness:

1. **Preflights** the MCP server's pinned version (ADR-0010) on MCP arms (C/D); a mismatch is an
   `infra_fail` and the cell is not scored against the wrong server.
2. **Captures the Harness tip once** at run-start by direct RPC and feeds it to every Task's
   verifier-private params (freshness baseline; the agent's own value is never trusted).
3. **Generates Run params** and splits them two ways (ADR-0009): prompt-injected (agent-safe:
   recipient, amount) go into the mount; verifier-private (secrets, the high-entropy nonce, the
   per-run code-task `BENCH_PASSWORD`) are held harness-side, never in the mount.
4. **Composes** the prompt (preamble + the arm preamble + ordered Task fragments + postamble),
   writes it as `INSTRUCTIONS.md` to the mount, and injects a thin pointer (ADR-0008).
5. **Drives the agent** (the mini-swe-agent fork over the LLM proxy; MCP on C/D, off on A/B).
6. **Verifies** each Proof independently by **direct CKB RPC** (never the MCP): on-chain checks
   for on-chain Tasks (ADR-0001), and a hidden Rust suite in a hermetic container for Code Tasks
   (ADR-0005). On TestNet the verifier egresses through the allowlisted proxy.
7. **Classifies** the run `pass / agent_fail / infra_fail / protocol_violation` and writes a
   frozen, versioned **flat-JSON result** with the resolved agent limits used for the cell (the
   source of truth; ADR-0012).

The matrix driver repeats this over the grid with **paired seeds** across arms (so `C - B` is
paired), then validates, aggregates, and renders the **static ladder chart + leaderboard**.

## Package layout

```
ckbbench/
  config.py        single source of truth: RPC URLs, MCP endpoint, LLM proxy, the A/B/C/D arm matrix
  ckb_rpc.py       the one direct-CKB-RPC client (used by run-params + verifier; never the MCP)
  suite/           Task/Suite model, registry loader + validation, composer, freeze, run-params
  verify/          per-task verifier: on-chain (direct RPC) + code-task (hidden suite, hermetic runner)
  run/             the orchestrator: preflight, arm config, agent driver, metrics, result schema, docker runner
  matrix/          matrix driver, ladder metrics (C-B + CI), flat-JSON store + validator, static render
containers/        agent image, hermetic verifier image, devnet sidecar, egress proxy, compose
suites/ckb-v1/     the v1 Suite registry (7 scored Tasks), frozen
site/              the rendered static report (built from results/, gitignored)
results/           per-run flat JSON (the source of truth; committed when a real run lands)
```

## Run it

Bootstrap and test (see the repo README for the venv setup):

```bash
scripts/test.sh                 # all wired layers (pytest + coverage), docker-free, fast
CKBBENCH_DOCKER=1 scripts/test.sh   # also build the images + the container integration proof
```

Build the report from stored results:

```bash
python -m ckbbench.matrix.build_site results/1.0.0 site/
```

Run the full production matrix from the shell (needs the LLM proxy reachable):

```bash
scripts/run-matrix.sh --suite suites/ckb-v1 --models model1,model2
scripts/run-matrix.sh --suite suites/ckb-v1 --models m1 --dry-run
```

Set `CKBBENCH_DOCKER=1` to wire the docker runner and proxy violation check (see
`scripts/run-matrix.sh` header for env vars).

**Cleanup (default on):** after each cell the harness removes the agent container, the
`ckbbench-work` volume, harness-owned host dirs under `ckbbench-runs/`, and per-cell
allowlist temp files; after a matrix launch it also removes `ckbbench-cargo-cache`.
Pass `--keep` or set `CKBBENCH_KEEP=1` to leave them for debugging. Compose services
(proxy/devnet) are operator-owned and are not torn down. Only `ckbbench-*` named
resources are removed.

Run the matrix (needs the LLM proxy reachable; the production agent factory is
`ckbbench.run.agent_factory.make_agent_factory`):

```python
from ckbbench.matrix.driver import MatrixGrid, run_matrix
from ckbbench.run.agent_factory import make_agent_factory
from ckbbench.suite.registry import load_suite
suite = load_suite("suites/ckb-v1")
run_matrix(suite, MatrixGrid(models=("claude-opus-4-8", "gpt-5.5")),
           registry_root="suites/ckb-v1", results_base=".", site_dir="site",
           agent_factory=make_agent_factory())   # wires the fork (CkbMcpAgent + LitellmModel) over the LLM proxy
```

`MatrixGrid(chains=None)` is the default and uses the Suite's declared `chain_profile`; pass
`chains=(...)` only for an explicitly cross-chain suite or experiment.

Regenerate the v1 suite freeze after editing tasks:

```bash
bash scripts/freeze-v1-suite.sh
```

## Configuration

All runtime endpoints are centralized in `ckbbench/config.py` and overridable by env var (each new
`CKBBENCH_*` name also honors the legacy `BENCH_*`/`MCP_*` name). Copy `.env.example` to `.env` to
retarget without code edits. These are not secrets; the DevNet genesis keys are public dev.toml
test keys (ADR-0007).

### DevNet chain-state lifecycle

Every production Docker **DevNet** cell starts from a freshly created chain. The lifecycle
controller (`ckbbench.run.devnet`) runs inside the cell, before MCP preflight, the run-start tip,
run parameters, the agent factory and any model call, so B and C cannot inherit one another's
transactions, spent inputs, indexer state or tip history. A reset failure is an `infra_fail`
artifact with no task verdicts and no agent.

| Operator action | Chain state |
| --- | --- |
| `./bench up` | starts the stack; existing state is reused |
| `./bench down` | stops services; the `ckbbench-devnet-data` volume is **retained** |
| `./bench reset` | stops services and removes the inspected, benchmark-owned state volume |
| per-cell preparation | always a fresh volume for production Docker DevNet |
| `--keep` | retains debugging resources; the **next** cell still resets |
| `--force` | skips the outer readiness preflight; it never skips per-cell preparation |

Both standalone destructive proofs (`containers/validate.sh` and the milestone's isolated evidence)
hold the shared project lock from before their inventory until after cleanup. Ownership labels prove
a volume belongs to this project but not which operation created it, so "absent at preflight" is
only durable while no other project operation can run. Each entrypoint acquires that lock itself;
there is no inherited or delegated mode, because nothing in the environment can prove possession of
a locked file description. `./bench test --docker` therefore holds no lock across its docker-free
layers.

Mutable state lives only in the `ckbbench-devnet-data` volume, labelled
`com.ckbbench.owner=ckbbench` / `com.ckbbench.role=devnet-data`; tracked configuration under
`containers/devnet/config/` is mounted read-only. A same-named volume without those labels is
foreign and is never removed. TestNet and local runs are never reset by this lifecycle.

Each managed result records `devnet_state`: lifecycle policy, `ckb_dev`, genesis hash, the
deterministic config digest, and the prepared tip. Prepared tips differ between cells by design --
the miner runs continuously -- so validation compares the immutable identity, not the tip.

### Agent-visible cell context

The harness gives every arm the same facts about the cell's chain, so a no-MCP agent never has to
guess an internal service name. These are set per cell and outrank any same-named host variable:

| Variable | Value |
| --- | --- |
| `CKBBENCH_CHAIN_PROFILE` | `devnet` or `testnet`, from the cell (not the suite default) |
| `CKB_RPC_URL` | reachable from the agent's namespace: the sidecar service name on Docker DevNet, the configured URL complete otherwise |
| `CKB_SENDER_PRIVKEY` | DevNet cells only: the public `dev.toml` genesis fixture. Published material — never fund it and never reuse it on TestNet or Mainnet |
| `CKB_SDK_HOME` | Docker cells only (an agent-image contract): the path holding the pinned offline `@ckb-ccc/core` install, which is also importable as a plain `@ckb-ccc/core` from any workspace. Local runs have no SDK path |

`CKBBENCH_TESTNET_SENDER_PRIVKEY` and its legacy alias `BENCH_TESTNET_SENDER_PRIVKEY` are forwarded
into the container only on a TestNet cell, and only when the host exports them, so an operator's
live-chain key never reaches a DevNet run. A local cell additionally blanks the signer names its
chain must not carry, because local execution inherits the host environment. The composed
instructions name a chain's signer variables (both TestNet aliases) so the agent can find whichever
one the operator set; no signer value is ever rendered into a prompt, a result artifact, or a
verifier-private file.

## What is v1-complete vs deferred

**Complete and tested:** the full pipeline above, the A/B/C/D arms, both chain profiles, the
container topology (network-enforced OFF-arm isolation, hermetic verifier, docker agent egress),
the flat-JSON store + validator, the ladder chart + leaderboard, the production `agent_factory`
(`ckbbench.run.agent_factory`) wiring the fork over the LLM proxy, proxy-log violation reader,
matrix launch CLI (`scripts/run-matrix.sh`), GitHub Actions CI, and the v1 suite's **7 scored
Tasks** wired end to end.

**Proven live:** the full path has been run end to end with a real model over the live LLM proxy
and the live MCP server, verifying by direct testnet RPC: the read-only on-chain Tasks pass on the
MCP arms (C/D preflight v1.6.12, write proofs via `mcp_call`, the verifier confirms each by direct
RPC) and the static site renders from the resulting flat-JSON. Arm isolation held live: the no-MCP
arms (A/B) get zero `mcp_call` surface and fall back to direct RPC, which is exactly the C-B signal
the ladder measures. The production factory applies arm-aware defaults (`step_limit_no_mcp=80` for
A/B, `40` for C/D); pass `step_limit` explicitly to force one budget for every arm.

**Operator launch prerequisites:** a reachable LLM proxy, optional `CKBBENCH_DOCKER=1` for
container-isolated agent egress, funded TestNet keys for the send-tx Task (via
`CKBBENCH_TESTNET_SENDER_PRIVKEY`), and pinned agent/verifier images when recording a release
(`CKBBENCH_AGENT_IMAGE`, `CKBBENCH_VERIFIER_IMAGE`). When those env vars are unset, the harness
falls back to `sha256:` digests in the suite manifest (`docker_image_digest`) for image selection;
the digest also pins the suite freeze hash for provenance.

**Deferred (tracked in RECOMMENDATION):** per-task token/time attribution, the MCP-provenance flag
and RPC-fallback gap table, and the family-trajectory chart.
