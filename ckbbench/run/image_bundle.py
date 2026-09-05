"""Portable, integrity-checked bundles for frozen benchmark images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

from ckbbench.run.suite_release import SuiteReleaseError, load_suite_release
from ckbbench.run.task_attempt import canonical_json_bytes

BUNDLE_SCHEMA_VERSION = "ckbbench-image-bundle-v1"
ARCHIVE_FILENAME = "images.tar"
MANIFEST_FILENAME = "manifest.json"
EXPECTED_PLATFORM = ("linux", "arm64")
RELEASE_FAMILY = "independent-task-suite-v1"
INSPECT_TIMEOUT_SECONDS = 60
TRANSFER_TIMEOUT_SECONDS = 1800
MAX_MANIFEST_BYTES = 1 << 20
_SUITE_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_OCI_BLOB = re.compile(r"^blobs/sha256/([0-9a-f]{64})$")
_OCI_INDEX_MEDIA_TYPES = frozenset({
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
})
_OCI_MANIFEST_MEDIA_TYPES = frozenset({
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
})


class ImageBundleError(RuntimeError):
    """A frozen image bundle is unsafe, incomplete, or inconsistent with its suite."""


@dataclass(frozen=True)
class FrozenImage:
    role: str
    image_id: str
    os: str = EXPECTED_PLATFORM[0]
    architecture: str = EXPECTED_PLATFORM[1]

    def to_dict(self) -> dict[str, str]:
        return {
            "architecture": self.architecture,
            "image_id": self.image_id,
            "os": self.os,
            "role": self.role,
        }


@dataclass(frozen=True)
class ImageBundleManifest:
    archive_sha256: str
    suite_semver: str
    suite_freeze_sha256: str
    images: tuple[FrozenImage, ...]
    schema_version: str = BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BUNDLE_SCHEMA_VERSION:
            raise ImageBundleError("image bundle schema is unsupported")
        if not _is_sha256(self.archive_sha256) or not _is_sha256(self.suite_freeze_sha256):
            raise ImageBundleError("image bundle contains an invalid digest")
        if not isinstance(self.suite_semver, str) or _SUITE_SEMVER.fullmatch(self.suite_semver) is None:
            raise ImageBundleError("image bundle contains an invalid suite version")
        if tuple(image.role for image in self.images) != ("agent", "verifier"):
            raise ImageBundleError("image bundle must contain agent and verifier in role order")
        if any((image.os, image.architecture) != EXPECTED_PLATFORM for image in self.images):
            raise ImageBundleError("image bundle contains an unsupported image platform")
        if any(not _is_image_id(image.image_id) for image in self.images):
            raise ImageBundleError("image bundle contains an invalid image ID")
        if len({image.image_id for image in self.images}) != len(self.images):
            raise ImageBundleError("image bundle repeats an image ID")

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_sha256": self.archive_sha256,
            "images": [image.to_dict() for image in self.images],
            "schema_version": self.schema_version,
            "suite_freeze_sha256": self.suite_freeze_sha256,
            "suite_semver": self.suite_semver,
        }


Run = Callable[..., subprocess.CompletedProcess[str]]


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _is_image_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and _is_sha256(value[7:])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_docker(
    argv: Sequence[str],
    *,
    run: Run,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = run(
            list(argv),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        raise ImageBundleError(
            f"Docker image operation failed safely ({type(exc).__name__})"
        ) from None
    if completed.returncode != 0:
        raise ImageBundleError("Docker image operation returned an unusable status")
    return completed


def _inspect_image(image: FrozenImage, *, run: Run) -> None:
    completed = _run_docker(
        ("docker", "image", "inspect", image.image_id),
        run=run,
        timeout=INSPECT_TIMEOUT_SECONDS,
    )
    try:
        rows = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        raise ImageBundleError("Docker image inspection returned malformed JSON") from None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ImageBundleError("Docker image inspection returned an invalid record")
    row = rows[0]
    config = row.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        row.get("Id") != image.image_id
        or (row.get("Os"), row.get("Architecture")) != (image.os, image.architecture)
        or not isinstance(labels, dict)
        or labels.get("org.ckbbench.role") != image.role
        or labels.get("org.ckbbench.release-family") != RELEASE_FAMILY
    ):
        raise ImageBundleError("Docker image identity, platform, or role differs from the bundle")


def _release_images(suite_root: Path | str) -> tuple[str, str, tuple[FrozenImage, ...]]:
    try:
        release = load_suite_release(suite_root)
    except (OSError, SuiteReleaseError) as exc:
        raise ImageBundleError("suite release is invalid") from exc
    agent = release.suite.pins.agent_image_digest
    verifier = release.suite.pins.verifier_image_digest
    if not _is_image_id(agent) or not _is_image_id(verifier):
        raise ImageBundleError("suite release does not pin both image IDs")
    return (
        release.suite.suite_semver,
        release.freeze_sha256,
        (FrozenImage("agent", agent), FrozenImage("verifier", verifier)),
    )


def _safe_archive_name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ImageBundleError("Docker archive contains an invalid member name")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise ImageBundleError("Docker archive contains an invalid member name") from None
    canonical = value[:-1] if value.endswith("/") else value
    path = PurePosixPath(canonical)
    if (
        not canonical
        or str(path) != canonical
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ImageBundleError("Docker archive contains an unsafe member name")
    return canonical


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ImageBundleError("Docker archive manifest repeats a field")
        document[key] = value
    return document


def _read_archive_json(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
    label: str,
) -> Any:
    member = members.get(name)
    if member is None or not member.isfile() or member.size > MAX_MANIFEST_BYTES:
        raise ImageBundleError(f"{label} is missing or oversized")
    stream = archive.extractfile(member)
    if stream is None:
        raise ImageBundleError(f"{label} cannot be read")
    payload = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) != member.size:
        raise ImageBundleError(f"{label} changed while it was being read")
    try:
        return json.loads(payload, object_pairs_hook=_unique_json_object)
    except (UnicodeError, json.JSONDecodeError):
        raise ImageBundleError(f"{label} is malformed") from None


def _archive_member_sha256(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    label: str,
) -> str:
    stream = archive.extractfile(member)
    if stream is None:
        raise ImageBundleError(f"{label} cannot be read")
    digest = hashlib.sha256()
    observed = 0
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        observed += len(chunk)
        digest.update(chunk)
    if observed != member.size:
        raise ImageBundleError(f"{label} changed while it was being read")
    return digest.hexdigest()


def _oci_descriptor_path(
    descriptor: Any,
    members: dict[str, tarfile.TarInfo],
    label: str,
) -> tuple[str, str]:
    if not isinstance(descriptor, dict):
        raise ImageBundleError(f"{label} is malformed")
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if not _is_image_id(digest) or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ImageBundleError(f"{label} has an invalid digest or size")
    path = f"blobs/sha256/{digest[7:]}"
    member = members.get(path)
    if member is None or not member.isfile() or member.size != size:
        raise ImageBundleError(f"{label} does not match its blob")
    media_type = descriptor.get("mediaType")
    if not isinstance(media_type, str):
        raise ImageBundleError(f"{label} has no media type")
    return path, media_type


def _oci_image_files(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    descriptor: dict[str, Any],
    image: FrozenImage,
) -> tuple[str, tuple[str, ...]]:
    current: Any = descriptor
    for depth in range(4):
        path, media_type = _oci_descriptor_path(current, members, "OCI image descriptor")
        document = _read_archive_json(archive, members, path, "OCI image metadata")
        if not isinstance(document, dict) or document.get("schemaVersion") != 2:
            raise ImageBundleError("OCI image metadata has an unsupported shape")
        if media_type in _OCI_INDEX_MEDIA_TYPES:
            candidates = []
            manifests = document.get("manifests")
            if not isinstance(manifests, list):
                raise ImageBundleError("OCI image index has no manifest array")
            for candidate in manifests:
                if not isinstance(candidate, dict):
                    raise ImageBundleError("OCI image index contains a malformed descriptor")
                platform = candidate.get("platform")
                if (
                    isinstance(platform, dict)
                    and platform.get("os") == image.os
                    and platform.get("architecture") == image.architecture
                    and candidate.get("mediaType") in _OCI_MANIFEST_MEDIA_TYPES
                ):
                    candidates.append(candidate)
            if len(candidates) != 1:
                raise ImageBundleError("OCI image index does not select one frozen platform")
            current = candidates[0]
            continue
        if media_type not in _OCI_MANIFEST_MEDIA_TYPES:
            raise ImageBundleError("OCI image descriptor has an unsupported media type")
        config = document.get("config")
        layers = document.get("layers")
        if not isinstance(layers, list) or not layers:
            raise ImageBundleError("OCI image manifest has no layers")
        config_path, _config_type = _oci_descriptor_path(config, members, "OCI image config")
        layer_paths = tuple(
            _oci_descriptor_path(layer, members, "OCI image layer")[0]
            for layer in layers
        )
        return config_path, layer_paths
    raise ImageBundleError("OCI image metadata nesting exceeds its limit")


def _inspect_oci_archive(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    images: tuple[FrozenImage, ...],
    docker_manifest: list[Any],
) -> None:
    layout = _read_archive_json(archive, members, "oci-layout", "OCI layout marker")
    if layout != {"imageLayoutVersion": "1.0.0"}:
        raise ImageBundleError("OCI layout marker is unsupported")
    index = _read_archive_json(archive, members, "index.json", "OCI archive index")
    if not isinstance(index, dict) or index.get("schemaVersion") != 2:
        raise ImageBundleError("OCI archive index has an unsupported shape")
    descriptors = index.get("manifests")
    if not isinstance(descriptors, list) or len(descriptors) != len(images):
        raise ImageBundleError("OCI archive index does not contain exactly the frozen images")

    by_digest: dict[str, dict[str, Any]] = {}
    for descriptor in descriptors:
        path, _media_type = _oci_descriptor_path(descriptor, members, "OCI archive descriptor")
        digest = f"sha256:{path.rsplit('/', 1)[1]}"
        if digest in by_digest:
            raise ImageBundleError("OCI archive index repeats an image")
        by_digest[digest] = descriptor
    if set(by_digest) != {image.image_id for image in images}:
        raise ImageBundleError("OCI archive index differs from the frozen image IDs")

    expected_rows = {
        _oci_image_files(archive, members, by_digest[image.image_id], image)
        for image in images
    }
    observed_rows: set[tuple[str, tuple[str, ...]]] = set()
    for row in docker_manifest:
        config = _safe_archive_name(row["Config"])
        layers = row["Layers"]
        if not isinstance(layers, list) or not layers:
            raise ImageBundleError("Docker archive image has no layers")
        observed_rows.add((config, tuple(_safe_archive_name(layer) for layer in layers)))
    if observed_rows != expected_rows or len(observed_rows) != len(images):
        raise ImageBundleError("Docker archive manifest differs from the frozen OCI images")

    for name, member in members.items():
        match = _OCI_BLOB.fullmatch(name)
        if match is None:
            if name not in {"blobs", "blobs/sha256", "index.json", "manifest.json", "oci-layout"}:
                raise ImageBundleError("OCI archive contains an unexpected member")
            continue
        if not member.isfile():
            raise ImageBundleError("OCI archive blob is not a regular file")
        if _archive_member_sha256(archive, member, "OCI archive blob") != match.group(1):
            raise ImageBundleError("OCI archive contains a corrupt blob")


def _inspect_archive(path: Path, images: tuple[FrozenImage, ...]) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ImageBundleError("image archive must be a non-empty regular file")
    try:
        with tarfile.open(path, mode="r:") as archive:
            raw_members = archive.getmembers()
            names = [_safe_archive_name(member.name) for member in raw_members]
            if len(names) != len(set(names)):
                raise ImageBundleError("Docker archive repeats a member name")
            if any(not (member.isfile() or member.isdir()) for member in raw_members):
                raise ImageBundleError("Docker archive contains a link or special file")
            members = dict(zip(names, raw_members, strict=True))
            document = _read_archive_json(
                archive, members, "manifest.json", "Docker archive manifest"
            )
            if not isinstance(document, list) or len(document) != len(images):
                raise ImageBundleError("Docker archive does not contain exactly the frozen images")
            referenced: set[str] = {"manifest.json"}
            for row in document:
                if not isinstance(row, dict) or set(row) != {"Config", "Layers", "RepoTags"}:
                    raise ImageBundleError("Docker archive manifest has an unsupported shape")
                if row["RepoTags"] not in (None, []):
                    raise ImageBundleError("image bundle must not carry mutable repository tags")
                referenced.add(_safe_archive_name(row["Config"]))
                layers = row["Layers"]
                if not isinstance(layers, list) or not layers:
                    raise ImageBundleError("Docker archive image has no layers")
                referenced.update(_safe_archive_name(layer) for layer in layers)
            if not referenced <= set(names):
                raise ImageBundleError("Docker archive is missing frozen image content")
            if "index.json" in members or "oci-layout" in members:
                if not {"index.json", "oci-layout"} <= set(members):
                    raise ImageBundleError("Docker archive contains an incomplete OCI layout")
                _inspect_oci_archive(archive, members, images, document)
                return
            expected_configs = {image.image_id[7:] for image in images}
            observed_configs: set[str] = set()
            for row in document:
                config = _safe_archive_name(row["Config"])
                if "/" in config or not config.endswith(".json"):
                    raise ImageBundleError(
                        "classic Docker archive contains an invalid image config"
                    )
                config_digest = config[:-5]
                if config_digest not in expected_configs:
                    raise ImageBundleError(
                        "Docker archive config differs from the frozen image IDs"
                    )
                member = members[config]
                if _archive_member_sha256(
                    archive, member, "classic Docker image config"
                ) != config_digest:
                    raise ImageBundleError("classic Docker image config is corrupt")
                config_document = _read_archive_json(
                    archive, members, config, "classic Docker image config"
                )
                rootfs = config_document.get("rootfs") if isinstance(config_document, dict) else None
                diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
                layers = row["Layers"]
                if (
                    not isinstance(rootfs, dict)
                    or rootfs.get("type") != "layers"
                    or not isinstance(diff_ids, list)
                    or len(diff_ids) != len(layers)
                ):
                    raise ImageBundleError("classic Docker image rootfs is malformed")
                for layer, diff_id in zip(layers, diff_ids, strict=True):
                    layer_name = _safe_archive_name(layer)
                    if not _is_image_id(diff_id):
                        raise ImageBundleError("classic Docker image has an invalid layer digest")
                    if _archive_member_sha256(
                        archive,
                        members[layer_name],
                        "classic Docker image layer",
                    ) != diff_id[7:]:
                        raise ImageBundleError("classic Docker image layer is corrupt")
                observed_configs.add(config_digest)
            if observed_configs != expected_configs:
                raise ImageBundleError("Docker archive is missing frozen image content")
    except ImageBundleError:
        raise
    except (OSError, tarfile.TarError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImageBundleError("image archive is unreadable") from exc


def _manifest_from_dict(document: Any) -> ImageBundleManifest:
    if not isinstance(document, dict) or set(document) != {
        "archive_sha256",
        "images",
        "schema_version",
        "suite_freeze_sha256",
        "suite_semver",
    }:
        raise ImageBundleError("image bundle manifest has an unsupported shape")
    rows = document["images"]
    if not isinstance(rows, list):
        raise ImageBundleError("image bundle images must be an array")
    images = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "architecture", "image_id", "os", "role",
        }:
            raise ImageBundleError("image bundle contains an invalid image record")
        images.append(FrozenImage(**row))
    return ImageBundleManifest(
        archive_sha256=document["archive_sha256"],
        suite_semver=document["suite_semver"],
        suite_freeze_sha256=document["suite_freeze_sha256"],
        images=tuple(images),
        schema_version=document["schema_version"],
    )


def load_bundle(bundle: Path | str) -> tuple[ImageBundleManifest, Path]:
    root = Path(bundle)
    if root.is_symlink() or not root.is_dir():
        raise ImageBundleError("image bundle must be a regular directory")
    manifest_path = root / MANIFEST_FILENAME
    archive_path = root / ARCHIVE_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ImageBundleError("image bundle manifest must be a regular file")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ImageBundleError("image bundle manifest exceeds its byte limit")
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ImageBundleError("image archive must be a regular file")
    try:
        payload = manifest_path.read_bytes()
        document = json.loads(payload, object_pairs_hook=_unique_json_object)
        manifest = _manifest_from_dict(document)
    except ImageBundleError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImageBundleError("image bundle manifest is unreadable") from exc
    if payload != canonical_json_bytes(manifest.to_dict()):
        raise ImageBundleError("image bundle manifest is not canonical")
    if _sha256_file(archive_path) != manifest.archive_sha256:
        raise ImageBundleError("image archive digest differs from its manifest")
    _inspect_archive(archive_path, manifest.images)
    return manifest, archive_path


def verify_bundle(bundle: Path | str, suite_root: Path | str) -> ImageBundleManifest:
    manifest, _archive = load_bundle(bundle)
    suite_semver, freeze_sha256, images = _release_images(suite_root)
    if (
        manifest.suite_semver != suite_semver
        or manifest.suite_freeze_sha256 != freeze_sha256
        or manifest.images != images
    ):
        raise ImageBundleError("image bundle differs from the selected suite release")
    return manifest


def export_bundle(
    suite_root: Path | str,
    output: Path | str,
    *,
    run: Run = subprocess.run,
) -> ImageBundleManifest:
    suite_semver, freeze_sha256, images = _release_images(suite_root)
    destination = Path(output)
    if destination.name in {"", ".", ".."} or destination.exists() or destination.is_symlink():
        raise ImageBundleError("image bundle output must be a fresh directory")
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise ImageBundleError("image bundle output parent is unavailable") from exc
    if not parent.is_dir():
        raise ImageBundleError("image bundle output parent must be a directory")
    destination = parent / destination.name
    for image in images:
        _inspect_image(image, run=run)

    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise ImageBundleError("image bundle output could not be reserved") from exc
    reservation = destination.stat(follow_symlinks=False)
    temporary: Path | None = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".build-", dir=destination))
        archive = temporary / ARCHIVE_FILENAME
        _run_docker(
            (
                "docker", "image", "save", "--output", str(archive),
                *(image.image_id for image in images),
            ),
            run=run,
            timeout=TRANSFER_TIMEOUT_SECONDS,
        )
        _inspect_archive(archive, images)
        manifest = ImageBundleManifest(
            archive_sha256=_sha256_file(archive),
            suite_semver=suite_semver,
            suite_freeze_sha256=freeze_sha256,
            images=images,
        )
        manifest_path = temporary / MANIFEST_FILENAME
        manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
        os.replace(archive, destination / ARCHIVE_FILENAME)
        os.replace(manifest_path, destination / MANIFEST_FILENAME)
        temporary.rmdir()
        return manifest
    except BaseException:
        try:
            current = destination.stat(follow_symlinks=False)
        except OSError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (
            reservation.st_dev,
            reservation.st_ino,
        ):
            shutil.rmtree(destination, ignore_errors=True)
        raise


def import_bundle(
    bundle: Path | str,
    suite_root: Path | str,
    *,
    run: Run = subprocess.run,
) -> ImageBundleManifest:
    manifest = verify_bundle(bundle, suite_root)
    archive = Path(bundle) / ARCHIVE_FILENAME
    _run_docker(
        ("docker", "image", "load", "--input", str(archive)),
        run=run,
        timeout=TRANSFER_TIMEOUT_SECONDS,
    )
    for image in manifest.images:
        _inspect_image(image, run=run)
    return manifest


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ImageBundleError("invalid image-bundle arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="ckbbench images")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--suite", required=True)
    export.add_argument("--output", required=True)
    for name in ("verify", "import"):
        command = commands.add_parser(name)
        command.add_argument("--suite", required=True)
        command.add_argument("--bundle", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Run = subprocess.run,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "export":
            manifest = export_bundle(args.suite, args.output, run=run)
        elif args.command == "verify":
            manifest = verify_bundle(args.bundle, args.suite)
        elif args.command == "import":
            manifest = import_bundle(args.bundle, args.suite, run=run)
        else:
            raise ImageBundleError("unsupported image-bundle command")
        print(
            f"{manifest.suite_semver}\t{manifest.suite_freeze_sha256}\t"
            f"{manifest.archive_sha256}",
            file=stdout,
        )
        return 0
    except ImageBundleError as exc:
        print(f"FAIL: {exc}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
