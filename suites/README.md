# suites/

The versioned Suite registries (ADR-0008). Each suite is an immutable, git-tagged directory:
a `manifest.json` (index + ordered Task list + suite-level pins) plus one directory per Task
(prompt fragment, score, verifier spec, param schema). Frozen via `ckbbench.suite.freeze`.

## Active suite: `ckb-v1/` at `3.0.0`

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

The registry directory name (`ckb-v1/`) is stable; `suite_semver` in the manifest is the identity
that distinguishes incompatible snapshots, and it is what result rows and freeze hashes record.

- **Major bump** - required for any change to the task set, a task identity, a verifier contract,
  the maximum score, or the task-delivery order. `1.0.0` -> `2.0.0` retired `task-02-epoch`,
  `task-03-blockhash`, and `task-07-spore-script` and moved the maximum from 130 to 100.
- `2.0.0` -> `3.0.0` keeps the same tasks and scores but replaces discretionary scheduling with
  the fixed order above. The two versions must never be combined in one comparison.
- **Minor bump** - additive, non-breaking metadata only.

Results produced under a previous `suite_semver` remain valid under their own stored version and
freeze hash. They are never migrated or rewritten.

## Release status

`3.0.0` is the active frozen phase-one measurement suite. Results from earlier versions remain
historical artifacts and are not part of the active report.
