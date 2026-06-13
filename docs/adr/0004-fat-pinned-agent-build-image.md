# Fat, fully-pinned agent build image

## Context

Code Tasks require building CKB contracts, and the Verifier compiles its own hidden Rust test suite
regardless of how the agent's artifact is handled. The agent therefore needs a real build environment,
and we run a large matrix (models x chains x arms x runs) where image weight and per-run setup cost
matter.

## Decision

The agent container is a **full build environment baked at image-build time**, not installed per run:
build-essential, a **pinned Rust toolchain** (so the agent can build CKB contracts), and a **pinned
Node + TypeScript** (for TS work and to match the Verifier's lightweight executables). Versions are
pinned in the Dockerfile / a committed tool-versions manifest for reproducibility, and that manifest
doubles as suite provenance.

Because the Verifier must carry the same Rust toolchain anyway (it compiles the Hidden suite), the
"agent ships source vs. binary" choice is no longer container-shaping and stays revisitable (see
ADR-0003).

**Concrete CKB-toolchain prerequisites (from build-phase spikes).** The Rust on-chain path is
ckb-script-templates (cargo-generate + ckb-testtool), not OffCKB. Building it needs: Rust stable + the
`riscv64imac-unknown-none-elf` target, **Clang 18+**, `cargo-generate` (which itself needs
`pkg-config` + `libssl-dev` to compile), and `make`/`git`. The Node side needs `build-essential` +
`python3` for any native npm deps. These are baked, pinned, at image-build time.

## Consequences

The agent image is heavy and toolchain friction counts as part of the measured run, which we accept as
realistic (developers compile). **Open sub-decision:** the version-pinning mechanism. NVM is rejected
for image builds because it is shell-sourced and unreliable in non-interactive Docker layers; the
leading candidate is `mise` (pins Rust and Node from one `.tool-versions` file, non-interactive-safe),
with a pinned Node base image as the simpler fallback.
