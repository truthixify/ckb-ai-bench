# CKB AI Bench

A versioned benchmark suite that measures whether the CKB AI MCP server measurably improves an AI
coding agent at Nervos CKB development. This glossary fixes the language the suite is built and
discussed in. It is a glossary only, not a spec.

## Language

**Task**:
The atomic unit of the suite: a prompt (what to do + where to write the Proof), a score amount (its
weight), and a verifier executable (the program the harness runs to grade it). One run may contain
several Tasks, all stated up front.
_Avoid_: problem, challenge, question, test (reserve "test" for the verifier's hidden suite).

**Verifier executable**:
The self-contained program a Task carries that the harness runs automatically to grade it: it reads
the Proof at the known path and returns pass/fail. Its language is per-Task: lightweight TypeScript/
Node for most checks, a Rust suite for CKB-VM contract validation. It is the per-Task body of the
Verifier.
_Avoid_: grader script, check script.

**Score amount**:
The weight a single Task contributes to a run's score.
_Avoid_: points, weight (in prose), value.

**Suite**:
A versioned, immutable registry of Task directories plus suite-level pins (image digest, MCP version,
chain profiles, toolchain versions). Scored as a unit; never cross-ranked against another version.
_Avoid_: benchmark (the whole product), test set, version.

**Composed prompt**:
The full task instructions, assembled deterministically from a preamble (rules, arm specifics, the
`done` instruction), every Task's prompt fragment in the Suite's defined order, and a postamble. It is
written as an **instructions file in the Mounted folder**; the prompt actually injected at run-start is
a thin pointer telling the agent to read that file (so the agent can re-reference it). It is hashed
into the Suite freeze. In v1 the fragments are strictly independent: no Task references another Task's
output.
_Avoid_: full prompt, system prompt, mega-prompt.

**Run params**:
The concrete per-run values a pre-step generates from each Task's parameter schema before the agent
wakes (fresh addresses, nonce amounts, random values, private keys). Split into two classes.
_Avoid_: run config, fixtures, seeds.

**Prompt-injected params**:
The agent-safe subset of Run params the prompt builder renders into the Composed prompt (recipient,
amount) — the values the agent legitimately needs to do the Task.
_Avoid_: public params, task inputs.

**Verifier-private params**:
The secret subset of Run params (private keys, expected answers) held harness-side, never in the
Mounted folder during the run, injected into the container only at verify time after `done`. Exposure
would let the agent cheat.
_Avoid_: secrets file, answer key.

**Proof**:
The artifact a Task requires the agent to produce as evidence of completion, written to a known path
under the run's output area. For on-chain Tasks the Proof is a transaction ID in a named text file;
for code Tasks the Proof is a built artifact (a contract binary). The agent is graded on the Proof,
never on its narration.
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
to the tests (anti-cheat). Withheld from the output area until the verify stage, or present but
prompt-forbidden.
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
C minus B delta.
_Avoid_: condition (use for the ladder as a whole), mode, variant.

**Chain profile**:
The CKB network a run targets, scored separately. DevNet: a sidecar `nervos/ckb --chain dev` node (+
miner + in-process indexer) on its own docker network, pristine per run, reached by RPC. TestNet: the
live self-hosted testnet archive node, reached by RPC. Both reachable by agent and Verifier over the
network (symmetric). Never merged into one score.
_Avoid_: network, env (overloaded).

**Genesis account**:
A pre-funded DevNet account whose private key is public, from the `nervos/ckb` dev.toml genesis
(issued-cells). Used to fund DevNet Tasks (DevNet has no faucet). Deterministic by chainspec, so
reproducible within a suite.
_Avoid_: dev account, funded key, test account.

**Ladder chart**:
The site's primary reporting surface: the condition ladder (A->B->C->D) on X, one line per model
colored by family, score on Y, a confidence band on every point, chain as a toggle. The B->C slope is
the visible MCP value (`C - B`). The leaderboard is the secondary surface beneath it.
_Avoid_: results graph, the chart (in prose, when ambiguous).

**Mounted folder**:
The host-bind-mounted directory shared with the agent container. During the run it holds the
agent-readable area (the Composed prompt instructions file, and where Proofs are written). After `done`
the harness reads the Proofs from it and feeds them to the hermetic Verifier container; the Hidden suite
and Verifier-private params live with the Verifier, never in the agent's view.
_Avoid_: output dir, workdir, shared volume.

**Harness tip**:
The chain tip block number captured by the harness at run-start, as infrastructure. Captured for both
chains (the sidecar DevNet node is already up and network-reachable, like TestNet). On TestNet it is the
source of truth for transaction freshness (the Verifier requires every claimed transaction to have
landed on or after it).
_Avoid_: baseline (too vague), start block.

**Agent tip**:
The tip block number the agent captures itself and writes to its Proof, as a standalone skill probe.
Never used for integrity. The agent is instructed to capture it first; it passes when it is at least
the Harness tip and no more than 24 blocks ahead (an absolute window from run-start). Uniform across
both chains.
_Avoid_: reported tip, claimed tip.
