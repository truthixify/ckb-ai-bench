"""`./bench` must contact each external readiness surface exactly once per invocation.

An earlier preflight called `cmd_status` and then repeated the same required subset, so one
`./bench run` made two `GET /models` requests and four MCP POSTs. It then failed because the
readiness request carried no credential.

These tests drive the real `scripts/ckbbench` control flow with every external command replaced by a
non-networking fake, so the counts and the fail-closed ordering are observed rather than asserted in
prose. Nothing here can reach a network: `docker`, `curl` and the Python interpreter are all stubs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "scripts" / "ckbbench"
KEY_CANARY = "sk-synthetic-fixture-canary"
SYNTHETIC_BASE = "https://proxy.example/v1"

# One stub interpreter for every `$PY ...` call the script makes. It records argv so each readiness
# surface can be counted, and answers each call the way a healthy environment would.
FAKE_PYTHON = r"""#!/bin/sh
printf '%s\n' "$*" >> "@LOG@"
# Which credential survived dotenv loading. Synthetic by construction: this fixture's .env is the
# only one on the isolated repository, so recording it cannot disclose a real value.
printf 'SELECTED_KEY=%s\n' "${CKBBENCH_LLM_API_KEY:-<unset>}" >> "@ENVLOG@"
printf 'SELECTED_BASE=%s\n' "${CKBBENCH_LLM_API_BASE:-<unset>}" >> "@ENVLOG@"
case "$*" in
  *ckbbench.run.llm_readiness*)
    if [ -n "$LLM_FAIL" ]; then echo "authentication rejected; check CKBBENCH_LLM_API_KEY (HTTP 401)"; exit 1; fi
    echo "https://proxy.example/v1 ready (HTTP 200)"; exit 0 ;;
  *load_reviewed_profile*)
    # bind_model_profile asks the harness for the reviewed endpoint before any external seam.
    if [ -n "$PROFILE_REFUSE" ]; then exit 1; fi
    echo "$CKBBENCH_LLM_API_BASE"; exit 0 ;;
  *is_phase_one_suite*) exit 0 ;;
  *safe_api_base*) echo "https://proxy.example/v1"; exit 0 ;;
  *-c*) exit 0 ;;
  -)  # mcp_preflight feeds its program on stdin
    cat > /dev/null
    if [ -n "$MCP_FAIL" ]; then echo "error: mcp down" >&2; exit 1; fi
    echo "ok version=1.6.13 tools=51"; exit 0 ;;
  *) exit 0 ;;
esac
"""

# `docker`/`curl` answer as a healthy local stack without touching anything real.
FAKE_DOCKER = r"""#!/bin/sh
printf 'docker %s\n' "$*" >> "@LOG@"
case "$1" in
  inspect) echo 'true'; exit 0 ;;
  volume) exit 1 ;;
  *) exit 0 ;;
esac
"""

FAKE_CURL = r"""#!/bin/sh
printf 'curl %s\n' "$*" >> "@LOG@"
case "$*" in
  *get_blockchain_info*) echo '{"result":{"chain":"ckb_dev"}}'; exit 0 ;;
  *get_tip_block_number*) echo "{\"result\":\"0x$(date +%s)\"}"; exit 0 ;;
  *) echo '{"result":{}}'; exit 0 ;;
