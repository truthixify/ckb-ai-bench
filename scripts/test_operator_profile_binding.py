"""`./bench smoke|run` must bind the reviewed model profile before any external seam (ADR-0014).

The wrappers hold the project lock, preflight Docker/MCP/LLM and then launch. If the profile is
resolved after any of that, readiness can check one endpoint while the cell runs against another,
and an alternate profile is only rejected once a real cell has already started.

These tests drive the real script with fake `docker`/`curl` on PATH, so a wrapper that reached an
external seam would be visible as a recorded call rather than as an actual request.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "scripts" / "ckbbench"

PROFILE_DOC = {
    "api_base": "https://proxy.example/v1",
    "api_style": "openai-responses",
    "drop_unsupported_params": True,
    "evidence_utc": "2026-08-15T09:30:00Z",
    "litellm_num_retries": 0,
    "max_agent_query_attempts": 1,
    "model_stability": "dated_snapshot",
    "probed_response_model": "gpt-probe-2026-02-11",
    "profile_id": "phase1-gpt-v3",
    "provider": "ckbuilders",
    "provider_request_timeout_seconds": 60,
    "reasoning_context": "all_turns",
    "reasoning_effort": "medium",
    "store": False,
    "requested_model": "gpt-probe-2026-02-11",
    "schema_version": "3",
    "temperature": 0,
    "usage_contract": "openai-responses-usage-v1",
}


def _seam_recorder(bin_dir: Path, name: str, log: Path) -> None:
    """A stand-in that records the call and fails, so nothing external can happen."""
    tool = bin_dir / name
    tool.write_text(
        "#!/bin/sh\n"
        f'printf "%s %s\\n" "{name}" "$*" >> "{log}"\n'
        "exit 1\n"
    )
    tool.chmod(0o755)


def _run(args: list[str], tmp_path: Path, env_extra: dict[str, str] | None = None):
    """Drive the real wrapper with every external seam faked, and report which ones it reached.

    Taking the project lock is reported as a seam too: it is shared machine state, and the guards
    under test all exist to fail before any of it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    runtime = tmp_path / "xdg"
    runtime.mkdir(exist_ok=True)
    log = tmp_path / "seams.log"
    for name in ("docker", "curl", "flock"):
        _seam_recorder(bin_dir, name, log)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "CKBBENCH_PYTHON": os.environ.get("CKBBENCH_PYTHON", os.sys.executable),
        "XDG_RUNTIME_DIR": str(runtime),
    }
    env.update(env_extra or {})
    proc = subprocess.run(
        ["/bin/bash", str(BENCH), *args], cwd=str(REPO), env=env,
        capture_output=True, text=True, timeout=180,
    )
    seams = log.read_text() if log.exists() else ""
    seams += "".join(f"lock {d.name}\n" for d in sorted(runtime.glob("ckbbench-*")))
    return proc, seams


@pytest.fixture
def candidate_profile(tmp_path: Path) -> Path:
    """A schema-valid file that is NOT the tracked profile's bytes."""
    path = tmp_path / "phase1-gpt.json"
    path.write_text(json.dumps(PROFILE_DOC, sort_keys=True, indent=2) + "\n")
    return path


def _env() -> dict[str, str]:
    return {"PYTHONPATH": f"{REPO}:{REPO / 'agent'}"}


def _forms(profile: Path) -> dict[str, list[str]]:
    """Every documented way to name a profile. `--` is optional, so it must change nothing."""
    return {
        "smoke": ["smoke", "--model-profile", str(profile)],
        "run": ["run", "--suite", "suites/ckb-v1", "--model-profile", str(profile)],
        # The exact separator form printed by `ckbbench run --help`.
        "run-separator": ["run", "--", "--suite", "suites/ckb-v1",
                          "--model-profile", str(profile), "--arms", "B,C", "--seeds", "1,2,3"],
        "run-equals": ["run", "--suite=suites/ckb-v1", f"--model-profile={profile}"],
    }


@pytest.mark.parametrize("command", ["smoke", "run", "run-separator", "run-equals"])
def test_an_unreviewed_profile_reaches_no_external_seam(command, tmp_path: Path, candidate_profile):
    """A schema-valid file that is not the tracked bytes must be refused before anything runs."""
    proc, seams = _run(_forms(candidate_profile)[command], tmp_path, _env())
    assert proc.returncode != 0
    assert "not the reviewed phase-one model profile" in proc.stdout + proc.stderr
    assert seams == "", f"an external seam was reached: {seams!r}"


@pytest.mark.parametrize("alias", ["CKBBENCH_LLM_API_BASE", "BENCH_API_BASE"])
@pytest.mark.parametrize("command", ["smoke", "run", "run-separator", "run-equals"])
def test_either_conflicting_endpoint_alias_is_refused_before_any_seam(
    alias, command, tmp_path: Path, candidate_profile
):
    """Precedence must not let a matching alias mask a conflicting one until after preflight."""
    env = {**_env(), alias: "https://elsewhere.example/v1"}
    proc, seams = _run(_forms(candidate_profile)[command], tmp_path, env)
    assert proc.returncode != 0
    # Whichever guard speaks first, the wrapper refuses before touching anything external.
    assert seams == "", f"an external seam was reached: {seams!r}"


def test_smoke_requires_the_reviewed_profile(tmp_path: Path):
    proc, seams = _run(["smoke"], tmp_path)
    assert proc.returncode != 0
    assert "requires --model-profile" in proc.stdout + proc.stderr
    assert seams == ""


