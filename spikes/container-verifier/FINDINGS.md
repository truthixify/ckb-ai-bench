# Spike: Container Verifier - hidden-suite grading across the container boundary - FINDINGS (2026-06-12)

Goal: prove the headline Code-Task grading guarantee (a hidden suite the agent never sees
grades the agent's authored binary, and a fresh per-run secret defeats a cheat) still holds
when the agent build and the grade run in SEPARATE CONTAINERS, across the real trust
boundary. The Tier-1 `code-task` spike proved this NATIVELY on the host; this spike proves
the SAME guarantee survives the container split (ADR-0004 fat pinned toolchain image,
ADR-0005 hermetic verifier fed by the mounted folder, ADR-0002 hide-proof Proof, ADR-0009
verifier-private params injected post-`done`).

The load-bearing property is CONTENT ISOLATION + INJECTION TIMING, enforced by WHAT IS
MOUNTED into each stage, not by trusting the agent.

## Topology

One pinned toolchain image (`toolchain.Dockerfile` -> `ckb-toolchain:spike`), used for TWO
distinct `docker run` stages with DIFFERENT mount sets:

```
                         build/release/hashlock
   AGENT stage  ------------------------------------->  artifact dir (the channel)
   (mounts source ws READ-ONLY: contract sources only,       |
    NO tests/, NO password; builds in the cargo volume,      |  agent binary, READ-ONLY (:ro)
    copies ONLY the binary to the artifact dir)              |
                                                              v
   VERIFIER stage  <----  hidden suite (tests/) RW   +   agent binary :ro   +   BENCH_PASSWORD
   (separate run; cwd = suite ws; TOP=/artifact so the suite        (injected ONLY now,
    grades the agent binary; `cargo test` exit code = grade)         after the agent stage)
```

The agent stage and the verifier stage never share a mount that carries the hidden suite or
the secret. The agent's workspace `Cargo.toml` lists ONLY `contracts/hashlock` (not `tests`),
so the workspace builds with the suite genuinely absent.

## Trust boundary (what each container can and cannot see)

| | AGENT container | VERIFIER container |
|---|---|---|
| contract sources (`contracts/`, Makefile, Cargo.lock) | yes (mounted READ-ONLY) | no |
| hidden suite (`tests/src/tests.rs`) | NO (not mounted, not in its Cargo.toml) | yes (RW, to compile) |
| `BENCH_PASSWORD` (verifier-private run param) | NO (env unset; no literal in ws) | yes (env, injected at verify time only) |
| the agent's built binary | yes (builds it, copies it to the artifact dir) | yes, but READ-ONLY (`:ro`) - cannot tamper |
| runs as | host uid (artifacts host-owned) | host uid, no write rights to the artifact |

The agent binary is the ONLY thing that crosses from agent to verifier. The suite finds it
via ckb-testtool's documented `$TOP/build/$MODE/<name>` lookup (`Context::default`), with
`TOP=/artifact` pointing at the read-only mount, so the verifier grades exactly the binary
the agent built from its submitted sources (the "rebuild from agent sources before grading"
harness rule, tracked in `code-task/FINDINGS.md`).

## Image strategy and why

PREFERRED strategy (a): ONE pinned toolchain image, content isolation enforced by the MOUNT
SET across two `docker run` invocations, not by the image. Reasons: both stages need the
same Rust toolchain anyway (ADR-0004: the verifier compiles the hidden suite, the agent
compiles the contract), `rust:1-slim` was already pulled locally, and a single image keeps
re-runs fast. The image bakes the exact ADR-0004 CKB on-chain prereqs at build time:

```
rustc 1.96.0 / cargo 1.96.0
Debian clang version 19.1.7      (satisfies find_clang's "16+" and ADR-0004's "Clang 18+")
GNU Make 4.4.1
riscv64imac-unknown-none-elf target installed
```

Production refinement (ADR-0005, tracked below): split into TWO pinned images, a fat agent
image and a clean hermetic verifier image, for pollution hygiene. That split is NOT the
load-bearing part; this spike proves the load-bearing part (content + timing), which the
single image with distinct mounts already enforces.

## Injection-timing proof

The hidden suite and the password are absent during the AGENT stage and present only in the
VERIFIER stage. This is enforced, not asserted on trust:

- The agent stage is invoked with NO `tests/` in its mount and NO `-e BENCH_PASSWORD`. The
  checks below confirm both are absent from the agent container (filesystem and env).
- The verifier stage is a SEPARATE `docker run` that adds the `tests/` mount and the
  `-e BENCH_PASSWORD` env. The fresh secret is generated from `/dev/urandom` AFTER the agent
  builds, so it cannot have leaked into the agent stage even in principle.
- The suite's `password()` PANICS if `BENCH_PASSWORD` is unset (inherited from the
  code-task suite), so the verifier refuses to grade without a per-run secret (Rule 12).

## The decisive results (each by process exit code; `bash run-spike.sh` -> 11/11, exit 0)