esac
"""

# The wrapper sources `$REPO/.env` with `set -a`, so a test that merely exports synthetic values
# has them overwritten by the developer's real file and then asserts against the wrong string. Every
# test here runs an isolated repository whose only `.env` is synthetic.
LINKED = ("ckbbench", "configs", "suites", "containers", "agent")
COPIED = ("scripts",)


def _isolated_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in COPIED:
        shutil.copytree(REPO / name, repo / name)
    for name in LINKED:
        (repo / name).symlink_to(REPO / name)
    (repo / ".env").write_text(
        f"CKBBENCH_LLM_API_BASE={SYNTHETIC_BASE}\n"
        f"CKBBENCH_LLM_API_KEY={KEY_CANARY}\n"
    )
    return repo


def _stub(path: Path, body: str, log: Path, envlog: Path) -> None:
    path.write_text(body.replace("@LOG@", str(log)).replace("@ENVLOG@", str(envlog)))
    path.chmod(0o755)


@pytest.fixture
def bench(tmp_path: Path):
    """Run `./bench <args>` in an isolated repository with every external command stubbed."""
    repo = _isolated_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    envlog = tmp_path / "env.log"
    py = tmp_path / "fake-python"
    _stub(py, FAKE_PYTHON, log, envlog)
    _stub(bin_dir / "docker", FAKE_DOCKER, log, envlog)
    _stub(bin_dir / "curl", FAKE_CURL, log, envlog)

    def run(args: list[str], env_extra: dict[str, str] | None = None,
            dotenv_base: str | None = None, xtrace: bool = False):
        # The wrapper sources `.env` with `set -a` BEFORE resolving LLM_BASE, so a configured base
        # under test must come from that file, not from the caller's environment.
        if dotenv_base is not None:
            (repo / ".env").write_text(
                f"CKBBENCH_LLM_API_BASE={dotenv_base}\n"
                f"CKBBENCH_LLM_API_KEY={KEY_CANARY}\n"
            )
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path),
            "XDG_RUNTIME_DIR": str(tmp_path),
            "CKBBENCH_PYTHON": str(py),
        }
        env.update(env_extra or {})
        bash = ["/bin/bash", "-x"] if xtrace else ["/bin/bash"]
        proc = subprocess.run([*bash, str(repo / "scripts" / "ckbbench"), *args],
                              cwd=str(repo), env=env, capture_output=True, text=True, timeout=180)
        calls = log.read_text().splitlines() if log.exists() else []
        selected = envlog.read_text().splitlines() if envlog.exists() else []
        return proc, calls, selected

    return run


def _llm_checks(calls: list[str]) -> int:
    return sum(1 for c in calls if "ckbbench.run.llm_readiness" in c)


def _mcp_checks(calls: list[str]) -> int:
    # `mcp_preflight` is the only caller that feeds its program on stdin.
    return sum(1 for c in calls if c.strip() == "-")


# --- one readiness pass ---------------------------------------------------------------------------

def test_status_makes_one_llm_and_one_mcp_check(bench):
    proc, calls, _sel = bench(["status"])
    assert _llm_checks(calls) == 1, f"expected one LLM readiness request, saw {_llm_checks(calls)}"
    assert _mcp_checks(calls) == 1, f"expected one MCP preflight, saw {_mcp_checks(calls)}"
    assert proc.returncode == 0
    assert "PASS  LLM proxy" in proc.stdout


def test_status_skips_mcp_when_disabled_and_still_checks_the_endpoint_once(bench):
    proc, calls, _sel = bench(["status"], {"CKBBENCH_MCP_DISABLE": "1"})
    assert _llm_checks(calls) == 1
    assert _mcp_checks(calls) == 0
    assert "SKIP  MCP" in proc.stdout


@pytest.mark.parametrize("arms,expected_mcp", [
    ("B", 0),      # B needs no MCP surface
    ("B,C", 1),
    ("C,D", 1),
])
def test_a_live_preflight_checks_each_surface_once(arms, expected_mcp, bench, tmp_path):
    """`preflight_live` used to call cmd_status and then repeat the whole required subset."""
    proc, calls, _sel = bench(["run", "--docker", "--", "--suite", "suites/ckb-v1",
                         "--model-profile", "configs/phase1-gpt.json", "--arms", arms,
                         "--seeds", "1"])
    assert _llm_checks(calls) == 1, f"expected one LLM readiness request, saw {_llm_checks(calls)}"
    assert _mcp_checks(calls) == expected_mcp
    assert "== preflight ==" in proc.stdout
    del proc


def test_a_failed_readiness_check_is_never_repeated_in_one_invocation(bench):
    proc, calls, _sel = bench(["run", "--docker", "--", "--suite", "suites/ckb-v1",
                         "--model-profile", "configs/phase1-gpt.json", "--arms", "B,C",
                         "--seeds", "1"], {"LLM_FAIL": "1"})
    assert _llm_checks(calls) == 1, "a failed readiness request must not be retried"
    assert proc.returncode != 0
    assert "preflight failed" in proc.stdout + proc.stderr


# --- fail closed, before anything can spend a model call -------------------------------------------

def test_an_llm_failure_stops_before_the_matrix_and_any_model_call(bench):
    proc, calls, _sel = bench(["run", "--docker", "--", "--suite", "suites/ckb-v1",
                         "--model-profile", "configs/phase1-gpt.json", "--arms", "B,C",
                         "--seeds", "1"], {"LLM_FAIL": "1"})
    assert proc.returncode != 0
    assert not any("ckbbench.matrix.launch" in c for c in calls), "the matrix must not be reached"
    assert not any("run-matrix" in c for c in calls)
    # The operator still learns which failure class it was.
    assert "authentication rejected" in proc.stdout


def test_an_mcp_failure_still_stops_the_run(bench):
    proc, _calls, _sel = bench(["run", "--docker", "--", "--suite", "suites/ckb-v1",
                          "--model-profile", "configs/phase1-gpt.json", "--arms", "B,C",
                          "--seeds", "1"], {"MCP_FAIL": "1"})
    assert proc.returncode != 0
    assert "preflight failed" in proc.stdout + proc.stderr


def test_the_status_line_classifies_the_failure_for_the_operator(bench):
    proc, _calls, _sel = bench(["status"], {"LLM_FAIL": "1"})
    assert proc.returncode != 0
    assert "FAIL  LLM proxy" in proc.stdout
    assert "authentication rejected" in proc.stdout


# --- the credential never leaves the environment ---------------------------------------------------

def test_the_fixture_isolates_the_repository_dotenv(bench):
    """The wrapper sources `$REPO/.env` with `set -a`; without isolation it wins over the fixture."""
    _proc, _calls, selected = bench(["status"])
    chose_synthetic_key = f"SELECTED_KEY={KEY_CANARY}" in selected
    chose_synthetic_base = f"SELECTED_BASE={SYNTHETIC_BASE}" in selected
    assert chose_synthetic_key, "the isolated .env did not supply the credential under test"
    assert chose_synthetic_base, "the isolated .env did not supply the endpoint under test"


def test_the_credential_never_appears_in_argv_or_output(bench):
    """`ps` shows argv to every local user, so the key must never be an argument."""
    proc, calls, _sel = bench(["status"])
    leaked_calls = sum(1 for c in calls if KEY_CANARY in c)
    leaked_output = KEY_CANARY in proc.stdout + proc.stderr
    assert leaked_calls == 0, "the credential reached a command line"
    assert not leaked_output, "the credential reached operator output"
    readiness = [c for c in calls if "ckbbench.run.llm_readiness" in c]
    assert readiness, "the readiness helper was never invoked"
    # Endpoint and credential both cross in the environment: the helper takes no arguments.
    takes_no_arguments = readiness[0].strip().endswith("ckbbench.run.llm_readiness")
    assert takes_no_arguments, "the readiness helper was given an argument"


@pytest.mark.parametrize("outcome", ["ready", "failing"])
def test_the_configured_base_never_reaches_argv_or_output(outcome, bench):
    """A configured base can itself carry userinfo or a token.

    Both branches matter: the failure path is where the raw base used to be printed, wrapping a
    sanitized refusal in the unsafe value it exists to suppress.
    """
    unsafe = "https://user:sk-synthetic-base-canary@proxy.example/v1"
    env = {"LLM_FAIL": "1"} if outcome == "failing" else {}
    proc, calls, _sel = bench(["status"], env, dotenv_base=unsafe)
    leaked_calls = sum(1 for c in calls if "sk-synthetic-base-canary" in c)
    leaked_output = "sk-synthetic-base-canary" in proc.stdout + proc.stderr
    assert leaked_calls == 0, "the configured base reached a command line"
    assert not leaked_output, "the configured base reached operator output"


def test_no_operator_output_line_interpolates_the_raw_configured_base():
    """The raw base may be assigned into a child environment, never printed.

    `setup` cannot reach its summary inside the isolated fixture, so this checks the invariant at
    the source instead of asserting a line that never runs.
    """
    text = (REPO / "scripts" / "ckbbench").read_text()
    offenders = [
        line.strip() for line in text.splitlines()
        if "$LLM_BASE" in line
        and ("info " in line or "die " in line or "echo " in line)
    ]
    assert offenders == [], f"these print the raw configured base: {offenders}"

    # It may still cross into a child process's environment, which is not argv and not output.
    assignments = [line.strip() for line in text.splitlines()
                   if 'CKBBENCH_LLM_API_BASE="$LLM_BASE"' in line]
    assert assignments, "the base must still reach the harness through the environment"


def test_no_curl_invocation_carries_an_authorization_argument(bench):
    _proc, calls, _sel = bench(["status"])
    offending = sum(1 for c in calls
                    if c.startswith("curl ") and ("Authorization" in c or KEY_CANARY in c))
    assert offending == 0, "a curl invocation carried an authorization argument"


def test_the_credential_is_absent_when_a_check_fails(bench):
    proc, calls, _sel = bench(["status"], {"LLM_FAIL": "1"})
    leaked = KEY_CANARY in proc.stdout + proc.stderr or any(KEY_CANARY in c for c in calls)
    assert not leaked, "the credential surfaced on a failure path"


# --- unrelated gates keep their behavior -----------------------------------------------------------

def test_the_non_llm_gates_still_run_and_still_fail_closed(bench, tmp_path):
    """Removing duplication must not weaken venv, container, DevNet or MCP checks."""
    proc, _calls, _sel = bench(["status"])
    for expected in ("venv", "container ckbbench-proxy", "container ckbbench-devnet-node",
                     "container ckbbench-devnet-miner", "devnet RPC", "devnet chain is ckb_dev",
                     "devnet miner advancing", "MCP"):
        assert expected in proc.stdout, f"{expected!r} disappeared from the readiness report"
    del tmp_path


def test_a_dry_run_still_contacts_nothing(bench):
    proc, calls, _sel = bench(["run", "--docker", "--", "--suite", "suites/ckb-v1",
                         "--model-profile", "configs/phase1-gpt.json", "--arms", "B,C",
                         "--seeds", "1", "--dry-run"])
    assert _llm_checks(calls) == 0, "a dry run must make no readiness request"
    assert _mcp_checks(calls) == 0
    assert not any(c.startswith("docker ") for c in calls), "a dry run must not touch Docker"
    del proc


def test_profile_binding_still_precedes_every_external_seam(bench, tmp_path):
    """An unreviewed profile must be refused before the lock, Docker, or any readiness request."""
    candidate = tmp_path / "phase1-gpt.json"
    candidate.write_text((REPO / "configs" / "phase1-gpt.json").read_text().replace(
        "gpt-5.6-sol", "gpt-other"))
    proc, calls, _sel = bench(["run", "--docker", "--", "--suite", "suites/ckb-v1",
                         "--model-profile", str(candidate), "--arms", "B,C", "--seeds", "1"],
                        {"PROFILE_REFUSE": "1"})
    assert proc.returncode != 0
    assert _llm_checks(calls) == 0 and _mcp_checks(calls) == 0
    assert not any(c.startswith("docker ") for c in calls)


def test_the_readiness_helper_is_reachable_and_takes_no_arguments():
    """The script invokes `-m ckbbench.run.llm_readiness` with no arguments; that must hold."""
    proc = subprocess.run(
        [os.environ.get("CKBBENCH_PYTHON", "python3"), "-m", "ckbbench.run.llm_readiness",
         "--api-key", "sk-synthetic-argv-canary"],
        cwd=str(REPO), capture_output=True, text=True, timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO),
             "HOME": os.environ.get("HOME", "")},
    )
    # Refused, and the rejected argument is never echoed: argparse's default error would print it.
    assert proc.returncode == 2
    echoed = "sk-synthetic-argv-canary" in proc.stdout + proc.stderr
    assert not echoed, "the rejected argument was echoed back to the operator"
    assert "takes no arguments" in proc.stderr


# --- shell tracing must not become a disclosure channel --------------------------------------------

BASE_CANARY = "sk-synthetic-xtrace-base-canary"


@pytest.mark.parametrize("command,expected", [
    (["status"], "PASS  LLM proxy"),
    (["help"], "operator CLI for CKB AI Bench"),
])
def test_bash_xtrace_discloses_neither_the_key_nor_the_configured_base(command, expected, bench):
    """`bash -x` traces `.env` assignments as `source` runs, before any later redaction can help.

    The operator command must still work: disabling tracing is a safety measure, not a refusal.
    """
    unsafe = f"https://user:{BASE_CANARY}@proxy.example/v1"
    proc, calls, _sel = bench(command, dotenv_base=unsafe, xtrace=True)

    rendered = proc.stdout + proc.stderr
    key_leaked = KEY_CANARY in rendered or any(KEY_CANARY in c for c in calls)
    base_leaked = BASE_CANARY in rendered or any(BASE_CANARY in c for c in calls)
    assert not key_leaked, "the credential appeared under shell tracing"
    assert not base_leaked, "the configured base appeared under shell tracing"
    # And the command still did its job.
    assert expected in proc.stdout
    # The only line tracing may emit is the one that disables it.
    traced = [ln for ln in proc.stderr.splitlines() if ln.startswith("+")]
    assert traced in ([], ["+ set +x"]), f"unexpected trace output: {traced[:3]}"


def test_bash_xtrace_leaves_the_readiness_classification_intact(bench):
    """A failing check under tracing still reports its class, and still leaks nothing."""
    unsafe = f"https://user:{BASE_CANARY}@proxy.example/v1"
    proc, calls, _sel = bench(["status"], {"LLM_FAIL": "1"}, dotenv_base=unsafe, xtrace=True)

    assert proc.returncode != 0
    assert "FAIL  LLM proxy" in proc.stdout
    assert "authentication rejected" in proc.stdout
    rendered = proc.stdout + proc.stderr
    leaked = (KEY_CANARY in rendered or BASE_CANARY in rendered
              or any(KEY_CANARY in c or BASE_CANARY in c for c in calls))
    assert not leaked, "tracing disclosed a secret on the failure path"


def test_an_inherited_xtrace_shellopt_is_also_cleared(bench):
    """Some shells export SHELLOPTS, which re-enables tracing without a `-x` flag."""
    unsafe = f"https://user:{BASE_CANARY}@proxy.example/v1"
    proc, _calls, _sel = bench(["help"], {"SHELLOPTS": "xtrace"}, dotenv_base=unsafe)
    rendered = proc.stdout + proc.stderr
    leaked = KEY_CANARY in rendered or BASE_CANARY in rendered
    assert not leaked, "an inherited xtrace shell option disclosed a secret"
