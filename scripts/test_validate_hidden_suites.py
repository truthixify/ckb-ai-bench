from pathlib import Path

import pytest

from scripts.validate_hidden_suites import (
    MAX_CANDIDATE_BYTES,
    HiddenSuiteError,
    external_directory,
    validate_candidate,
)


def test_external_directory_rejects_repository_root_ancestors_and_descendants(tmp_path: Path):
    from scripts.validate_hidden_suites import ROOT

    for unsafe in ("/", ROOT, ROOT.parent, ROOT / "generated"):
        with pytest.raises(HiddenSuiteError):
            external_directory(unsafe, "test path")
    assert external_directory(tmp_path, "test path") == tmp_path.resolve()


def test_candidate_must_be_a_bounded_regular_file(tmp_path: Path):
    good = tmp_path / "good"
    good.write_bytes(b"binary")
    validate_candidate(good, "candidate")

    empty = tmp_path / "empty"
    empty.touch()
    with pytest.raises(HiddenSuiteError):
        validate_candidate(empty, "candidate")

    large = tmp_path / "large"
    large.write_bytes(b"x" * (MAX_CANDIDATE_BYTES + 1))
    with pytest.raises(HiddenSuiteError):
        validate_candidate(large, "candidate")

    link = tmp_path / "link"
    link.symlink_to(good)
    with pytest.raises(HiddenSuiteError):
        validate_candidate(link, "candidate")
