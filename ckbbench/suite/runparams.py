"""Two-class run-params pre-step (ADR-0009).

Generates concrete per-run values from a Task's parameter schema before the agent wakes,
splitting prompt-injected (agent-safe) from verifier-private (secrets). Verifier-private
values must never be written into the mount during the agent's run.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ckbbench.suite.model import ParamSpec, Task

RpcCallable = Callable[[str, list[Any]], Any]

BASE_SHANNONS = 100 * 100_000_000  # 100 CKB
_NONCE_OFFSET_SPACE = 2**31 * 4 + 4  # ~33 bits of entropy in the low shannons


@dataclass(frozen=True)
class RunParams:
    """Concrete run values split by security class."""

    prompt_injected: dict[str, Any]
    verifier_private: dict[str, Any]


def high_entropy_nonce_amount_shannons() -> str:
    """Per-run nonce amount: 100 CKB base plus ~33 bits of random low-shannon offset."""
    offset = secrets.randbelow(2**31) * 4 + secrets.randbelow(4)
    return str(BASE_SHANNONS + offset)


def make_rpc_client(rpc_url: str) -> RpcCallable:
    """Build a direct CKB JSON-RPC client (Verifier must use direct RPC, never MCP)."""

    def call(method: str, params: list[Any]) -> Any:
        body = json.dumps({"id": 1, "jsonrpc": "2.0", "method": method, "params": params}).encode()
        req = urllib.request.Request(
            rpc_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                payload = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise RuntimeError(f"RPC {method} to {rpc_url} failed: {exc}") from exc
        if "error" in payload:
            raise RuntimeError(f"RPC {method} error: {payload['error']}")
        return payload["result"]

    return call


def _generate_value(
    spec: ParamSpec,
    cache: dict[str, Any],
    rpc: RpcCallable,
) -> Any:
    """Generate one param value; reuse cached draws for the same generator kind."""
    if spec.generator in cache:
        return cache[spec.generator]
    if spec.generator == "static":
        if spec.static_value is None:
            raise ValueError(f"param {spec.name!r} uses static generator without static_value")
        value: Any = spec.static_value
    elif spec.generator == "harness_tip":
        tip_hex = rpc("get_tip_block_number", [])
        value = int(tip_hex, 16)
    elif spec.generator == "high_entropy_nonce_amount_shannons":
        value = high_entropy_nonce_amount_shannons()
    elif spec.generator == "recipient_args":
        if spec.static_value is not None:
            value = spec.static_value
        else:
            raise ValueError(f"param {spec.name!r} recipient_args requires static_value in v1")
    else:
        raise ValueError(f"unknown generator {spec.generator!r}")
    cache[spec.generator] = value
    return value


def generate_run_params(
    task: Task,
    rpc_url: str,
    *,
    rpc: RpcCallable | None = None,
) -> RunParams:
    """Generate concrete run values for ``task`` using direct RPC where required."""
    client = rpc if rpc is not None else make_rpc_client(rpc_url)
    cache: dict[str, Any] = {}
    prompt_injected: dict[str, Any] = {}
    verifier_private: dict[str, Any] = {}

    for spec in task.param_schema:
        value = _generate_value(spec, cache, client)
        if spec.param_class == "prompt":
            prompt_injected[spec.name] = value
        else:
            verifier_private[spec.name] = value

    return RunParams(prompt_injected=prompt_injected, verifier_private=verifier_private)


def write_prompt_injected(
    params: RunParams,
    mount_dir: Path | str,
    *,
    filename: str = "task.json",
) -> Path:
    """Write prompt-injected params into the agent-readable mount area."""
    mount = Path(mount_dir)
    mount.mkdir(parents=True, exist_ok=True)
    path = mount / filename
    path.write_text(json.dumps(params.prompt_injected, indent=2, sort_keys=True) + "\n")
    return path


def write_verifier_private(
    params: RunParams,
    verifier_dir: Path | str,
    *,
    filename: str = "secret.json",
) -> Path:
    """Write verifier-private params into a harness-only directory (never the mount)."""
    vdir = Path(verifier_dir)
    vdir.mkdir(parents=True, exist_ok=True)
    path = vdir / filename
    path.write_text(json.dumps(params.verifier_private, indent=2, sort_keys=True) + "\n")
    return path