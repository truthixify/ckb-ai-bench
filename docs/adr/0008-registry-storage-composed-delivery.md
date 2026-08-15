# Suite stores Tasks as a registry; delivers them as one composed prompt

## Context

A Suite must store Tasks and deliver them to the agent. Storage and delivery pull in different
directions: a Code Task's verifier is a Rust crate (cannot be inlined into JSON), while the agent is
meant to receive all instructions up front and work through them in a single pass to `done`.

## Decision

**Storage and delivery are deliberately different shapes.**

- **Storage = a registry of Task directories.** Each Task is a directory holding its prompt fragment,
  verifier code (TS or a Rust crate), metadata, and Score amount. Freezing hashes each Task directory;
  the top-level manifest is an index plus suite-level pins (separate agent and verifier
  image IDs, MCP version, chain profiles,
  toolchain versions) and the ordered Task list. Authoring a Task = adding a directory.
- **Delivery = a Composed prompt written to the mount, reached by a pointer.** At run-start the harness
  assembles preamble + every Task's prompt fragment in the manifest's order + postamble and writes it
  as an **instructions file in the Mounted folder**; the prompt actually injected is a thin pointer
  telling the agent to read that file. This is fairer than injecting a wall of text once, because the
  agent can re-reference the file later instead of relying on it staying in context. The order is
  load-bearing: it is how "do this first" (start devnet, capture the agent tip) is enforced. The
  Composed prompt is hashed into the Suite freeze, so "what the agent saw" is reproducible per arm.

Because the instructions file lives on the mount *during* the run, the Mounted folder is **partitioned
by timing**: an agent-readable area present during the run (instructions + Proof-write locations) and a
withheld area (Verifier executables, Hidden suite, Verifier-private params) that only appears after
`done`. The partition was implicit before; with an on-mount instructions file it is now an explicit
invariant.

**Scoring stays independent per Task.** In v1, prompt fragments are **strictly independent**: no Task
may reference another Task's output. Intra-run dependencies (e.g. "use the contract Task 4 deployed")
are deferred to a later version because they reintroduce failure-cascade and raise variance.

## Consequences

The manifest's Task order matters for prompt assembly even though scoring is order-independent. The
Composed prompt is a first-class authored artifact (deterministic preamble + fragments + postamble),
not naive concatenation, so it can be reviewed and hashed. Because all fragments are visible to the
agent at once, Task fragments must not leak values another Task's Verifier keys on.
