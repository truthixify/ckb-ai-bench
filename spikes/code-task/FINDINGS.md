# Spike: Code-Task build + hidden-suite grading — FINDINGS (2026-06-12)

Goal (Tier-1 #1): prove the headline Code-Task grading path end to end — an agent-authored
contract is built to a CKB-VM binary, and a hidden Rust `ckb-testtool` suite (withheld from the
agent) runs against that binary and objectively separates a correct submission from a wrong one
(ADR-0002, ADR-0005). Run natively (host already has the pinned toolchain).

## Toolchain (host, all present — confirms ADR-0004 prereqs)

Rust 1.95.0, `cargo-generate` 0.23.4, Clang 18.1.8, `riscv64imac-unknown-none-elf` target, make.

## What was done (live)

1. `cargo generate gh:cryptape/ckb-script-templates workspace` → workspace shell (`ws/`).
2. `make generate CRATE=hashlock` → a contract crate + auto-appended test into `tests/src/tests.rs`.
3. Baseline `make build` of the generated stub → real binary `build/release/hashlock`
   (ELF 64-bit RISC-V, statically linked, stripped, ~22 KB) in 24 s. Toolchain works.
4. Authored a **meaningful contract** (`contracts/hashlock/src/main.rs`): a "password lock" that
   unlocks only when the first `GroupInput` witness byte-equals the lock's `args` (exit 0), else
   rejects (exit 6 wrong, exit 5 missing witness). Compiled first try.
5. Wrote a **hidden suite** (`tests/src/tests.rs`) encoding intent (Rule 9): correct password
   unlocks; wrong password rejected; missing witness rejected.

## The decisive result

- **Correct contract → 3/3 tests pass.** `verify_tx` ran the binary in CKB-VM: correct unlock at
  18 597 cycles; wrong password → script error code 6; missing witness → script error code 5.
- **Wrong contract (the always-`return 0` stub) → `make test` exits non-zero (2/3 fail).** The stub
  authorizes everything, so `wrong_password_is_rejected` and `missing_witness_is_rejected` both
  fail (the stub wrongly unlocks at 5 765 cycles); only the correct-password case passes.

This is exactly the Code-Task guarantee: a hidden suite the agent never sees runs the agent's
authored binary and catches a cheating/wrong submission. `make test`'s exit code is the grade.

## Conclusions

1. `ckb-script-templates` is the Code-Task tool: scaffold → build → grade, no OffCKB, off-chain,
   deterministic. The grading signal is the suite's process exit code.
2. The contract authoring uses only `ckb-std` (`load_script`, `load_witness`, `Source::GroupInput`,
   `script.args().unpack()`); no extra crates were needed for a non-trivial rule.
3. The generated `make build CONTRACT=<name>` path also expects a `<name>-sim` native-simulator
   crate; plain `make build` (all crates) is the simple path and is what the harness should call.

## Hardening after adversarial review (2026-06-12)

Adversarial reviewers (grok-build, grok-composer) correctly observed that the first cut did NOT
prove the *hidden*-suite guarantee: the suite's password was a compile-time `const` co-located with
the agent's contract, so a cheat that hardcodes that literal (ignoring the lock args) would pass
3/3 without implementing the rule. That proves "catches the always-0 stub", not "is hide-proof".

Fix applied: the password is now a **verifier-private run param** injected at verify time via the
`BENCH_PASSWORD` env var (`tests/src/tests.rs` `password()`), modelling ADR-0009. A correct contract
reads the lock `args` at runtime and passes for ANY password; a hardcode cheat passes only for its
baked-in guess. Proven live:

| Contract | suite password = leaked `open-sesame-42` | suite password = fresh harness secret |
|---|---|---|
| correct (reads args) | pass | **pass** (exit 0) |
| hardcode cheat (baked literal) | pass (exit 0) | **FAIL** (exit 2) |
| always-`0` stub | fail | fail |

So a contract that does not actually read the lock args cannot survive a per-run secret it never
saw. In the real harness this means the suite + its run-param password are injected post-`done`
into the hermetic Verifier (ADR-0005), never visible in the agent's `contracts/` mount.

Residual (tracked, not spike-blocking): the rejection tests accept any `verify_tx` error, not the
specific exit codes 5/6 — a future suite should assert exit codes; and the harness must REBUILD the
binary from agent sources before grading (never trust a stale `build/release/`). The password-lock
is a deliberately simple objective rule; harder CKB tasks (multi-cell, types, DAO/ACP) come later.

## Reproduce

```
cd ws
rustup target add riscv64imac-unknown-none-elf   # once
make build                                        # -> build/release/hashlock
make test CARGO_ARGS="-- --nocapture"             # hidden suite; exit 0 = pass
BENCH_PASSWORD=any-fresh-secret make test         # correct contract still passes (reads args)
```

The correct contract is the committed state of `contracts/hashlock/src/main.rs`. To re-prove the
negatives: replace `program_entry` body with `{ 0 }` (always-authorize) OR hardcode a literal
password (ignore args), `make build`, then `BENCH_PASSWORD=fresh-secret make test` → non-zero exit.
