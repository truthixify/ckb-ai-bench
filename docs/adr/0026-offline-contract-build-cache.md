# Source-complete offline contract build cache

## Context

Contract attempts run with `CARGO_NET_OFFLINE=true`, and grading rebuilds submitted source under
`--network none`. The original agent image cached a host build of `ckb-std`, but not the public
dependency graph Cargo needs to resolve a fresh `ckb-script-templates` workspace. A source-only
reference therefore failed before compilation because registry entries for optional simulator and
template test dependencies were absent.

This was an execution-environment defect. A correct contract binary already passed every hidden
suite, while the same contract source could not reach the compiler through the production rebuild
path.

## Decision

The agent image carries a locked, public-only Cargo bake graph for `ckb-std 1.1.0`,
`ckb-testtool 1.1.1`, `ckb-x64-simulator 1.1.0`, and `serde_json 1.0.151`. Image construction must:

- fetch the exact lock graph;
- resolve the full template graph with Cargo networking disabled;
- compile the default graph for the host with Cargo networking disabled; and
- compile the default graph for `riscv64imac-unknown-none-elf` with Cargo networking disabled.

Hidden suite sources and reference contract implementations remain outside the agent image. The
Docker integration gate copies the public source reference into a fresh workspace, removes prior
build output and its source lockfile, rebuilds it through the production runner with no network,
and requires the hidden verifier to accept the resulting artifact.

The corrected image is released by `suites/ckb-core-v2` as suite `5.0.1`. The `5.0.0` registry and
image pin remain unchanged so existing evidence keeps its original identity.

## Consequences

The agent image is larger because it retains public host-side template dependencies that are not
linked into the RISC-V binary. That cost is paid once at image construction. Benchmark attempts no
longer depend on package-index access, and a model is not scored down because the frozen toolchain
omitted dependencies required by the task's prescribed workspace layout.

Results from `5.0.0` and `5.0.1` retain separate suite identities and must not be pooled.
