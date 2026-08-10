"""Single source of truth for run-time constants and live-infrastructure references.

Volatile facts (RPC URLs, the MCP endpoint, the LLM proxy) live here ONCE so docs and code do
not drift. Every value is overridable by environment variable so a run can be retargeted
without code edits (the matrix driver relies on that). The canonical key list, with the live
defaults and the retargeting contract, is enumerated in ``.env.example`` at the repo root.

Env-var compatibility: the existing agent fork and the spikes use the older ``BENCH_*`` /
``MCP_URL`` / ``MCP_PINNED_VERSION`` names. To avoid Phase 4 silently reading a different
default than the rest of the codebase, each constant accepts BOTH the new ``CKBBENCH_*`` name
(preferred) AND the legacy name as a fallback.

These are NOT secrets. The DevNet genesis keys are public dev.toml test keys by design
(ADR-0007); no private credential is ever stored in this repo.
"""

from __future__ import annotations

import os
from types import MappingProxyType


def _env(*names: str, default: str) -> str:
    """First set env var among ``names`` wins, else ``default``. Lets new CKBBENCH_* names take
    precedence while still honoring the legacy BENCH_*/MCP_* names the spikes and agent use."""
    for name in names:
        val = os.getenv(name)
        if val is not None:
            return val
    return default


# --- LLM access -----------------------------------------------------------------------------
# The local LLM proxy (OpenAI-compatible). Recommended models: openai + grok families.
# Routed through litellm in the agent driver (Phase 4). Legacy name: BENCH_API_BASE / BENCH_API_KEY.
LLM_API_BASE = _env("CKBBENCH_LLM_API_BASE", "BENCH_API_BASE", default="http://localhost:18321/v1")
LLM_API_KEY = _env("CKBBENCH_LLM_API_KEY", "BENCH_API_KEY", default="sk-noauth")

# --- MCP server (the thing under test) ------------------------------------------------------
# Pinned version is enforced at preflight (ADR-0010); a mismatch aborts a scored run. The
# default points at the live shared dev endpoint, which is the only working instance today; a
# scored suite retargets this (via CKBBENCH_MCP_URL) to the deployed pinned instance.
# Legacy names: MCP_URL / MCP_PINNED_VERSION.
MCP_URL = _env("CKBBENCH_MCP_URL", "MCP_URL", default="https://mcp.ckbdev.com/ckbai")
MCP_PINNED_VERSION = _env("CKBBENCH_MCP_VERSION", "MCP_PINNED_VERSION", default="1.6.13")

# --- Chain profiles (scored separately, never merged) ---------------------------------------
# Reachability contract: these defaults are the addresses as seen FROM THE HARNESS HOST.
# DevNet is a nervos/ckb --chain dev sidecar (ADR-0007). The default is the sidecar's
# host-published port for local/harness-side use; INSIDE the docker network the orchestrator
# (Phase 3) addresses it by its compose service name and overrides CKBBENCH_DEVNET_RPC
# accordingly. TestNet is the self-hosted archive node (inventory: 192.168.0.73).
DEVNET_RPC = _env("CKBBENCH_DEVNET_RPC", default="http://127.0.0.1:8114")
TESTNET_RPC = _env("CKBBENCH_TESTNET_RPC", default="http://192.168.0.73:18114")

# --- Container images (digest pins at release time) -------------------------------------------
# Override to pin a release image without code edits. Supports repo:tag or repo@sha256:... refs.
# Consumed by ckbbench.run.runner (code-task build/verify) and agent_factory (docker agent).
_DEFAULT_AGENT_IMAGE = "ckbbench-agent:latest"
_DEFAULT_VERIFIER_IMAGE = "ckbbench-verifier:latest"
AGENT_IMAGE = _env("CKBBENCH_AGENT_IMAGE", default=_DEFAULT_AGENT_IMAGE)
VERIFIER_IMAGE = _env("CKBBENCH_VERIFIER_IMAGE", default=_DEFAULT_VERIFIER_IMAGE)