| Check | Asserts | Exit |
|---|---|---|
| BUILD (correct) | agent container builds the correct contract | 0 |
| BUILD (ELF) | output is a RISC-V CKB-VM ELF in the artifact dir | 0 |
| ISOLATION-SUITE (mount) | `tests/` dir absent from the agent workspace mount | 1 (absent) |
| ISOLATION-SUITE (content) | hidden grader signature absent from agent ws AND cargo cache | 1 (no match) |
| ISOLATION-SECRET (env) | `BENCH_PASSWORD` unset in the agent container env | 0 |
| ISOLATION-SECRET (fs) | no `BENCH_PASSWORD` literal anywhere in the agent workspace | 1 (no match) |
| GRADE-PASS | verifier grades the CORRECT contract PASS with a FRESH secret it never saw | 0 |
| READ-ONLY | write into the `:ro` agent-artifact mount is refused | 2 (refused) |
| BUILD (cheat) | agent container rebuilds the CHEAT from its sources | 0 |
| PRECHECK (cheat) | a real RISC-V cheat binary exists (so GRADE-FAIL is not vacuous) | 0 |
| GRADE-FAIL | verifier grades the CHEAT FAIL with the SAME fresh secret | 101 (fail) |

GRADE-FAIL fails for the RIGHT reason: the cheat hardcodes the guess `open-sesame-42` and
ignores the lock args, so against the fresh random secret the suite's `correct_password_unlocks`
fails with script exit code 6 ("wrong password"), and `cargo test` exits 101. The correct
contract reads the lock args at runtime and passes for ANY secret. So a contract that does
not actually implement the rule cannot survive a per-run secret it never saw, even across
the container boundary.

## Conclusions

1. The hidden-suite grading guarantee survives the container split: the agent builds in one
   container that provably lacks the suite and the secret; a separate hermetic container
   grades the resulting binary read-only with the suite and the secret injected only then.
2. Content isolation is enforced by the mount set (agent's Cargo.toml omits `tests`; the
   suite and secret are never mounted/passed into the agent stage), not by trust.
3. The verifier grades the binary built from the agent's submitted sources, located via
   ckb-testtool's `$TOP/build/release` lookup against a read-only mount it cannot tamper with.

## Residual / tracked caveats

- ONE image vs TWO (ADR-0005): this spike uses one pinned toolchain image for both stages
  and enforces isolation via mounts. Production should split into a fat agent image and a
  clean hermetic verifier image for pollution hygiene. The split is a refinement, not the
  load-bearing guarantee, which is proven here.
- Build determinism (the hard-won part; documented honestly because early runs were flaky).
  The ROOT cause of intermittent BUILD failures was cargo writing its high-churn `target/`
  tree into a Docker host BIND MOUNT: cargo creates and writes object files in
  `target/.../deps/` faster than the overlay bind settles, giving transient ENOENT errors
  (`couldn't create a temp dir ... deps/rustcXXXX` / `could not write output ... .rcgu.o: No
  such file or directory`) that aborted the build. It reproduced ~1 in 3-5 builds and a plain
  retry did not help (it reused the corrupt tree). The FIX: the agent build mounts its source
  ws READ-ONLY and builds in a per-stage subdir of a dedicated WORK volume (a real Docker
  volume, NOT a host bind mount), then copies ONLY the final binary to the host artifact dir
  (a single write). Proven reliable: 10/10 clean builds with this layout. A short-lived
  earlier theory (per-stage cargo caches to dodge a root-owned-subdir race) was wrong and
  made things worse: three separate caches each did the slow, flaky cold `crates.io` index
  fetch. Final design uses ONE shared cargo cache, pre-warmed once before the timed checks,
  so every stage runs warm. The cache holds only upstream crate sources; the ISOLATION-SUITE
  content check greps it and confirms the run's hidden grader and secret are NOT in it.
- Single-instance only (not concurrency-safe): the spike uses fixed-named Docker volumes
  (`ckb-cv-cargo`, `ckb-cv-work`) and fixed staging dirs, so two copies of `run-spike.sh`
  running at once stomp on each other's shared volumes (cargo registry lock conflicts,
  half-copied workspaces) and produce spurious BUILD/GRADE failures. This was in fact the
  dominant cause of the "intermittent" failures seen during development (a second invocation
  was running in parallel). Run one at a time. Production would namespace volumes per run id.
- Negative-check soundness: GRADE-FAIL is only meaningful if the cheat binary actually built
  (a missing binary makes the suite fail for the WRONG reason -- no cell to deploy -- which
  would falsely "pass" a negative check). A PRECHECK asserts a real RISC-V cheat binary
  exists before GRADE-FAIL runs, so the FAIL is provably the wrong-password rejection.
- Read-only refusal exit code: `sh -c 'echo > /ro/file'` reports the refused redirection as
  exit 2 (not 1); the check asserts 2. A first run mis-asserted 1 and FAILED LOUD until
  corrected, which is the intended fail-loud behavior.
- The password-lock is a deliberately simple objective rule (inherited from `code-task`);
  harder CKB tasks (multi-cell, types, DAO/ACP) come later and are out of scope here.

## Reproduce

```
bash spikes/container-verifier/run-spike.sh     # builds the image (cached), runs 11 checks, exit 0
```

The script stages everything from the proven `spikes/code-task/ws` sources, generates a
fresh `/dev/urandom` `BENCH_PASSWORD` per run (a TEST value, never a real credential), and
tears down the shared cargo + work volumes and staged dirs on exit. It only stops/removes
resources it started; it does not touch unrelated containers.