def test_smoke_cannot_spend_a_real_cell_on_an_arbitrary_model(tmp_path: Path):
    """Smoke is hardwired to the phase-one suite and has no dry run, so --model is never valid."""
    proc, seams = _run(["smoke", "--model", "gpt-anything"], tmp_path, _env())
    assert proc.returncode != 0
    assert "cannot run the phase-one suite" in proc.stdout + proc.stderr
    assert seams == "", f"an external seam was reached: {seams!r}"


@pytest.mark.parametrize("extra", [
    [],
    ["--arms", "B"],
    ["--seeds", "1"],
])
@pytest.mark.parametrize("separator", [[], ["--"]])
def test_an_arbitrary_non_dry_phase_one_model_reaches_no_seam(separator, extra, tmp_path: Path):
    args = ["run", *separator, "--suite", "suites/ckb-v1", "--models", "gpt-anything", *extra]
    proc, seams = _run(args, tmp_path, _env())
    assert proc.returncode != 0
    assert "cannot execute the phase-one suite" in proc.stdout + proc.stderr
    assert seams == "", f"an external seam was reached: {seams!r}"


# Every documented way to name no usable profile for a real phase-one run.
UNPROFILED_FORMS = {
    "no-model-input": ["--suite", "suites/ckb-v1", "--arms", "B"],
    "separator": ["--", "--suite", "suites/ckb-v1", "--arms", "B", "--seeds", "1"],
    "equals-suite": ["--suite=suites/ckb-v1", "--arms=B"],
    "empty-profile": ["--suite", "suites/ckb-v1", "--model-profile="],
    "empty-profile-separator": ["--", "--suite=suites/ckb-v1", "--model-profile="],
}


@pytest.mark.parametrize("form", sorted(UNPROFILED_FORMS))
def test_a_non_dry_phase_one_run_without_a_profile_reaches_no_seam(form, tmp_path: Path):
    """No model input at all must fail as closed as the wrong one; Python's refusal is too late."""
    proc, seams = _run(["run", *UNPROFILED_FORMS[form]], tmp_path, _env())
    assert proc.returncode != 0
    assert "needs --model-profile" in proc.stdout + proc.stderr
    assert seams == "", f"an external seam was reached: {seams!r}"


DRY_FORMS = {
    "models": ["--suite", "suites/ckb-v1", "--models", "gpt-anything", "--dry-run"],
    "separator": ["--", "--suite", "suites/ckb-v1", "--models", "gpt-anything", "--dry-run"],
    "equals": ["--suite=suites/ckb-v1", "--models=gpt-anything", "--dry-run"],
}


@pytest.mark.parametrize("form", sorted(DRY_FORMS))
def test_a_development_dry_run_stays_local(form, tmp_path: Path):
    """A dry run starts no cell, so it must take no lock and contact nothing external."""
    proc, seams = _run(["run", *DRY_FORMS[form]], tmp_path, _env())
    assert "cannot execute the phase-one suite" not in proc.stdout + proc.stderr
    assert "needs --model-profile" not in proc.stdout + proc.stderr
    assert seams == "", f"an external seam was reached: {seams!r}"


def test_a_dry_run_still_refuses_an_unreviewed_profile(tmp_path: Path, candidate_profile):
    """Local does not mean unchecked: a dry run must not bless a profile a real run would refuse."""
    args = ["run", "--suite", "suites/ckb-v1", "--model-profile", str(candidate_profile),
            "--dry-run"]
    proc, seams = _run(args, tmp_path, _env())
    assert proc.returncode != 0
    assert "not the reviewed phase-one model profile" in proc.stdout + proc.stderr
    assert seams == ""


def test_smoke_refuses_both_inputs_together(tmp_path: Path, candidate_profile):
    proc, seams = _run(
        ["smoke", "--model-profile", str(candidate_profile), "--model", "other"],
        tmp_path, _env(),
    )
    assert proc.returncode != 0
    assert "mutually exclusive" in proc.stdout + proc.stderr
    assert seams == ""


def test_the_operator_help_names_the_profile_path_not_an_arbitrary_model():
    help_text = subprocess.run(
        ["/bin/bash", str(BENCH), "run", "--help"], cwd=str(REPO),
        capture_output=True, text=True, timeout=60,
    ).stdout
    assert "--model-profile configs/phase1-gpt.json" in help_text
    assert "development/dry-run only" in help_text.lower()


# --- two model sources is invalid for every run, and must fail before anything shared -------------

CONFLICT_FORMS = {
    "direct": ["--suite", "suites/ckb-v1", "--model-profile", "{p}", "--models", "gpt-other",
               "--arms", "B"],
    "separator": ["--", "--suite", "suites/ckb-v1", "--model-profile", "{p}",
                  "--models", "gpt-other", "--arms", "B"],
    "equals": ["--suite=suites/ckb-v1", "--model-profile={p}", "--models=gpt-other"],
    "reversed-order": ["--suite", "suites/ckb-v1", "--models", "gpt-other",
                       "--model-profile", "{p}"],
}


@pytest.mark.parametrize("form", sorted(CONFLICT_FORMS))
@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_a_profile_and_a_model_list_together_reach_no_seam(
    form, dry, tmp_path: Path, candidate_profile
):
    """The matrix layer rejects the combination, but only after the wrapper could preflight."""
    args = [a.format(p=candidate_profile) for a in CONFLICT_FORMS[form]]
    proc, seams = _run(["run", *args, *dry], tmp_path, _env())
    assert proc.returncode != 0
    assert "mutually exclusive" in proc.stdout + proc.stderr
    assert seams == "", f"an external seam was reached: {seams!r}"
