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


DEFAULT_RPC_TIMEOUT = 30.0


def make_rpc_client(rpc_url: str, *, timeout: float = DEFAULT_RPC_TIMEOUT) -> RpcCallable:
    """Build a direct CKB JSON-RPC client (Verifier must use direct RPC, never MCP).

    ``timeout`` bounds each request so this pre-step (which runs BEFORE the agent and gates the
    whole run) cannot hang forever on a slow or unreachable node.
    """

    def call(method: str, params: list[Any]) -> Any:
        body = json.dumps({"id": 1, "jsonrpc": "2.0", "method": method, "params": params}).encode()
        req = urllib.request.Request(
            rpc_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise RuntimeError(f"RPC {method} to {rpc_url} failed: {exc}") from exc
        if "error" in payload:
            raise RuntimeError(f"RPC {method} error: {payload['error']}")
        return payload["result"]

    return call


def _draw_value(spec: ParamSpec, rpc: RpcCallable) -> Any:
    """Produce ONE fresh value for ``spec`` (no caching). static values are per-spec; the
    generators that need a per-run draw (tip, nonce) are drawn here once per call."""
    if spec.generator == "static":
        if spec.static_value is None:
            raise ValueError(f"param {spec.name!r} uses static generator without static_value")
        return spec.static_value
    if spec.generator == "harness_tip":
        return int(rpc("get_tip_block_number", []), 16)
    if spec.generator == "high_entropy_nonce_amount_shannons":
        return high_entropy_nonce_amount_shannons()
    if spec.generator == "recipient_args":
        if spec.static_value is None:
            raise ValueError(f"param {spec.name!r} recipient_args requires static_value in v1")
        return spec.static_value
    raise ValueError(f"unknown generator {spec.generator!r}")


def generate_run_params(
    task: Task,
    rpc_url: str,
    *,
    rpc: RpcCallable | None = None,
) -> RunParams:
    """Generate concrete run values for ``task`` using direct RPC where required.

    Value sharing is EXPLICIT, keyed on ``ParamSpec.share_group`` (ADR-0009): specs in the same
    non-None share_group draw a single value and both receive it (e.g. the amount the agent
    sends and the nonce the Verifier checks). Specs with no share_group draw independently, so
    two unrelated params can never silently collide on one value. A share_group must be internally
    consistent: every spec in it must use the same generator and static_value, else it is a
    registry authoring error and we fail loud.
    """
    client = rpc if rpc is not None else make_rpc_client(rpc_url)
    shared: dict[str, Any] = {}            # share_group -> the single drawn value
    shared_spec: dict[str, ParamSpec] = {}  # share_group -> the first spec (for consistency check)
    prompt_injected: dict[str, Any] = {}
    verifier_private: dict[str, Any] = {}

    for spec in task.param_schema:
        if spec.share_group is not None:
            prior = shared_spec.get(spec.share_group)
            if prior is None:
                shared[spec.share_group] = _draw_value(spec, client)
                shared_spec[spec.share_group] = spec
            elif (prior.generator, prior.static_value) != (spec.generator, spec.static_value):
                raise ValueError(
                    f"share_group {spec.share_group!r} mixes incompatible specs: "
                    f"{prior.name!r} ({prior.generator}/{prior.static_value!r}) vs "
                    f"{spec.name!r} ({spec.generator}/{spec.static_value!r})"
                )
            value = shared[spec.share_group]
        else:
            value = _draw_value(spec, client)
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
    mount_dir: Path | str | None = None,
) -> Path:
    """Write verifier-private params into a harness-only directory (never the mount).

    The trust boundary (ADR-0009) is that secrets never land where the agent can read them. To
    make a mis-wire impossible rather than merely conventional, pass ``mount_dir``: if
    ``verifier_dir`` resolves inside it, we refuse loudly instead of writing the secret into the
    agent's view.
    """
    vdir = Path(verifier_dir).resolve()
    if mount_dir is not None:
        mount = Path(mount_dir).resolve()
        if vdir == mount or mount in vdir.parents:
            raise ValueError(
                f"refusing to write verifier-private params into the agent mount: "
                f"{vdir} is inside {mount} (ADR-0009 trust boundary)"
            )
    vdir.mkdir(parents=True, exist_ok=True)
    path = vdir / filename
    path.write_text(json.dumps(params.verifier_private, indent=2, sort_keys=True) + "\n")
    return path