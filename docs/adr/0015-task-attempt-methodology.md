# Benchmark campaigns run and record one isolated Task attempt at a time

> **Status: accepted (2026-09-01) by project-operator approval after recorded
> self-review; no independent review is claimed.** This ADR changes the evidence and execution model
> only for campaign suites. Legacy matrix suites and result rows remain historical artifacts under the
> contracts that produced them.

## Context

The legacy matrix proved that the benchmark can run a frozen five-Task suite in Docker, grade independent
Proofs, retain provider usage and build a deterministic report. It deliberately delivered all Tasks
to one agent session and wrote one result row for that complete matrix cell. That was sufficient for
initial feasibility work, but it creates three problems for a broader benchmark:

- one difficult Task can consume the shared step or wall-time budget before later Tasks run;
- retrying one infrastructure failure requires repeating every Task and spending the associated
  model tokens again; and
- run-level tokens, timings and failures cannot be attributed cleanly to individual Tasks.

The campaign architecture also needs to exercise CKB AI against the network it serves. The legacy matrix
`docs-only-v1` surface avoided a wrong-chain comparison because its scored chain was local DevNet and
the hosted CKB AI instance was TestNet-bound. Keeping that restriction would leave the product's
chain-aware assistance unmeasured. Removing it without a network, signer and funding contract would
replace one confound with another.

The project owner therefore asked for each Task to run through setup, execution, grading, metadata
save and teardown independently; support for running or retrying one Task; preflight checks for the
model endpoint, CKB AI, RPC, network, signer and funds; thinking level as a reported variable;
generous per-Task budgets; manual report generation; and TestNet rather than DevNet where public
deployed contracts are required.

This ADR fixes those methodology boundaries before the attempt schema and executor are built.

## Decision

### 1. The evidence unit is a Task attempt

A **Task attempt** is one execution of one frozen Task under one complete experimental identity. It
owns exactly one agent process, workspace, set of prompt-safe and verifier-private parameters,
setup state, grading operation and cleanup boundary.

The surrounding units are:

- A **trial** is a predeclared comparison slot. Matching B and C Task attempts share a trial label,
  but do not share mutable resources.
- A **batch** is an operator plan that schedules independent Task attempts. It is not an agent
  session and is not an evidence row.
- A **campaign manifest** is the immutable, pre-execution declaration of every accepted batch,
  model variant, Task, arm, trial, execution order, retry rule and stopping rule in one benchmark
  campaign. It defines the complete accepted evidence universe.
- A **suite result** is a report-time aggregate over a complete declared set of Task attempts. It is
  never produced by preserving one agent process across Tasks.
- A **report manifest** is the deterministic post-execution resolution of one campaign manifest. It
  names every artifact that campaign required or produced and makes no new inclusion choice.

The campaign manifest is frozen before the first accepted preflight, provisioning action or external
request. It fixes the complete Task/arm/trial schedule, all constituent batches, execution order,
retry ceilings and stopping rules. It cannot be extended, narrowed or combined with a later campaign
after any included outcome is observed. A later campaign is separate evidence and is not pooled into
the earlier campaign's estimate. Scheduling is not adapted to observed scores. B/C order is
counterbalanced across matching trials so one arm does not systematically run first against a moving
public service.

"Frozen" means the campaign's canonical bytes are published atomically under a new opaque campaign
ID and digest and are never overwritten. Any schedule, policy or source change requires a new
campaign ID before accepted activity begins.

A campaign reaches a complete terminal state only when every planned slot has either an eligible
score or has exhausted its declared infrastructure retry with immutable terminal evidence. A frozen
global safety-stop rule may depend only on allowlisted infrastructure or isolation signals, never on
scores, usage, cost or model behavior. It may mark later slots `not_started`; those slots remain
visible and no affected B/C or suite estimate is produced. An operator cancellation or other
undeclared stop makes the campaign diagnostic-only. It cannot produce an accepted comparison or be
restarted through a new campaign that is then pooled with its partial evidence.

