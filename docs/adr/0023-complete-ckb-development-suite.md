# The publication suite covers eight independently graded CKB capabilities

> **Status: accepted.** This decision releases a larger task set for independent-attempt
> campaigns. It does not authorize a model, CKB AI, public-chain, signer, faucet or transaction
> action.

## Context

The first independent-attempt release preserved the five historical capabilities while changing
their execution model. That was sufficient to prove isolated setup, execution, grading, evidence
publication and teardown, but it left contract engineering represented by one lock script. A
publication benchmark needs broader deterministic coverage without making a failed contract consume
the budget of another task.

CKB exposes group-relative inputs and outputs to scripts, typed `since` values, cell data hashes and
lock-script hashes through its script syscalls. The production Simple UDT implementation also gives
an established baseline for owner authorization, little-endian `u128` amounts and checked group
totals. The new contracts use those protocol boundaries rather than application-specific APIs.

Primary references:

- [CKB transaction structure](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0022-transaction-structure/0022-transaction-structure.md)
- [ckb-std 1.1.0 high-level syscalls](https://github.com/nervosnetwork/ckb-std/blob/v1.1.0/src/high_level.rs)
- [CKB production Simple UDT](https://github.com/nervosnetwork/ckb-production-scripts/blob/master/c/simple_udt.c)

## Decision

Suite `5.0.0` is released from `suites/ckb-core-v1`. It contains eight scored tasks totalling 100
points in this immutable order:

| Task | Points | Chain | Steps | Agent seconds | Provider calls |
| --- | ---: | --- | ---: | ---: | ---: |
| `task-01-tip` | 5 | TestNet | 40 | 600 | 160 |
| `task-06-sudt-script` | 5 | local hermetic | 40 | 600 | 160 |
| `task-04-send-tx` | 15 | TestNet | 80 | 1,200 | 320 |
| `task-08-type-id-data-cell` | 15 | TestNet | 100 | 1,800 | 400 |
| `task-05-hashlock` | 15 | local hermetic | 120 | 2,400 | 480 |
| `task-09-since-lock` | 15 | local hermetic | 100 | 1,800 | 400 |
| `task-10-data-guard` | 10 | local hermetic | 100 | 1,800 | 400 |
| `task-11-token-conservation` | 20 | local hermetic | 120 | 2,400 | 480 |

The two 5-point tasks are controls. The signed TestNet tasks carry 30 points. Four hermetic contract
tasks carry 60 points. Token conservation is weighted highest because one proof must handle script
grouping, binary encoding, checked arithmetic and owner authorization.

The five retained tasks keep their prompts, verifiers and execution contracts; only their weights
and position in this new release change. Releases `3.0.0` and `4.0.0` remain separate immutable
artifacts and their evidence must never be pooled with `5.0.0`.

### Relative since lock

The script args are exactly eight little-endian bytes encoding a valid relative threshold. Every
`GroupInput` must carry a valid relative value with the same metric and a value at least the
threshold. Absolute values, invalid flag combinations, incompatible metrics, lower values and
malformed args fail. Unrelated global inputs are outside the contract.

### Data guard

The script args are exactly one expected 32-byte cell data hash. Creation has zero `GroupInput`
cells; update has one. Exactly one `GroupOutput` is required, and every participating cell must have
the expected data hash. Extra grouped inputs or outputs, missing outputs, malformed args and data
mismatches fail. Unrelated type groups are outside the contract.

### Token conservation

The script args are exactly one 32-byte owner lock-script hash. A matching input lock enables owner
mode; an output lock does not. Outside owner mode, the first 16 bytes of every `GroupInput` and
`GroupOutput` data field encode a little-endian `u128`. Checked sums permit transfer and burn and
reject unauthorized minting, short data and overflow. Unrelated type groups are not counted.

### Verifier-private challenge

New hidden suites read `CKBBENCH_CHALLENGE`. The grader supplies the same fresh value under that
generic name and the legacy `BENCH_PASSWORD` alias. Neither name enters the agent workspace or build
stage. Keeping the alias preserves the old Hashlock verifier without making a password-specific name
part of new contracts.

### Mutation boundary

Every new hidden suite accepts its canonical reference binary and rejects explicit semantic mutants:

| Contract | Rejected defects |
| --- | --- |
| Since lock | accept all; check first input only; use global inputs; compare raw numeric values |
| Data guard | accept all; use global cells; ignore input data; ignore group shape |
| Token conservation | reject burns; check first cell only; use global cells; accept owner output; wrap overflow |

The unified Rust gate uses locked dependencies and offline Cargo execution. Candidate binaries,
fixtures and Cargo state are generated outside the repository. A mutant counts as rejected only when
the hidden verifier assertions run and fail; compilation failure is not mutation evidence.

### Release identity

The canonical freeze binds all task-directory bytes, including hidden suites, reference binaries and
mutants; exact task order and weights; composed prompts; execution contracts; campaign ceilings;
toolchains; role images; retry policy and scoring schema. Every file is regular, non-symlinked and at
most 1 MiB. Historical release manifests and freezes are regression-pinned by digest.

The three new budgets are conservative model-neutral exceptions under
`core-suite-budget-policy-v1`. They are not represented as paid calibration evidence. B and C always
derive identical limits from the same task contract.

## Consequences

- Each contract is attempted, graded and cleaned independently.
- A timeout or verifier failure remains local to its task slot.
- The report can identify all four hidden-verifier tasks and derives task counts from evidence.
- TestNet tasks still require the frozen network, signer, dependency and funding contracts before
  setup begins; local tasks receive none of those capabilities.
- The suite tests protocol fundamentals, not production contract completeness. It does not cover
  upgrade governance, extension scripts, dep-group management, economic policy, audit quality or
  mainnet deployment.
- The fixed budgets are suitable for matched B/C treatment comparisons within this release. They do
  not make scores from different suite versions directly comparable.
