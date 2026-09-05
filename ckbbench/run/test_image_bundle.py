from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

import ckbbench.run.image_bundle as image_bundle_module
from ckbbench.run.image_bundle import (
    ARCHIVE_FILENAME,
    MANIFEST_FILENAME,
    FrozenImage,
    ImageBundleError,
    _inspect_archive,
    _safe_archive_name,
    export_bundle,
    import_bundle,
    load_bundle,
    main,
    verify_bundle,
)
from ckbbench.run.task_attempt import canonical_json_bytes

SUITE = Path("suites/ckb-core-v2")
_FAKE_LAYERS = {
    "agent": b"agent-layer",
    "verifier": b"verifier-layer",
}
_FAKE_CONFIGS = {
    role: canonical_json_bytes({
        "role": role,
        "rootfs": {
            "diff_ids": [f"sha256:{hashlib.sha256(layer).hexdigest()}"],
            "type": "layers",
        },
    })
    for role, layer in _FAKE_LAYERS.items()
}
_FAKE_IMAGES = tuple(
    FrozenImage(role, f"sha256:{hashlib.sha256(payload).hexdigest()}")
    for role, payload in _FAKE_CONFIGS.items()
)
_FAKE_CONFIG_BY_ID = {
    image.image_id: _FAKE_CONFIGS[image.role]
    for image in _FAKE_IMAGES
}
_FAKE_LAYER_BY_ID = {
    image.image_id: _FAKE_LAYERS[image.role]
    for image in _FAKE_IMAGES
}


@pytest.fixture(autouse=True)
def _use_synthetic_release(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        image_bundle_module,
        "_release_images",
        lambda _suite: ("5.0.1", "f" * 64, _FAKE_IMAGES),
    )


