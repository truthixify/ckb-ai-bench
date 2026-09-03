# Binary rewards with verifier diagnostic counts

## Context

A task verifier previously retained only pass or fail. That is the correct reward contract, but it
hides useful evidence when an agent completes part of a multi-assertion on-chain check or passes some
tests in a hidden Rust suite. Two zero-score submissions can therefore represent materially different
engineering progress.

Turning those checks into weighted sub-scores would change the benchmark. The existing criteria were
not designed as interchangeable units, their relative weights were not declared before execution,
and some checks stop as soon as an earlier prerequisite fails. Diagnostic progress must therefore be
kept separate from reward.

## Decision

The official task reward remains binary: a verifier pass receives the task's full declared score and
a verifier failure receives zero. Each new independent task-attempt result also carries one strict
`diagnostics` object under its grade:

| Field | Meaning |
| --- | --- |
| `status` | `complete`, `partial`, `not_evaluated`, or `unavailable` |
| `criteria_passed` | independently verified criteria that passed |
| `criteria_failed` | independently verified criteria that failed |
| `criteria_not_evaluated` | declared criteria not reached |
| `criteria_total` | the sum of the other three counts |

All counts are bounded non-Boolean integers. Available diagnostics require a positive total.
`complete` has no unevaluated criteria, `partial` has failed and unevaluated criteria,
`not_evaluated` has no evaluated criteria, and `unavailable` uses structural zeroes. A diagnostic
record that contradicts the grade is rejected. Failure to extract a diagnostic record never reverses
an otherwise trustworthy verifier pass or failure.

On-chain verifiers use fixed, ordered assertion boundaries. A decisive ordinary failure records the
number of earlier boundaries that passed, that one boundary as failed, and every later boundary as
not evaluated. Missing proof files use `not_evaluated` when the checker has a declared total.
Malformed verifier configuration and unusable infrastructure observations use `unavailable` rather
than inventing progress.

Code-task verifiers inspect the aggregate terminal summary emitted by the pinned Rust test harness.
Exactly one non-empty summary is accepted. Ambiguous or malformed output, ignored, measured or
filtered cases, unreasonable counts, an exit-status contradiction, and build failure all produce
`unavailable`. Captured verifier output and hidden test names are ephemeral and never enter the
result or report dataset.

New results use `ckbbench-task-attempt-result-v3`. The reader continues to accept version 2,
represents its absent diagnostic as `unavailable`, and reproduces its canonical bytes without adding
the new field. Campaign report datasets use version 2 and show verifier counts in the attempt table,
separately from task score.

## Consequences

- Existing task prompts, verifiers, weights, budgets, suite freezes, model profiles and comparison
  arithmetic do not change.
- A failed task still contributes zero correctness reward even when some criteria passed.
- Historical version-2 attempts remain valid but cannot acquire diagnostic detail retroactively.
- Criterion counts are unweighted and meaningful only within the same task and verifier version.
  They are not a cross-task or cross-suite performance metric.
- Sequential on-chain checks expose which boundary stopped evaluation but not every independent
  defect that might exist later in the proof.
- A Rust suite that does not finish with one trustworthy aggregate summary retains its binary grade
  when possible but reports diagnostic detail as unavailable.
