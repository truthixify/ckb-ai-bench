"""The test session must not read a developer's real mini-swe-agent config.

The vendored fork loads a GLOBAL dotenv at import time. If a run honours the caller's
`MSWEA_GLOBAL_CONFIG_DIR`, importing the fork pulls that file's endpoints and credentials into the
test process, and a unit test's outcome depends on whose machine ran it.

These tests launch pytest in a subprocess with hostile pre-existing values and a canary-bearing
global dotenv, which is the only way to observe conftest-import-time behaviour.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CANARY = "sk-live-hostile-global-config"

# Written by the probe test into the subprocess's own report; never asserted from this process.
PROBE = '''
import json, os, pathlib

def test_report_environment(tmp_path):
    import minisweagent  # noqa: F401  - importing the fork is the point

    report = {
        "config_dir": os.environ.get("MSWEA_GLOBAL_CONFIG_DIR"),
        "silent": os.environ.get("MSWEA_SILENT_STARTUP"),
        "endpoint": os.environ.get("CKBBENCH_LLM_API_BASE"),
        "key": os.environ.get("CKBBENCH_LLM_API_KEY"),
        "canary_in_environ": any("@CANARY@" in str(v) for v in os.environ.values()),
    }
    pathlib.Path("@REPORT@").write_text(json.dumps(report))
'''


def _run_probe(tmp_path: Path, order: str):
    """Run the probe under pytest with a hostile global config already exported."""
    hostile = tmp_path / "developer-config"
    hostile.mkdir()
    (hostile / ".env").write_text(
        f"CKBBENCH_LLM_API_BASE=https://developer.example/v1\n"
        f"CKBBENCH_LLM_API_KEY={CANARY}\n"
    )
    report = tmp_path / "report.json"
    test_file = tmp_path / f"test_probe_{order}.py"
    test_file.write_text(PROBE.replace("@CANARY@", CANARY).replace("@REPORT@", str(report)))

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": f"{REPO}:{REPO / 'agent'}",
        # Hostile pre-existing values: a correct conftest overrides both.
        "MSWEA_GLOBAL_CONFIG_DIR": str(hostile),
        "MSWEA_SILENT_STARTUP": "",
        "CKBBENCH_LLM_API_BASE": "https://leaked.example/v1",
        "CKBBENCH_LLM_API_KEY": CANARY,
    }
    selection = ["ckbbench/run/test_agent_factory.py::test_the_reviewed_profile_builds_only_the_responses_model",
                 str(test_file)]
    if order == "probe_first":
        selection.reverse()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *selection],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300,
    )
    return proc, (json.loads(report.read_text()) if report.exists() else None)


@pytest.mark.parametrize("order", ["probe_first", "probe_last"])
def test_a_hostile_global_config_is_never_read_in_either_order(order, tmp_path: Path):
    proc, report = _run_probe(tmp_path, order)

    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert report is not None, "the probe test did not run"
    # The caller's directory must lose, unconditionally.
    assert report["config_dir"] != str(tmp_path / "developer-config")
    assert "developer-config" not in (report["config_dir"] or "")
    assert report["silent"] == "1", "the fork's startup banner must be forced silent"
    # Neither the exported endpoint nor anything from that dotenv survives into a test.
    assert report["endpoint"] is None and report["key"] is None
    assert report["canary_in_environ"] is False
    assert CANARY not in proc.stdout + proc.stderr


def test_a_test_run_leaves_no_artifact_in_the_repository(tmp_path: Path):
    before = {p.name for p in REPO.iterdir()}
    proc, report = _run_probe(tmp_path, "probe_last")
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert report is not None, "the probe test did not run"
    after = {p.name for p in REPO.iterdir()}
    assert after - before == set(), f"a test run created {sorted(after - before)} in the repo"


# --- an in-process session must hand the caller's environment back untouched ----------------------
#
# `pytest.main()` returns into a process that still owns its environment. The reversals above are
# written from INSIDE pytest and so cannot see what the session leaves behind.

MANAGED = ("MSWEA_GLOBAL_CONFIG_DIR", "MSWEA_SILENT_STARTUP",
           "CKBBENCH_LLM_API_BASE", "BENCH_API_BASE",
           "CKBBENCH_LLM_API_KEY", "BENCH_API_KEY")

OUTER = '''
import json, os, pathlib, sys

# Caller state: distinctive values, including an empty string, which is NOT the same as absent.
CALLER = {
    "MSWEA_GLOBAL_CONFIG_DIR": "@HOSTILE@",
    "MSWEA_SILENT_STARTUP": "",
    "CKBBENCH_LLM_API_BASE": "https://caller.example/v1",
    "BENCH_API_BASE": "",
    "CKBBENCH_LLM_API_KEY": "@CANARY@",
    "BENCH_API_KEY": "@CANARY@-alias",
}
os.environ.update(CALLER)

import pytest


class _CaptureOwnedPath:
    """Records the EXACT directory root conftest.py installed for this session.

    Globbing the temp root instead would assert a global absence condition, and a concurrent
    session legitimately owns a matching directory.
    """

    def __init__(self):
        self.owned = None

    def pytest_sessionstart(self, session):
        del session
        self.owned = os.environ.get("MSWEA_GLOBAL_CONFIG_DIR")


capture = _CaptureOwnedPath()
rc = pytest.main(["-p", "no:cacheprovider", "-q", "@TARGET@"], plugins=[capture])

report = {
    "pytest_rc": int(rc),
    "restored": {name: os.environ.get(name) == expected for name, expected in CALLER.items()},
    "present": {name: name in os.environ for name in CALLER},
    "owned_path": capture.owned,
    "owned_path_exists_after": bool(capture.owned) and pathlib.Path(capture.owned).exists(),
    "config_dir_after": os.environ.get("MSWEA_GLOBAL_CONFIG_DIR"),
    "sentinel_survived": pathlib.Path("@SENTINEL@").is_dir() if "@SENTINEL@" else None,
}
pathlib.Path("@REPORT@").write_text(json.dumps(report))
'''


def _run_outer(tmp_path: Path, *, sentinel: str = ""):
    """Run a real in-process pytest session under a hostile caller environment."""
    hostile = tmp_path / "developer-config"
    hostile.mkdir(exist_ok=True)
    (hostile / ".env").write_text(f"CKBBENCH_LLM_API_KEY={CANARY}\n")
    report = tmp_path / "outer-report.json"
    script = tmp_path / "outer.py"
    target = ("ckbbench/run/test_agent_factory.py"
              "::test_the_reviewed_profile_builds_only_the_responses_model")
    script.write_text(
        OUTER.replace("@HOSTILE@", str(hostile))
        .replace("@CANARY@", CANARY)
        .replace("@REPORT@", str(report))
        .replace("@SENTINEL@", sentinel)
        .replace("@TARGET@", target)
    )
    proc = subprocess.run(
        [sys.executable, str(script)], cwd=str(REPO), timeout=300, capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
             "PYTHONPATH": f"{REPO}:{REPO / 'agent'}"},
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert report.exists(), proc.stdout[-2000:] + proc.stderr[-2000:]
    return proc, json.loads(report.read_text()), str(hostile)


def test_a_completed_in_process_session_restores_every_managed_variable(tmp_path: Path):
    """A session that deletes the caller's provider variables and never puts them back is a bug."""
    proc, observed, hostile = _run_outer(tmp_path)

    assert observed["pytest_rc"] == 0, "the inner session must pass"
    unrestored = [name for name, ok in observed["restored"].items() if not ok]
    assert unrestored == [], f"pytest did not restore {unrestored}"
    absent = [name for name, present in observed["present"].items() if not present]
    assert absent == [], f"pytest deleted {absent} from the caller's environment"
    # An empty string must come back as an empty string, not as absent.
    assert observed["restored"]["BENCH_API_BASE"] is True
    assert observed["restored"]["MSWEA_SILENT_STARTUP"] is True
    # The caller's own config directory is theirs again.
    assert observed["config_dir_after"] == hostile
    assert CANARY not in proc.stdout + proc.stderr


