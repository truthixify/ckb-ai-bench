"""Load and validate a Suite from a registry directory (ADR-0008).

The registry is a ``manifest.json`` index plus one directory per Task holding
``meta.json`` and ``prompt.txt``. Validation fails loud on any violation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_MAX_REGISTRY_FILE_BYTES = 1 << 20  # 1 MiB cap per registry file (prompt/meta); larger = error

from ckbbench.run.retry_policy import RETRY_POLICY_ID, RETRY_POLICY_SHA256
from ckbbench.suite.model import (
    OnchainVerifierSpec,
    ParamSpec,
    Suite,
    SuitePins,
    Task,
)
from ckbbench.suite.execution_contract import (
    TASK_EXECUTION_SCHEMA_VERSION,
    TaskExecutionContract,
    TaskExecutionContractError,
)

_MANIFEST_REQUIRED = ("suite_semver", "chain_profile", "mcp_server_version", "tasks")
_META_REQUIRED = ("id", "proof_file", "score", "kind")
_PIN_KEYS = frozenset({
    "agent_image_digest",
    "verifier_image_digest",
    "mcp_tools_digest",
    "scoring_schema_version",
    "retry_policy_id",
    "retry_policy_sha256",
    "toolchain_versions",
})
# The agent and verifier are different images with different contents; one value cannot identify
# both. A 2.0.0 registry must not carry the retired singular key, even silently as extra data.
_LEGACY_PIN_KEY = "docker_image_digest"
_ROLE_PIN_RE = re.compile(r"sha256:[0-9a-f]{64}")
# An all-zero digest is well-formed but identifies nothing; the brief lists it with TO_BE_FILLED as
# a forbidden placeholder.
_NULL_PIN = "sha256:" + "0" * 64


def _is_released(suite_semver: Any) -> bool:
    """Major version >= 2 marks a released suite that must carry both exact role pins.

    Development and synthetic registries stay at 1.x and may omit pins entirely, which the brief
    requires; only a real release carries the stricter invariant.
    """
    if not isinstance(suite_semver, str):
        return False
    head = suite_semver.split(".", 1)[0]
    return head.isdigit() and int(head) >= 2
_RESERVED_MANIFEST_KEYS = frozenset({
    *_MANIFEST_REQUIRED,
    "note",
    "tasks",
    "task_execution_schema_version",
    *_PIN_KEYS,
})


class RegistryError(ValueError):
    """Raised when a registry directory violates the Suite contract."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise RegistryError("registry input contains a duplicate JSON key")
        document[key] = value
    return document


def load_suite(registry_dir: Path | str) -> Suite:
    """Load and validate a Suite from ``registry_dir``."""
    root = Path(registry_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise RegistryError(f"missing manifest.json in {root}")

    manifest = _load_json(manifest_path, "manifest.json")
    for key in _MANIFEST_REQUIRED:
        if key not in manifest:
            raise RegistryError(f"manifest.json missing required field {key!r}")

    task_ids = manifest["tasks"]
    if not isinstance(task_ids, list) or not task_ids:
        raise RegistryError("manifest.json tasks must be a non-empty ordered list")

    pins = _parse_pins(manifest)
    tasks: list[Task] = []
    seen_ids: set[str] = set()
    proof_files: dict[str, str] = {}

    for task_id in task_ids:
        if not isinstance(task_id, str) or not task_id:
            raise RegistryError(f"invalid task id in manifest: {task_id!r}")
        tdir = root / task_id
        if not tdir.is_dir():
            raise RegistryError(f"manifest task {task_id!r} has no directory at {tdir}")

        meta = _load_json(tdir / "meta.json", f"{task_id}/meta.json")
        _validate_meta(meta, task_id)

        tid = meta["id"]
        if tid in seen_ids:
            raise RegistryError(f"duplicate task id {tid!r}")
        seen_ids.add(tid)

        proof_file = meta["proof_file"]
        if not isinstance(proof_file, str) or not proof_file.strip():
            raise RegistryError(f"task {tid!r} missing proof_file")
        proof_files[tid] = proof_file

        score = meta["score"]
        if not isinstance(score, int) or score <= 0:
            raise RegistryError(f"task {tid!r} score must be a positive integer, got {score!r}")

        prompt_path = tdir / "prompt.txt"
        if not prompt_path.is_file():
            raise RegistryError(f"task {tid!r} missing prompt.txt")
        prompt_fragment = _read_text_guarded(prompt_path, f"{task_id}/prompt.txt")

        kind = meta["kind"]
        verifier = _parse_verifier(meta, tid, tdir)
        param_schema = _parse_param_schema(meta.get("param_schema", []), tid)

        scored = meta.get("scored", True)
        if not isinstance(scored, bool):
            raise RegistryError(f"task {tid!r} 'scored' must be a boolean, got {scored!r}")

        execution = None
        if "execution" in meta:
            try:
                execution = TaskExecutionContract.from_dict(meta["execution"])
            except TaskExecutionContractError as exc:
                raise RegistryError(f"task {tid!r} execution contract is invalid: {exc}") from exc

        tasks.append(
            Task(
                id=tid,
                prompt_fragment=prompt_fragment,
                score=score,
                proof_file=proof_file,
                kind=kind,
                verifier=verifier,
                param_schema=param_schema,
                scored=scored,
                execution=execution,
            )
        )

    _validate_fragment_independence(tasks, proof_files)

    execution_schema = manifest.get("task_execution_schema_version")
    if execution_schema is not None and execution_schema != TASK_EXECUTION_SCHEMA_VERSION:
        raise RegistryError("manifest task execution schema version is unsupported")
    major = str(manifest["suite_semver"]).split(".", 1)[0]
    requires_execution = major.isdigit() and int(major) >= 4
    if requires_execution and execution_schema is None:
        raise RegistryError("an independent-Task suite must declare its execution schema")
    if execution_schema is not None:
        missing = [task.id for task in tasks if task.execution is None]
        if missing:
            raise RegistryError("every Task in an execution-contract suite needs a contract")
        contract_ids = [task.execution.contract_id for task in tasks if task.execution is not None]
        if len(contract_ids) != len(set(contract_ids)):
            raise RegistryError("Task execution contract IDs must be unique")
        if (
            pins.retry_policy_id != RETRY_POLICY_ID
            or pins.retry_policy_sha256 != RETRY_POLICY_SHA256
        ):
            raise RegistryError("an execution-contract suite must pin the supported retry policy")

    return Suite(
        suite_semver=manifest["suite_semver"],
        chain_profile=manifest["chain_profile"],
        mcp_server_version=manifest["mcp_server_version"],
        tasks=tuple(tasks),
        pins=pins,
        task_execution_schema_version=execution_schema,
    )


def _read_text_guarded(path: Path, label: str) -> str:
    """Read a registry text file, refusing one larger than the cap and giving a clear error on
    non-UTF8 content (rather than a raw UnicodeDecodeError leaking out of load)."""
    size = path.stat().st_size
    if size > _MAX_REGISTRY_FILE_BYTES:
        raise RegistryError(f"{label} is {size} bytes, over the {_MAX_REGISTRY_FILE_BYTES}-byte cap")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryError(f"{label} is not valid UTF-8: {exc}") from exc


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(
            _read_text_guarded(path, label),
            object_pairs_hook=_unique_object,
        )
    except RegistryError:
        raise
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"{label} must be a JSON object")
    return data


