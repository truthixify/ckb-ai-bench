# Spike (NOT production): the pinned, fat CKB build toolchain image (ADR-0004).
#
# This single image stands in for the pinned toolchain that BOTH the agent stage and
# the verifier stage need (both compile Rust; the verifier compiles the hidden suite,
# the agent compiles the contract). In production ADR-0005 splits this into two pinned
# images (a fat agent image and a clean hermetic verifier image) for pollution hygiene;
# this spike proves the LOAD-BEARING guarantee, which is content isolation + injection
# timing enforced by the MOUNT SET, not by the image. See FINDINGS.md.
#
# Toolchain prereqs are exactly ADR-0004's CKB on-chain set:
#   Rust stable + riscv64imac-unknown-none-elf target, Clang 18+, make, git, llvm tools.
FROM rust:1-slim

# Pinned at image-build time, not per run (ADR-0004: baked, not installed per run).
# Debian 13 (trixie) ships clang 19, which satisfies find_clang's "clang 16+" and
# ADR-0004's "Clang 18+". llvm tools (llvm-ar / llvm-strip / llvm-objcopy) come with it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      clang \
      llvm \
      lld \
      make \
      git \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The CKB-VM target. Baked so neither stage installs it per run.
RUN rustup target add riscv64imac-unknown-none-elf

# A non-root user so a read-only mount write-attempt fails for the ordinary reason
# (permissions / ro mount), matching how the verifier runs without write rights.
# The agent and verifier stages select uid/gid at `docker run` time via --user.

# Record the toolchain provenance (ADR-0004: the manifest doubles as suite provenance).
RUN { rustc --version; cargo --version; clang --version | head -1; \
      make --version | head -1; \
      echo "riscv64imac-unknown-none-elf: $(rustup target list --installed | grep riscv)"; \
    } > /tool-versions.txt

CMD ["sleep", "infinity"]
