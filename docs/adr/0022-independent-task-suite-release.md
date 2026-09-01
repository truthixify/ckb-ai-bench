# Independent Tasks use a task-scoped immutable suite release

> **Status: accepted.** This decision defines the first suite release for the independent-attempt
> methodology in ADR-0015. It does not authorize a provider request, CKB AI request, public-chain
> request, signer action, transaction, or container build.

## Context

The historical suite gives one agent a shared whole-suite budget. Independent attempts instead need
enough time for one task without allowing a difficult task to consume another task's budget. The
harness must also distinguish agent execution from controller preflight, setup, grading, and cleanup.
TestNet work needs immutable network, dependency, signer, funding, and output-resource requirements;
local code and documentation work must not inherit those chain capabilities.

The project owner delegated bounded benchmark-policy decisions to the benchmark operator before this
release. No accepted per-task calibration campaign exists yet. Calling the selected ceilings
"calibrated" would therefore overstate the evidence.

## Decision

Suite `4.0.0` is released from `suites/ckb-independent-v1`. Its five existing scored tasks retain
their prompts, verifiers, scores, and fixed order:

| Task | Chain | Steps | Agent seconds | Provider calls | Output-token limit |
| --- | --- | ---: | ---: | ---: | --- |
| `task-01-tip` | TestNet | 40 | 600 | 160 | unavailable (`null`) |
| `task-04-send-tx` | TestNet | 80 | 1,200 | 320 | unavailable (`null`) |
| `task-06-sudt-script` | local hermetic | 40 | 600 | 160 | unavailable (`null`) |
| `task-08-type-id-data-cell` | TestNet | 100 | 1,800 | 400 | unavailable (`null`) |
| `task-05-hashlock` | local hermetic | 120 | 2,400 | 480 | unavailable (`null`) |

The provider-call ceiling is four times the step ceiling so the frozen model-turn retry policy can
be enforced without creating an undeclared fifth attempt. The selected provider protocol does not
impose a trustworthy whole-task output-token ceiling, so the schema records `null` instead of a
fictional limit. B and C derive the same budget bytes from one task contract.

Each task separately freezes preflight, setup, grading, and teardown deadlines. Those clocks are
controller limits and never reduce the task's agent wall clock. The freeze publishes both per-field
campaign ceilings and the combined worst case, including the sole whole-task retry and its cooldown.

Every budget carries `owner-approved-exception` evidence under decision
`independent-task-budget-policy-v1`, approved by the benchmark operator under the owner's delegated
authority. The evidence contains no invented pilot maxima. A future calibrated release must bind
the exact non-accepted attempt artifacts and publish a new suite digest rather than rewriting this
decision.

The TestNet profile fixes `ckb_testnet` and its Pudge genesis. The transaction tasks additionally
freeze the standard secp256k1-blake160 dependency, a constrained signer-policy identity, maximum
transfer, fee reserve, safety margin, minimum cell count, confirmation floor, reserved resources,
and expected output resources. Tip is read-only and carries neither signer nor funding. Local
hermetic tasks reject public-chain identity, deployed dependencies, signing, funding, and chain
resources.

Each task declares the minimum CKB AI capability it needs: documentation search and reads under the
CKB documentation resource namespace. TestNet tasks also require the concrete campaign surface to
attest the live chain. These are minimum requirements, not a claim that an unobserved server catalog
is compatible. Campaign freezing still requires exact control and treatment profiles from one
observed catalog before execution.

The suite freeze binds task order and authored task-directory bytes, prompt fragments, composed
stage prompts, execution contracts, role-image IDs, exact toolchain versions, scoring schema, and
the canonical whole-task retry policy. A campaign may supply model, thinking level, trial challenge,
and concrete treatment profiles, but it derives every task budget, score, chain identity, execution
requirement, and resource-equivalence policy from this release.

## Consequences

- One task's timeout or budget exhaustion cannot consume another task's budget.
- Reaching an agent ceiling remains scored behavior; it is not converted into infrastructure failure.
- A task receives at most one fresh whole-task retry, and only after an allowlisted unscored
  infrastructure failure with complete cleanup.
- Signed TestNet attempts cannot begin without pre-leased capacity and an exact constrained policy.
- Offline listing, loading, planning, and fake calibration do not contact external systems.
- The release records conservative exceptions openly until paid per-task calibration justifies a
  successor budget release.
- The historical suite remains an immutable separate release.
