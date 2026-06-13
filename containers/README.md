# containers/

Production docker assets for the CKB AI Bench harness (Phase 3). Promoted from proven spikes
under `spikes/` (egress-proxy, devnet-e2e, container-verifier).

## Topology (ADR-0006, ADR-0007)

| Service | Networks | Role |
|---------|----------|------|
| `ckbbench-proxy` | `net-internal` + `net-egress` | Sole bridge internal to outside; logs all egress |
| `ckbbench-devnet-node` | `net-internal` | nervos/ckb:v0.207.0 RPC + indexer sidecar |
| `ckbbench-devnet-miner` | `net-internal` | Advances devnet tip |
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