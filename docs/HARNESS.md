# CKB AI Bench Harness (v1)

The production harness that runs the benchmark and renders its results. This is the "how it fits
together and how to run it" guide; the *why* lives in `docs/RECOMMENDATION.md` and `docs/adr/`.

## What it does

For each matrix cell `(suite, chain, arm, model, seed)` the harness:

1. **Preflights** the MCP server's pinned version (ADR-0010) on MCP arms (C/D); a mismatch is an
   `infra_fail` and the cell is not scored against the wrong server.
2. **Captures the Harness tip once** at run-start by direct RPC and feeds it to every Task's
   verifier-private params (freshness baseline; the agent's own value is never trusted).
3. **Derives randomized prompt values** once from the matrix seed and splits Run params two ways
   (ADR-0009): prompt-injected values are held until their Task is released; verifier-private values
   remain harness-side throughout. The same seed derives the same transaction amount and cell
   payload in every arm; fresh chain state and private verifier material remain cell-local.
4. **Releases one Task at a time** in manifest order by replacing `INSTRUCTIONS.md` and publishing
   only that Task's parameter file. Proof-file presence unlocks the next Task in the same agent
   session; proof correctness remains the verifier's job after submission (ADR-0008).
5. **Drives the agent** (the mini-swe-agent fork over the LLM proxy; MCP on C/D, off on A/B).
6. **Verifies** each Proof independently by **direct CKB RPC** (never the MCP): on-chain checks
   for on-chain Tasks (ADR-0001), and a hidden Rust suite in a hermetic container for Code Tasks
   (ADR-0005). On TestNet the verifier egresses through the allowlisted proxy.
7. **Classifies** the run `pass / agent_fail / infra_fail / protocol_violation` and writes a
   frozen, versioned **flat-JSON result** with the resolved agent limits, the MCP surface profile,
   the model profile and its digest, the returned model identity, the seed-derivation version, and
   the run's provider token evidence (the source of truth; ADR-0012, ADR-0013, ADR-0014).

The matrix driver runs adjacent **paired-seed blocks** across arms, alternating which treatment goes
first between blocks. This keeps `C - B` paired without systematically running every C cell hours
after every B cell. It then validates, aggregates, and renders the static report.

Campaign execution uses a parallel Task-attempt evidence foundation without changing the legacy
matrix path. `ckbbench.run.task_attempt` defines the intent, ownership journal, result and cleanup
receipt schemas; `ckbbench.run.attempt_store` publishes and validates their immutable canonical JSON
envelopes. `ckbbench.run.task_preflight` validates one reserved attempt before paid generation. It
checks exact execution inputs, recent generation-compatibility evidence, a separate non-generation
provider readiness operation, the pinned CKB AI surface, chain agreement, constrained signer,
funding, dependencies and fresh outputs through injected adapters. B and C run the same readiness
sequence; only later execution changes model-visible treatment. ADR-0018 defines attempt evidence and
ADR-0019 defines the preflight boundary.

`ckbbench.run.single_task` serializes one attempt through intent, claims, preflight, setup, one agent,
checked stop, one grade, result publication and cleanup. Results are sealed before teardown. Recovery
never resumes preflight, setup, an agent or grading; it records an interrupted infrastructure result
when needed and reconciles only journaled resources. Setup adapters must leave every planned resource
safe for cleanup even when setup is only partly successful, and cleanup adapters must be idempotent
because a process can stop after an external release but before its journal entry is published.
`ckbbench.run.campaign` freezes the accepted evidence universe before execution: ordered
batches, adjacent counterbalanced B/C slots, exact model variants, Task budgets, chain and treatment
profiles, source pins, and the executable retry and stopping-policy digests. The manifest is
canonical JSON published once under an opaque campaign ID. `ckbbench.run.campaign_operator` derives
progress only from that manifest and validated immutable attempt envelopes. A shared host lock allows
one accepted scheduler at a time. Scored outcomes continue the declared order; one completely cleaned
infrastructure failure receives its sole fresh whole-Task retry; active, corrupt, skipped or
incompletely cleaned evidence stops scheduling.

The manual accepted-resolution command requires every slot to be terminal and names every intent,
preflight-requirements file, ownership-journal entry, preflight-evidence file, result, receipt and
retry by digest. It never scans for a favorable result. Directory scans can produce only the
different, explicitly exploratory schema. Task or batch execution never rebuilds a report. The
production CLI composes the frozen release, model profile, private runtime root and optional signer
pool only for an explicitly authorized execution command. Planning, freezing, listing, report and
exploratory commands remain offline and do not construct those adapters.

```bash
./bench campaign tasks --suite suites/ckb-core-v1

# Capture one reviewed public treatment catalog before freezing a campaign. This is a bounded live
# operation and its fresh destination must not already exist.
./bench campaign capture-surfaces \
  --output-dir configs/ckb-ai-surfaces-v1 \
  --authorized-by-user

SURFACE_ROOT=configs/ckb-ai-surfaces-v1
RELEASE_ARGS=(
  --suite suites/ckb-core-v1
  --chain-profile configs/chains/local-hermetic-v1.json
  --chain-profile configs/chains/ckb-testnet-pudge-v1.json
  --treatment-profile "$SURFACE_ROOT/ckb-ai-control-local-v1.json"
  --treatment-profile "$SURFACE_ROOT/ckb-ai-treatment-local-v1.json"
  --treatment-profile "$SURFACE_ROOT/ckb-ai-control-testnet-v1.json"
  --treatment-profile "$SURFACE_ROOT/ckb-ai-treatment-testnet-v1.json"
)
./bench campaign freeze --draft campaign-draft.json --output campaign.json "${RELEASE_ARGS[@]}"
./bench campaign plan --manifest campaign.json "${RELEASE_ARGS[@]}"

# Live commands require Docker isolation and one explicit authorization. A signed campaign also
# supplies an owner-private mode-0600 signer pool outside the repository.
RUNTIME_ARGS=(
  --model-profile model-profile-id
  --private-runtime-root benchmark-output/campaigns/campaign-id/private
  --repository-root .
  --authorized-by-user
  # --signer-pool /absolute/private/path/signer-pool.json
)
CKBBENCH_DOCKER=1 ./bench campaign run-task --manifest campaign.json \
  --slot slot-id "${RELEASE_ARGS[@]}" "${RUNTIME_ARGS[@]}"
CKBBENCH_DOCKER=1 ./bench campaign run-batch --manifest campaign.json \
  --batch batch-id "${RELEASE_ARGS[@]}" "${RUNTIME_ARGS[@]}"
CKBBENCH_DOCKER=1 ./bench campaign retry --manifest campaign.json \
  --attempt attempt-id "${RELEASE_ARGS[@]}" "${RUNTIME_ARGS[@]}"
CKBBENCH_DOCKER=1 ./bench campaign recover --manifest campaign.json \
  --attempt attempt-id "${RELEASE_ARGS[@]}" "${RUNTIME_ARGS[@]}"

# A calibration is one explicitly selected, non-accepted Task attempt.
./bench campaign calibrate --manifest campaign.json --slot slot-id \
  --calibration-id calibration-00000000000000000000000000000000 \
  --attempt-root benchmark-output/calibration/attempt \
  --output benchmark-output/calibration/evidence.json \
  --authorized-by-user "${RELEASE_ARGS[@]}"

# Report resolution is always a separate operator action.
./bench campaign report --manifest campaign.json \
  --attempt-root benchmark-output/campaigns/campaign-id/attempts \
  --output benchmark-output/campaigns/campaign-id/report-resolution.json \
  "${RELEASE_ARGS[@]}"

# Static publication is a second manual action over that exact accepted resolution.
./bench campaign build-report --manifest campaign.json \
  --attempt-root benchmark-output/campaigns/campaign-id/attempts \
  --resolution benchmark-output/campaigns/campaign-id/report-resolution.json \
  --output benchmark-output/campaigns/campaign-id/site \
  "${RELEASE_ARGS[@]}"
```

The report builder refuses an incomplete or exploratory resolution, an existing destination, output
inside the immutable attempt store, and tracked source changes. It writes canonical `dataset.json`
and a self-contained `index.html`, binding both to the rendering commit and deterministic Git-tree
digest. Task correctness, infrastructure health, whole-Task retries and acquisition usage remain
separate; chain profiles, model variants and thinking levels are never pooled.

The treatment profile paths above are campaign inputs produced from one exact observed CKB AI
catalog; they are not generic placeholders the harness may infer. ADR-0020 defines the campaign and
operator boundary, ADR-0022 defines the first independent release, and ADR-0023 defines the current
eight-Task release. The legacy matrix continues
to write `RunResult` `1.8.0`.

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
suites/ckb-v1/     historical shared-session Suite registry (5 scored Tasks, 100 points, 3.0.0)
suites/ckb-core-v1/  current independent-attempt Suite registry (8 scored Tasks, 100 points)
suites/ckb-independent-v1/  immutable 5-Task independent-attempt release
benchmark-output/  local, gitignored runtime evidence
  site/            the rendered static report
  results/         per-run flat JSON, grouped by suite version
  smoke/           isolated one-cell smoke output
```

## Run it

Bootstrap and test (see the repo README for the venv setup):

```bash
scripts/test.sh                 # all wired layers (pytest + coverage), docker-free, fast
CKBBENCH_DOCKER=1 scripts/test.sh   # also build the images + the container integration proof
```

Build the report from stored results:

```bash
python -m ckbbench.matrix.build_site benchmark-output/results/3.0.0 benchmark-output/site/

# combine reviewed cohorts without rewriting their result JSON
python -m ckbbench.matrix.build_site \
  --manifest benchmark-output/report-manifest.json benchmark-output/site/
```

The build validates all rows before rendering. It derives the displayed `Results through` UTC value
from the newest canonical production run ID, preserving byte-identical rebuilds for the same input.
If no canonical timestamp exists, the page says `timestamp unavailable` rather than displaying a
fake generation time. Current rows in one results directory may reference any committed profile
under `configs/models/`; each row must match a profile's exact identity and digest. A multi-cohort
manifest combines separate result directories and names the exact tracked profile for each one.
The report keeps models as separate cohorts, provides all-model comparison tables, and never pools
different model identities into one B/C estimate. The condition-ladder chart uses a labelled model
selector and plots exactly one model at a time.

Run the full production matrix from the shell (needs the LLM proxy reachable):

```bash
# list and run a supported model configuration
./bench models
./bench run --docker -- --suite suites/ckb-v1 --profile gpt-5.6-luna

# development dry run only: --models cannot execute a real cell for the phase-one suite
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
| `CARGO_NET_OFFLINE` | `true` in every cell. Agent-side Cargo commands and the grader resolve only from their available cache instead of observing different dependency versions |

`CKBBENCH_TESTNET_SENDER_PRIVKEY` and its legacy alias `BENCH_TESTNET_SENDER_PRIVKEY` are forwarded
into the container only on a TestNet cell, and only when the host exports them, so an operator's
live-chain key never reaches a DevNet run. A local cell additionally blanks the signer names its
chain must not carry, because local execution inherits the host environment. The composed
instructions name a chain's signer variables (both TestNet aliases) so the agent can find whichever
one the operator set; no signer value is ever rendered into a prompt, a result artifact, or a
verifier-private file.

Cargo is deliberately offline before the agent starts, not only during grading. The Docker agent
and build stage use the same frozen agent image and its baked Cargo cache. This prevents a workspace
from building against a crate downloaded during the model run and then failing only because the
hermetic grader cannot download that crate. Ordinary web research remains governed separately by
the arm's egress policy.

## What is v1-complete vs deferred

**Complete and tested:** the full pipeline above, the A/B/C/D arms, both chain profiles, the
container topology (network-enforced OFF-arm isolation, hermetic verifier, docker agent egress),
the flat-JSON store + validator, the ladder chart + leaderboard, the production `agent_factory`
(`ckbbench.run.agent_factory`) wiring the fork over the LLM proxy, proxy-log violation reader,
matrix launch CLI (`scripts/run-matrix.sh`), GitHub Actions CI, and the v1 suite's **5 scored
Tasks** wired end to end.

**Proven live:** the full path has been run end to end with a real model over the live LLM proxy
and the live MCP server, verifying by direct testnet RPC: the read-only on-chain Tasks pass on the
MCP arms (C/D preflight v1.6.12, write proofs via `mcp_call`, the verifier confirms each by direct
RPC) and the static site renders from the resulting flat-JSON. That run predates the phase-one
surface decision below and is not how a scored phase-one cell now reaches the chain.

**MCP surface (ADR-0013).** Scored phase-one runs are DevNet-only, and the pinned CKB AI endpoint is
TestNet-bound, so C/D run under one fixed profile, `docs-only-v1`: exactly the `search_resources`
tool plus reserved `resources/read` calls whose URI begins `ckb://docs/`. Every other tool name —
`search_tools`, every `rpc_*`, `dev_*` and `ckb_*` tool, faucet, signing, deployment and transaction
submission — is absent from the model-visible catalog and rejected locally before any request. A and
B run under `off`: no MCP client, no MCP vocabulary, no interception. Every arm reads live chain
state, signs, submits and confirms through the selected `CKB_RPC_URL`, so the chain path is
identical on both sides of `C - B`. The configured profile is persisted as `mcp_surface_profile` in
every result and validated before aggregation or rendering. The headline is therefore scoped to *the
marginal effect of the pinned CKB AI documentation surface over ordinary web research on the frozen
five-task DevNet suite* — not to the full hosted tool suite, its chain tools, its account, its
faucet, or its deployment helpers.

The production factory gives every arm one budget — `step_limit=120`,
`cost_limit=0.0`, `wall_time_limit_seconds=1200` — so a `C - B` difference cannot be attributed to a
different ceiling. A programmatic `make_agent_factory(step_limit=N)` still applies that one value to
A, B, C and D. Each result persists the limits read from the agent's actual runtime config, and the
store validator rejects a result set whose concrete B and C budgets disagree.
A graded row that exits with `LimitsExceeded` or `TimeExceeded` keeps its raw score and task
outcomes and remains in the matched B/C comparison. The report counts the stop and shows it in the
run explorer; the fixed shared ceiling is part of the benchmark contract.

**Model profile and token evidence (ADR-0014).** An accepted phase-one run selects one tracked JSON
profile under `configs/models/`. Each profile fixes the exact requested model, safe API base,
protocol settings, bounded request extensions, `drop_params`, **zero** LiteLLM retries and at
most **four** benchmark-owned attempts per model turn. B and C receive the same immutable profile.
Only the closed transient set is retried, after fixed 4, 8 and 16 second waits; configuration,
authentication and agent failures stop immediately. Use `./bench models` to see the aliases and
their model and protocol identities.

```bash
# accepted phase-one dry run (prints the profile provenance and the cell count; sends nothing)
python -m ckbbench.matrix.launch --suite suites/ckb-v1 \
  --profile gpt-5.6-luna --arms B,C --seeds 1,2,3 --dry-run

# one smoke cell under the same profile
./bench smoke --profile gpt-5.6-luna
```

`--models` remains for development and dry runs only and cannot produce an accepted phase-one
artifact; it is mutually exclusive with `--profile`. A scored run takes its endpoint from the
selected profile, not an ambient base URL. Set `CKBBENCH_LLM_API_KEY`; keys are never rendered or
persisted.

Thinking level is part of the model variant, not a display preference (ADR-0017). The profile's
stored `reasoning_effort` is exposed as `thinking_level`, and the requested model, thinking level,
profile ID and exact profile digest derive one `model_variant_id`. Reports group, filter and compare
that variant rather than the model string alone. Different thinking levels are separate series and
never share a B/C estimate. `provider-default` and `unsupported` are explicit states; both omit the
reasoning request field instead of inventing an effort. Legacy matrix rows bind this metadata through
their exact profile ID and digest. Independent task-attempt artifacts record it directly.

The accepted phase-one wire contract is the **OpenAI Responses API** at root `/responses`
(ADR-0014, ADR-0016). LiteLLM 1.72.0 drops Responses `extra_body` before its HTTP handler, so a
narrow pinned adapter inserts non-empty profile-bound request extensions at that final boundary and
refuses URL, model or top-level collisions. The provider reports usage as `input_tokens` / `output_tokens` /
`total_tokens`; the harness keeps its long-standing public field names and maps `input`→`prompt_tokens` and
`output`→`completion_tokens` at exactly one boundary, `_read_usage()` in `agent/ckb_model.py`. Local
provider evidence under `research/handoff/` keeps the native names so the wire shape is not obscured.
The Responses conversation is stateless, so the harness sends the complete prepared history on every
request. It normalizes documented HTTP failures and HTTP-200 Responses documents with
`status: "failed"` into the same closed error taxonomy. Classification prefers a documented
top-level `error_type` and uses only allowlisted status/code fallbacks; response text, headers and
bodies never become telemetry or retry-policy inputs.

Each result records `model_profile_id`, `model_profile_sha256`, `model_response_id` and a `metrics`
block with `model_calls`, `provider_attempts`, `provider_responses`, `provider_retry_count`,
`provider_retry_delay_seconds`, `provider_failure_counts`, `prompt_tokens`, `completion_tokens`,
`total_tokens`, `token_usage_status`, `provider_failure_category`, `history_compaction_count`,
`history_dropped_groups`, `history_dropped_items` and `history_max_prepared_bytes` (result schema
`1.8.0`). The bound profile resolves the row's thinking level and model-variant ID without rewriting
the historical JSON. Schema 1.8 also records `run_params_derivation=seeded-sha256-v1`:

| Status | Meaning |
| --- | --- |
| `not_started` | no model call, attempt or response; all token fields null |
| `complete` | every attempt answered, every response carried valid usage under one model identity, and `model_calls == provider_attempts == provider_responses` |
| `incomplete` | an attempt failed, usage was missing or malformed, or the returned model drifted |

Every supported profile permits one first provider attempt plus at most three benchmark-owned recovery
attempts. LiteLLM's own retries remain zero. Only `rate_limit`, `timeout`, `connection`, `server`,
`protocol` and `other_provider` are retried. `authentication`, `authorization`, `request`,
`unsupported`, `context_window`, internal harness errors, agent errors, MCP calls, grading and whole
cells are not. If every model turn eventually receives a usable response under the pinned identity,
the cell may be graded even though its token usage is `incomplete`. Such a row contributes
correctness but is excluded from exact token and wall-time efficiency comparisons. Tokens reported
by received responses remain visible as an observed lower-bound total; the report never treats them
as the complete bill or silently substitutes zero for an unanswered attempt. An unanswered turn,
harness error or model drift remains `infra_fail`. Retry waiting is still part of the raw
`total_wall_seconds` and remains visible with `provider_retry_delay_seconds`; the current bounds are
a 300-second provider inactivity timeout and a 1200-second agent wall budget.

Every retry of one model turn reuses the same deep-copied prepared input. Before the first attempt,
the harness validates a closed Responses-history schema, removes only replay-unsafe output metadata,
and serializes the exact input. Shell and MCP observations are untrusted output, so their rendered
text is first limited to 32,768 UTF-8 bytes per turn, preserving a deterministic head and tail. If
the resulting history exceeds the profile's 131,072-byte ceiling, the
`prefix-tail-groups-v1` policy preserves the fixed instruction prefix and the newest contiguous
complete response/tool-observation groups, inserts one fixed compaction notice, and drops whole old
groups only. It never separates a function call from its output. Unknown item fields, malformed
call/output pairs, an irreducible provider response, or any profile drift fail before a provider
request. One such terminal local failure remains a valid `infra_fail` row with one more model call
than provider attempts; it cannot become scored evidence or invalidate unrelated rows. Profiles
either disable provider truncation explicitly or omit the unsupported field, while the same
deterministic local bounds apply in both cases. The four history metrics report how much local
compaction occurred without retaining conversation content.

When `provider_attempts` exceeds `provider_responses`, `provider_failure_category` names why the
unanswered attempts failed — one of `authentication`, `authorization`, `rate_limit`, `timeout`,
`connection`, `server`, `request`, `protocol`, `unsupported`, `context_window`, `other_provider`, or
`multiple` when a single run hit more than one. It is `null` whenever every attempt was answered.
Read it as triage, not as a diagnosis: `authentication` points at the key, `connection`/`timeout` at
the proxy or network, `rate_limit` at pacing, `context_window` at the task or turn budget. It is
derived from positively identified exception types or documented closed provider error tokens at the
provider boundary, never from provider text, so it carries no URL, prompt, completion or credential
and cannot be used to reconstruct the failed request.
`provider_failure_counts` gives the exact allowlisted count behind that summary;
`provider_retry_count` and `provider_retry_delay_seconds` state how much bounded recovery actually
occurred.

**Reading a cell with no scored runs:** Pass@1 has an `infra_fail`-free denominator, so a cell whose
runs all failed infrastructure has *no* Pass@1. The report shows `n/a` and plots no point or interval;
a zero there would claim a measured null effect that was never measured. The infrastructure- and
protocol-failure rates are still published for that cell, so an unusable arm stays visible rather
than disappearing.

**Correctness comparison eligibility:** a chart segment, leaderboard `C - B`, or evaluative lead signal requires
the declared publication floor: at least three scored runs per arm, equal scored counts, identical
scored-seed multisets and no infrastructure-excluded run in either arm. This is a presentation floor,
not a claim of statistical power. When it is not met, raw weighted and task differences may still
appear in the detailed tables. Exact token and wall-time C-minus-B require the same matched scored
seeds to have complete usage. Until then, the report shows observed response-token and elapsed-time
means, their descriptive difference, and provider response coverage under a separate `Observed only`
status. The report labels provisional correctness evidence
completion-conditioned, states the recorded/scored denominators beside the lead metric, and renders
the verdict `Inconclusive`. Causal interpretation still requires comparable, predeclared trials.

Report-byte reproducibility is revision-scoped. A retained site digest is reproduced with the
reporting code from the commit that produced it; an intentional renderer change can produce new
HTML bytes from unchanged result rows. Preserve the old digest as historical provenance and record
the new digest instead of treating that expected change as evidence mutation.

**Dual phase-one reporting:** the ladder keeps suite-perfect Pass@1 as its strict headline and also
publishes suite-pass counts, weighted task score, per-task pass rates, observed response-token totals
and agent wall time for B and C. The summary tables show raw values, sample counts, infrastructure
and protocol-violation health, and descriptive C-minus-B differences of arm means. Infrastructure
failures enter neither correctness nor efficiency means, so any surviving mean is explicitly marked
as conditioned on completion when exclusions exist. Incomplete usage is counted and excluded from
exact token and wall-time comparisons. Its observed response tokens, raw elapsed time, response
coverage and retry delay remain visible; observed arm differences are labelled descriptive and are
not used to infer provider billing. These descriptive deltas are not labelled as paired inference.

The generated page leads with this Phase One summary, then presents effectiveness, task outcomes,
efficiency, the full condition ladder and run health. It renders only chains with retained evidence
and never copies results across chains.

**Operator launch prerequisites (phase one, DevNet):** a reachable LLM proxy, optional
`CKBBENCH_DOCKER=1` for container-isolated agent egress, and pinned agent/verifier images when
recording a release (`CKBBENCH_AGENT_IMAGE`, `CKBBENCH_VERIFIER_IMAGE`). **No funded keys are
needed:** the send-tx Task signs with the public dev.toml genesis fixture on DevNet. A TestNet cell
is a separate, non-phase-one capability and is the only case that reads
`CKBBENCH_TESTNET_SENDER_PRIVKEY`; that key is scoped to TestNet cells and never offered to a DevNet
one. When those env vars are unset, the harness
falls back to the suite manifest's role pins (`agent_image_digest` for the agent,
`verifier_image_digest` for the verifier) for image selection. Each is an exact local image ID used
verbatim, never composed into a `name@sha256:...` repository reference. Both role pins are recorded
in the suite freeze hash for provenance.

