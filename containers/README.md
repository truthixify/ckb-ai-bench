# containers/

Production docker assets for the harness (Phase 3):

- the fat pinned **agent image** (ADR-0004: Rust + riscv target + Clang 18+ + Node + cargo-generate),
- the hermetic **verifier image** (ADR-0005: its own toolchain, no agent-side pollution),
- the **DevNet sidecar** wiring (nervos/ckb --chain dev + miner, ADR-0007),
- the **egress proxy** + internal-only network + per-arm allowlist (ADR-0006),
- the compose/orchestration that wires them per arm.

Promoted from the proven spikes under `spikes/` (egress-proxy, devnet-sidecar, container-verifier).
