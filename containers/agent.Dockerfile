# Production fat pinned agent build image (ADR-0004).
#
# Bakes the full CKB contract toolchain + Node + the agent fork's Python deps at image-build
# time so the agent can run on the internal-only network (ADR-0006) with no per-run installs.
# Graded rebuilds use image-local CARGO_HOME (no shared cargo volume) under --network none.
#
# Pin manifest: /tool-versions.txt (suite provenance per ADR-0004).
# Build context: repo root (see containers/validate.sh).

FROM rust:1.95-slim

# Pinned tool versions (baked, not per-run).
ARG NODE_MAJOR=22
ARG CARGO_GENERATE_VERSION=0.21.2
# Numeric uid used for offline bake gate and matching typical host --user grades.
ARG BAKE_UID=1000
ARG BAKE_GID=1000

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

# Named /work seed: empty volume mounts inherit these perms (sticky + world-writable).
RUN mkdir -p /work && chmod 1777 /work

# Image-local CARGO_HOME for non-root graded rebuilds (no shared cargo named volume).
ENV CARGO_HOME=/opt/ckbbench-cargo
ENV PATH="/opt/ckbbench-cargo/bin:/usr/local/cargo/bin:${PATH}"
RUN mkdir -p /opt/ckbbench-cargo \
 && cp -a /usr/local/cargo/. /opt/ckbbench-cargo/ 2>/dev/null || true \
 && groupadd -g "${BAKE_GID}" bench || true \
 && useradd -u "${BAKE_UID}" -g "${BAKE_GID}" -m -d /home/bench bench || true \
 && chown -R "${BAKE_UID}:${BAKE_GID}" /opt/ckbbench-cargo

# Bake contract-side crates only (never hidden suite sources) as non-root, then offline gate.
# Agent-added crates outside this bake fail offline grade as agent_fail (by design).
# COPY --chown so bake uid can write Cargo.lock/target (plain COPY is root-owned).
COPY --chown=${BAKE_UID}:${BAKE_GID} containers/bake/agent-deps/ /tmp/agent-bake/
WORKDIR /tmp/agent-bake
USER ${BAKE_UID}:${BAKE_GID}
# Fail image build if fetch/offline gate incomplete (no soft fallback).
RUN cargo fetch \
 && CARGO_NET_OFFLINE=true cargo check
USER root
# World rwx so host --user UID:GID (any non-root) can use image-local cargo offline.
RUN rm -rf /tmp/agent-bake \
 && chmod -R a+rwX /opt/ckbbench-cargo

# Pinned CKB transaction SDK, installed from a lockfile so a graded run never needs the network.
# The /node_modules symlink is load-bearing: Node's ESM resolver walks parent directories only, so
# a task workspace mounted at an arbitrary absolute path resolves the package by reaching the root.
COPY containers/bake/agent-node/package.json containers/bake/agent-node/package-lock.json /opt/ckbbench-node/
RUN cd /opt/ckbbench-node \
 && npm ci --omit=dev --no-audit --no-fund \
 && ln -s /opt/ckbbench-node/node_modules /node_modules \
 && chmod -R a+rX /opt/ckbbench-node
ENV CKB_SDK_HOME=/opt/ckbbench-node

# The agent fork is deliberately NOT copied here. The mini-swe-agent controller and the MCP client
# run in the host harness process; this image is only the command/build environment. Shipping the
# fork would put a generic MCP client and the configured endpoint inside every arm's shell, giving a
# no-MCP arm a route to the product under test.

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
      echo "@ckb-ccc/core: $(node -p "require('/opt/ckbbench-node/node_modules/@ckb-ccc/core/package.json').version")"; \
      cargo generate --version; \
      make --version | head -1; \
      echo "riscv64imac-unknown-none-elf: $(rustup target list --installed | grep riscv || true)"; \
      echo "CARGO_HOME=${CARGO_HOME}"; \
    } > /tool-versions.txt

# Runtime grade uses docker --user; leave USER root so entrypoint/compose are unchanged.
# /work is the build-workspace seed; production DockerEnvironment overrides it with the cell mount.
WORKDIR /work
CMD ["sleep", "infinity"]
