from __future__ import annotations

from pathlib import Path

import pytest

from ckbbench.run.suite_release import load_treatment_profile
from ckbbench.run.surface_capture import (
    CAPTURE_REQUEST_LIMIT,
    PROFILE_FILENAMES,
    SurfaceCaptureError,
    capture_and_publish,
    capture_profiles,
)
from ckbbench.run.treatment_surface import profile_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_SURFACE_ROOT = REPOSITORY_ROOT / "configs" / "ckb-ai-surfaces-v1"
PUBLIC_PROFILE_DIGESTS = {
    "ckb-ai-control-local-v1":
        "1df8e66c4cde2c4f6b80c834aadc5f82f7cba5ee5860d379b51fe54b17994ecc",
    "ckb-ai-control-testnet-v1":
        "e84386e1166049fd36d114a5608721164c325471b77a2ba35abfb93f849d8fb7",
    "ckb-ai-treatment-local-v1":
        "6dce24826b54e17be0ee1b365c6e9dbf47a646837f35b1facb4ba6664bb4b259",
    "ckb-ai-treatment-testnet-v1":
        "595a7a6dac0712708e4a9d8fa5776b5900e560703e64ffd5d905f3c8e2777120",
}


def _tools() -> list[dict]:
    return [
        {
            "description": f"Public operation {name}",
            "inputSchema": {"properties": {}, "type": "object"},
            "name": name,
        }
        for name in (
            "dev_get_genesis_hash",
            "rpc_get_block_hash",
            "rpc_get_blockchain_info",
            "rpc_get_tip_block_number",
            "search_resources",
        )
    ]


def _resources() -> list[dict]:
    return [{
        "mimeType": "text/markdown",
        "name": "Reference",
        "uri": "ckb://docs/reference/transaction-structure",
    }]


class CatalogClient:
    def __init__(self, *, url: str = "", request_limit: int = 0) -> None:
        self.url = url
        self.request_limit = request_limit
        self.request_count = 0

    def initialize(self) -> dict:
        self.request_count += 1
        return {"serverInfo": {"name": "ckb-ai-mcp", "version": "1.6.13"}}

    def list_tools(self) -> list[dict]:
        self.request_count += 1
        return _tools()

    def list_resources(self) -> list[dict]:
        self.request_count += 1
        return _resources()


def test_capture_uses_exactly_one_complete_catalog_and_derives_four_profiles():
    client = CatalogClient()

    profiles = capture_profiles(client)

    assert client.request_count == CAPTURE_REQUEST_LIMIT
    assert tuple(profile.profile_id + ".json" for profile in profiles) == PROFILE_FILENAMES
    assert [profile.claims_live_chain for profile in profiles] == [False, True, False, True]
    assert [profile.allowed_tools for profile in profiles] == [
        (),
        (),
        ("search_resources",),
        ("search_resources",),
    ]
    assert len({profile.catalog_sha256 for profile in profiles}) == 1


def test_public_surface_profiles_are_canonical_and_pinned():
    paths = tuple(sorted(PUBLIC_SURFACE_ROOT.glob("*.json")))
    assert tuple(path.name for path in paths) == PROFILE_FILENAMES

    profiles = tuple(load_treatment_profile(path) for path in paths)

    assert all(profile_bytes(profile) == path.read_bytes() for path, profile in zip(paths, profiles))
    assert {profile.profile_id: profile.sha256 for profile in profiles} == PUBLIC_PROFILE_DIGESTS
    assert {profile.server_name for profile in profiles} == {"ckb-ai-mcp"}
    assert {profile.server_version for profile in profiles} == {"1.6.13"}
    assert len({profile.catalog_sha256 for profile in profiles}) == 1
    assert [profile.allowed_tools for profile in profiles] == [
        (),
        (),
        ("search_resources",),
        ("search_resources",),
    ]
    assert [profile.allowed_resource_prefixes for profile in profiles] == [
        (),
        (),
        ("ckb://docs/",),
        ("ckb://docs/",),
    ]


def test_capture_publishes_only_canonical_profiles_in_one_fresh_directory(tmp_path: Path):
    output = tmp_path / "surfaces"

    profiles = capture_and_publish(
        output,
        endpoint="https://example.invalid/mcp",
        client_factory=CatalogClient,
    )

    assert tuple(sorted(path.name for path in output.iterdir())) == PROFILE_FILENAMES
    before = tuple(path.read_bytes() for path in sorted(output.iterdir()))
    assert all(payload.endswith(b"\n") for payload in before)
    assert len(profiles) == 4
    assert not any("private" in path.read_text(encoding="ascii").lower() for path in output.iterdir())


def test_capture_refuses_catalog_drift_wrong_request_accounting_and_existing_output(
    tmp_path: Path,
):
    class WrongVersion(CatalogClient):
        def initialize(self) -> dict:
            self.request_count += 1
            return {"serverInfo": {"name": "ckb-ai-mcp", "version": "9.9.9"}}

    class ExtraRequest(CatalogClient):
        def list_resources(self) -> list[dict]:
            self.request_count += 2
            return _resources()

    with pytest.raises(SurfaceCaptureError, match="version differs"):
        capture_profiles(WrongVersion())
    with pytest.raises(SurfaceCaptureError, match="exactly three"):
        capture_profiles(ExtraRequest())

    output = tmp_path / "surfaces"
    output.mkdir()
    with pytest.raises(SurfaceCaptureError, match="already exists"):
        capture_and_publish(output, client_factory=CatalogClient)


@pytest.mark.parametrize("kind", ["missing-parent", "symlink-parent"])
def test_capture_refuses_unsafe_publication_before_client_construction(
    tmp_path: Path,
    kind: str,
):
    calls = 0

    def factory(**_kwargs):
        nonlocal calls
        calls += 1
        return CatalogClient()

    if kind == "missing-parent":
        output = tmp_path / "missing" / "surfaces"
    else:
        actual = tmp_path / "actual"
        actual.mkdir()
        linked = tmp_path / "linked"
        linked.symlink_to(actual, target_is_directory=True)
        output = linked / "surfaces"

    with pytest.raises(SurfaceCaptureError, match="parent"):
        capture_and_publish(output, client_factory=factory)
    assert calls == 0
    assert not output.exists()


def test_capture_never_replaces_a_concurrently_claimed_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    output = tmp_path / "surfaces"
    original_mkdir = Path.mkdir
    raced = False

    def racing_mkdir(path: Path, *args, **kwargs):
        nonlocal raced
        if path == output and not raced:
            raced = True
            original_mkdir(path)
            (path / "owned-by-other-process").write_text("keep", encoding="ascii")
            raise FileExistsError(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(SurfaceCaptureError, match="already exists"):
        capture_and_publish(output, client_factory=CatalogClient)

    assert (output / "owned-by-other-process").read_text(encoding="ascii") == "keep"
    assert not any(output.parent.glob(".ckbbench-surfaces-*"))
