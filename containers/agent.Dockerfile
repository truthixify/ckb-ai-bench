# Production fat pinned agent build image (ADR-0004).
#
# Bakes the full CKB contract toolchain + Node + the agent fork's Python deps at image-build
# time so the agent can run on the internal-only network (ADR-0006) with no per-run installs.
# Promoted from spikes/container-verifier/toolchain.Dockerfile and spikes/egress-proxy.
#
# Pin manifest: /tool-versions.txt (suite provenance per ADR-0004).

FROM rust:1.95-slim

# Pinned tool versions (baked, not per-run).
ARG NODE_MAJOR=22
ARG CARGO_GENERATE_VERSION=0.21.2

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
      pkg-config \
      libssl-dev \
      python3 \
      python3-pip \
      python3-venv \
 && rm -rf /var/lib/apt/lists/*

# Node 22 (ADR-0004: pinned Node for TS work; NVM rejected for non-interactive Docker layers).
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# CKB-VM target + cargo-generate (ckb-script-templates scaffolding path).
RUN rustup target add riscv64imac-unknown-none-elf \
 && cargo install cargo-generate --locked --version "${CARGO_GENERATE_VERSION}"

# Agent fork (read-only carrier; harness does not modify agent/ at run time).
COPY agent/ /agent/

# Python deps: spike-requirements.txt pins + litellm/tenacity for the agent driver path.
COPY agent/spike-requirements.txt /tmp/agent-requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages \
      -r /tmp/agent-requirements.txt \
      litellm==1.72.0 \
      tenacity==9.1.2

# Toolchain provenance manifest (ADR-0004).
RUN { \
      echo "image: ckbbench-agent"; \
      rustc --version; \
      cargo --version; \
      clang --version | head -1; \
      node --version; \
      npm --version; \
      cargo generate --version; \
      make --version | head -1; \
      echo "riscv64imac-unknown-none-elf: $(rustup target list --installed | grep riscv || true)"; \
    } > /tool-versions.txt

WORKDIR /agent
CMD ["sleep", "infinity"]