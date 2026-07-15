# Production hermetic verifier image (ADR-0005).
#
# Its OWN pinned Rust + Node toolchain, no agent-side pollution, no MCP, no web tools beyond
# what grading needs. Compiles + runs the hidden Rust suite and runs TS verifier executables.
# Graded stages use image-local CARGO_HOME under --network none (no shared cargo volume).
#
# Build context: repo root (must include suites/ for bake). Never copy hidden suite sources
# into the agent image — this file is verifier-only.
#
# Pin manifest: /tool-versions.txt

FROM rust:1.95-slim

ARG NODE_MAJOR=22
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
 && rm -rf /var/lib/apt/lists/*

# Node 22 for TS verifier executables (ADR-0005: lightweight executables, not agent pollution).
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN rustup target add riscv64imac-unknown-none-elf

# Named /work seed for empty named-volume mounts (build stage uses /work; verify uses suite).
RUN mkdir -p /work && chmod 1777 /work

# Image-local CARGO_HOME for non-root offline grade.
ENV CARGO_HOME=/opt/ckbbench-cargo
ENV PATH="/opt/ckbbench-cargo/bin:/usr/local/cargo/bin:${PATH}"
RUN mkdir -p /opt/ckbbench-cargo \
 && cp -a /usr/local/cargo/. /opt/ckbbench-cargo/ 2>/dev/null || true \
 && groupadd -g "${BAKE_GID}" bench || true \
 && useradd -u "${BAKE_UID}" -g "${BAKE_GID}" -m -d /home/bench bench || true \
 && chown -R "${BAKE_UID}:${BAKE_GID}" /opt/ckbbench-cargo

# Bake hidden-suite graph deps (fetch + offline compile gate as non-root). Sources removed after.
# Build context must be repo root so this path exists.
# COPY --chown so bake uid can write target/ under the workspace.
COPY --chown=${BAKE_UID}:${BAKE_GID} suites/ckb-v1/task-05-hashlock/hidden/ /tmp/verifier-bake/
WORKDIR /tmp/verifier-bake
USER ${BAKE_UID}:${BAKE_GID}
RUN cargo fetch \
 && CARGO_NET_OFFLINE=true cargo test --release --no-run
USER root
# World rwx so host --user (any non-root uid) can read/write image-local cargo offline.
RUN rm -rf /tmp/verifier-bake \
 && chmod -R a+rwX /opt/ckbbench-cargo

RUN { \
      echo "image: ckbbench-verifier"; \
      rustc --version; \
      cargo --version; \
      clang --version | head -1; \
      node --version; \
      npm --version; \
      make --version | head -1; \
      echo "riscv64imac-unknown-none-elf: $(rustup target list --installed | grep riscv || true)"; \
      echo "CARGO_HOME=${CARGO_HOME}"; \
    } > /tool-versions.txt

WORKDIR /suite
CMD ["sleep", "infinity"]
