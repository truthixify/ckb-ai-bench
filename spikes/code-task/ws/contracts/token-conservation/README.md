# Token Conservation

Reference implementation of sUDT-style owner mode and checked token amount conservation.

Build the canonical binary from the workspace root with Rust 1.95.0:

```bash
TOP="$PWD/" BUILD_DIR=build/release CARGO_NET_OFFLINE=true \
  make -e -C contracts/token-conservation build CARGO_ARGS="--locked --offline"
```

Semantic mutation features are `mutant-equal-only`, `mutant-first-only`,
`mutant-global-source`, `mutant-owner-output` and `mutant-wrapping-sum`. Add
`--features <feature>` to `CARGO_ARGS` to build one; the unified hidden-suite gate verifies that
every released mutant is rejected.
