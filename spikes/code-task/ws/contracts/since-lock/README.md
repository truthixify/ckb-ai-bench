# Since Lock

Reference implementation for a group-relative CKB `since` threshold lock.

Build the canonical binary from the workspace root with Rust 1.95.0:

```bash
TOP="$PWD/" BUILD_DIR=build/release CARGO_NET_OFFLINE=true \
  make -e -C contracts/since-lock build CARGO_ARGS="--locked --offline"
```

Semantic mutation features are `mutant-accept-all`, `mutant-first-only`,
`mutant-global-source` and `mutant-numeric-compare`. Add `--features <feature>` to `CARGO_ARGS` to
build one; the unified hidden-suite gate verifies that every released mutant is rejected.
