"""Two-class run-params pre-step (ADR-0009).

Derives concrete seeded values from a Task's parameter schema before the agent wakes,
splitting prompt-injected (agent-safe) from verifier-private (secrets). Verifier-private
values must never be written into the mount during the agent's run.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ckbbench.ckb_rpc import DEFAULT_RPC_TIMEOUT, RpcCallable, make_rpc_client
from ckbbench.suite.model import ParamSpec, Task

BASE_SHANNONS = 100 * 100_000_000  # 100 CKB
_NONCE_OFFSET_SPACE = 2**33
RUN_PARAMS_DERIVATION_VERSION = "seeded-sha256-v1"


@dataclass(frozen=True)
class RunParams:
    """Concrete run values split by security class."""

    prompt_injected: dict[str, Any]
    verifier_private: dict[str, Any]


def high_entropy_nonce_amount_shannons() -> str:
    """Per-run nonce amount: 100 CKB base plus ~33 bits of random low-shannon offset."""
    offset = secrets.randbelow(2**31) * 4 + secrets.randbelow(4)
    return str(BASE_SHANNONS + offset)


def fresh_blob_hex_32() -> str:
    """256 fresh bits per draw as lowercase 0x + 64 hex digits.

    token_bytes(32) rather than token_hex: the contract is exactly 32 random bytes, and the caller
    formats them, so a future formatting change cannot quietly shorten the entropy.
    """
    return "0x" + secrets.token_bytes(32).hex()


def derive_seeded_bytes(seed: int, task_id: str, draw_id: str, length: int) -> bytes:
    """Derive stable task material without sharing mutable RNG state between cells."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an int")
    if not task_id or not draw_id:
        raise ValueError("task_id and draw_id must be non-empty")
    if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        raise ValueError("length must be a positive int")

    context = json.dumps(
        [RUN_PARAMS_DERIVATION_VERSION, seed, task_id, draw_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hashlib.sha256(context + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(output[:length])


def _draw_value(
    spec: ParamSpec,
    rpc: RpcCallable,
    *,
    seeded_bytes: Callable[[int], bytes] | None = None,
) -> Any:
    """Produce one value for ``spec`` from seeded bytes or a standalone secure draw."""
    if spec.generator == "static":
        if spec.static_value is None:
            raise ValueError(f"param {spec.name!r} uses static generator without static_value")
        return spec.static_value
    if spec.generator == "fresh_blob_hex_32":
        raw = secrets.token_bytes(32) if seeded_bytes is None else seeded_bytes(32)
        return "0x" + raw.hex()
    if spec.generator == "harness_tip":
        return int(rpc("get_tip_block_number", []), 16)
    if spec.generator == "high_entropy_nonce_amount_shannons":
        if seeded_bytes is None:
            return high_entropy_nonce_amount_shannons()
        offset = int.from_bytes(seeded_bytes(8), "big") % _NONCE_OFFSET_SPACE
        return str(BASE_SHANNONS + offset)
    if spec.generator == "recipient_args":
        if spec.static_value is None:
            raise ValueError(f"param {spec.name!r} recipient_args requires static_value in v1")
        return spec.static_value
    raise ValueError(f"unknown generator {spec.generator!r}")


def generate_run_params(
    task: Task,
    rpc_url: str,
    *,
    seed: int,
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
    prompt_shared_groups = {
        spec.share_group
        for spec in task.param_schema
        if spec.param_class == "prompt" and spec.share_group is not None
    }

    for index, spec in enumerate(task.param_schema):
        draw_id = (
            f"share:{spec.share_group}"
            if spec.share_group is not None
            else f"param:{index}:{spec.param_class}:{spec.name}"
        )

        def draw(length: int, *, identity: str = draw_id) -> bytes:
            return derive_seeded_bytes(seed, task.id, identity, length)

        seeded_draw = (
            draw
            if spec.param_class == "prompt" or spec.share_group in prompt_shared_groups
            else None
        )

        if spec.share_group is not None:
            prior = shared_spec.get(spec.share_group)
            if prior is None:
                shared[spec.share_group] = _draw_value(spec, client, seeded_bytes=seeded_draw)
                shared_spec[spec.share_group] = spec
            elif (prior.generator, prior.static_value) != (spec.generator, spec.static_value):
                raise ValueError(
                    f"share_group {spec.share_group!r} mixes incompatible specs: "
                    f"{prior.name!r} ({prior.generator}/{prior.static_value!r}) vs "
                    f"{spec.name!r} ({spec.generator}/{spec.static_value!r})"
                )
            value = shared[spec.share_group]
        else:
            value = _draw_value(spec, client, seeded_bytes=seeded_draw)
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
    # filename must be a bare name, not a path: a "../mount/secret.json" or absolute filename
    # would escape vdir after the directory guard and could land in the mount.
    if filename != Path(filename).name or filename in ("", ".", ".."):
        raise ValueError(f"filename must be a bare name, not a path: {filename!r}")

    vdir = Path(verifier_dir).resolve()
    mount = Path(mount_dir).resolve() if mount_dir is not None else None

    def _inside_mount(p: Path) -> bool:
        return mount is not None and (p == mount or mount in p.parents)

    if _inside_mount(vdir):
        raise ValueError(
            f"refusing to write verifier-private params into the agent mount: "
            f"{vdir} is inside {mount} (ADR-0009 trust boundary)"
        )
    vdir.mkdir(parents=True, exist_ok=True)
    path = vdir / filename
    # Re-check the FINAL resolved path (an existing symlink at vdir/filename could redirect the
    # write into the mount even though vdir itself is clean). Refuse a symlink target too.
    final = path.resolve()
    if path.is_symlink() or _inside_mount(final) or final.parent != vdir:
        raise ValueError(
            f"refusing to write verifier-private params: final path {final} escapes {vdir} "
            f"or points into the agent mount (ADR-0009 trust boundary)"
        )
    path.write_text(json.dumps(params.verifier_private, indent=2, sort_keys=True) + "\n")
    return path
