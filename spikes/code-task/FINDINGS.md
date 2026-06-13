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

## Reproduce

```
cd ws
rustup target add riscv64imac-unknown-none-elf   # once
make build                                        # -> build/release/hashlock
make test CARGO_ARGS="-- --nocapture"             # hidden suite; exit 0 = pass
```

The correct contract is the committed state of `contracts/hashlock/src/main.rs`. To re-prove the
negative case, replace `program_entry` with `{ 0 }`, `make build`, `make test` → non-zero exit.