Accepted execution is serialized: at most one attempt in a campaign may be between resource
reservation and a terminal cleanup receipt at a time, and at most one paid agent may be active. The
operator must not run another accepted campaign concurrently on the same benchmark host, provider
credentials, RPC allocation or funding pool. The campaign manifest records this concurrency contract.
Exploratory work may use another topology only when it is labelled non-accepted and cannot enter an
accepted report.

The old `seed` field was only a repeated-trial label; it did not seed provider sampling. Campaigns
use an explicit trial or repetition identity. A provider sampling seed, when supported, is separate
provenance and must not be inferred from that label.

Every attempt has an opaque unique `attempt_id`, allocated before preflight and never reused. Its
immutable identity binds at least:

- campaign, batch, execution-plan and trial identities;
- suite, Task and Task-content digests;
- arm and treatment-profile identity;
- chain track and observed chain identity;
- model-variant and reviewed profile digests;
- Task-budget identity;
- trial-challenge, prompt/run-parameter derivation and exact prompt-safe parameter digests;
- a hiding commitment to attempt-specific verifier-private integrity values and the
  initial-resource-equivalence digest;
- agent, verifier and toolchain pins; and
- repository revision, canonical execution-source-tree digest and execution-concurrency contract.

The attempt schema may add fields, but it may not weaken these bindings or make an attempt ID
overwriteable. Before either arm in a trial starts, the supervisor publishes one
immutable trial-challenge manifest containing the arm-neutral logical challenge and equivalence
policy. Each attempt intent references that manifest and separately binds its concrete prompt-safe
parameters and verifier-private integrity values. A verifier-private commitment must use canonical
bytes plus at least 256 bits of random blinding material so low-entropy private values cannot be
recovered from a public digest. The result binds the commitment; the blinding material remains
verifier-side evidence.

### 2. Every Task attempt has an independent lifecycle

The lifecycle is fixed:

```text
derive and publish intent -> reserve and journal -> preflight -> setup -> execute -> stop agent -> grade
-> save result -> teardown -> save cleanup receipt
```

The harness supervisor, not the agent, owns the lifecycle and all deadlines.

1. **Derive and publish intent** derives concrete parameters from the trial-challenge manifest, draws
   fresh attempt-specific integrity values, stores the verifier-private bundle in supervisor-only
   durable storage, and atomically writes the immutable intent with the public parameter digest and
   hiding commitment before any resource reservation or external action.
2. **Reserve and journal** atomically claims runtime names, the signing identity and any spendable
   inputs. An append-only ownership journal records each claim before the resource can be used or a
   corresponding mutation can occur. It stores only an opaque signer handle and public address, never
   private signing material.
3. **Preflight** proves the selected dependencies and reserved resources are fit before a paid model
   generation.
4. **Setup** creates only attempt-owned workspace and chain resources and materializes only the
   prompt-safe parameters for the agent. Every acquisition or mutation is preceded by a durable
   ownership-journal entry.
5. **Execute** starts one fresh agent with only this Task's instructions and files. No prior agent
   messages, files or history are imported.
6. **Stop agent** must complete before any verifier-private value or Hidden suite is exposed.
7. **Grade** runs the Task's independent Verifier even when the agent reports failure or reaches its
   budget, provided the harness can grade safely.
8. **Save result** atomically publishes one immutable result artifact before teardown. It commits to
   the attempt intent and terminal pre-teardown ownership-journal digest and contains the observed
   grade and execution evidence, not a prediction of cleanup success.
9. **Teardown** removes only resources proven to belong to the attempt by the ownership journal and
   appends each disposition to the same hash-chained journal. TestNet effects are permanent evidence
   and are not undone.
10. **Save cleanup receipt** atomically publishes an immutable receipt linked to the attempt-intent,
    result, pre-teardown journal-prefix and terminal journal digests. It records the disposition of
    every owned resource.

