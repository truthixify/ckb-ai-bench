import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.validate_suite_candidates as candidate_validation

from ckbbench.suite.model import ProjectVerifierSpec, Task
from ckbbench.verify.diagnostics import VerificationDiagnostics
from ckbbench.verify.onchain import Verdict
from scripts.validate_suite_candidates import (
    ROOT,
    Candidate,
    CandidateValidationError,
    _assert_verdict,
    assert_release_contains_no_candidates,
    candidate_plan,
    external_directory,
    qualification_bundle_sha256,
    validate_spore_toolchain,
)


SUITE = ROOT / "suites" / "ckb-core-v3"


def _candidate(expected_pass: bool) -> Candidate:
    task = Task("candidate", "prompt", 4, "build/release/candidate", "code", "hidden")
    return Candidate(task, Path("candidate"), "candidate", expected_pass, True)


def _candidate_bundle(root: Path) -> tuple[Path, tuple[Task, ...]]:
    source_task = Task(
        "source-candidate",
        "prompt",
        4,
        "build/release/tool",
        "project",
        ProjectVerifierSpec("address_codec", 1, "hidden"),
    )
    binary_task = Task(
        "binary-candidate",
        "prompt",
        4,
        "build/release/contract",
        "code",
        "hidden",
    )
    bundle = root / "qualification"
    for name, content in (
        ("source-candidate/reference/Makefile", "build:\n\ttrue\n"),
        ("source-candidate/reference/tool.py", "print('reference')\n"),
        ("source-candidate/alternate/Makefile", "build:\n\ttrue\n"),
        ("source-candidate/alternate/tool.py", "print('alternate')\n"),
        ("source-candidate/mutants/wrong/Makefile", "build:\n\ttrue\n"),
        ("source-candidate/mutants/wrong/tool.py", "print('wrong')\n"),
        ("binary-candidate/reference/contract", "reference-binary\n"),
        ("binary-candidate/mutants/wrong", "mutant-binary\n"),
    ):
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="ascii")
    return bundle, (source_task, binary_task)


def test_candidate_plan_reads_a_separate_qualification_bundle(monkeypatch, tmp_path: Path):
    bundle, tasks = _candidate_bundle(tmp_path)
    digest = qualification_bundle_sha256(bundle)
    monkeypatch.setattr(
        candidate_validation,
        "load_suite",
        lambda _root: SimpleNamespace(
            tasks=tasks,
            pins=SimpleNamespace(qualification_bundle_sha256=digest),
        ),
    )

    plan = candidate_plan(SUITE, bundle)
    source_tasks = {
        candidate.task.id
        for candidate in plan
        if candidate.label.endswith("/reference") and candidate.source_tree
    }
    for task_id in source_tasks:
        labels = {candidate.label for candidate in plan if candidate.task.id == task_id}
        assert f"{task_id}/reference" in labels
        assert f"{task_id}/alternate" in labels
        assert any(f"{task_id}/mutants/" in label for label in labels)
    assert source_tasks == {"source-candidate"}
    assert {candidate.label for candidate in plan} == {
        "source-candidate/reference",
        "source-candidate/alternate",
        "source-candidate/mutants/wrong",
        "binary-candidate/reference",
        "binary-candidate/mutants/wrong",
    }


def test_released_suite_contains_no_qualification_candidates(tmp_path: Path):
    assert_release_contains_no_candidates(SUITE)

    leaked = tmp_path / "suite" / "task" / "reference"
    leaked.mkdir(parents=True)
    with pytest.raises(CandidateValidationError, match="contains private qualification"):
        assert_release_contains_no_candidates(tmp_path / "suite")


def test_candidate_plan_refuses_a_bundle_inside_the_release(tmp_path: Path):
    bundle = tmp_path / "suite" / "task" / "qualification"
    bundle.mkdir(parents=True)
    with pytest.raises(CandidateValidationError, match="separate directory"):
        candidate_plan(tmp_path / "suite", bundle)


def test_candidate_plan_refuses_an_unpinned_or_changed_bundle(monkeypatch, tmp_path: Path):
    bundle, tasks = _candidate_bundle(tmp_path)
    pins = SimpleNamespace(qualification_bundle_sha256=None)
    monkeypatch.setattr(
        candidate_validation,
        "load_suite",
        lambda _root: SimpleNamespace(tasks=tasks, pins=pins),
    )
    with pytest.raises(CandidateValidationError, match="does not pin"):
        candidate_plan(SUITE, bundle)

    pins.qualification_bundle_sha256 = qualification_bundle_sha256(bundle)
    (bundle / "source-candidate" / "reference" / "tool.py").write_text(
        "print('changed')\n", encoding="ascii"
    )
    with pytest.raises(CandidateValidationError, match="differs from the suite pin"):
        candidate_plan(SUITE, bundle)


def test_candidate_plan_refuses_a_symlinked_bundle(monkeypatch, tmp_path: Path):
    bundle, tasks = _candidate_bundle(tmp_path)
    link = tmp_path / "qualification-link"
    link.symlink_to(bundle, target_is_directory=True)
    monkeypatch.setattr(
        candidate_validation,
        "load_suite",
        lambda _root: SimpleNamespace(tasks=tasks),
    )
    with pytest.raises(CandidateValidationError, match="separate directory"):
        candidate_plan(SUITE, link)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support is unavailable")
def test_qualification_bundle_refuses_special_files(tmp_path: Path):
    bundle, _tasks = _candidate_bundle(tmp_path)
    os.mkfifo(bundle / "source-candidate" / "reference" / "unexpected.pipe")

    with pytest.raises(CandidateValidationError, match="non-regular file"):
        qualification_bundle_sha256(bundle)


