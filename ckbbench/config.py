"""Single source of truth for run-time constants and live-infrastructure references.

Volatile facts (RPC URLs, the MCP endpoint, the LLM proxy) live here ONCE so docs and code do
not drift. Anything here is overridable by environment variable so a run can be retargeted
without code edits (the matrix driver and CI both rely on that).

These are NOT secrets. The DevNet genesis keys are public dev.toml test keys by design
(ADR-0007); no private credential is ever stored in this repo.
"""

from __future__ import annotations

import os

# --- LLM access -----------------------------------------------------------------------------
# The local LLM proxy (OpenAI-compatible). Recommended models: openai + grok families.
# Routed through litellm in the agent driver (Phase 4).
LLM_API_BASE = os.getenv("CKBBENCH_LLM_API_BASE", "http://localhost:18321/v1")
LLM_API_KEY = os.getenv("CKBBENCH_LLM_API_KEY", "sk-noauth")

# --- MCP server (the thing under test) ------------------------------------------------------
# Pinned version is enforced at preflight (ADR-0010); a mismatch aborts a scored run.
MCP_URL = os.getenv("CKBBENCH_MCP_URL", "https://mcp.ckbdev.com/ckbai")
MCP_PINNED_VERSION = os.getenv("CKBBENCH_MCP_VERSION", "1.6.12")

# --- Chain profiles (scored separately, never merged) ---------------------------------------
# DevNet: a nervos/ckb --chain dev sidecar brought up per run (ADR-0007). The URL is the
# sidecar's address on the harness docker network; default targets a local sidecar.
DEVNET_RPC = os.getenv("CKBBENCH_DEVNET_RPC", "http://127.0.0.1:8114")
# TestNet: the self-hosted testnet archive node (inventory: 192.168.0.73).
TESTNET_RPC = os.getenv("CKBBENCH_TESTNET_RPC", "http://192.168.0.73:18114")

CHAIN_PROFILES = ("devnet", "testnet")


def rpc_url_for(chain: str) -> str:
    """Resolve a chain profile name to its RPC URL (the only thing that differs between
    DevNet and TestNet verification - ADR-0005 symmetry)."""
    if chain == "devnet":
        return DEVNET_RPC
    if chain == "testnet":
        return TESTNET_RPC
    raise ValueError(f"unknown chain profile {chain!r}; expected one of {CHAIN_PROFILES}")


# --- The condition ladder (ADR / RECOMMENDATION) --------------------------------------------
# Each arm fixes whether the MCP is present and whether the prompt permits web research.
# The headline result is the C - B delta.
ARMS = ("A", "B", "C", "D")

# arm -> (mcp_enabled, web_research_allowed). Egress policy is derived from web_research_allowed
# in Phase 3 (A/D block to an allowlist; B/C permit observed web).
ARM_MATRIX = {
    "A": (False, False),  # floor: innate ability
    "B": (False, True),   # value of ordinary web research
    "C": (True, True),    # MCP value on top of web  <- headline
    "D": (True, False),   # curated MCP vs stale web (diagnostic)
}