The intent, append-only ownership journal, result and cleanup receipts form the complete attempt
envelope. None is overwritten. A crash before result publication is recovered as an unscored
infrastructure failure: recovery seals an infrastructure result from the durable journal and performs
cleanup, but never resumes the agent or converts the interrupted attempt into correctness evidence. A
crash after result publication preserves that result and proceeds only with teardown.

A failed cleanup receipt remains immutable. Reconciliation appends a new receipt that names the prior
receipt digest and records the remaining resources; it never replaces the failure. An attempt is
eligible for accepted comparison only when its receipt chain ends in complete cleanup, and all earlier
cleanup failures remain infrastructure-health evidence. Before another accepted attempt starts, the
supervisor must prove that no prior ownership journal in the campaign's resource domains remains
active or unreconciled.

A scored agent failure, malformed Proof or verifier rejection does not stop a batch from scheduling
the next Task after clean teardown. A failed cleanup, invalid shared preflight, operator
cancellation or another condition that makes later isolation unprovable may pause the batch.

### 3. Campaigns measure a Task-scoped, TestNet-aware CKB AI surface

The primary comparison remains B versus C:

- **B:** ordinary web research, direct RPC and benchmark-controlled signing; no MCP client, MCP
  vocabulary or callable MCP action.
- **C:** everything B receives, plus the exact CKB AI assistance surface declared for that Task.

Each frozen Task names an exact CKB AI requirement. In the current release, the local and TestNet
requirements both expose only `search_resources` and `ckb://docs/` resource reads. Direct RPC and
constrained signing remain symmetric harness capabilities outside the treatment. A later suite may
declare a broader Task-relevant surface, but doing so requires a new requirement ID and exact
tool/resource set rather than inheriting the server's unrestricted current catalog. Unknown, newly
advertised, additional or undeclared capabilities default to denied at discovery, dispatch and
release validation.

The task design continues to enforce ADR-0002:

- CKB AI may be used as a means, including chain queries and non-custodial transaction or deployment
  plumbing that supports the actual engineering task.
- It may not supply the authored artifact or hidden answer that the Task exists to measure.

Server-owned faucet access, server-owned funds, custodial accounts and signing with an identity not
controlled by the attempt are outside the scored treatment. A CKB AI tool may submit a transaction
or otherwise perform declared plumbing only when it operates on C's attempt-owned inputs and the same
class of constrained signing capability independently available to B. This prevents a C score from
measuring privileged capital or an unrecorded account rather than CKB AI assistance.

Private keys are held by a supervisor-controlled local signer outside the agent workspace and
container namespace. Raw key bytes, key-file paths and unrestricted signing interfaces are never
exposed to the model, its tool output, ordinary web access or hosted CKB AI. The frozen Task defines a
signing policy that limits the signer to the attempt's chain identity, leased inputs, permitted output
shape, maximum transfer, and a bounded fee range. A request outside that policy fails closed and is
recorded as a protocol violation. The runtime uses a fixed 100,000-shannon minimum fee and the Task's
fee reserve as the maximum. The public policy includes the exact request fields and an unsigned
transaction template with the leased inputs and dependencies already encoded. The agent supplies the
task outputs and output data in a fixed `SIGNING_REQUEST.json` workspace file, then invokes the
reserved signer action with that filename. The harness opens only that exact owner-written regular
file, refuses links
and oversized input, and passes the decoded object through the unchanged signing policy. Because a
refusal already determines the attempt's score, the first refusal stops the agent and retains only an
allowlisted failure category. A remote tool may receive public chain data or an already signed
transaction, not signing material.

Each accepted attempt receives a distinct signing identity. Once assigned, that identity is retired
from accepted attempts even if capacity remains; it cannot later become another trial's signer.
Optional capacity reclamation is a separately recorded harness operation after the attempt envelope
is complete and never changes the attempt's score, usage or chain evidence.

The controller preflights CKB AI for both B and C in a matched comparison so availability is not an
arm-specific environmental difference. Only C exposes it to the model. Each result records the
surface-profile identity and digest; public copy names the measured surface rather than claiming the
effect of every capability the service has ever exposed.

### 4. Chain choice is a Task requirement, not a global fallback

