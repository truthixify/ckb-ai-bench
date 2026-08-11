# containers/

Production docker assets for the CKB AI Bench harness (Phase 3). Promoted from proven spikes
under `spikes/` (egress-proxy, devnet-e2e, container-verifier).

## Topology (ADR-0006, ADR-0007)

| Service | Networks | Role |
|---------|----------|------|
| `ckbbench-proxy` | `net-internal` + `net-egress` | Sole bridge internal to outside; logs all egress |
| `ckbbench-devnet-node` | `net-internal`, `net-rpc` | nervos/ckb:v0.207.0 RPC + indexer sidecar; tracked config read-only, mutable state in the `ckbbench-devnet-data` volume, host RPC published on loopback only |
| `ckbbench-devnet-miner` | `net-internal` | Advances devnet tip; shares the same state volume |
| `ckbbench-agent` | `net-internal` only | Fat pinned agent image; no direct off-host route |
| Verifier one-shots (`ckbbench/run/runner.py`) | `net-internal` | Hermetic grade via direct RPC |

- `net-internal` (`ckbbench-net-internal`): `internal: true`, NO NAT. Agent attaches ONLY here.
- `net-egress` (`ckbbench-net-egress`): normal bridge with outbound NAT. ONLY the proxy is on it.

Per-arm egress (`ckbbench/config.py` `EGRESS_MODE_BY_ARM`):

- **A/D (block):** proxy allowlist permits only chain RPC + MCP (if arm allows MCP) + proxy.
- **B/C (observe):** `proxy/allowlist.observe` permits web; proxy still logs every destination.

## Images

| Dockerfile | Image tag | ADR |
|------------|-----------|-----|
| `agent.Dockerfile` | `ckbbench-agent:latest` | ADR-0004 (fat pinned toolchain + agent fork) |
| `verifier.Dockerfile` | `ckbbench-verifier:latest` | ADR-0005 (hermetic verifier toolchain) |
| `proxy/proxy.Dockerfile` | `ckbbench-proxy:latest` | ADR-0006 (tinyproxy baked) |

Both toolchain images write `/tool-versions.txt` (pinned rust 1.95, clang 18+, riscv64imac target).

## Per-arm wiring

```bash
# Build allowlist for arm D on devnet, write compose .env
python3 containers/compose_builder.py --arm D --chain devnet

# Bring up topology with that allowlist
cd containers && docker compose --env-file .env.arm up -d
```

Block-mode allowlists are emitted directly by `build_allowlist.py` (rule lines only: chain RPC +
proxy + MCP-if-enabled). Observe arms mount `proxy/allowlist.observe`.

## Integration validation (docker required)

`containers/validate.sh` is the integration proof layer (image builds, devnet RPC, no-NAT check).
It refuses to run when `ckbbench-devnet-data` already exists, because that volume is operator chain
state it must not disturb; it removes only a volume it created itself, through the labelled
lifecycle path.

It takes the shared project lock (`scripts/lib/lock.sh`) before that inventory and holds it through
teardown, so a concurrent `./bench up` cannot create operator state during the image build and have
it torn down afterwards. It always acquires that lock itself, whether run directly or through
`./bench test --docker`, and prints `lock: acquired`. A concurrent operation makes it exit with the
owner's pid before any Docker call.

**Chain state.** All mutable DevNet state lives in the labelled `ckbbench-devnet-data` volume;
`containers/devnet/config/` is tracked configuration mounted read-only. Production Docker DevNet
cells recreate that volume before every cell (`ckbbench.run.devnet`), `./bench down` retains it, and
`./bench reset` removes it after proving the ownership labels. The legacy
`containers/devnet/config/data/` directory is no longer mounted and is left untouched.
It is NOT part of pytest (needs docker + minutes to build images).

```bash
# Opt-in via unified test runner:
CKBBENCH_DOCKER=1 bash scripts/test.sh

# Or directly:
bash containers/validate.sh
```

Default `scripts/test.sh` stays docker-free for fast local loops.

## Safety

- Tear-down targets ONLY `ckbbench-*` resources.
- Never stop containers you did not start (e.g. `redclaw-uitest-*`).
- Production runs delete agent containers, `ckbbench-work` (per cell), and
  `ckbbench-cargo-cache` (after matrix) by default; pass `--keep` / `CKBBENCH_KEEP=1`
  to retain them. Compose stack (proxy/devnet) stays up unless you `compose down`.