def _validate_meta(meta: dict[str, Any], task_dir_name: str) -> None:
    for key in _META_REQUIRED:
        if key not in meta:
            raise RegistryError(f"task {task_dir_name}/meta.json missing required field {key!r}")
    if meta["id"] != task_dir_name:
        raise RegistryError(
            f"task directory {task_dir_name!r} meta id {meta['id']!r} must match directory name"
        )
    if meta["kind"] not in ("onchain", "code"):
        raise RegistryError(f"task {task_dir_name!r} kind must be 'onchain' or 'code'")


def _parse_verifier(meta: dict[str, Any], tid: str, tdir: Path) -> OnchainVerifierSpec | str:
    kind = meta["kind"]
    if kind == "onchain":
        for key in ("check", "rpc_method"):
            if key not in meta:
                raise RegistryError(f"onchain task {tid!r} meta.json missing {key!r}")
        rpc_params = meta.get("rpc_params", [])
        if not isinstance(rpc_params, list):
            raise RegistryError(f"onchain task {tid!r} rpc_params must be a list")
        return OnchainVerifierSpec(
            check=meta["check"],
            rpc_method=meta["rpc_method"],
            rpc_params=tuple(rpc_params),
        )
    verifier_dir = meta.get("verifier_dir")
    if not isinstance(verifier_dir, str) or not verifier_dir.strip():
        raise RegistryError(f"code task {tid!r} meta.json missing verifier_dir")
    path = tdir / verifier_dir
    if not path.is_dir():
        raise RegistryError(f"code task {tid!r} verifier_dir {verifier_dir!r} not found at {path}")
    return verifier_dir


