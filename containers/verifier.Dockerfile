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

FROM rust:1.95-slim@sha256:e14e87345b4d5964ddcc3491d27ee046a0f23820f340c3c1e24da6880141f7c0

LABEL org.ckbbench.role="verifier" \
      org.ckbbench.release-family="independent-task-suite-v1"

ARG NODE_VERSION=22.14.0
ARG NODE_SHA256_X64=69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec
ARG NODE_SHA256_ARM64=08bfbf538bad0e8cbb0269f0173cca28d705874a67a22f60b57d99dc99e30050
ARG CKB_DEBUGGER_VERSION=1.1.1
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
      protobuf-compiler \
      libssl-dev \
 && rm -rf /var/lib/apt/lists/*

# Node pinned to the EXACT .tool-versions version, not a mutable NodeSource major stream: a
# `setup_22.x` install resolves to whatever 22.x is current, which cannot back a frozen suite pin.
# ckbbench/suite/test_toolchain_pins.py asserts this literal equals .tool-versions.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) node_arch=x64; node_sha256="$NODE_SHA256_X64" ;; \
      arm64) node_arch=arm64; node_sha256="$NODE_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
      -o /tmp/node.tar.xz; \
    echo "${node_sha256}  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
      --exclude CHANGELOG.md --exclude LICENSE --exclude README.md; \
    rm -f /tmp/node.tar.xz; \
    test "$(node --version)" = "v${NODE_VERSION}" \
      || { echo "node $(node --version) is not the pinned v${NODE_VERSION}" >&2; exit 1; }; \
    test "$(npm --version)" != ""

RUN rustup target add riscv64imac-unknown-none-elf \
 && cargo install ckb-debugger --locked --version "${CKB_DEBUGGER_VERSION}"

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

# Bake every released hidden-suite graph (fetch + offline compile gate as non-root). Sources are
# removed afterwards. COPY --chown lets the bake uid write each target directory.
COPY --chown=${BAKE_UID}:${BAKE_GID} suites/ckb-core-v3/ /tmp/verifier-bake-suite/
WORKDIR /tmp/verifier-bake-suite
USER ${BAKE_UID}:${BAKE_GID}
RUN set -eux; \
    find . -path '*/hidden/Cargo.toml' -print | sort > /tmp/hidden-manifests; \
    test "$(wc -l < /tmp/hidden-manifests)" -eq 10; \
    while IFS= read -r manifest; do \
      cargo fetch --locked --manifest-path "$manifest"; \
      CARGO_NET_OFFLINE=true cargo test --release --locked --offline --no-run --manifest-path "$manifest"; \
    done < /tmp/hidden-manifests
USER root
# World rwx so host --user (any non-root uid) can read/write image-local cargo offline.
RUN rm -rf /tmp/verifier-bake-suite /tmp/hidden-manifests \
 && chmod -R a+rwX /opt/ckbbench-cargo

# Candidate JavaScript bytecode is executed with the same pinned CKB runtime package used to
# compile it in the agent image.
COPY containers/bake/agent-node/package.json containers/bake/agent-node/package-lock.json /opt/ckbbench-node/
RUN cd /opt/ckbbench-node \
 && npm ci --omit=dev --no-audit --no-fund \
 && ln -s /opt/ckbbench-node/node_modules /node_modules \
 && chmod -R a+rX /opt/ckbbench-node
ENV CKB_SDK_HOME=/opt/ckbbench-node

RUN { \
      echo "image: ckbbench-verifier"; \
      rustc --version; \
      cargo --version; \
      clang --version | head -1; \
      node --version; \
      npm --version; \
      echo "@ckb-ccc/core: $(node -p "require('/opt/ckbbench-node/node_modules/@ckb-ccc/core/package.json').version")"; \
      echo "@ckb-ccc/spore: $(node -p "require('/opt/ckbbench-node/node_modules/@ckb-ccc/spore/package.json').version")"; \
      echo "ckb-testtool-js: $(node -p "require('/opt/ckbbench-node/node_modules/ckb-testtool/package.json').version")"; \
      echo "esbuild: $(node -p "require('/opt/ckbbench-node/node_modules/esbuild/package.json').version")"; \
      ckb-debugger --version; \
      make --version | head -1; \
      echo "riscv64imac-unknown-none-elf: $(rustup target list --installed | grep riscv || true)"; \
      echo "CARGO_HOME=${CARGO_HOME}"; \
    } > /tool-versions.txt

WORKDIR /suite
CMD ["sleep", "infinity"]
