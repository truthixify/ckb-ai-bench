#!/usr/bin/env python3
"""Exercise a private qualification bundle against a released suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from ckbbench.run.runner import RunnerConfig, make_docker_runner
from ckbbench.suite.freeze import hash_task_dir
from ckbbench.suite.model import ProjectVerifierSpec, Task
from ckbbench.suite.registry import load_suite
from ckbbench.verify.codetask import CODE_CHALLENGE_ENV, grade_code_task
from ckbbench.verify.onchain import Verdict, ckb_blake2b, molecule_script
from ckbbench.verify.projecttask import grade_project_task


ROOT = Path(__file__).resolve().parents[1]
MAX_CANDIDATE_FILE_BYTES = 1 << 20
MAX_CANDIDATE_TREE_BYTES = 64 << 20
MAX_CANDIDATE_TREE_ENTRIES = 4096
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_SPORE_CODE_HASH = "0x685a60219309029d01310311dba953d67029170ca4848a4ff638e57002130a0d"
_SPORE_PROBE_DATA = (
    "0x50000000100000002c00000050000000180000006170706c69636174696f6e2f"
    "6f637465742d73747265616d20000000"
    + "ab" * 32
)
_SPORE_PROBE_TYPE_ARGS = "0x" + "11" * 32
_SPORE_PROBE_LOCK = {
    "code_hash": "0x" + "22" * 32,
    "hash_type": "type",
    "args": "0x" + "33" * 20,
}
_SPORE_PROBE_SCRIPT = r"""
import {
  packRawSporeData,
  SCRIPTS_SPORE_TESTNET,
  SporeAction,
  WitnessLayout,
  assembleCreateSporeAction,
} from "@ckb-ccc/spore/advancedBarrel";
import { readFileSync } from "node:fs";