def test_qualification_bundle_binds_directories_and_bounds_its_tree(
    tmp_path: Path,
    monkeypatch,
):
    bundle, _tasks = _candidate_bundle(tmp_path)
    before = qualification_bundle_sha256(bundle)
    (bundle / "empty-directory").mkdir()
    assert qualification_bundle_sha256(bundle) != before

    monkeypatch.setattr(candidate_validation, "MAX_CANDIDATE_TREE_ENTRIES", 2)
    with pytest.raises(CandidateValidationError, match="too many entries"):
        qualification_bundle_sha256(bundle)


def test_qualification_bundle_bounds_total_file_bytes(tmp_path: Path, monkeypatch):
    bundle = tmp_path / "qualification"
    bundle.mkdir()
    (bundle / "one").write_bytes(b"123")
    (bundle / "two").write_bytes(b"456")
    monkeypatch.setattr(candidate_validation, "MAX_CANDIDATE_TREE_BYTES", 5)

    with pytest.raises(CandidateValidationError, match="tree exceeds"):
        qualification_bundle_sha256(bundle)


def test_verdict_gate_requires_assertion_backed_results():
    complete_pass = Verdict(
        "candidate",
        True,
        "passed",
        "proof",
        VerificationDiagnostics.completed(3, 0),
    )
    _assert_verdict(_candidate(True), complete_pass)

    assertion_failure = Verdict(
        "candidate",
        False,
        "hidden suite failed (exit 101)",
        "proof",
        VerificationDiagnostics.completed(2, 1),
    )
    _assert_verdict(_candidate(False), assertion_failure)

    for invalid in (
        Verdict("candidate", True, "passed", "proof", VerificationDiagnostics.unavailable()),
        Verdict("candidate", False, "rebuild from sources failed", "", VerificationDiagnostics.unavailable()),
        Verdict("candidate", False, "hidden suite failed (exit 101)", "", VerificationDiagnostics.unavailable()),
    ):
        with pytest.raises(CandidateValidationError):
            _assert_verdict(_candidate(invalid.passed), invalid)


def test_external_scratch_path_rejects_repository_relatives(tmp_path: Path):
    for path in ("/", ROOT.parent, ROOT, ROOT / "scratch"):
        with pytest.raises(CandidateValidationError):
            external_directory(path, "scratch")
    assert external_directory(tmp_path, "scratch") == tmp_path.resolve()


def test_spore_probe_binds_the_pinned_sdk_deployment_and_encoding():
    calls = []

    def run(argv):
        calls.append(argv)
        return 0, json.dumps(
            candidate_validation._expected_spore_probe_document(
                {
                    "output_index": 0,
                    "transaction_hash": "0x5e8d2a517d50fd4bb4d01737a7952a1f1d35c8afc77240695bb569cd7d9d5a1f",
                },
                "1.5.17",
            ),
            separators=(",", ":"),
        )

    validate_spore_toolchain(SUITE, "sha256:" + "1" * 64, run=run)
    assert len(calls) == 1
    assert calls[0][:8] == [
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        "--network", "none", "sha256:" + "1" * 64,
    ]
    assert "@ckb-ccc/spore/advancedBarrel" in calls[0][-1]
    assert "SCRIPTS_SPORE_TESTNET.V2" in calls[0][-1]
    assert "SporeVersion" not in calls[0][-1]


def test_each_candidate_gets_an_independent_grading_deadline(
    monkeypatch,
    tmp_path: Path,
):
    plan = (
        _candidate(True),
        Candidate(_candidate(True).task, Path("alternate"), "alternate", True, True),
    )
    runners: list[object] = []
    observed: list[object] = []

    monkeypatch.setattr(candidate_validation, "validate_spore_toolchain", lambda *_a, **_k: None)
    monkeypatch.setattr(candidate_validation, "candidate_plan", lambda _root, _bundle: plan)
    monkeypatch.setattr(candidate_validation, "qualification_bundle_sha256", lambda _root: "a" * 64)
    monkeypatch.setattr(candidate_validation, "hash_task_dir", lambda _path: "a" * 64)

    def make_runner(_config):
        runner = object()
        runners.append(runner)
        return runner

    def grade(candidate, _suite, runner, _challenge, _scratch):
        observed.append(runner)
        return Verdict(
            candidate.task.id,
            True,
            "passed",
            "proof",
            VerificationDiagnostics.completed(1, 0),
        )

    monkeypatch.setattr(candidate_validation, "make_docker_runner", make_runner)
    monkeypatch.setattr(candidate_validation, "_grade_candidate", grade)

    count, passed, rejected = candidate_validation.validate_suite_candidates(
        SUITE,
        tmp_path / "qualification",
        agent_image="sha256:" + "1" * 64,
        verifier_image="sha256:" + "2" * 64,
        scratch_root=tmp_path,
        repetitions=2,
    )

    assert (count, passed, rejected) == (2, 4, 0)
    assert observed == runners
    assert len({id(runner) for runner in runners}) == 4


@pytest.mark.parametrize(
    "return_code,output",
    (
        (1, "{}"),
        (0, "not-json"),
        (0, "{}"),
        (0, '{"sdk_version":"1.5.18"}'),
    ),
)
def test_spore_probe_fails_closed(return_code: int, output: str):
    with pytest.raises(CandidateValidationError, match="Spore SDK image probe"):
        validate_spore_toolchain(
            SUITE,
            "sha256:" + "1" * 64,
            run=lambda _argv: (return_code, output),
        )