def test_a_session_removes_exactly_its_own_config_directory(tmp_path: Path):
    """The EXACT path the session owned, captured while it ran -- not a glob of the temp root."""
    _proc, observed, _hostile = _run_outer(tmp_path)

    owned = observed["owned_path"]
    assert owned, "the session's owned config directory was never observed"
    assert owned != str(tmp_path / "developer-config"), "the caller's directory must not be reused"
    assert "ckbbench-agent-config-" in owned
    assert observed["owned_path_exists_after"] is False, (
        "the session must remove the directory it created"
    )


def test_a_directory_owned_by_another_session_is_left_alone(tmp_path: Path):
    """Absence of OUR directory is the property; absence of every matching directory is not.

    A concurrent pytest session legitimately owns a same-prefix directory. Removing it, or failing
    because it exists, would break the repository's non-destructive ownership rule.
    """
    sentinel = Path(tempfile.mkdtemp(prefix="ckbbench-agent-config-isolation-sentinel-"))
    (sentinel / "owned-by-another-session").write_text("do not touch")
    try:
        _proc, observed, _hostile = _run_outer(tmp_path, sentinel=str(sentinel))

        assert observed["sentinel_survived"] is True, (
            "a directory belonging to another session must survive"
        )
        assert observed["owned_path_exists_after"] is False
        assert observed["owned_path"] != str(sentinel)
        assert (sentinel / "owned-by-another-session").read_text() == "do not touch"
    finally:
        # Only the directory THIS test created.
        shutil.rmtree(sentinel, ignore_errors=True)
