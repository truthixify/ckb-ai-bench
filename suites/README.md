# suites/

The versioned Suite registries (ADR-0008). Each suite is an immutable, git-tagged directory:
a `manifest.json` (index + ordered Task list + suite-level pins) plus one directory per Task
(prompt fragment, score, verifier spec, param schema). Frozen via `ckbbench.suite.freeze`.

## Historical shared-session suite: `ckb-v1/` at `3.0.0`

Exactly **five scored Tasks totalling 100 points**, in this order:

| Task | Points | Capability |
| --- | --- | --- |
| `task-01-tip` | 10 | chain read bound to the run's own tip |
| `task-04-send-tx` | 25 | construct, sign, and broadcast a transaction |
| `task-06-sudt-script` | 10 | identify a canonical mainnet type script |
| `task-08-type-id-data-cell` | 25 | derive Type-ID args and deploy a data cell |
| `task-05-hashlock` | 30 | author and build a RISC-V lock script binary, graded under `ckb-testtool` |

The controller releases tasks one at a time in this exact proof-before-next-task order. Later task
text and parameter files are absent until the preceding proof exists. Hashlock is last so the
long-running code task cannot consume the shared cell budget before the other four independent
tasks have produced their proofs.

Every Task is scored; there are no placeholder scaffolds. Storage shape (Task directories) is
deliberately different from delivery shape (one staged agent session) - see ADR-0008.

The agent and verifier are separate images with different contents, so the manifest pins them
independently as `agent_image_digest` and `verifier_image_digest`. Each is an exact local Docker
image ID (`sha256:` + 64 lowercase hex) passed to Docker verbatim. `CKBBENCH_AGENT_IMAGE` and
`CKBBENCH_VERIFIER_IMAGE` remain runtime overrides and take precedence.

## Versioning policy

Each registry directory is stable within its execution model; `suite_semver` in the manifest is the
identity that distinguishes incompatible snapshots, and it is what result rows and freeze hashes
record.

- **Major bump** - required for any change to the task set, a task identity, a verifier contract,
  the maximum score, or the task-delivery order. `1.0.0` -> `2.0.0` retired `task-02-epoch`,
  `task-03-blockhash`, and `task-07-spore-script` and moved the maximum from 130 to 100.
- `2.0.0` -> `3.0.0` keeps the same tasks and scores but replaces discretionary scheduling with
  the fixed order above. The two versions must never be combined in one comparison.
- **Minor bump** - additive, non-breaking metadata only.

Results produced under a previous `suite_semver` remain valid under their own stored version and
freeze hash. They are never migrated or rewritten.

## Independent-attempt suite: `ckb-independent-v1/` at `4.0.0`

The independent-attempt registry retains the same five Tasks, scores, authored prompts and
verifiers, but gives every Task its own immutable execution contract. Each contract fixes the chain
track, B/C-symmetric agent budget, harness deadlines, treatment requirement, resource policy and
whole-Task retry policy. On-chain Tasks use the pinned TestNet profile; documentation lookup and
code compilation remain local and hermetic.

The controller executes one Task per clean attempt. A difficult or failed Task cannot consume
another Task's budget or workspace, and the campaign manifest derives its Task limits, scores,
chains and requirements from this release rather than accepting them from a runtime adapter.
ADR-0022 defines the release contract.

## Complete development suite: `ckb-core-v1/` at `5.0.0`

The current independent-attempt release expands the registry to eight Tasks while retaining a
100-point scale. It keeps two controls and two TestNet transaction tasks, then grades four local,
hermetic Rust contracts: Hashlock, a relative-since lock, a grouped-cell data guard and checked token
conservation with owner mode.

Every Task has its own immutable execution contract. All four code tasks rebuild from submitted
source and run hidden `ckb-testtool` suites. The three new hidden suites are mutation-tested against
13 known-bad binaries so accepting the canonical reference alone cannot make the gate green.
ADR-0023 defines the task semantics, weights, budgets and limitations.

## Release status

`3.0.0` remains frozen historical evidence for the shared-session runner. `4.0.0` remains the first
independent-attempt release. `5.0.0` is the current publication suite. Evidence from different suite
versions is never pooled.
