"""Arm resolution: ladder cell to concrete run config (ADR-0011, RECOMMENDATION §2/§6).

Maps an arm letter (A/B/C/D) to MCP presence, web-research permission, egress mode,
and arm-specific composed-prompt preamble bits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ckbbench.config import ARM_MATRIX, EGRESS_MODE_BY_ARM

EgressMode = Literal["block", "observe"]

_NO_WEB_RESEARCH = (
    "You must NOT use web research. Do not fetch documentation or search the web. "
    "Rely only on your training knowledge, the tools available to you, and direct "
    "chain interaction."
)

_WEB_RESEARCH_ALLOWED = (
    "You may use web research if needed to complete the tasks."
)

# Chain-neutral by construction: naming a chain here would hand C/D a chain fact A/B never see,
# and would be wrong whenever the cell's chain is not the one named. The selected chain reaches
# every arm identically through the composed chain context and the agent environment (plan §8.1).
# The MCP surface is documentation only (ADR-0013), so steering chain work to it would point the
# model at an endpoint bound to a chain this run is not graded on.
_MCP_STEERING = (
    "Use mcp_call only for CKB documentation and reference lookup. For live chain state, signing, "
    "transaction submission and confirmation, use the selected endpoint in CKB_RPC_URL."
)


@dataclass(frozen=True)
class ArmConfig:
    """Concrete run configuration for one ladder arm."""

    arm: str
    mcp_enabled: bool
    web_research_allowed: bool
    egress_mode: EgressMode
    prompt_preamble: str


def _build_prompt_preamble(*, web_research_allowed: bool, mcp_enabled: bool) -> str:
    parts: list[str] = []
    if web_research_allowed:
        parts.append(_WEB_RESEARCH_ALLOWED)
    else:
        parts.append(_NO_WEB_RESEARCH)
    if mcp_enabled:
        parts.append(_MCP_STEERING)
    return "\n\n".join(parts)


def resolve_arm(arm: str) -> ArmConfig:
    """Resolve ``arm`` from ``ARM_MATRIX`` + ``EGRESS_MODE_BY_ARM``."""
    if arm not in ARM_MATRIX:
        raise ValueError(f"unknown arm {arm!r}; expected one of {tuple(ARM_MATRIX)}")

    mcp_enabled, web_research_allowed = ARM_MATRIX[arm]
    egress_mode: EgressMode = EGRESS_MODE_BY_ARM[arm]  # type: ignore[assignment]

    return ArmConfig(
        arm=arm,
        mcp_enabled=mcp_enabled,
        web_research_allowed=web_research_allowed,
        egress_mode=egress_mode,
        prompt_preamble=_build_prompt_preamble(
            web_research_allowed=web_research_allowed,
            mcp_enabled=mcp_enabled,
        ),
    )