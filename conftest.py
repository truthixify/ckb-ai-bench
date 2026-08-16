"""Repository-wide test guards.

`build_cell_allowlist()` and `compose_env_for_arm()` write into `containers/proxy` in production.
A test that writes there pollutes the working tree and makes concurrent test processes contend for
the same paths, so the suite proves it creates none.

The guard is deliberately NON-DESTRUCTIVE: absence at fixture setup is not ownership. Another
pytest process legitimately creating a file during this test must survive, so a violation is
reported and the offending path named, never unlinked.

The second guard makes the suite's offline boundary fail loud: the MCP controller, the RPC client
and the model client all speak over sockets, so a test that reaches one of them for real must abort
rather than quietly contact a live service. It covers Python's ``socket.connect``/``connect_ex``
only -- not a subprocess, a DNS lookup, or a non-Python mechanism.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest



# The vendored agent fork loads a GLOBAL dotenv at import time (`minisweagent/__init__.py`). On a
# developer machine that file holds real endpoints and credentials, and importing the fork would read
# them into the test process for the rest of the session.
#
# This runs at conftest import, BEFORE collection imports anything, and overrides unconditionally: a
# value the caller already exported must not win, or a test outcome would depend on whose shell
# launched it. The directory is unique per session, so concurrent runs cannot share or inherit state.
_AGENT_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="ckbbench-agent-config-"))

# Endpoint aliases the harness reads. Cleared before collection so no developer shell or global
# dotenv can decide a test outcome, and restored per test so one test cannot leak into the next.
ENDPOINT_VARS = ("CKBBENCH_LLM_API_BASE", "BENCH_API_BASE",
                 "CKBBENCH_LLM_API_KEY", "BENCH_API_KEY")
AGENT_VARS = ("MSWEA_GLOBAL_CONFIG_DIR", "MSWEA_SILENT_STARTUP")
MANAGED_VARS = AGENT_VARS + ENDPOINT_VARS

# Snapshotted BEFORE anything is changed, and covering every name this session touches. An
# in-process `pytest.main()` returns to a caller that still owns its environment, so restoring only
# the two overridden names would silently delete the caller's four provider values. `None` means
# absent, which is not the same as an empty string.
_REPLACED_ENVIRONMENT = {name: os.environ.get(name) for name in MANAGED_VARS}

os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(_AGENT_CONFIG_DIR)
os.environ["MSWEA_SILENT_STARTUP"] = "1"
for _name in ENDPOINT_VARS:
    os.environ.pop(_name, None)


def pytest_sessionfinish(session, exitstatus):
    """Remove only the directory this session created, and put back exactly what it replaced."""
    del session, exitstatus
    shutil.rmtree(_AGENT_CONFIG_DIR, ignore_errors=True)
    for name, value in _REPLACED_ENVIRONMENT.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def _deterministic_endpoint_environment():
    """No test inherits or leaves an endpoint variable, whatever imported the agent fork."""
    before = {name: os.environ.get(name) for name in ENDPOINT_VARS}
    for name in ENDPOINT_VARS:
        os.environ.pop(name, None)
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def proxy_dir() -> Path:
    """The watched directory. A seam so the guard's own tests never touch the real one."""
    return Path(__file__).resolve().parent / "containers" / "proxy"


def allowlists(directory: Path | None = None) -> set[Path]:
    return set((directory or proxy_dir()).glob("allowlist.*.built"))


def violation_message(created: set[Path]) -> str:
    return (
        "an allowlist appeared in containers/proxy during this test. If this test wrote it, pass "
        "proxy_dir=/out_dir=tmp_path instead. The file is NOT removed: it may belong to a "
        f"concurrent process. Paths: {sorted(p.name for p in created)}"
    )


@pytest.fixture(autouse=True)
def _no_new_repository_allowlists():
    before = allowlists()
    yield
    created = allowlists() - before
    assert not created, violation_message(created)


# Exact name, not a prefix: `localhost.attacker` and `127.example` are off-host.
_LOCAL_NAMES = frozenset({"localhost"})


def _is_loopback(address: object) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    if not isinstance(host, str):
        return False
    if host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Any off-host socket connection during a test is a failure, not a slow test.

    Unix sockets and loopback stay available so a temp-file or subprocess helper is unaffected.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def deny(name):
        def guard(self, address, *args, **kwargs):
            if self.family == socket.AF_UNIX or _is_loopback(address):
                return (real_connect if name == "connect" else real_connect_ex)(
                    self, address, *args, **kwargs
                )
            raise AssertionError(
                f"a test attempted an outbound {name}() to {address!r}; this suite is offline"
            )

        return guard

    monkeypatch.setattr(socket.socket, "connect", deny("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", deny("connect_ex"))


@pytest.mark.parametrize("address,local", [
    ("localhost", True),
    ("127.0.0.1", True),
    ("127.1.2.3", True),
    ("::1", True),
    ("[::1]", True),
    ("0:0:0:0:0:0:0:1", True),
    ("localhost.attacker", False),
    ("localhost.", False),
    ("LOCALHOST", False),
    ("127.example", False),
    ("127.0.0.1.attacker", False),
    ("::10", False),
    ("0.0.0.0", False),
    ("10.0.0.1", False),
    ("93.184.216.34", False),
    ("", False),
    (None, False),
    (7, False),
])
def test_loopback_detection_is_exact(address, local):
    """Prefix matching once treated `localhost.attacker` and `::10` as local."""
    assert _is_loopback(address) is local
    assert _is_loopback((address, 443)) is local
