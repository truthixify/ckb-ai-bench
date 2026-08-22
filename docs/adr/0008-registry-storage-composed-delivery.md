# Suite stores Tasks as a registry and releases them sequentially in one session

## Context

A Suite must store Tasks and deliver them to the agent. Storage and delivery pull in different
directions: a Code Task's verifier is a Rust crate, while a shared agent budget requires a fixed task
order that cannot depend on the model voluntarily following prompt wording.

## Decision

**Storage and delivery are deliberately different shapes.**

- **Storage = a registry of Task directories.** Each Task is a directory holding its prompt fragment,
  verifier code (TS or a Rust crate), metadata, and Score amount. Freezing hashes each Task directory;
  the top-level manifest is an index plus suite-level pins (separate agent and verifier
  image IDs, MCP version, chain profiles,
  toolchain versions) and the ordered Task list. Authoring a Task = adding a directory.
- **Delivery = staged instructions in one continuous agent session.** Before the agent starts, the
  harness draws every Task's run parameters once but publishes only the first Task's prompt-safe
  parameters and prompt fragment. The injected prompt is a thin pointer to `INSTRUCTIONS.md`. When
  the current Proof path becomes a regular file inside the mount, the controller atomically replaces
  `INSTRUCTIONS.md`, publishes the next Task's parameters, and announces the release in the agent's
  next observation. Later Task prompts and parameter files do not exist in the mount before release.
  Each stage prompt and the pointer are hashed into the Suite freeze.

Because the instructions file lives on the mount during the run, the Mounted folder is partitioned
by timing: the current instructions and prompt-safe parameters are agent-readable; later prompts and
parameters remain controller-side; Verifier executables, Hidden suites and Verifier-private params
remain withheld for the whole agent run.

**Scoring stays independent per Task.** In v1, prompt fragments are **strictly independent**: no Task
may reference another Task's output. Intra-run dependencies (e.g. "use the contract Task 4 deployed")
are deferred to a later version because they reintroduce failure-cascade and raise variance.

## Consequences

The manifest's Task order controls delivery even though scoring remains independent. Proof presence
only unlocks the next stage; it does not assert correctness. The normal Verifier grades every Proof
after submission, so the controller is not a grading oracle.

Task independence does not grant scheduling freedom. Early submission is refused until every stage
has been released. Creating a future reserved Proof or parameter path is a `TaskOrderViolation`, and
one model response cannot execute additional commands after a stage transition. Suite `3.0.0` orders
the short chain read, transaction, fixed-identity lookup and Type-ID deployment before the
long-running hashlock build.
