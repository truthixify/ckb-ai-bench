# Data Guard

Reference implementation for a type script that binds grouped cell data to one expected hash.

Build the canonical binary from the workspace root with Rust 1.95.0:

```bash
TOP="$PWD/" BUILD_DIR=build/release CARGO_NET_OFFLINE=true \
  make -e -C contracts/data-guard build CARGO_ARGS="--locked --offline"
```

Semantic mutation features are `mutant-accept-all`, `mutant-global-source`,
`mutant-output-only` and `mutant-shape-blind`. Add `--features <feature>` to `CARGO_ARGS` to build
one; the unified hidden-suite gate verifies that every released mutant is rejected.