Every Task declares one execution environment:

- **TestNet** is the default for on-chain Tasks and is mandatory when the Task depends on public
  deployed contracts or chain-aware CKB AI tools.
- **Local hermetic** execution is the default for chain-independent Code Tasks. The agent may author
  code in a container and the Verifier may use `ckb-testtool` without a live chain.
- **DevNet** is an explicit opt-in for a Task whose chain state is fully self-contained and whose CKB
  AI treatment is chain-neutral. It must never silently replace TestNet when a public deployment is
  part of the Task.

One report may contain multiple chain tracks, but it never pools DevNet and TestNet observations for
the same metric. A TestNet suite aggregate may include locally graded chain-neutral Code Tasks; those
rows inherit the suite track for grouping and are labelled local in their Task provenance. A DevNet
on-chain attempt and a TestNet on-chain attempt are different evidence and cannot substitute for one
another.

For a matched B/C Task comparison, both arms use the same chain track, reviewed RPC profile, chain
identity, confirmation policy and funding policy. They receive distinct attempt-owned keys and input
cells where reuse would create contention, but those resources are generated by the same procedure
and start with equivalent usable capacity. TestNet identity is proven from direct RPC using stable
network facts such as the genesis hash and chain identifier. A chain-aware CKB AI surface must attest
the same network before either arm spends model tokens. A name such as `testnet` without matching
identity evidence is insufficient. Chain-neutral documentation surfaces record that they make no
live-chain claim.

### 5. TestNet isolation is logical, not a pretend reset

Public TestNet cannot be reset or torn down. TestNet setup therefore creates attempt-specific
resources and binds every Proof to them:

- fresh prompt-safe challenge parameters and verifier-private integrity values;
- a captured harness tip and explicit confirmation policy;
- an attempt-specific address or resource namespace where the Task needs one;
- reserved spendable input cells that no concurrent attempt may use; and
- enough nonce or freshness material to reject a stale or borrowed Proof.

Matching B/C attempts share one trial-challenge manifest and logical challenge digest. Concrete
prompt-safe values may differ only where consumable or freshness values cannot safely be reused; both
arms must derive them through the same frozen arm-neutral equivalence policy. Their results bind the
exact prompt-safe digests and hiding commitments to their attempt-specific verifier-private values.
Arm-specific keys, input cells, transaction nonces and other consumable chain objects likewise may
differ only under that policy. The initial-resource-equivalence digest commits the canonical
capacity, cell count and type, confirmation state and Task-specific properties that the policy
compares. A validator must reject a B/C pair whose trial-challenge or equivalence-policy bindings
differ, or whose concrete parameters do not satisfy that policy.

Teardown releases unused local leases and removes containers, workspaces, allowlists and secret
material. It retires the attempt signing identity from accepted reuse. It does not delete a committed
transaction and does not send a compensating transaction merely to make the chain look clean.
Permanent transaction hashes, block identities and consumed capacity remain part of the attempt
evidence.

DevNet, when explicitly selected, starts from a fresh attempt-owned generation and removes only that
generation after the result is sealed. Local hermetic Tasks create no chain state.

### 6. Preflight is a fail-closed evidence boundary

Every attempt runs a Task-derived preflight before its first paid model generation. The preflight
must prove, as applicable:

- the campaign, execution plan, attempt intent, suite, Task, model variant, budget, images and
  toolchain match their reviewed digests;
- the checked-out repository revision and canonical execution-source-tree digest match the campaign,
  with no staged or tracked execution-input drift and no untracked file inside an execution-input
  boundary;
- the selected model profile binds a recent, bounded authenticated generation-compatibility probe
  for the exact provider protocol, route and model, within the maximum evidence age frozen by the
  campaign;
- the provider profile and credentials are present and its bounded profile-defined readiness
  operation succeeds; a non-generation check proves current reachability and authentication only and
  is not misrepresented as a generation test;
- CKB AI initializes at the pinned version, exposes the required exact surface and rejects surface
  drift;