def resolve_agent_image(*, suite_digest: str | None = None) -> str:
    """Return the agent image ref: env override, then suite manifest digest, then default."""
    explicit = os.getenv("CKBBENCH_AGENT_IMAGE")
    if explicit:
        return explicit
    if suite_digest and suite_digest.startswith("sha256:"):
        return f"{_DEFAULT_AGENT_IMAGE.rsplit(':', 1)[0]}@{suite_digest}"
    return _DEFAULT_AGENT_IMAGE


def resolve_verifier_image(*, suite_digest: str | None = None) -> str:
    """Return the verifier image ref: env override, then suite manifest digest, then default."""
    explicit = os.getenv("CKBBENCH_VERIFIER_IMAGE")
    if explicit:
        return explicit
    if suite_digest and suite_digest.startswith("sha256:"):
        return f"{_DEFAULT_VERIFIER_IMAGE.rsplit(':', 1)[0]}@{suite_digest}"
    return _DEFAULT_VERIFIER_IMAGE

# --- TestNet signing (operator-provided; never committed) -----------------------------------
# Funded sender key for task-04-send-tx on TestNet. The harness does not inject this into task
# prompts; the operator must ensure the agent can access it (e.g. forward via docker env).
TESTNET_SENDER_PRIVKEY = _env("CKBBENCH_TESTNET_SENDER_PRIVKEY", "BENCH_TESTNET_SENDER_PRIVKEY", default="")

# --- DevNet signing (public development fixture; DEVNET ONLY) -------------------------------
# The genesis key for the first secp256k1 issued cell of containers/devnet/config/specs/dev.toml
# (lock args 0xc8328aab..., also the block_assembler, so mining rewards keep it funded). This is the
# standard `ckb init --chain dev` fixture: published, so anyone can spend whatever its lock holds on
# any chain (ADR-0007). It is NOT a secret and NOT an operator credential -- never fund it and never
# reuse it on TestNet or Mainnet, which is why signer selection is keyed on the cell's chain.
DEVNET_GENESIS_PRIVKEY = "0xd00c06bfd800d27397002dca6fb0993d5ba6399b4238b2f29ee9deb97593d2bc"

CHAIN_PROFILES = ("devnet", "testnet")


def rpc_url_for(chain: str) -> str:
    """Resolve a chain profile name to its RPC URL (the only thing that differs between
    DevNet and TestNet verification - ADR-0005 symmetry)."""
    if chain == "devnet":
        return DEVNET_RPC
    if chain == "testnet":
        return TESTNET_RPC
    raise ValueError(f"unknown chain profile {chain!r}; expected one of {CHAIN_PROFILES}")


# --- The condition ladder (ADR-0011 / RECOMMENDATION) ---------------------------------------
# Each arm fixes whether the MCP is present and whether the prompt permits web research.
# The headline result is the C - B delta. LADDER_ORDER is the X-axis order for the chart.
ARMS = ("A", "B", "C", "D")
LADDER_ORDER = ARMS  # A -> B -> C -> D on the ladder chart's X axis (ADR-0011)

# arm -> (mcp_enabled, web_research_allowed). Immutable so a later phase cannot corrupt the
# ladder by reassigning an entry at run time.
ARM_MATRIX = MappingProxyType({
    "A": (False, False),  # floor: innate ability
    "B": (False, True),   # value of ordinary web research
    "C": (True, True),    # MCP value on top of web  <- headline
    "D": (True, False),   # curated MCP vs stale web (diagnostic)
})

# Egress policy per arm is a SEPARATE, explicit invariant, not silently derived from the web
# flag: on A/D the container egress is BLOCKED to an allowlist (chain RPC + MCP + proxy) at the
# network layer (ADR-0006); on B/C ordinary web is OBSERVED through the proxy but permitted.
# Phase 3 consumes this to configure the proxy per arm. Keeping it explicit means a future
# "prompt-only" regression cannot quietly weaken the hard network control.
EGRESS_MODE_BY_ARM = MappingProxyType({
    "A": "block",
    "B": "observe",
    "C": "observe",
    "D": "block",
})
