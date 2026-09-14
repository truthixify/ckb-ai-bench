"""Registry load/validate tests (ADR-0008 strict independence, fail-loud contract)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ckbbench.run.retry_policy import RETRY_POLICY_ID, RETRY_POLICY_SHA256
from ckbbench.suite.execution_contract import (
    TASK_EXECUTION_SCHEMA_VERSION,
    BudgetCalibration,
    HarnessDeadlines,
    TaskBudgetProfile,
    TaskExecutionContract,
    TreatmentRequirement,
)
from ckbbench.suite.freeze import freeze
from ckbbench.suite.registry import RegistryError, load_suite


FIXTURE_CONSTANT = "0x5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e"


def execution_contract(contract_id: str) -> dict:
    return TaskExecutionContract(
        contract_id=contract_id,
        chain_track="testnet",
        chain_profile_id="ckb-testnet-pudge-v1",
        chain_profile_sha256="1" * 64,
        budget=TaskBudgetProfile(
            profile_id=f"{contract_id}-budget",
            step_limit=20,
            wall_time_limit_seconds=480,
            provider_call_limit=20,
            output_token_limit=None,
        ),
        harness_deadlines=HarnessDeadlines(120, 120, 180, 120),
        treatment=TreatmentRequirement(
            requirement_id="ckb-ai-testnet-docs-v1",
            claims_live_chain=True,
            required_tools=("search_resources",),
            required_resource_prefixes=("ckb://docs/",),
        ),
        signer_required=False,
        signing_policy_id=None,
        funding=None,
        required_dependencies=(),
        required_resource_kinds=("runtime-name", "workspace"),
        expected_output_resource_kinds=("workspace",),
        run_params_derivation="task-run-params-v1",
        resource_equivalence_policy_id="read-only-chain-equivalence-v1",
        calibration=BudgetCalibration(
            status="calibrated",
            evidence_sha256s=(("a" if contract_id.endswith("a") else "b") * 64,),
            observed_max_steps=10,
            observed_max_wall_seconds=200,
            observed_max_provider_calls=10,
        ),
    ).to_dict()


def build_registry(
    root: Path,
    *,
    tasks: list[dict] | None = None,
    manifest_overrides: dict | None = None,
) -> Path:
    """Build a minimal valid registry fixture (no spikes/ dependency)."""
    default_tasks = [
        {
            "id": "task-a",
            "proof_file": "proof_a.txt",
            "score": 10,
            "kind": "onchain",
            "check": "constant_hex",
            "rpc_method": "constant",
            "rpc_params": [FIXTURE_CONSTANT],
            "fragment": f"Write the constant {FIXTURE_CONSTANT} to proof_a.txt.",
        },
        {
            "id": "task-b",
            "proof_file": "proof_b.txt",
            "score": 5,
            "kind": "onchain",
            "check": "epoch_number",
            "rpc_method": "get_current_epoch",
            "fragment": "Write epoch to proof_b.txt.",
        },
    ]
    task_defs = tasks if tasks is not None else default_tasks
    manifest = {
        "suite_semver": "1.0.0",
        "chain_profile": "devnet",
        "mcp_server_version": "1.6.12",
        "agent_image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "verifier_image_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "toolchain_versions": {"rust": "1.85.0"},
        "tasks": [t["id"] for t in task_defs],
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)

    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    for t in task_defs:
        tdir = root / t["id"]
        tdir.mkdir()
        meta = {k: v for k, v in t.items() if k not in ("budget_basis", "fragment")}
        (tdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        (tdir / "prompt.txt").write_text(t.get("fragment", f"Do {t['id']}.\n"))
        if "budget_basis" in t:
            (tdir / "budget-basis.json").write_text(
                json.dumps(t["budget_basis"], indent=2, sort_keys=True) + "\n"
            )

    return root


def test_good_registry_loads_ordered_tasks(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    suite = load_suite(root)
    assert suite.suite_semver == "1.0.0"
    assert suite.chain_profile == "devnet"
    assert [t.id for t in suite.tasks] == ["task-a", "task-b"]
    assert suite.tasks[0].prompt_fragment.startswith("Write the constant")
    assert suite.pins.agent_image_digest == "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert suite.pins.verifier_image_digest == "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert suite.pins.agent_image_digest != suite.pins.verifier_image_digest
    assert suite.pins.toolchain_versions["rust"] == "1.85.0"


def test_independent_task_suite_requires_and_freezes_every_execution_contract(tmp_path: Path):
    task_defs = [
        {
            "id": "task-a",
            "proof_file": "proof_a.txt",
            "score": 10,
            "kind": "onchain",
            "check": "constant_hex",
            "rpc_method": "constant",
            "rpc_params": [FIXTURE_CONSTANT],
            "fragment": "Write a chain proof.",
            "execution": execution_contract("execution-task-a"),
        },
        {
            "id": "task-b",
            "proof_file": "proof_b.txt",
            "score": 5,
            "kind": "onchain",
            "check": "epoch_number",
            "rpc_method": "get_current_epoch",
            "fragment": "Write another chain proof.",
            "execution": execution_contract("execution-task-b"),
        },
    ]
    root = build_registry(
        tmp_path / "reg",
        tasks=task_defs,
        manifest_overrides={
            "suite_semver": "4.0.0",
            "chain_profile": "task-scoped",
            "task_execution_schema_version": TASK_EXECUTION_SCHEMA_VERSION,
            "retry_policy_id": RETRY_POLICY_ID,
            "retry_policy_sha256": RETRY_POLICY_SHA256,
        },
    )

    suite = load_suite(root)
    assert suite.task_execution_schema_version == TASK_EXECUTION_SCHEMA_VERSION
    assert all(task.execution is not None for task in suite.tasks)
    frozen = freeze(suite, root)
    for task in suite.tasks:
        assert frozen["tasks"][task.id]["execution_contract_sha256"] == task.execution.sha256
    assert frozen["task_execution_schema_version"] == TASK_EXECUTION_SCHEMA_VERSION
    assert frozen["pins"]["retry_policy_id"] == RETRY_POLICY_ID
    assert frozen["pins"]["retry_policy_sha256"] == RETRY_POLICY_SHA256


def test_independent_task_suite_refuses_missing_schema_contract_and_duplicate_contract_id(
    tmp_path: Path,
):
    root = build_registry(
        tmp_path / "missing-schema",
        manifest_overrides={"suite_semver": "4.0.0"},
    )
    with pytest.raises(RegistryError, match="declare its execution schema"):
        load_suite(root)

    root = build_registry(
        tmp_path / "missing-contract",
        manifest_overrides={
            "suite_semver": "4.0.0",
            "task_execution_schema_version": TASK_EXECUTION_SCHEMA_VERSION,
        },
    )
    with pytest.raises(RegistryError, match="every Task"):
        load_suite(root)

    task_defs = []
    for task_id in ("task-a", "task-b"):
        task_defs.append({
            "id": task_id,
            "proof_file": f"{task_id}.txt",
            "score": 5,
            "kind": "onchain",
            "check": "epoch_number",
            "rpc_method": "get_current_epoch",
            "fragment": f"Write {task_id}.",
            "execution": execution_contract("same-contract"),
        })
    root = build_registry(
        tmp_path / "duplicate-contract",
        tasks=task_defs,
        manifest_overrides={
            "suite_semver": "4.0.0",
            "task_execution_schema_version": TASK_EXECUTION_SCHEMA_VERSION,
        },
    )
    with pytest.raises(RegistryError, match="IDs must be unique"):
        load_suite(root)


def test_registry_wraps_execution_contract_errors_without_echoing_contract_data(tmp_path: Path):
    contract = execution_contract("execution-task-a")
    contract["budget"]["step_limit"] = True
    root = build_registry(
        tmp_path / "bad-contract",
        tasks=[{
            "id": "task-a",
            "proof_file": "proof.txt",
            "score": 5,
            "kind": "onchain",
            "check": "epoch_number",
            "rpc_method": "get_current_epoch",
            "fragment": "Write a proof.",
            "execution": contract,
        }],
        manifest_overrides={
            "suite_semver": "4.0.0",
            "task_execution_schema_version": TASK_EXECUTION_SCHEMA_VERSION,
        },
    )
    with pytest.raises(RegistryError, match="execution contract is invalid") as error:
        load_suite(root)
    assert "search_resources" not in str(error.value)


def test_missing_task_dir_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    shutil.rmtree(root / "task-b")
    with pytest.raises(RegistryError, match="has no directory"):
        load_suite(root)


def test_duplicate_id_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "a.txt",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a", "task-a"]},
    )
    with pytest.raises(RegistryError, match="duplicate task id"):
        load_suite(root)


def test_non_positive_score_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "a.txt",
                "score": 0,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a"]},
    )
    with pytest.raises(RegistryError, match="score must be a positive integer"):
        load_suite(root)


def test_missing_proof_file_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "   ",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a"]},
    )
    with pytest.raises(RegistryError, match="missing proof_file"):
        load_suite(root)


@pytest.mark.parametrize(
    "proof_file",
    (
        "/tmp/proof.txt",
        "../proof.txt",
        "build/../proof.txt",
        "build//proof.txt",
        "build\\proof.txt",
        "proof.txt/",
    ),
)
def test_proof_file_must_be_a_canonical_relative_path(
    tmp_path: Path,
    proof_file: str,
):
    root = build_registry(
        tmp_path / "reg",
        tasks=[{
            "id": "task-a",
            "proof_file": proof_file,
            "score": 1,
            "kind": "onchain",
            "check": "x",
            "rpc_method": "m",
            "fragment": "a",
        }],
        manifest_overrides={"tasks": ["task-a"]},
    )
    with pytest.raises(RegistryError, match="bounded relative file path"):
        load_suite(root)


def test_fragment_referencing_other_proof_file_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "proof_a.txt",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "Write proof_a.txt only.",
            },
            {
                "id": "task-b",
                "proof_file": "proof_b.txt",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "fragment": "Also read proof_a.txt for context.",
            },
        ],
    )
    with pytest.raises(RegistryError, match="strict independence violated"):
        load_suite(root)


def test_independence_does_not_false_positive_on_substring(tmp_path: Path):
    # A fragment containing another proof name only as a substring of a longer token must not be
    # flagged. task-a's proof is "out.txt"; task-b
    # legitimately mentions its own "checkout.txt"-like token, which contains "out.txt".
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a", "proof_file": "out.txt", "score": 1, "kind": "onchain",
                "check": "x", "rpc_method": "m", "fragment": "Write to out.txt.",
            },
            {
                "id": "task-b", "proof_file": "result.txt", "score": 1, "kind": "onchain",
                "check": "x", "rpc_method": "m", "fragment": "Write your checkout.txt summary to result.txt.",
            },
        ],
    )
    # Must load cleanly: "out.txt" appears only inside "checkout.txt", not as a standalone token.
    suite = load_suite(root)
    assert [t.id for t in suite.tasks] == ["task-a", "task-b"]


def test_oversized_prompt_file_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "prompt.txt").write_text("x" * ((1 << 20) + 1))
    with pytest.raises(RegistryError, match="over the"):
        load_suite(root)


def test_scored_flag_parsed_and_defaults_true(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    # default registry tasks have no 'scored' -> defaults True
    suite = load_suite(root)
    assert all(t.scored for t in suite.tasks)


def test_scored_false_loads_as_unscored(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a", "proof_file": "a.txt", "score": 1, "kind": "onchain",
                "check": "constant_hex", "rpc_method": "constant",
                "scored": False, "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a"]},
    )
    suite = load_suite(root)
    assert suite.tasks[0].scored is False


def test_non_bool_scored_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a", "proof_file": "a.txt", "score": 1, "kind": "onchain",
                "check": "constant_hex", "rpc_method": "constant",
                "scored": "yes", "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a"]},
    )
    with pytest.raises(RegistryError, match="'scored' must be a boolean"):
        load_suite(root)


def test_non_utf8_meta_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "meta.json").write_bytes(b"\xff\xfe not utf8")
    with pytest.raises(RegistryError, match="not valid UTF-8"):
        load_suite(root)


@pytest.mark.parametrize("relative_path", ("manifest.json", "task-a/meta.json"))
def test_registry_refuses_duplicate_json_keys(tmp_path: Path, relative_path: str):
    root = build_registry(tmp_path / "reg")
    path = root / relative_path
    payload = path.read_text(encoding="utf-8")
    path.write_text(payload.replace("{", '{"duplicate": 1, "duplicate": 2,', 1))

    with pytest.raises(RegistryError, match="duplicate JSON key"):
        load_suite(root)


def test_missing_manifest_raises(tmp_path: Path):
    with pytest.raises(RegistryError, match="missing manifest.json"):
        load_suite(tmp_path / "nope")


def test_manifest_missing_required_field_raises(tmp_path: Path):
    root = tmp_path / "reg"
    root.mkdir()
    (root / "manifest.json").write_text('{"suite_semver": "1.0.0"}\n')
    with pytest.raises(RegistryError, match="missing required field"):
        load_suite(root)


def test_empty_tasks_list_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg", manifest_overrides={"tasks": []})
    with pytest.raises(RegistryError, match="non-empty ordered list"):
        load_suite(root)


def test_code_task_loads_verifier_dir(tmp_path: Path):
    root = tmp_path / "reg"
    build_registry(
        root,
        tasks=[
            {
                "id": "code-1",
                "proof_file": "out.rbc",
                "score": 20,
                "kind": "code",
                "verifier_dir": "hidden",
                "fragment": "Build contract.",
            },
        ],
        manifest_overrides={"tasks": ["code-1"]},
    )
    (root / "code-1" / "hidden").mkdir()
    suite = load_suite(root)
    assert suite.tasks[0].kind == "code"
    assert suite.tasks[0].verifier == "hidden"


def test_code_task_missing_verifier_dir_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "code-1",
                "proof_file": "out.rbc",
                "score": 20,
                "kind": "code",
                "fragment": "Build contract.",
            },
        ],
        manifest_overrides={"tasks": ["code-1"]},
    )
    with pytest.raises(RegistryError, match="missing verifier_dir"):
        load_suite(root)


def test_code_task_refuses_escaping_or_symlinked_verifier_dir(tmp_path: Path):
    task = {
        "id": "code-1",
        "proof_file": "out.rbc",
        "score": 20,
        "kind": "code",
        "verifier_dir": "../outside",
        "fragment": "Build contract.",
    }
    root = build_registry(tmp_path / "escaping", tasks=[task])
    (root / "outside").mkdir()
    with pytest.raises(RegistryError, match="one relative directory name"):
        load_suite(root)

    task["verifier_dir"] = "hidden"
    root = build_registry(tmp_path / "linked", tasks=[task])
    outside = tmp_path / "hidden"
    outside.mkdir()
    (root / "code-1" / "hidden").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RegistryError, match="not a regular directory"):
        load_suite(root)


@pytest.mark.parametrize("verifier_dir", ("hidden\\escape", "hidden/escape", "../hidden", "/hidden"))
def test_code_task_verifier_dir_is_one_portable_component(
    tmp_path: Path, verifier_dir: str
):
    root = build_registry(
        tmp_path / "reg",
        tasks=[{
            "id": "code-1",
            "proof_file": "out.rbc",
            "score": 20,
            "kind": "code",
            "verifier_dir": verifier_dir,
            "fragment": "Build contract.",
        }],
        manifest_overrides={"tasks": ["code-1"]},
    )
    with pytest.raises(RegistryError, match="one relative directory name"):
        load_suite(root)


@pytest.mark.parametrize("starter_dir", ("starter\\escape", "starter/escape", "../starter", "/starter"))
def test_project_task_starter_dir_is_one_portable_component(
    tmp_path: Path, starter_dir: str
):
    task = {
        "id": "project-1",
        "proof_file": "build/release/tool",
        "score": 4,
        "kind": "project",
        "check": "address_codec",
        "case_count": 8,
        "starter_dir": starter_dir,
        "fragment": "Build a tool.",
    }
    root = build_registry(tmp_path / "reg", tasks=[task])
    with pytest.raises(RegistryError, match="one relative directory name"):
        load_suite(root)


def test_project_task_loads_supported_checker_starter_and_report_metadata(tmp_path: Path):
    task = {
        "id": "project-1",
        "proof_file": "build/release/tool",
        "score": 4,
        "kind": "project",
        "check": "address_codec",
        "case_count": 8,
        "starter_dir": "starter",
        "verifier_dir": "hidden",
        "report": {
            "category": "Addressing",
            "freshness": "Hidden cases are challenge-derived.",
            "name": "Address tool",
            "objective": "Encode and decode CKB addresses.",
            "proof": "Executable project.",
            "verification": "Independent hidden vectors check every result.",
        },
        "fragment": "Build an address tool.",
    }
    root = build_registry(tmp_path / "reg", tasks=[task])
    starter = root / "project-1" / "starter"
    starter.mkdir()
    (starter / "source.py").write_text("pass\n")
    hidden = root / "project-1" / "hidden"
    hidden.mkdir()
    (hidden / "Cargo.toml").write_text("[package]\nname='hidden'\nversion='0.1.0'\n")

    loaded = load_suite(root).tasks[0]

    assert loaded.kind == "project"
    assert loaded.verifier.check == "address_codec"
    assert loaded.verifier.case_count == 8
    assert loaded.verifier.verifier_dir == "hidden"
    assert loaded.starter_dir == "starter"
    assert loaded.report is not None
    assert loaded.report.name == "Address tool"


def test_project_task_refuses_unknown_checker(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[{
            "id": "project-1",
            "proof_file": "build/release/tool",
            "score": 4,
            "kind": "project",
            "check": "looks_valid_but_has_no_verifier",
            "case_count": 8,
            "fragment": "Build a tool.",
        }],
    )

    with pytest.raises(RegistryError, match="unsupported check"):
        load_suite(root)


@pytest.mark.parametrize(
    ("check", "case_count", "limit"),
    (("address_codec", 24, 23), ("transaction_balancer", 9, 8)),
)
def test_project_task_refuses_more_cases_than_its_checker_defines(
    tmp_path: Path, check: str, case_count: int, limit: int
):
    root = build_registry(
        tmp_path / "reg",
        tasks=[{
            "id": "project-1",
            "proof_file": "build/release/tool",
            "score": 4,
            "kind": "project",
            "check": check,
            "case_count": case_count,
            "fragment": "Build a tool.",
        }],
    )

    with pytest.raises(RegistryError, match=rf"between 1 and {limit}"):
        load_suite(root)


def test_project_task_refuses_missing_or_symlinked_hidden_verifier(tmp_path: Path):
    task = {
        "id": "project-1",
        "proof_file": "build/release/tool",
        "score": 4,
        "kind": "project",
        "check": "address_codec",
        "case_count": 8,
        "verifier_dir": "hidden",
        "fragment": "Build a tool.",
    }
    root = build_registry(tmp_path / "missing", tasks=[task])
    with pytest.raises(RegistryError, match="not a regular directory"):
        load_suite(root)

    root = build_registry(tmp_path / "linked", tasks=[task])
    target = tmp_path / "outside-hidden"
    target.mkdir()
    (root / "project-1" / "hidden").symlink_to(target, target_is_directory=True)
    with pytest.raises(RegistryError, match="not a regular directory"):
        load_suite(root)


def test_hidden_verifier_refuses_content_excluded_from_freeze(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[{
            "id": "code-1",
            "proof_file": "contract",
            "score": 4,
            "kind": "code",
            "verifier_dir": "hidden",
            "fragment": "Build a contract.",
        }],
    )
    generated = root / "code-1" / "hidden" / "target" / "cached-binary"
    generated.parent.mkdir(parents=True)
    generated.write_text("unbound\n")

    with pytest.raises(RegistryError, match="excluded from the suite freeze"):
        load_suite(root)


def test_starter_tree_refuses_nested_symlinks(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[{
            "id": "project-1",
            "proof_file": "build/release/tool",
            "score": 4,
            "kind": "project",
            "check": "address_codec",
            "case_count": 8,
            "starter_dir": "starter",
            "fragment": "Build a tool.",
        }],
    )
    starter = root / "project-1" / "starter"
    nested = starter / "nested"
    nested.mkdir(parents=True)
    (nested / "link").symlink_to(root / "manifest.json")

    with pytest.raises(RegistryError, match="cannot contain symlinks"):
        load_suite(root)


@pytest.mark.parametrize("generated_path", ("target/cache", "__pycache__/tool.pyc", ".git/config"))
def test_starter_tree_refuses_content_excluded_from_freeze(
    tmp_path: Path, generated_path: str
):
    root = build_registry(
        tmp_path / "reg",
        tasks=[{
            "id": "project-1",
            "proof_file": "build/release/tool",
            "score": 4,
            "kind": "project",
            "check": "address_codec",
            "case_count": 8,
            "starter_dir": "starter",
            "fragment": "Build a tool.",
        }],
    )
    generated = root / "project-1" / "starter" / generated_path
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("unbound\n")

    with pytest.raises(RegistryError, match="excluded from the suite freeze"):
        load_suite(root)


@pytest.mark.parametrize("registry_file", ("manifest.json", "task-a/meta.json", "task-a/prompt.txt"))
def test_registry_files_cannot_be_symlinks(tmp_path: Path, registry_file: str):
    root = build_registry(tmp_path / "reg")
    path = root / registry_file
    external = tmp_path / (path.name + ".external")
    external.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(external)

    with pytest.raises(RegistryError, match="regular file"):
        load_suite(root)


def test_onchain_task_cannot_declare_starter_tree(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "starter").mkdir()
    meta_path = root / "task-a" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["starter_dir"] = "starter"
    meta_path.write_text(json.dumps(meta) + "\n")

    with pytest.raises(RegistryError, match="cannot declare starter_dir"):
        load_suite(root)


def test_version_six_requires_report_metadata_for_every_task(tmp_path: Path):
    task = {
        "id": "task-a",
        "proof_file": "proof.txt",
        "score": 4,
        "kind": "onchain",
        "check": "constant_hex",
        "rpc_method": "constant",
        "rpc_params": [FIXTURE_CONSTANT],
        "fragment": "Write the constant.",
        "execution": execution_contract("execution-task-a"),
    }
    root = build_registry(
        tmp_path / "reg",
        tasks=[task],
        manifest_overrides={
            "suite_semver": "6.0.0",
            "task_execution_schema_version": TASK_EXECUTION_SCHEMA_VERSION,
            "retry_policy_id": RETRY_POLICY_ID,
            "retry_policy_sha256": RETRY_POLICY_SHA256,
            "qualification_bundle_sha256": "d" * 64,
        },
    )

    with pytest.raises(RegistryError, match="needs report metadata"):
        load_suite(root)


def test_version_six_requires_qualification_bundle_pin(tmp_path: Path):
    task = {
        "id": "task-a",
        "proof_file": "proof.txt",
        "score": 4,
        "kind": "onchain",
        "check": "constant_hex",
        "rpc_method": "constant",
        "rpc_params": [FIXTURE_CONSTANT],
        "fragment": "Write the constant.",
        "execution": execution_contract("execution-task-a"),
        "report": {
            "category": "Protocol",
            "freshness": "Challenge values are generated per attempt.",
            "name": "Constant",
            "objective": "Write a protocol constant.",
            "proof": "A checked proof file.",
            "verification": "The verifier checks the exact value.",
        },
    }
    root = build_registry(
        tmp_path / "reg",
        tasks=[task],
        manifest_overrides={
            "suite_semver": "6.0.0",
            "task_execution_schema_version": TASK_EXECUTION_SCHEMA_VERSION,
            "retry_policy_id": RETRY_POLICY_ID,
            "retry_policy_sha256": RETRY_POLICY_SHA256,
        },
    )

    with pytest.raises(RegistryError, match="must pin its qualification bundle"):
        load_suite(root)


def test_param_schema_parsed(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "send-1",
                "proof_file": "tx_id.txt",
                "score": 15,
                "kind": "onchain",
                "check": "tx",
                "rpc_method": "get_transaction",
                "param_schema": [
                    {
                        "name": "send_amount_shannons",
                        "class": "prompt",
                        "generator": "high_entropy_nonce_amount_shannons",
                        "share_group": "nonce",
                    },
                    {
                        "name": "harness_tip",
                        "class": "verifier",
                        "generator": "harness_tip",
                    },
                ],
                "fragment": "Send CKB.",
            },
        ],
        manifest_overrides={"tasks": ["send-1"]},
    )
    suite = load_suite(root)
    assert len(suite.tasks[0].param_schema) == 2
    assert suite.tasks[0].param_schema[0].param_class == "prompt"
    assert suite.tasks[0].param_schema[0].share_group == "nonce"


def test_param_schema_invalid_share_group_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = [
        {"name": "x", "class": "prompt", "generator": "static", "static_value": "y", "share_group": 1},
    ]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="share_group must be a non-empty string"):
        load_suite(root)


def test_invalid_param_schema_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "task-a",
                "proof_file": "a.txt",
                "score": 1,
                "kind": "onchain",
                "check": "x",
                "rpc_method": "m",
                "param_schema": [{"name": "x", "class": "nope", "generator": "static"}],
                "fragment": "a",
            },
        ],
        manifest_overrides={"tasks": ["task-a"]},
    )
    with pytest.raises(RegistryError, match="class must be"):
        load_suite(root)


def test_meta_id_must_match_directory_name(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["id"] = "wrong-id"
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="must match directory name"):
        load_suite(root)


def test_invalid_json_raises(tmp_path: Path):
    root = tmp_path / "reg"
    root.mkdir()
    (root / "manifest.json").write_text("{not json\n")
    with pytest.raises(RegistryError, match="not valid JSON"):
        load_suite(root)


@pytest.mark.parametrize(
    "task_id",
    ["", "../outside", "task-a/subdir", "task\\x", "/absolute", ".", ".."],
)
def test_invalid_manifest_task_id_raises(tmp_path: Path, task_id: str):
    root = build_registry(tmp_path / "reg", manifest_overrides={"tasks": [task_id]})
    with pytest.raises(RegistryError, match="invalid task id"):
        load_suite(root)


def test_manifest_task_directory_must_not_be_a_symlink(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    real_task = root / "task-a-real"
    (root / "task-a").rename(real_task)
    (root / "task-a").symlink_to(real_task, target_is_directory=True)

    with pytest.raises(RegistryError, match="regular directory"):
        load_suite(root)


def test_missing_prompt_txt_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "prompt.txt").unlink()
    with pytest.raises(RegistryError, match="missing prompt.txt"):
        load_suite(root)


def test_meta_not_object_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    (root / "task-a" / "meta.json").write_text("[]\n")
    with pytest.raises(RegistryError, match="must be a JSON object"):
        load_suite(root)


def test_meta_missing_required_field_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    del meta["kind"]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="missing required field"):
        load_suite(root)


def test_invalid_kind_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["kind"] = "wasm"
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="kind must be"):
        load_suite(root)


def test_onchain_missing_check_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    del meta["check"]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="missing 'check'"):
        load_suite(root)


def test_onchain_invalid_rpc_params_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["rpc_params"] = "nope"
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="rpc_params must be a list"):
        load_suite(root)


def test_code_verifier_dir_missing_on_disk_raises(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        tasks=[
            {
                "id": "code-1",
                "proof_file": "out.rbc",
                "score": 20,
                "kind": "code",
                "verifier_dir": "hidden",
                "fragment": "Build contract.",
            },
        ],
        manifest_overrides={"tasks": ["code-1"]},
    )
    with pytest.raises(RegistryError, match="verifier_dir .* not found"):
        load_suite(root)


def test_param_schema_must_be_list_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = {}
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="param_schema must be a list"):
        load_suite(root)


def test_param_schema_entry_must_be_object_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = ["bad"]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="must be an object"):
        load_suite(root)


def test_param_schema_missing_field_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = [{"class": "prompt", "generator": "static", "static_value": "x"}]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="missing 'name'"):
        load_suite(root)


def test_param_schema_unknown_generator_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = [{"name": "x", "class": "prompt", "generator": "magic"}]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="unknown generator"):
        load_suite(root)


def test_param_schema_static_requires_value_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = [{"name": "x", "class": "prompt", "generator": "static"}]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="static generator requires static_value"):
        load_suite(root)


def test_param_schema_recipient_static_value_type_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["param_schema"] = [
        {"name": "r", "class": "prompt", "generator": "recipient_args", "static_value": 1},
    ]
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="must be a string"):
        load_suite(root)


def test_invalid_toolchain_versions_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg", manifest_overrides={"toolchain_versions": "bad"})
    with pytest.raises(RegistryError, match="toolchain_versions must be an object"):
        load_suite(root)


@pytest.mark.parametrize(
    "bad",
    ("short", "A" * 64, "sha256:" + "a" * 64, 1, True),
)
def test_invalid_qualification_bundle_digest_raises(tmp_path: Path, bad):
    root = build_registry(
        tmp_path / "reg",
        manifest_overrides={"qualification_bundle_sha256": bad},
    )
    with pytest.raises(RegistryError, match="qualification_bundle_sha256"):
        load_suite(root)


def test_qualification_bundle_digest_is_typed_and_frozen(tmp_path: Path):
    digest = "c" * 64
    root = build_registry(
        tmp_path / "reg",
        manifest_overrides={"qualification_bundle_sha256": digest},
    )
    suite = load_suite(root)
    assert suite.pins.qualification_bundle_sha256 == digest
    assert suite.pins.extra == {}
    assert freeze(suite, root)["pins"]["qualification_bundle_sha256"] == digest


def test_non_integer_score_raises(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    meta = json.loads((root / "task-a" / "meta.json").read_text())
    meta["score"] = "10"
    (root / "task-a" / "meta.json").write_text(json.dumps(meta) + "\n")
    with pytest.raises(RegistryError, match="score must be a positive integer"):
        load_suite(root)

AGENT_PIN = "sha256:" + "a" * 64
VERIFIER_PIN = "sha256:" + "b" * 64


@pytest.mark.parametrize("key", ["agent_image_digest", "verifier_image_digest"])
@pytest.mark.parametrize(
    "bad",
    [
        "TO_BE_FILLED",
        "latest",
        "ckbbench-agent:latest",
        "sha256:abc",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "ckbbench-agent@sha256:" + "a" * 64,
        123,
        ["sha256:" + "a" * 64],
    ],
)
def test_malformed_role_pin_is_rejected(tmp_path: Path, key, bad):
    root = build_registry(tmp_path / "reg", manifest_overrides={key: bad})
    with pytest.raises(RegistryError, match=key):
        load_suite(root)


def test_retired_singular_pin_is_rejected(tmp_path: Path):
    """One value cannot identify two different images; the retired key must not pass silently."""
    root = build_registry(
        tmp_path / "reg", manifest_overrides={"docker_image_digest": AGENT_PIN}
    )
    with pytest.raises(RegistryError, match="docker_image_digest"):
        load_suite(root)


def test_absent_role_pins_are_allowed_for_synthetic_suites(tmp_path: Path):
    root = build_registry(tmp_path / "reg")
    manifest = json.loads((root / "manifest.json").read_text())
    del manifest["agent_image_digest"]
    del manifest["verifier_image_digest"]
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    suite = load_suite(root)
    assert suite.pins.agent_image_digest is None
    assert suite.pins.verifier_image_digest is None


@pytest.mark.parametrize("missing", ["agent_image_digest", "verifier_image_digest"])
def test_released_suite_requires_both_role_pins(tmp_path: Path, missing):
    """Without the pin, that role silently falls back to a mutable default the freeze never named."""
    root = build_registry(tmp_path / "reg", manifest_overrides={"suite_semver": "2.0.0"})
    manifest = json.loads((root / "manifest.json").read_text())
    del manifest[missing]
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with pytest.raises(RegistryError, match=missing):
        load_suite(root)


def test_released_suite_rejects_equal_role_pins(tmp_path: Path):
    """One value cannot identify two different images."""
    root = build_registry(
        tmp_path / "reg",
        manifest_overrides={
            "suite_semver": "2.0.0",
            "agent_image_digest": AGENT_PIN,
            "verifier_image_digest": AGENT_PIN,
        },
    )
    with pytest.raises(RegistryError, match="must differ"):
        load_suite(root)


def test_released_suite_accepts_two_distinct_pins(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        manifest_overrides={
            "suite_semver": "2.0.0",
            "agent_image_digest": AGENT_PIN,
            "verifier_image_digest": VERIFIER_PIN,
        },
    )
    suite = load_suite(root)
    assert suite.pins.agent_image_digest == AGENT_PIN
    assert suite.pins.verifier_image_digest == VERIFIER_PIN


def test_development_suite_may_still_omit_both_pins(tmp_path: Path):
    """The brief keeps absent pins legal for synthetic/development registries."""
    root = build_registry(tmp_path / "reg", manifest_overrides={"suite_semver": "1.4.0"})
    manifest = json.loads((root / "manifest.json").read_text())
    del manifest["agent_image_digest"]
    del manifest["verifier_image_digest"]
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    suite = load_suite(root)
    assert suite.pins.agent_image_digest is None
    assert suite.pins.verifier_image_digest is None


def test_development_suite_may_share_a_pin_value(tmp_path: Path):
    root = build_registry(
        tmp_path / "reg",
        manifest_overrides={
            "suite_semver": "1.4.0",
            "agent_image_digest": AGENT_PIN,
            "verifier_image_digest": AGENT_PIN,
        },
    )
    assert load_suite(root).pins.agent_image_digest == AGENT_PIN


@pytest.mark.parametrize("key", ["agent_image_digest", "verifier_image_digest"])
def test_all_zero_placeholder_pin_is_rejected(tmp_path: Path, key):
    """Well-formed but identifies nothing; the brief lists it as a forbidden placeholder."""
    root = build_registry(tmp_path / "reg", manifest_overrides={key: "sha256:" + "0" * 64})
    with pytest.raises(RegistryError, match="all-zero"):
        load_suite(root)