- direct RPC is reachable and returns the expected chain identity;
- CKB AI's live-chain identity matches direct RPC exactly;
- the reserved signing identity is valid, derives the expected address, is inaccessible to the agent
  and enforces the frozen Task signing policy;
- live spendable capacity meets the Task's declared maximum transfer, fee reserve and safety margin;
- the durable ownership journal proves exclusive leases on every input and runtime name the attempt
  may use;
- required deployed contracts and dependencies exist at their pinned identities; and
- output paths and runtime resource names are fresh, non-symlinked and unowned by another attempt.

An explicit provisioning operation may generate and fund a pool of single-assignment signing
identities before a benchmark. A Task run must not silently generate an unfunded key, call a faucet or
refill itself. Funding transactions and their costs are provisioning provenance, not model
performance.

Preflight requests made by the controller are recorded separately from agent usage. A failed
preflight publishes an infrastructure result with no correctness score and no agent token claim, then
releases its reservations and publishes a cleanup receipt. In a paired batch, treatment readiness is
checked before B as well as C so a B result is not collected while its matched C condition is already
known to be unavailable.

Preflight and provisioning artifacts use fixed allowlisted fields. They never retain credentials,
private signing material, raw provider or CKB AI bodies, response content, or secret-bearing URLs and
headers. Sanitization failure is itself a fail-closed infrastructure error.

### 7. Budgets are generous, per-Task, frozen and symmetric

The old whole-suite step and wall-time limits are not copied to every Task. Each frozen Task declares
one budget profile with at least its agent step limit, agent wall-time limit and any provider-call or
output constraints the selected protocol can enforce.

Task budgets are:

- calibrated using bounded, explicitly non-accepted pilots before the suite is frozen;
- generous enough for a qualified model to attempt the Task;
- model-neutral within a suite release;
- byte-identical for B and C; and
- persisted in every attempt and validated before comparison.

If a model needs a different ceiling, that run belongs to a different budget methodology and is not
combined in the same comparison. Thinking level may change model behavior, but it does not silently
change the Task budget.

Preflight, setup, grading and teardown have separate harness deadlines. They do not consume the
agent's step budget. Provider retry waiting during execution remains part of observed agent wall
time. Reaching an agent step or wall-time limit is scored model behavior, not `infra_fail`: a valid
Proof still passes, and a missing or invalid Proof fails. The stop reason remains explicit in the
result and no later Task loses budget because of it.

### 8. Provider retries and Task reruns are different events

A provider-turn retry occurs inside one Task attempt under the reviewed model profile. It remains
bounded by an exact allowlist, attempt ceiling and delay schedule, and its telemetry remains in that
attempt. If failed provider calls make billing or token usage unknowable, correctness may still be
retained when grading is trustworthy, but the attempt is excluded from exact efficiency metrics.

A whole-Task rerun always receives a new `attempt_id`, fresh workspace, fresh attempt-specific
integrity material and fresh chain resources while retaining the planned slot's trial-challenge
manifest. It links immutably to its predecessor. The accepted whole-Task policy permits exactly one
infrastructure retry per planned Task/arm/trial slot. Before campaign execution, the task-attempt
suite freezes one versioned, model-neutral whole-Task retry policy containing the retryable failure
stages and categories, conditional retry placement and cooldown. The campaign references its
canonical digest and cannot alter it; B and C use it identically. A retry is permitted only after the
predecessor has an unscored allowlisted infrastructure result and a terminal successful cleanup
receipt. Under the current stopping rule, that failure pauses the command; a later invocation checks
provider readiness before using the eligible retry. No second whole-Task retry is accepted.

- **Infrastructure retry:** the predecessor was unscored because of a frozen, allowlisted
  infrastructure failure. A retry that produces an eligible score supplies the slot's correctness
  observation; a second infrastructure failure leaves the slot unresolved. Every attempt remains
  visible in infrastructure-health and acquisition-cost evidence.
