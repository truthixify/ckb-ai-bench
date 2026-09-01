"""Immutable chain identities used by independently executed Tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ckbbench.run.task_attempt import artifact_sha256

CHAIN_PROFILE_SCHEMA_VERSION = "ckbbench-chain-profile-v1"

ChainTrack = Literal["testnet", "local-hermetic"]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_HASH32 = re.compile(r"^0x[0-9a-f]{64}$")


class ChainProfileError(ValueError):
    """A chain profile is malformed or contradicts its execution track."""


def _exact(document: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != fields:
        raise ChainProfileError(f"{label} must contain exactly the reviewed fields")
    return document


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ChainProfileError(f"{label} must be a bounded public identifier")
    return value


@dataclass(frozen=True)
class ChainProfile:
    profile_id: str
    chain_track: ChainTrack
    chain_id: str | None
    genesis_hash: str | None
    schema_version: str = CHAIN_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _id(self.profile_id, "chain profile ID")
        if self.chain_track not in {"testnet", "local-hermetic"}:
            raise ChainProfileError("chain profile track is unsupported")
        if self.chain_track == "testnet":
            _id(self.chain_id, "chain ID")
            if not isinstance(self.genesis_hash, str) or _HASH32.fullmatch(
                self.genesis_hash
            ) is None or self.genesis_hash == "0x" + "0" * 64:
                raise ChainProfileError("TestNet profile needs a 32-byte genesis hash")
        elif self.chain_id is not None or self.genesis_hash is not None:
            raise ChainProfileError("local-hermetic profile cannot carry public-chain identity")
        if self.schema_version != CHAIN_PROFILE_SCHEMA_VERSION:
            raise ChainProfileError("chain profile schema version is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "chain_track": self.chain_track,
            "genesis_hash": self.genesis_hash,
            "profile_id": self.profile_id,
            "schema_version": self.schema_version,
        }

    @property
    def sha256(self) -> str:
        return artifact_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, document: Any) -> ChainProfile:
        return cls(**_exact(document, {
            "chain_id",
            "chain_track",
            "genesis_hash",
            "profile_id",
            "schema_version",
        }, "chain profile"))


LOCAL_HERMETIC_PROFILE = ChainProfile(
    profile_id="local-hermetic-v1",
    chain_track="local-hermetic",
    chain_id=None,
    genesis_hash=None,
)
