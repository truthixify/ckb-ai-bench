# CKB AI Bench

A versioned benchmark suite that measures whether the CKB AI MCP server measurably improves an AI
coding agent at Nervos CKB development. This glossary fixes the language the suite is built and
discussed in. It is a glossary only, not a spec; version-specific behavior is fixed by the
applicable suite contract and ADRs.

## Language

**Task**:
The atomic unit of the suite: a prompt (what to do + where to write the Proof), a score amount (its
weight), and a verifier executable (the program the harness runs to grade it). A legacy matrix cell
delivers several Tasks to one agent. A campaign attempt delivers exactly one Task to one fresh agent.
_Avoid_: problem, challenge, question, test (reserve "test" for the verifier's hidden suite).

**Task attempt**:
The campaign evidence unit: one execution of one frozen Task under one complete experimental
identity, with its own agent, workspace, parameters, resource journal, grade and cleanup boundary.
_Avoid_: run (when task-attempt scope matters), cell.

**Trial**:
A predeclared comparison slot pairing matching B and C Task attempts. It is a repetition label, not a
claim that provider sampling was seeded.
_Avoid_: seed, retry.

**Campaign manifest**:
The immutable declaration frozen before accepted execution that names every Task, arm, trial, model
variant, execution order, retry ceiling and stopping rule that may enter one accepted report.
_Avoid_: results list, report selection.

**Attempt envelope**:
The complete append-only task-attempt record: attempt intent, ownership journal, result, and cleanup or
reconciliation receipt chain.
_Avoid_: result file (when referring to the complete evidence).

**Verifier executable**:
The self-contained program a Task carries that the harness runs automatically to grade it: it reads
the Proof at the known path and returns pass/fail. Its language is per-Task: lightweight TypeScript/
Node for most checks, a Rust suite for CKB-VM contract validation. It is the per-Task body of the
Verifier.
_Avoid_: grader script, check script.

**Score amount**:
The weight a single Task contributes to the applicable cell or trial-level suite score.
_Avoid_: points, weight (in prose), value.

**Suite**:
A versioned, immutable registry of Task directories plus suite-level pins (image digest, MCP version,
chain profiles, toolchain versions). The legacy matrix scores a complete matrix cell. The campaign
runner records each Task separately and derives a suite aggregate only from a complete
campaign-manifest trial. Evidence from different suite versions is never pooled.
_Avoid_: benchmark (the whole product), test set, version.

**Composed prompt**:
The legacy matrix instructions assembled from the preamble, every Task fragment in suite order and
the postamble. The campaign runner instead creates one deterministic instructions file for one Task
attempt; it does not compose or reveal other Tasks. Both forms are hashed into their applicable
freeze.
_Avoid_: full prompt, system prompt, mega-prompt.

**Run params**:
The concrete values the harness derives before an agent starts (fresh addresses, nonce amounts and
random values). The legacy matrix derives them per matrix cell; the campaign runner derives and
commits them per Task attempt. Signing keys remain supervisor-side and are not agent parameters.
Split into two classes.
_Avoid_: run config, fixtures, seeds.

**Prompt-injected params**:
The agent-safe subset of Run params the prompt builder renders into the applicable instructions
(recipient, amount) — the values the agent legitimately needs to do the Task.
_Avoid_: public params, task inputs.

**Verifier-private params**:
The secret subset of Run params (expected answers, integrity nonces and commitment blinding material)
held supervisor-side, never in the Mounted folder while the agent is active, and exposed only to the
Verifier after the agent has stopped. Signing keys are a separate local-signer concern and are never
agent parameters. Exposure would let the agent cheat.
_Avoid_: secrets file, answer key.

**Proof**:
The artifact a Task requires the agent to produce as evidence of completion, written to a known path
under the run or attempt output area. For on-chain Tasks the Proof is a transaction ID in a named text
file; for code Tasks the Proof is a built artifact (a contract binary). The agent is graded on the
Proof, never on its narration.
_Avoid_: answer, result, submission, output (too vague).

**Verifier**:
The component that grades a Task by reading its Proof and checking it independently. For on-chain
Tasks it validates the transaction against the chain by direct CKB RPC. For code Tasks it runs a
hidden test suite the agent never sees. The Verifier always uses direct RPC, never the MCP server.
_Avoid_: checker, grader, scorer, judge.

**On-chain Task**:
A Task whose pass criteria is a real effect on a CKB chain (a transaction that landed). The Proof is
the transaction ID; the Verifier confirms the transaction exists and has the required properties.
_Avoid_: transaction task, RPC task.

**Code Task**:
A Task whose pass criteria is a built code artifact (e.g. a smart-contract binary). The Verifier runs
a hidden test suite against the artifact off-chain. The agent never sees the suite.
_Avoid_: contract task, build task.

**Hidden suite**:
The Verifier-only test set for a Code Task. Kept out of the agent's reach so it cannot tailor output
to the tests (anti-cheat). It is never present in an agent-accessible path and is mounted read-only
into the Verifier only after the agent has stopped.
_Avoid_: test suite (ambiguous), grader tests.

**MCP as a means**:
Using an MCP tool for plumbing in service of a Task whose real subject is something else (deploying,
funding, sending a marker transaction). Allowed and intended: it is part of the MCP's value.
_Avoid_: helper use, MCP assist.

**MCP as the answer**:
Using an MCP tool to perform the very thing a Task is asking the agent to engineer (e.g. authoring a
deployment script). Disallowed; the headline Code Tasks defend against it structurally because the
Proof is the agent-authored artifact, which no MCP tool can produce.
_Avoid_: MCP cheating, tool shortcut.

**Arm**:
One condition in the experiment ladder (A floor, B web research, C MCP+web, D MCP-only) that fixes
whether the MCP is present and whether the prompt permits web research. The headline result is the
C minus B delta. A campaign's primary accepted comparison is B versus C under one frozen Task-level
treatment profile; historical A and D evidence keeps its legacy matrix meaning.
_Avoid_: condition (use for the ladder as a whole), mode, variant.

**Chain profile**:
The CKB network a run targets, scored separately. DevNet: a sidecar `nervos/ckb --chain dev` node (+
miner + in-process indexer) on its own docker network, fresh per applicable run or attempt, reached by
RPC. TestNet: a reviewed live TestNet RPC profile, reached by RPC. Both are reachable by agent and
Verifier under the frozen network policy. They are never merged into one score; campaign Tasks
default to TestNet and use DevNet only when a Task explicitly opts in.
_Avoid_: network, env (overloaded).

**Genesis account**:
A pre-funded DevNet account whose private key is public, from the `nervos/ckb` dev.toml genesis
(issued-cells). Used to fund DevNet Tasks (DevNet has no faucet). Deterministic by chainspec, so
reproducible within a suite.
_Avoid_: dev account, funded key, test account.

**Ladder chart**:
The legacy matrix site's primary reporting surface: the condition ladder (A->B->C->D) on X for one
selected model, score on Y, and chain kept separate. The B->C slope is the visible MCP value
(`C - B`). Campaign reporting retains outcome-independent B/C presentation while treating different
thinking levels as separate model variants that may be compared side by side.
_Avoid_: results graph, the chart (in prose, when ambiguous).

**Mounted folder**:
The host-bind-mounted directory shared with the agent container. During the run it holds the
agent-readable instructions and the location where Proofs are written. After the agent stops, the
harness reads the Proof from it and feeds it to the hermetic Verifier container; the Hidden suite and
Verifier-private params live with the Verifier, never in the agent's view. In a campaign the folder
belongs to exactly one Task attempt.
_Avoid_: output dir, workdir, shared volume.

**Harness tip**:
The chain tip block number captured by the supervisor at the start of the applicable run or Task
attempt. On TestNet it is the source of truth for transaction freshness: the Verifier requires every
claimed transaction to have landed on or after it. The campaign runner captures a fresh tip
independently for every on-chain Task attempt; no earlier Task supplies it.
_Avoid_: baseline (too vague), start block.

**Agent tip**:
The tip block number the agent captures itself and writes to its Proof, as a standalone skill probe.
Never used for integrity. The agent is instructed to capture it first; it passes when it is at least
the Harness tip and no more than 24 blocks ahead (an absolute window from the applicable run or
attempt start). Uniform across both chains.
_Avoid_: reported tip, claimed tip.