- **Additional trial:** the predecessor produced a scored pass, agent failure, budget exhaustion or
  protocol violation. The campaign may not rerun or replace it. Any further attempt belongs to a new
  campaign whose complete schedule is frozen before that campaign begins and is reported separately.

There is no implicit "latest wins" rule and no file replacement. A lineage link may not cross Task,
arm, trial, model variant, chain, treatment or budget identity. Retrying a TestNet attempt uses new
integrity parameters and spendable inputs so the prior attempt's chain effect cannot satisfy it.

Efficiency has two explicit views. **Attempt usage** describes each individual attempt.
**Acquisition usage** for a planned slot sums model tokens, provider cost and elapsed execution time
across the entire infrastructure-retry lineage, including the failed predecessor. B/C efficiency
deltas use acquisition usage, not only the successful attempt. If any lineage member lacks complete
exact usage, exact token, cost and time deltas for that matched slot are `null`; known lower bounds and
response coverage remain visible. Controller preflight and provisioning costs are reported separately
as campaign overhead and never hidden inside agent usage.

### 9. Report inclusion is explicit and outcome-aware

The following rules apply before aggregation:

- A verifier pass or fail is the correctness observation when execution and grading integrity are
  valid. Agent narration and completion wording are not correctness evidence. A valid Proof passes
  even after an agent or budget stop, unless a protocol violation invalidates the treatment.
- `agent_fail`, budget exhaustion with an invalid or missing Proof, and `protocol_violation` remain
  scored outcomes. An invalid or missing Proof scores zero; a protocol violation takes precedence
  over a verifier pass and scores zero. They are never converted to infrastructure failures to
  improve a result.
- Preflight, harness, provider-without-correctness-evidence, grading-infrastructure and unreconciled
  cleanup failures are infrastructure evidence and do not enter correctness denominators. A cleanup
  failure that is later reconciled remains in health evidence but no longer excludes the already
  sealed correctness observation.
- Every infrastructure failure remains in health denominators, including one superseded by a valid
  infrastructure retry.
- Tokens, cost and time enter an exact efficiency comparison only through complete acquisition usage
  under the selected provider contract. Known lower bounds may be displayed but not differenced as
  exact usage.
- A trial-level suite score exists only when the report manifest contains one eligible scored
  observation for every required Task. A missing or unresolved Task makes the suite score `null`,
  not zero. Task-level evidence remains reportable.
- B/C deltas require matched Task, trial, chain track, model variant, treatment contract, budget,
  prompt/run-parameter derivation, trial-challenge and resource-equivalence-policy bindings. Concrete
  parameters must validate under that shared policy. Unmatched rows remain visible but are not a
  treatment estimate.
- Different TestNet and DevNet tracks, suite versions, Task versions, thinking levels or budget
  methodologies are never pooled.

The campaign manifest is frozen before accepted work begins. Its report manifest must resolve exactly
that one campaign and every intent, journal, result, receipt and retry artifact it required or
produced. It may resolve the single declared infrastructure-retry lineage for each slot, but it may
not choose among campaigns, omit an unfavorable Task, arm or trial, drop a scored or failed attempt,
or add an opportunistic rerun.

### 10. Thinking level is part of the model variant

A task-attempt model variant is not just a requested model string. Its identity includes:

- requested model and returned-model acceptance policy;
- thinking or reasoning level, including explicit `provider-default` or `unsupported` states;
- temperature, truncation, context and other reviewed inference settings;
- provider protocol and routing contract;
- provider-turn retry policy; and
- canonical profile digest.

B and C must use the same model variant. Results from different thinking levels are separate series
that may be shown side by side, but they are not repetitions of one another and are never pooled into
one B/C estimate. The model-variant contract defines the concrete profile and display fields.

### 11. Reporting is manual and manifest-driven

Execution commands persist attempt artifacts and stop. They do not rebuild or publish the report.