### `./bench diagnose` — troubleshooting, not a benchmark arm

When a cell fails with `provider_failure_category: "request"` and the counts alone cannot say why,
`./bench diagnose --profile <alias>` runs **one** arm-B cell and writes
`benchmark-output/diagnostic/<run_id>.diag.json`. `--artifact-root <dir>` may override the common
output root for controlled testing.

It is deliberately rigid after profile selection: suite `3.0.0`, arm **B**, seed 1, MCP off,
local `ckb_dev`, one agent container, at most **16** provider requests and a **600-second** parent
deadline. There is no arm, seed, model, endpoint, image, retry, cleanup, MCP or ceiling override —
an override would make the diagnostic describe something other than the path that failed.

It **never grades, never writes a `RunResult`, and no report reads its output.** Diagnostic schema
`2.2.0` is bounded (16 records, 32 KiB) and content-free: per attempt it records which exception
family the call ended in, whether the pinned HTTPX handler was entered, a nullable integer HTTP
status from a positively identified LiteLLM API exception, and the structural shape of the Responses
input. It never retains a prompt, completion, command, identifier, exception message, response body,
header, request or URL.

`http_status` is the value LiteLLM carried on its exception, not an independently intercepted wire
status. It establishes a returned HTTP condition only beside `transport_state: response_seen`; on a
no-response transport failure it may be a client-assigned synthetic status and must not be read as a
server response.

The supervising parent owns the deadline, every container name and label, cleanup and publication,
because a worker killed at the deadline cannot clean up after itself. If cleanup cannot be proved,
the run publishes a fixed `instrumentation_ok: false` envelope rather than evidence.

Only the parent-supervised diagnostic overlays the agent's `<workspace>/target` and
`<workspace>/build` paths with anonymous Docker volumes. `target/` covers Cargo's default output;
`build/` covers a code task's declared `build/release/<binary>` proof. Cargo can keep its internal
hard links there, while the parent disposes both volumes through the ownership-proved
agent container ID with one `docker rm -v`. Other workspace files stay in the host bind and are
content-scrubbed. That scrub still refuses every host hard link because it cannot atomically exclude
an outside alias. Ordinary benchmark agents do not receive these diagnostic mounts.

**A live execution needs separate explicit authorization.** A successful diagnostic establishes no
task score, no treatment effect and no provider fix — it narrows *why* a request failed. Do not
repeat it until a preferred answer appears; if one run does not identify a concrete difference, the
honest outcome is that the cause is still unresolved.

**Deferred (tracked in RECOMMENDATION):** per-task token/time attribution, the MCP-provenance flag
and RPC-fallback gap table, and the family-trajectory chart.
