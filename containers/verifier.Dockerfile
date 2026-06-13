# Production hermetic verifier image (ADR-0005).
#
# Its OWN pinned Rust + Node toolchain, no agent-side pollution, no MCP, no web tools beyond
# what grading needs. Compiles + runs the hidden Rust suite and runs TS verifier executables.
# Promoted from spikes/container-verifier/toolchain.Dockerfile (split from the agent image per
# ADR-0005 pollution hygiene).
#
# Pin manifest: /tool-versions.txt

FROM rust:1.95-slim

ARG NODE_MAJOR=22

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      clang \
      curl \
      git \
      llvm \
      lld \
      make \
 && rm -rf /var/lib/apt/lists/*

# Node 22 for TS verifier executables (ADR-0005: lightweight executables, not agent pollution).
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN rustup target add riscv64imac-unknown-none-elf

RUN { \
      echo "image: ckbbench-verifier"; \
      rustc --version; \
      cargo --version; \
      clang --version | head -1; \
      node --version; \
      npm --version; \
      make --version | head -1; \
      echo "riscv64imac-unknown-none-elf: $(rustup target list --installed | grep riscv || true)"; \
    } > /tool-versions.txt

WORKDIR /suite
CMD ["sleep", "infinity"]