An accepted report is generated later by an explicit command from the frozen campaign manifest and
an immutable resolved report manifest. The resolver has no inclusion choices: it must enumerate every
planned slot and every intent, ownership-journal entry, result, cleanup or reconciliation receipt and
retry artifact the campaign produced. The builder validates those artifacts plus every profile,
suite, Task, treatment, chain, budget and retry link before aggregation. It fails on missing, extra,
duplicate or contradictory evidence and produces deterministic output from the same campaign,
manifest and artifacts. The resolved report manifest also binds the report-builder repository
revision and canonical source-tree digest, and rendering fails if its current tracked inputs differ.

An operator may build a clearly marked exploratory preview from a results directory, but no such
directory scan is an accepted-data selection rule. Public provenance names the report manifest,
campaign and execution-plan digests, execution and report-builder revisions and source-tree digests,
and the exact attempt IDs behind every aggregate.

### 12. Legacy matrix artifacts are not migrated in place

This decision applies only to a campaign suite and attempt result schema:

- ADR-0001's freshness, nonce and structural integrity checks remain valid. Its "early Task records
  the tip" mechanism is superseded: the supervisor captures a fresh harness tip for every on-chain
  Task attempt.
- ADR-0002's MCP-as-means versus MCP-as-answer boundary remains valid and applies to every Task-level
  surface profile.
- ADR-0003's Task definition and independent Verifier remain valid.
- ADR-0004's pinned build-image and toolchain requirements remain valid per attempt.
- ADR-0005's clean hermetic Verifier container and checked post-agent secret injection remain valid
  per attempt.
- ADR-0006's egress observation and arm-specific enforcement remain valid; Task-scoped allowlists are
  derived from the frozen Task, chain and treatment profiles.
- ADR-0007's official CKB DevNet sidecar remains valid only for Tasks that explicitly opt into
  DevNet. It is not the campaign on-chain default or a TestNet substitute.
- ADR-0008's Task registry remains useful, but its continuous multi-Task agent session and staged
  delivery are superseded for campaign execution.
- ADR-0009's prompt-safe and verifier-private parameter split remains valid per Task attempt.
- ADR-0010's MCP version pin remains valid and is extended by Task-level surface and network pins.
- ADR-0011's outcome-independent reporting and separate chain/model views remain valid. Its legacy
  matrix presentation is extended so the reporting layer may compare thinking-level variants of the same requested model
  side by side while keeping them separate series.
- ADR-0012's immutable flat JSON and deterministic static reporting remain valid; its one-row-per-cell
  unit, seed identity, results-directory selection and automatic post-matrix rendering are superseded
  for campaign execution.
- ADR-0013 remains the exact legacy matrix DevNet treatment contract. Its DevNet default and
  `docs-only-v1` restriction are superseded for campaign execution.
- ADR-0014 remains legacy matrix model and token provenance. Campaign profiles extend the identity with
  first-class thinking level and per-Task evidence.

Historical legacy matrix results continue to validate only under their historical schema, suite,
profiles and renderer. They are not rewritten to resemble Task-attempt evidence.

## Consequences

- One hard or failed Task no longer consumes another Task's budget or forces the operator to rerun a
  complete suite.
- Per-Task correctness, tokens, time, provider health and CKB AI use can be attributed without
  reconstructing stage boundaries from one long agent transcript.
- The artifact set is larger: every attempt needs an intent, append-only ownership journal, result and
  cleanup-receipt chain, plus campaign, batch, preflight and report manifests.
- TestNet results are less resettable than DevNet, so integrity comes from unique attempt resources,
  funding leases, freshness proofs and explicit health classification rather than a false clean-chain
  claim.
- Exact CKB AI surface and network pinning add implementation work, but they make the treatment both
  broader than the legacy matrix's documentation slice and more defensible than exposing an unrestricted
  catalog.
- One predeclared infrastructure retry remains possible without deleting evidence, while finite
  stopping rules and campaign-level selection prevent selective reruns from quietly improving scores.
- Cross-model and cross-thinking tables remain descriptive. The controlled treatment estimate is
  within one matched model variant, Task, chain, budget and trial.
- Subsequent implementation may choose concrete field names and CLI syntax, but it must preserve
  this evidence, lifecycle and inclusion contract.
