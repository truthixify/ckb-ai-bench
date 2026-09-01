"""Bounded capture of the public CKB AI treatment catalogs."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ckbbench.config import MCP_PINNED_VERSION, MCP_URL
from ckbbench.run.treatment_surface import (
    TreatmentSurfaceError,
    TreatmentSurfaceProfile,
    profile_bytes,
)

CAPTURE_REQUEST_LIMIT = 3
PROFILE_FILENAMES = (
    "ckb-ai-control-local-v1.json",
    "ckb-ai-control-testnet-v1.json",
    "ckb-ai-treatment-local-v1.json",
    "ckb-ai-treatment-testnet-v1.json",
)


class SurfaceCaptureError(RuntimeError):
    """The catalog could not be captured within its immutable boundary."""


def _publication_target(output_dir: Path | str) -> Path:
    destination = Path(output_dir)
    if destination.name in {"", ".", ".."}:
        raise SurfaceCaptureError("surface-profile output directory is invalid")
    if destination.exists() or destination.is_symlink():
        raise SurfaceCaptureError("surface-profile output directory already exists")
    absolute = destination if destination.is_absolute() else Path.cwd() / destination
    candidate = absolute.parent
    while True:
        if candidate.is_symlink():
            raise SurfaceCaptureError("surface-profile output parent cannot contain a symlink")
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    try:
        parent = absolute.parent.resolve(strict=True)
    except OSError as exc:
        raise SurfaceCaptureError("surface-profile output parent is unavailable") from exc
    if not parent.is_dir():
        raise SurfaceCaptureError("surface-profile output parent must be a real directory")
    return parent / destination.name


class CatalogClient(Protocol):
    @property
    def request_count(self) -> int: ...

    def initialize(self) -> dict[str, Any]: ...

    def list_tools(self) -> list[dict[str, Any]]: ...

    def list_resources(self) -> list[dict[str, Any]]: ...


def _server_identity(initialized: Any, expected_version: str) -> tuple[str, str]:
    server = initialized.get("serverInfo") if isinstance(initialized, dict) else None
    name = server.get("name") if isinstance(server, dict) else None
    version = server.get("version") if isinstance(server, dict) else None
    if version != expected_version:
        raise SurfaceCaptureError("CKB AI server version differs from the configured pin")
    if not isinstance(name, str):
        raise SurfaceCaptureError("CKB AI initialize response lacks a server identity")
    return name, version


def capture_profiles(
    client: CatalogClient,
    *,
    expected_version: str = MCP_PINNED_VERSION,
) -> tuple[TreatmentSurfaceProfile, ...]:
    """Use exactly initialize, tools/list and resources/list to derive four public profiles."""
    before = client.request_count
    initialized = client.initialize()
    tools = client.list_tools()
    resources = client.list_resources()
    if client.request_count - before != CAPTURE_REQUEST_LIMIT:
        raise SurfaceCaptureError("CKB AI catalog capture did not use exactly three requests")
    server_name, server_version = _server_identity(initialized, expected_version)

    common = {
        "server_name": server_name,
        "server_version": server_version,
        "tools": tools,
        "resources": resources,
    }
    try:
        profiles = (
            TreatmentSurfaceProfile.from_catalogs(
                profile_id="ckb-ai-control-local-v1",
                claims_live_chain=False,
                allowed_tools=(),
                allowed_resource_prefixes=(),
                **common,
            ),
            TreatmentSurfaceProfile.from_catalogs(
                profile_id="ckb-ai-control-testnet-v1",
                claims_live_chain=True,
                allowed_tools=(),
                allowed_resource_prefixes=(),
                **common,
            ),
            TreatmentSurfaceProfile.from_catalogs(
                profile_id="ckb-ai-treatment-local-v1",
                claims_live_chain=False,
                allowed_tools=("search_resources",),
                allowed_resource_prefixes=("ckb://docs/",),
                **common,
            ),
            TreatmentSurfaceProfile.from_catalogs(
                profile_id="ckb-ai-treatment-testnet-v1",
                claims_live_chain=True,
                allowed_tools=("search_resources",),
                allowed_resource_prefixes=("ckb://docs/",),
                **common,
            ),
        )
    except TreatmentSurfaceError as exc:
        raise SurfaceCaptureError("CKB AI catalog cannot satisfy the released surfaces") from exc
    return profiles


def _publish_directory(output_dir: Path, profiles: tuple[TreatmentSurfaceProfile, ...]) -> None:
    try:
        parent = output_dir.parent.resolve(strict=True)
    except OSError as exc:
        raise SurfaceCaptureError("surface-profile output parent is unavailable") from exc
    if output_dir.exists() or output_dir.is_symlink():
        raise SurfaceCaptureError("surface-profile output directory already exists")
    if not parent.is_dir():
        raise SurfaceCaptureError("surface-profile output parent must be a real directory")

    temporary = Path(tempfile.mkdtemp(prefix=".ckbbench-surfaces-", dir=parent))
    published: list[tuple[Path, tuple[int, int]]] = []
    claimed = False
    try:
        temporary.chmod(0o755)
        for profile in profiles:
            path = temporary / f"{profile.profile_id}.json"
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(profile_bytes(profile))
                stream.flush()
                os.fsync(stream.fileno())
        if tuple(sorted(path.name for path in temporary.iterdir())) != PROFILE_FILENAMES:
            raise SurfaceCaptureError("surface-profile publication set is incomplete")
        try:
            output_dir.mkdir(mode=0o755)
        except FileExistsError:
            raise SurfaceCaptureError(
                "surface-profile output directory already exists"
            ) from None
        claimed = True
        for filename in PROFILE_FILENAMES:
            source = temporary / filename
            destination = output_dir / filename
            identity = (source.stat().st_dev, source.stat().st_ino)
            os.link(source, destination, follow_symlinks=False)
            published.append((destination, identity))
            source.unlink()
        directory = os.open(output_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        for path, identity in reversed(published):
            try:
                observed = path.stat(follow_symlinks=False)
                if (observed.st_dev, observed.st_ino) == identity:
                    path.unlink()
            except OSError:
                pass
        if claimed:
            try:
                output_dir.rmdir()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def capture_and_publish(
    output_dir: Path | str,
    *,
    endpoint: str = MCP_URL,
    expected_version: str = MCP_PINNED_VERSION,
    client_factory: Callable[..., CatalogClient] | None = None,
) -> tuple[TreatmentSurfaceProfile, ...]:
    """Capture once and publish only canonical profile records, never raw response bodies."""
    destination = _publication_target(output_dir)
    if client_factory is None:
        from ckb_mcp import CkbMcpClient

        client_factory = CkbMcpClient
    try:
        client = client_factory(url=endpoint, request_limit=CAPTURE_REQUEST_LIMIT)
        profiles = capture_profiles(client, expected_version=expected_version)
        _publish_directory(destination, profiles)
    except SurfaceCaptureError:
        raise
    except Exception as exc:
        raise SurfaceCaptureError(
            f"CKB AI catalog capture failed safely ({type(exc).__name__})"
        ) from None
    return profiles