def _parse_param_schema(raw: Any, tid: str) -> tuple[ParamSpec, ...]:
    if not isinstance(raw, list):
        raise RegistryError(f"task {tid!r} param_schema must be a list")
    specs: list[ParamSpec] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise RegistryError(f"task {tid!r} param_schema[{idx}] must be an object")
        for key in ("name", "class", "generator"):
            if key not in entry:
                raise RegistryError(f"task {tid!r} param_schema[{idx}] missing {key!r}")
        param_class = entry["class"]
        if param_class not in ("prompt", "verifier"):
            raise RegistryError(
                f"task {tid!r} param_schema[{idx}] class must be 'prompt' or 'verifier'"
            )
        generator = entry["generator"]
        allowed = {
            "fresh_blob_hex_32",
            "harness_tip",
            "high_entropy_nonce_amount_shannons",
            "recipient_args",
            "static",
        }
        if generator not in allowed:
            raise RegistryError(f"task {tid!r} param_schema[{idx}] unknown generator {generator!r}")
        static_value = entry.get("static_value")
        if generator == "static" and not isinstance(static_value, str):
            raise RegistryError(f"task {tid!r} param_schema[{idx}] static generator requires static_value")
        if generator == "recipient_args" and static_value is not None and not isinstance(static_value, str):
            raise RegistryError(f"task {tid!r} param_schema[{idx}] recipient_args static_value must be a string")
        share_group = entry.get("share_group")
        if share_group is not None and (not isinstance(share_group, str) or not share_group.strip()):
            raise RegistryError(
                f"task {tid!r} param_schema[{idx}] share_group must be a non-empty string when set"
            )
        specs.append(
            ParamSpec(
                name=entry["name"],
                param_class=param_class,
                generator=generator,
                static_value=static_value,
                share_group=share_group,
            )
        )
    return tuple(specs)


def _role_pin(manifest: dict[str, Any], key: str) -> str | None:
    """A declared role pin must be exactly ``sha256:`` plus 64 lowercase hex digits.

    Production passes this value straight to Docker as an immutable local image ID, so a tag, an
    uppercase digest, or a truncated value must fail closed rather than resolve to something else.
    """
    value = manifest.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryError(f"manifest {key} must be a string")
    if not _ROLE_PIN_RE.fullmatch(value):
        raise RegistryError(
            f"manifest {key} must be 'sha256:' followed by 64 lowercase hex digits"
        )
    if value == _NULL_PIN:
        raise RegistryError(f"manifest {key} is the all-zero placeholder digest")
    return value


def _parse_pins(manifest: dict[str, Any]) -> SuitePins:
    toolchain = manifest.get("toolchain_versions", {})
    if toolchain is not None and not isinstance(toolchain, dict):
        raise RegistryError("manifest toolchain_versions must be an object")
    # A released suite must carry both role pins, and they must differ. Absent pins stay legal for
    # synthetic/development registries, but a real release that omits one would silently resolve
    # that role to the mutable `latest` default while the freeze claimed an immutable image.
    if _LEGACY_PIN_KEY in manifest:
        raise RegistryError(
            f"manifest {_LEGACY_PIN_KEY} is retired; declare agent_image_digest and "
            "verifier_image_digest separately"
        )
    extra = {
        key: manifest[key]
        for key in manifest
        if key not in _RESERVED_MANIFEST_KEYS
    }
    agent_pin = _role_pin(manifest, "agent_image_digest")
    verifier_pin = _role_pin(manifest, "verifier_image_digest")
    retry_policy_id = manifest.get("retry_policy_id")
    retry_policy_sha256 = manifest.get("retry_policy_sha256")
    if retry_policy_id is not None and (
        not isinstance(retry_policy_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}", retry_policy_id) is None
    ):
        raise RegistryError("manifest retry_policy_id must be a bounded public identifier")
    if retry_policy_sha256 is not None and (
        not isinstance(retry_policy_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", retry_policy_sha256) is None
    ):
        raise RegistryError("manifest retry_policy_sha256 must be a lowercase SHA-256 digest")
    if (retry_policy_id is None) != (retry_policy_sha256 is None):
        raise RegistryError("manifest retry policy ID and digest must be present together")
    if _is_released(manifest.get("suite_semver")):
        for key, value in (("agent_image_digest", agent_pin),
                           ("verifier_image_digest", verifier_pin)):
            if value is None:
                raise RegistryError(
                    f"a released suite must declare {key}; without it that role would fall back "
                    "to a mutable default"
                )
        if agent_pin == verifier_pin:
            raise RegistryError(
                "agent_image_digest and verifier_image_digest must differ; the agent and verifier "
                "are different images and one value cannot identify both"
            )
    return SuitePins(
        agent_image_digest=agent_pin,
        verifier_image_digest=verifier_pin,
        mcp_tools_digest=manifest.get("mcp_tools_digest"),
        scoring_schema_version=manifest.get("scoring_schema_version"),
        retry_policy_id=retry_policy_id,
        retry_policy_sha256=retry_policy_sha256,
        toolchain_versions=dict(toolchain or {}),
        extra=extra,
    )


def _validate_fragment_independence(tasks: list[Task], proof_files: dict[str, str]) -> None:
    """ADR-0008 v1: no Task fragment may reference another Task's proof_file.

    Matched on a token boundary, not a raw substring, so a fragment that merely contains the
    proof name as an ordinary word (or as a substring of a longer filename) is not a false
    positive; only a standalone reference to another Task's exact proof_file trips it.
    """
    for task in tasks:
        for other_id, other_proof in proof_files.items():
            if other_id == task.id:
                continue
            if re.search(rf"(?<![\w.-]){re.escape(other_proof)}(?![\w.-])", task.prompt_fragment):
                raise RegistryError(
                    f"task {task.id!r} prompt_fragment references another task's proof_file "
                    f"{other_proof!r} (strict independence violated)"
                )
