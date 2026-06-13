"""Make the agent fork importable for run-layer tests that construct the real agent.

The harness package (`ckbbench`) imports cleanly with NO agent fork on the path: every
agent-fork import (minisweagent / ckb_agent / ckb_mcp / litellm) is lazy, inside the
function that needs it at run time (see orchestrate.run_cell and agent_factory). That keeps
`import ckbbench` and most unit tests free of the fork.

`test_agent_factory.py` is the exception: it exercises the integration seam by constructing a
real `CkbMcpAgent`, so it needs the fork (which lives in the un-packaged `agent/` dir) on
`sys.path`. The fork is a genuine run-time dependency, so putting it on the path for these tests
is faithful, not a workaround. Scoped to this directory's tests via conftest discovery.
"""

from __future__ import annotations

import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
if _AGENT_DIR.is_dir():  # pragma: no branch - the fork dir always exists in-repo; the guard only
    sys.path.insert(0, str(_AGENT_DIR))  # protects a checkout missing agent/ (the false branch).
