from __future__ import annotations

from pathlib import Path

import pytest

from ckbbench.run.surface_capture import (
    CAPTURE_REQUEST_LIMIT,
    PROFILE_FILENAMES,
    SurfaceCaptureError,
    capture_and_publish,
    capture_profiles,
)


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
