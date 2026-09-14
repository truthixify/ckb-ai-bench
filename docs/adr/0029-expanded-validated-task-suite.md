# The current suite covers 25 independently verified CKB capabilities

> **Status: implementation complete; independent review pending.** This decision describes the
> new task registry. Earlier suite directories and their evidence remain immutable and must not be
> pooled with this release.

## Context

The eight-task release proves the independent-attempt lifecycle but is too narrow for a broad CKB
development benchmark. Expanding coverage is useful only when every added capability has an
executable correctness oracle. Prompt review, agent narration, source-pattern checks and manual
judgment are not sufficient grading authorities.

## Decision

The proposed `6.0.0` release is defined in `suites/ckb-core-v3`. It contains 25 tasks worth four
points each; release remains pending independent review.
The order moves from protocol basics through transaction construction and signing, TestNet state
transitions, and finally contract implementation:

| Task | Chain | Verifier | Steps | Seconds |
| --- | --- | --- | ---: | ---: |
| `task-01-tip` | TestNet | direct RPC | 40 | 600 |
| `task-address-tool` | local | native behavioral cases | 60 | 900 |
| `task-06-sudt-script` | local | deterministic identity check | 40 | 600 |
| `task-cell-query-indexer` | local | native behavioral cases | 80 | 1,200 |
| `task-transaction-balancing` | local | native behavioral cases | 100 | 1,800 |
| `task-molecule-transaction-encoding` | local | native cases plus Rust oracle | 80 | 1,200 |
| `task-sighash-witness-groups` | local | native cases plus system-lock execution | 100 | 1,800 |
| `task-multisig-transaction-builder` | local | native cases plus system-lock execution | 100 | 1,800 |
| `task-dao-withdrawal-planner` | local | native behavioral cases | 100 | 1,800 |
| `task-cell-dependency-resolver` | local | native cases plus transaction execution | 80 | 1,200 |
| `task-ccc-transaction-builder-repair` | local | native behavioral cases | 100 | 1,800 |
| `task-04-send-tx` | TestNet | direct RPC | 80 | 1,200 |
| `task-multi-recipient-transfer` | TestNet | direct RPC | 100 | 1,800 |
| `task-08-type-id-data-cell` | TestNet | direct RPC | 100 | 1,800 |
| `task-type-id-upgrade` | TestNet | direct RPC | 100 | 1,800 |
| `task-xudt-issuance` | TestNet | direct RPC | 100 | 1,800 |
| `task-xudt-transfer` | TestNet | direct RPC | 120 | 2,400 |
| `task-acp-deposit` | TestNet | direct RPC | 100 | 1,800 |
| `task-spore-creation` | TestNet | direct RPC | 120 | 2,400 |
| `task-05-hashlock` | local | CKB-VM hidden suite | 120 | 2,400 |
| `task-09-since-lock` | local | CKB-VM hidden suite | 100 | 1,800 |
| `task-10-data-guard` | local | CKB-VM hidden suite | 100 | 1,800 |
| `task-11-token-conservation` | local | CKB-VM hidden suite | 120 | 2,400 |
| `task-grouped-cell-contract-repair` | local | CKB-VM hidden suite | 120 | 2,400 |
| `task-javascript-state-transition` | local | ckb-js-vm hidden suite | 100 | 1,800 |

Equal weighting makes the normalized score the percentage of tasks passed. It deliberately avoids
claiming that subjective task difficulty can be measured precisely. Per-task results remain visible
so readers can interpret which capabilities account for a model's score.

### Verifier boundaries

Native project tasks build submitted sources in the agent image, then execute the resulting tool
with networking disabled against hidden inputs. Candidate execution sees only the built artifact,
one public case and a writable output directory. Independent Python or Rust logic validates the
result; protocol-sensitive tasks additionally execute it through the real system lock or
`ckb-testtool`.

Contract tasks compile in the agent image and run only inside CKB-VM under hidden suites in the
verifier image. TestNet tasks are graded from direct RPC observations against attempt-specific setup
state. CKB AI output, agent claims and submitted proof metadata are never correctness authorities.

Every retained reference and alternate correct implementation must pass, every focused semantic
mutant must fail, and the complete decision set must reproduce across three runs with networking
disabled. Those candidates live in an operator-held qualification bundle outside the released suite.
The suite manifest pins the bundle's complete path-and-content digest. Run-specific case values and
verifier files are excluded from candidate build and execution mounts. Verifier implementations
remain public for reproducible review, so an agent allowed ordinary web research can discover their
structure. This is a contamination limitation, not a secrecy claim.

### Chain and signing boundaries

Sixteen tasks are local-hermetic and nine use `ckb-testnet-pudge-v1`. Signed TestNet workflows use a
separate lease and the narrow signing policy named by their execution contract. Policies bind the
allowed inputs, exact output scripts and data, dependency set, capacity or token conservation, fee
ceiling and attempt-specific values before the harness signs anything. The agent receives leased
inputs, dependencies, limits and request shape; exact output acceptance constraints remain private
to the signer and verifier.

The release pins the public TestNet secp256k1 dep group and these additional deployments:

| Deployment | Code hash | Hash type | Cell dep |
| --- | --- | --- | --- |
| xUDT | `0x25c29dc317811a6f6f3985a7a9ebc4838bd388d19d0feeecf0bcd60f6c0975bb` | `type` | transaction `0xbf6fb538763efec2a70a6a3dcb7242787087e1030c4e7d86585bc63a9d337f5f`, index 0, code |
| Anyone-Can-Pay | `0x3419a1c09eb2567f6552ee7a8ecffd64155cffe0f1796e6e61ec088d740c1356` | `type` | transaction `0xec26b0f85ed839ece5f11c4c4e837ec359f5adc4420410f6453b1f6b60fb96a6`, index 0, dep group |
| Spore V2 | `0x685a60219309029d01310311dba953d67029170ca4848a4ff638e57002130a0d` | `data1` | transaction `0x5e8d2a517d50fd4bb4d01737a7952a1f1d35c8afc77240695bb569cd7d9d5a1f`, index 0, code |

The manifest also binds a digest of every dependency cell. Preflight checks those identities against
the configured chain before a paid attempt starts.

### Toolchains and feasibility

Role-image IDs, Rust, Node, the CKB Rust crates, CCC, Spore, ckb-js-vm, the JavaScript test tool and
esbuild are exact release pins. The Spore feasibility gate compares the pinned SDK's V2 deployment
and encoded data bytes with the released verifier contract. The JavaScript task compiles
deterministically to QuickJS bytecode and executes it through the pinned ckb-js-vm and Rust
`ckb-testtool` path. Neither task is admitted based on a mock-only result.

### Budgets

Each task owns its step, provider-call, wall-time, preflight, setup, grading and teardown limits.
The limits are conservative model-neutral ceilings recorded by task-specific budget evidence.
They are not pooled across tasks: reaching one task's limit produces that task's scored outcome and
does not consume another task's budget.

## Consequences

- Evidence from `6.0.0` cannot be pooled with an earlier suite version.
- A score point represents one independently completed capability rather than a fractional verifier
  criterion.
- TestNet observations depend on public infrastructure and pinned deployments; infrastructure
  failures remain unscored evidence.
- Equal task weights improve interpretability but do not make the capabilities equally difficult.
- Owner-approved budget ceilings precede paid per-model calibration and remain part of the released
  execution identity.
- Adding, removing, reordering, reweighting or changing any task, verifier, deployment, image or
  execution contract requires a new suite version and freeze.