def _add(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def _docker_archive(
    path: Path,
    image_ids: tuple[str, ...],
    *,
    tagged: bool = False,
    corrupt_config: bool = False,
    corrupt_layer: bool = False,
) -> None:
    rows = []
    with tarfile.open(path, "w") as archive:
        for index, image_id in enumerate(image_ids):
            digest = image_id.removeprefix("sha256:")
            config = f"{digest}.json"
            layer = f"layer-{index}/layer.tar"
            payload = _FAKE_CONFIG_BY_ID[image_id]
            if corrupt_config and index == 0:
                payload = b"corrupt"
            layer_payload = _FAKE_LAYER_BY_ID[image_id]
            if corrupt_layer and index == 0:
                layer_payload = b"corrupt"
            _add(archive, config, payload)
            _add(archive, layer, layer_payload)
            rows.append({
                "Config": config,
                "Layers": [layer],
                "RepoTags": [f"example/image:{index}"] if tagged else None,
            })
        _add(archive, "manifest.json", json.dumps(rows).encode("utf-8"))


def _oci_archive(path: Path, *, corrupt_layer: bool = False) -> FrozenImage:
    def encoded(document) -> bytes:
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")

    config = b"{}"
    layer = b"layer"
    config_digest = hashlib.sha256(config).hexdigest()
    layer_digest = hashlib.sha256(layer).hexdigest()
    image_manifest = encoded({
        "config": {
            "digest": f"sha256:{config_digest}",
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": len(config),
        },
        "layers": [{
            "digest": f"sha256:{layer_digest}",
            "mediaType": "application/vnd.oci.image.layer.v1.tar",
            "size": len(layer),
        }],
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "schemaVersion": 2,
    })
    manifest_digest = hashlib.sha256(image_manifest).hexdigest()
    image_index = encoded({
        "manifests": [{
            "digest": f"sha256:{manifest_digest}",
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "platform": {"architecture": "arm64", "os": "linux"},
            "size": len(image_manifest),
        }],
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "schemaVersion": 2,
    })
    image_digest = hashlib.sha256(image_index).hexdigest()
    docker_manifest = encoded([{
        "Config": f"blobs/sha256/{config_digest}",
        "Layers": [f"blobs/sha256/{layer_digest}"],
        "RepoTags": None,
    }])
    top_index = encoded({
        "manifests": [{
            "digest": f"sha256:{image_digest}",
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "size": len(image_index),
        }],
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "schemaVersion": 2,
    })
    blobs = {
        config_digest: config,
        layer_digest: b"other" if corrupt_layer else layer,
        manifest_digest: image_manifest,
        image_digest: image_index,
    }
    with tarfile.open(path, "w") as archive:
        for digest, payload in blobs.items():
            _add(archive, f"blobs/sha256/{digest}", payload)
        _add(archive, "index.json", top_index)
        _add(archive, "manifest.json", docker_manifest)
        _add(archive, "oci-layout", encoded({"imageLayoutVersion": "1.0.0"}))
    return FrozenImage("agent", f"sha256:{image_digest}")


class Docker:
    def __init__(self, *, tagged: bool = False, wrong_role: bool = False) -> None:
        self.tagged = tagged
        self.wrong_role = wrong_role
        self.commands: list[list[str]] = []
        self.images = {image.image_id: image for image in _FAKE_IMAGES}

    def __call__(self, argv, **_kwargs):
        command = list(argv)
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            image = self.images[command[3]]
            role = "other" if self.wrong_role else image.role
            stdout = json.dumps([{
                "Architecture": image.architecture,
                "Config": {"Labels": {
                    "org.ckbbench.release-family": "independent-task-suite-v1",
                    "org.ckbbench.role": role,
                }},
                "Id": image.image_id,
                "Os": image.os,
            }])
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        if command[:3] == ["docker", "image", "save"]:
            output = Path(command[command.index("--output") + 1])
            _docker_archive(output, tuple(self.images), tagged=self.tagged)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ["docker", "image", "load"]:
            return subprocess.CompletedProcess(command, 0, stdout="Loaded", stderr="")
        raise AssertionError(command)


def _export(tmp_path: Path, docker: Docker | None = None):
    runner = docker or Docker()
    destination = tmp_path / "images"
    manifest = export_bundle(SUITE, destination, run=runner)
    return destination, manifest, runner


def test_export_publishes_one_verified_bundle_directory(tmp_path: Path):
    destination, manifest, docker = _export(tmp_path)

    assert {path.name for path in destination.iterdir()} == {
        ARCHIVE_FILENAME,
        MANIFEST_FILENAME,
    }
    assert verify_bundle(destination, SUITE) == manifest
    assert [command[:3] for command in docker.commands].count(
        ["docker", "image", "inspect"]
    ) == 2
    assert [command[:3] for command in docker.commands].count(
        ["docker", "image", "save"]
    ) == 1


def test_export_reserves_the_destination_before_docker_writes(tmp_path: Path):
    destination = tmp_path / "images"
    docker = Docker()

    def checked_run(argv, **kwargs):
        if list(argv)[:3] == ["docker", "image", "save"]:
            assert destination.is_dir()
        return docker(argv, **kwargs)

    export_bundle(SUITE, destination, run=checked_run)


def test_import_verifies_bytes_then_loads_and_rechecks_both_roles(tmp_path: Path):
    destination, manifest, _exporter = _export(tmp_path)
    docker = Docker()

    assert import_bundle(destination, SUITE, run=docker) == manifest
    assert [command[:3] for command in docker.commands] == [
        ["docker", "image", "load"],
        ["docker", "image", "inspect"],
        ["docker", "image", "inspect"],
    ]


def test_export_refuses_archives_that_would_restore_mutable_tags(tmp_path: Path):
    destination = tmp_path / "images"
    with pytest.raises(ImageBundleError, match="mutable repository tags"):
        export_bundle(SUITE, destination, run=Docker(tagged=True))
    assert not destination.exists()


def test_bundle_refuses_archive_tampering_before_docker(tmp_path: Path):
    destination, _manifest, _docker = _export(tmp_path)
    with (destination / ARCHIVE_FILENAME).open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(ImageBundleError, match="digest"):
        import_bundle(destination, SUITE, run=lambda *_a, **_k: pytest.fail("Docker was called"))


def test_bundle_refuses_noncanonical_or_wrong_suite_metadata(tmp_path: Path):
    destination, _manifest, _docker = _export(tmp_path)
    manifest_path = destination / MANIFEST_FILENAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with pytest.raises(ImageBundleError, match="not canonical"):
        load_bundle(destination)

    manifest_path.write_bytes(canonical_json_bytes(document))
    document["suite_semver"] = "99.0.0"
    manifest_path.write_bytes(canonical_json_bytes(document))
    with pytest.raises(ImageBundleError, match="selected suite"):
        verify_bundle(destination, SUITE)


def test_import_refuses_a_post_load_role_mismatch(tmp_path: Path):
    destination, _manifest, _docker = _export(tmp_path)
    with pytest.raises(ImageBundleError, match="role differs"):
        import_bundle(destination, SUITE, run=Docker(wrong_role=True))


def test_cli_reports_a_verified_bundle_without_exposing_docker_output(tmp_path: Path):
    destination, manifest, _docker = _export(tmp_path)
    stdout = io.StringIO()
    stderr = io.StringIO()

    assert main(
        ["verify", "--suite", str(SUITE), "--bundle", str(destination)],
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert manifest.archive_sha256 in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_manifest_requires_the_exact_frozen_role_order(tmp_path: Path):
    destination, manifest, _docker = _export(tmp_path)
    path = destination / MANIFEST_FILENAME
    document = manifest.to_dict()
    document["images"] = list(reversed(document["images"]))
    path.write_bytes(canonical_json_bytes(document))

    with pytest.raises(ImageBundleError, match="role order"):
        load_bundle(destination)


@pytest.mark.parametrize(
    "name",
    ("../layer.tar", "/layer.tar", "layers//layer.tar", "layers\\layer.tar", "layer\n.tar"),
)
def test_archive_member_names_must_be_canonical_and_relative(name: str):
    with pytest.raises(ImageBundleError, match="unsafe member name"):
        _safe_archive_name(name)


def test_realistic_oci_layout_binds_the_frozen_image_index(tmp_path: Path):
    archive = tmp_path / "images.tar"
    image = _oci_archive(archive)

    _inspect_archive(archive, (image,))


def test_oci_layout_rejects_a_blob_that_does_not_match_its_name(tmp_path: Path):
    archive = tmp_path / "images.tar"
    image = _oci_archive(archive, corrupt_layer=True)

    with pytest.raises(ImageBundleError, match="corrupt blob"):
        _inspect_archive(archive, (image,))


def test_classic_layout_rejects_a_config_that_does_not_match_its_image_id(
    tmp_path: Path,
):
    archive = tmp_path / "images.tar"
    _docker_archive(
        archive,
        tuple(image.image_id for image in _FAKE_IMAGES),
        corrupt_config=True,
    )

    with pytest.raises(ImageBundleError, match="config is corrupt"):
        _inspect_archive(archive, _FAKE_IMAGES)


def test_classic_layout_rejects_a_layer_that_does_not_match_its_diff_id(
    tmp_path: Path,
):
    archive = tmp_path / "images.tar"
    _docker_archive(
        archive,
        tuple(image.image_id for image in _FAKE_IMAGES),
        corrupt_layer=True,
    )

    with pytest.raises(ImageBundleError, match="layer is corrupt"):
        _inspect_archive(archive, _FAKE_IMAGES)