const packageDocument = JSON.parse(
  readFileSync("/opt/ckbbench-node/node_modules/@ckb-ccc/spore/package.json", "utf8"),
);
const script = SCRIPTS_SPORE_TESTNET.V2;
const dependency = script.cellDeps[0].cellDep;
const packed = packRawSporeData({
  contentType: "application/octet-stream",
  content: "0x" + "ab".repeat(32),
});
const lock = {
  codeHash: "0x" + "22".repeat(32),
  hashType: "type",
  args: "0x" + "33".repeat(20),
};
const type = {
  codeHash: script.codeHash,
  hashType: script.hashType,
  args: "0x" + "11".repeat(32),
};
const action = assembleCreateSporeAction({lock, type}, packed);
const witness = WitnessLayout.encode({
  type: "SighashAll",
  value: {seal: "0x", message: {actions: [action]}},
});
const decodedWitness = WitnessLayout.decode(witness);
const decodedAction = decodedWitness.value.message.actions[0];
const decodedSporeAction = SporeAction.decode(decodedAction.data);
const roundTrip = Buffer.from(WitnessLayout.encode(decodedWitness));
process.stdout.write(JSON.stringify({
  code_hash: script.codeHash,
  data: "0x" + Buffer.from(packed).toString("hex"),
  dep_type: dependency.depType,
  hash_type: script.hashType,
  output_index: dependency.outPoint.index,
  sdk_version: packageDocument.version,
  transaction_hash: dependency.outPoint.txHash,
  cobuild: {
    action_count: decodedWitness.value.message.actions.length,
    action_data_hash: decodedSporeAction.value.dataHash,
    action_script_hash: decodedAction.scriptHash,
    action_spore_id: decodedSporeAction.value.sporeId,
    action_to: {
      args: decodedSporeAction.value.to.value.args,
      code_hash: decodedSporeAction.value.to.value.codeHash,
      hash_type: decodedSporeAction.value.to.value.hashType,
    },
    action_type: decodedSporeAction.type,
    witness_round_trip: Buffer.from(witness).equals(roundTrip),
    witness_type: decodedWitness.type,
  },
}));
""".strip()


class CandidateValidationError(RuntimeError):
    """A private qualification candidate or its verification result is invalid."""


@dataclass(frozen=True)
class Candidate:
    task: Task
    path: Path
    label: str
    expected_pass: bool
    source_tree: bool


DockerProbe = Callable[[Sequence[str]], tuple[int, str]]


def _expected_spore_probe_document(
    dependency: dict[str, object], sdk_version: str
) -> dict[str, object]:
    """Return the independent values expected from the SDK probe."""
    type_code_hash = bytes.fromhex(_SPORE_CODE_HASH[2:])
    type_args = bytes.fromhex(_SPORE_PROBE_TYPE_ARGS[2:])
    type_hash = "0x" + ckb_blake2b(
        molecule_script(type_code_hash, "data1", type_args)
    ).hex()
    content_hash = "0x" + ckb_blake2b(
        bytes.fromhex(_SPORE_PROBE_DATA[2:])
    ).hex()
    return {
        "code_hash": _SPORE_CODE_HASH,
        "data": _SPORE_PROBE_DATA,
        "dep_type": "code",
        "hash_type": "data1",
        "output_index": dependency["output_index"],
        "sdk_version": sdk_version,
        "transaction_hash": dependency["transaction_hash"],
        "cobuild": {
            "action_count": 1,
            "action_data_hash": content_hash,
            "action_script_hash": type_hash,
            "action_spore_id": _SPORE_PROBE_TYPE_ARGS,
            "action_to": _SPORE_PROBE_LOCK,
            "action_type": "CreateSpore",
            "witness_round_trip": True,
            "witness_type": "SighashAll",
        },
    }


def _run_probe(argv: Sequence[str]) -> tuple[int, str]:
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return completed.returncode, completed.stdout


def validate_spore_toolchain(
    suite_root: Path,
    agent_image: str,
    *,
    run: DockerProbe = _run_probe,
) -> None:
    suite = load_suite(suite_root)
    matches = tuple(task for task in suite.tasks if task.id == "task-spore-creation")
    if len(matches) != 1 or matches[0].execution is None:
        raise CandidateValidationError("suite needs one executable Spore task")
    dependencies = tuple(
        row
        for row in matches[0].execution.required_dependencies
        if row.dependency_id == "spore-code"
    )
    if len(dependencies) != 1:
        raise CandidateValidationError("Spore task needs one code dependency")
    expected_version = suite.pins.toolchain_versions.get("@ckb-ccc/spore")
    if expected_version is None:
        raise CandidateValidationError("suite does not pin the Spore SDK")
    dependency = dependencies[0]
    argv = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--network",
        "none",
        agent_image,
        "node",
        "--input-type=module",
        "-e",
        _SPORE_PROBE_SCRIPT,
    ]
    try:
        return_code, output = run(argv)
        document = json.loads(output)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise CandidateValidationError("Spore SDK image probe failed") from None
    expected = _expected_spore_probe_document(
        {
            "output_index": dependency.output_index,
            "transaction_hash": dependency.transaction_hash,
        },
        expected_version,
    )
    if return_code != 0 or document != expected:
        raise CandidateValidationError("Spore SDK image probe differs from the released contract")


def external_directory(raw: str | Path, label: str) -> Path:
    if not str(raw).strip():
        raise CandidateValidationError(f"{label} resolved to an empty path")
    unresolved = Path(raw).expanduser()
    if unresolved.is_symlink():
        raise CandidateValidationError(f"{label} must not be a symlink")
    path = unresolved.resolve()
    root = ROOT.resolve()
    if path == Path(path.anchor) or path == root or path in root.parents or root in path.parents:
        raise CandidateValidationError(f"{label} must be outside the repository")
    return path


def _candidate_entries(path: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    try:
        path_mode = path.lstat().st_mode
    except OSError:
        raise CandidateValidationError(f"candidate is missing: {path}") from None
    if stat.S_ISLNK(path_mode):
        raise CandidateValidationError(f"candidate is a symlink: {path}")
    if stat.S_ISREG(path_mode):
        item_stat = path.lstat()
        if item_stat.st_size <= 0 or item_stat.st_size > MAX_CANDIDATE_FILE_BYTES:
            raise CandidateValidationError(
                f"candidate file size must be 1..{MAX_CANDIDATE_FILE_BYTES} bytes: {path}"
            )
        entries = ((path, item_stat),)
    elif stat.S_ISDIR(path_mode):
        found: list[tuple[Path, os.stat_result]] = []
        pending = [path]
        total_bytes = 0
        while pending:
            directory = pending.pop()
            try:
                children = os.scandir(directory)
            except OSError:
                raise CandidateValidationError(
                    f"candidate contains an unreadable entry: {path}"
                ) from None
            with children:
                for child in children:
                    item = Path(child.path)
                    try:
                        item_stat = child.stat(follow_symlinks=False)
                    except OSError:
                        raise CandidateValidationError(
                            f"candidate contains an unreadable entry: {path}"
                        ) from None
                    found.append((item, item_stat))
                    if len(found) > MAX_CANDIDATE_TREE_ENTRIES:
                        raise CandidateValidationError(
                            f"candidate contains too many entries: {path}"
                        )
                    mode = item_stat.st_mode
                    if stat.S_ISLNK(mode):
                        raise CandidateValidationError(f"candidate contains a symlink: {path}")
                    if stat.S_ISDIR(mode):
                        if item.name in {"build", "target"}:
                            raise CandidateValidationError(
                                f"candidate contains generated output: {path}"
                            )
                        pending.append(item)
                        continue
                    if not stat.S_ISREG(mode):
                        raise CandidateValidationError(
                            f"candidate contains a non-regular file: {path}"
                        )
                    if item_stat.st_size <= 0 or item_stat.st_size > MAX_CANDIDATE_FILE_BYTES:
                        raise CandidateValidationError(
                            f"candidate file size must be 1..{MAX_CANDIDATE_FILE_BYTES} bytes: {path}"
                        )
                    total_bytes += item_stat.st_size
                    if total_bytes > MAX_CANDIDATE_TREE_BYTES:
                        raise CandidateValidationError(
                            f"candidate tree exceeds {MAX_CANDIDATE_TREE_BYTES} bytes: {path}"
                        )
        entries = tuple(
            sorted(found, key=lambda row: row[0].relative_to(path).as_posix())
        )
    else:
        raise CandidateValidationError(f"candidate is not a regular file or directory: {path}")
    return entries


def _candidate_files(path: Path) -> tuple[Path, ...]:
    entries = _candidate_entries(path)
    files = tuple(item for item, item_stat in entries if stat.S_ISREG(item_stat.st_mode))
    if not files:
        raise CandidateValidationError(f"candidate contains no files: {path}")
    return files


def _read_candidate_file(path: Path) -> bytes:
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino, before.st_size)
                != (opened.st_dev, opened.st_ino, opened.st_size)
            ):
                raise CandidateValidationError("candidate file changed while it was inspected")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                content = source.read(MAX_CANDIDATE_FILE_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except CandidateValidationError:
        raise
    except OSError:
        raise CandidateValidationError("candidate file changed while it was inspected") from None
    if len(content) != opened.st_size or not 0 < len(content) <= MAX_CANDIDATE_FILE_BYTES:
        raise CandidateValidationError("candidate file changed while it was inspected")
    return content


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in _candidate_files(path):
        relative = item.name if path.is_file() else item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_read_candidate_file(item))
        digest.update(b"\0")
    return digest.hexdigest()


def qualification_bundle_sha256(candidate_root: Path) -> str:
    """Bind every private candidate byte and relative path into one suite pin."""
    if candidate_root.is_symlink() or not candidate_root.is_dir():
        raise CandidateValidationError("qualification bundle must be a regular directory")
    digest = hashlib.sha256()
    for item, item_stat in _candidate_entries(candidate_root):
        relative = item.relative_to(candidate_root).as_posix().encode("utf-8")
        is_file = stat.S_ISREG(item_stat.st_mode)
        content = _read_candidate_file(item) if is_file else b""
        digest.update(b"f" if is_file else b"d")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def assert_release_contains_no_candidates(suite_root: Path) -> None:
    forbidden = {"reference", "alternate", "mutants"}
    found = sorted(
        path.relative_to(suite_root).as_posix()
        for path in suite_root.rglob("*")
        if path.is_dir() and path.name in forbidden
    )
    if found:
        raise CandidateValidationError(
            "released suite contains private qualification directories"
        )


def candidate_plan(suite_root: Path, candidate_root: Path) -> tuple[Candidate, ...]:
    unresolved_candidate_root = candidate_root
    suite_root = suite_root.resolve()
    candidate_root = candidate_root.resolve()
    if (
        unresolved_candidate_root.is_symlink()
        or not candidate_root.is_dir()
        or candidate_root == suite_root
        or candidate_root in suite_root.parents
        or suite_root in candidate_root.parents
    ):
        raise CandidateValidationError("qualification bundle must be a separate directory")
    assert_release_contains_no_candidates(suite_root)
    suite = load_suite(suite_root)
    expected_bundle_sha256 = suite.pins.qualification_bundle_sha256
    if expected_bundle_sha256 is None:
        raise CandidateValidationError("suite does not pin its qualification bundle")
    if qualification_bundle_sha256(candidate_root) != expected_bundle_sha256:
        raise CandidateValidationError("qualification bundle differs from the suite pin")
    plan: list[Candidate] = []
    for task in suite.tasks:
        if task.kind not in {"code", "project"}:
            continue
        task_root = candidate_root / task.id
        reference_root = task_root / "reference"
        if not reference_root.exists():
            raise CandidateValidationError(f"{task.id} has no reference candidate")
        reference_tree = (reference_root / "Makefile").is_file()
        reference = (
            reference_root
            if reference_tree
            else reference_root / Path(task.proof_file).name
        )
        _candidate_files(reference)
        plan.append(Candidate(task, reference, f"{task.id}/reference", True, reference_tree))

        alternate = task_root / "alternate"
        if reference_tree:
            if not alternate.is_dir():
                raise CandidateValidationError(f"{task.id} has no alternate candidate")
            _candidate_files(alternate)
            if _tree_digest(reference_root) == _tree_digest(alternate):
                raise CandidateValidationError(f"{task.id} alternate is byte-identical to reference")
            plan.append(Candidate(task, alternate, f"{task.id}/alternate", True, True))
        elif alternate.exists():
            raise CandidateValidationError(f"{task.id} mixes binary reference and source alternate")

        mutant_root = task_root / "mutants"
        mutants = tuple(sorted(mutant_root.iterdir())) if mutant_root.is_dir() else ()
        if reference_tree and not mutants:
            raise CandidateValidationError(f"{task.id} has no semantic mutants")
        for mutant in mutants:
            _candidate_files(mutant)
            plan.append(
                Candidate(
                    task,
                    mutant,
                    f"{task.id}/mutants/{mutant.name}",
                    False,
                    mutant.is_dir(),
                )
            )
    if not plan:
        raise CandidateValidationError("suite contains no executable candidates")
    return tuple(plan)


def _binary_adapter(candidate: Candidate, root: Path) -> Path:
    source = root / "source"
    source.mkdir(parents=True)
    shutil.copyfile(candidate.path, source / "candidate")
    proof = Path(candidate.task.proof_file)
    if proof.is_absolute() or ".." in proof.parts:
        raise CandidateValidationError(f"{candidate.task.id} proof path is unsafe")
    (source / "Makefile").write_text(
        ".PHONY: build\n\n"
        "build:\n"
        f"\tmkdir -p {proof.parent.as_posix()}\n"
        f"\tcp candidate {proof.as_posix()}\n"
        f"\tchmod +x {proof.as_posix()}\n",
        encoding="ascii",
    )
    return source


def _assert_verdict(candidate: Candidate, verdict: Verdict) -> None:
    diagnostic = verdict.diagnostics
    if candidate.expected_pass:
        if not verdict.passed:
            raise CandidateValidationError(
                f"{candidate.label} failed: {verdict.reason}"
            )
        if (
            diagnostic.status != "complete"
            or diagnostic.criteria_failed != 0
            or diagnostic.criteria_passed == 0
        ):
            raise CandidateValidationError(
                f"{candidate.label} passed without complete verifier diagnostics"
            )
        return

    semantic_reasons = {
        "hidden behavioral case failed",
        "hidden protocol execution failed",
    }
    semantic_failure = verdict.reason.startswith("hidden suite failed") or verdict.reason in semantic_reasons
    if verdict.passed or not semantic_failure:
        raise CandidateValidationError(
            f"{candidate.label} was not rejected by verifier assertions"
        )
    if diagnostic.status not in {"complete", "partial"} or diagnostic.criteria_failed == 0:
        raise CandidateValidationError(
            f"{candidate.label} failed without a verifier assertion diagnostic"
        )


def _grade_candidate(
    candidate: Candidate,
    suite_root: Path,
    runner,
    challenge: str,
    scratch: Path,
) -> Verdict:
    candidate_root = candidate.path
    if not candidate.source_tree:
        candidate_root = _binary_adapter(candidate, scratch / "adapter")
    artifact = scratch / "artifact"
    if candidate.task.kind == "project":
        spec = candidate.task.verifier
        if not isinstance(spec, ProjectVerifierSpec):
            raise CandidateValidationError(f"{candidate.task.id} project verifier is invalid")
        return grade_project_task(
            candidate.task,
            candidate_root,
            spec,
            {CODE_CHALLENGE_ENV: challenge},
            runner,
            artifact_dir=artifact,
            suite_dir=suite_root / candidate.task.id,
        )
    hidden = suite_root / candidate.task.id / str(candidate.task.verifier)
    return grade_code_task(
        candidate.task,
        candidate_root,
        hidden,
        {CODE_CHALLENGE_ENV: challenge},
        runner,
        artifact_dir=artifact,
    )


def validate_suite_candidates(
    suite_root: Path,
    candidate_root: Path,
    *,
    agent_image: str,
    verifier_image: str,
    scratch_root: Path,
    repetitions: int,
) -> tuple[int, int, int]:
    if not _IMAGE_ID.fullmatch(agent_image) or not _IMAGE_ID.fullmatch(verifier_image):
        raise CandidateValidationError("candidate validation requires exact role image IDs")
    if agent_image == verifier_image:
        raise CandidateValidationError("agent and verifier image IDs must differ")
    if isinstance(repetitions, bool) or not 1 <= repetitions <= 3:
        raise CandidateValidationError("repetitions must be between 1 and 3")
    scratch_root.mkdir(parents=True, exist_ok=True)
    validate_spore_toolchain(suite_root, agent_image)
    plan = candidate_plan(suite_root, candidate_root)
    bundle_digest = qualification_bundle_sha256(candidate_root)
    task_hashes = {
        candidate.task.id: hash_task_dir(suite_root / candidate.task.id)
        for candidate in plan
    }
    passed = rejected = 0
    with tempfile.TemporaryDirectory(prefix="candidate-gate-", dir=scratch_root) as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir(mode=0o755)
        config = RunnerConfig(
            agent_image=agent_image,
            verifier_image=verifier_image,
            work_volume=str(work),
            uid=os.getuid(),
            gid=os.getgid(),
            max_build_retries=1,
        )
        for repetition in range(repetitions):
            challenge = hashlib.sha256(
                f"{load_suite(suite_root).suite_semver}\0{repetition}".encode("ascii")
            ).hexdigest()
            for index, candidate in enumerate(plan):
                candidate_scratch = root / f"run-{repetition}-{index}"
                candidate_scratch.mkdir()
                verdict = _grade_candidate(
                    candidate,
                    suite_root,
                    make_docker_runner(config),
                    challenge,
                    candidate_scratch,
                )
                _assert_verdict(candidate, verdict)
                if candidate.expected_pass:
                    passed += 1
                else:
                    rejected += 1
    for task_id, expected in task_hashes.items():
        if hash_task_dir(suite_root / task_id) != expected:
            raise CandidateValidationError(f"candidate validation modified {task_id}")
    if qualification_bundle_sha256(candidate_root) != bundle_digest:
        raise CandidateValidationError("qualification bundle changed during candidate validation")
    return len(plan), passed, rejected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--candidate-root")
    mode.add_argument("--assert-release-clean", action="store_true")
    parser.add_argument("--agent-image")
    parser.add_argument("--verifier-image")
    parser.add_argument("--scratch-root")
    parser.add_argument("--repetitions", type=int, default=1)
    args = parser.parse_args()
    try:
        suite_root = Path(args.suite).expanduser().resolve()
        if args.assert_release_clean:
            assert_release_contains_no_candidates(suite_root)
            print("released suite contains no qualification candidates")
            return 0
        if not args.agent_image or not args.verifier_image or not args.scratch_root:
            raise CandidateValidationError(
                "candidate validation needs both role images and a scratch root"
            )
        candidate_root = Path(args.candidate_root).expanduser()
        scratch_root = external_directory(args.scratch_root, "scratch root")
        candidates, passed, rejected = validate_suite_candidates(
            suite_root,
            candidate_root,
            agent_image=args.agent_image,
            verifier_image=args.verifier_image,
            scratch_root=scratch_root,
            repetitions=args.repetitions,
        )
    except (CandidateValidationError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(
        f"suite candidates: {candidates} candidates x {args.repetitions}; "
        f"{passed} correct passes, {rejected} mutants rejected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
