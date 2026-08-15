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
import socket
from pathlib import Path

import pytest


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
