"""CKB AI Bench harness.

A versioned benchmark that measures whether the CKB AI MCP server measurably improves an AI
coding agent at Nervos CKB development. The headline result is the ``C - B`` delta on the
condition ladder.

Sources of truth: ``docs/RECOMMENDATION.md`` (architecture) and ``docs/adr/`` (decisions).
Package subdivisions (suite / verify / run / matrix) are documented in their own ``__init__``.
"""

from __future__ import annotations

import os

# `import litellm` fetches its model-cost map over HTTPS at import time unless this is set
# (litellm/litellm_core_utils/get_model_cost_map.py), so a benchmark launch made an unannounced
# request to a third-party host and pulled an unfrozen input from `main`. Every harness entry point
# imports this package before any model code, so this is the one place that reliably precedes it.
#
# Assigned, not `setdefault`: a stale ambient "False" would otherwise re-enable the remote fetch.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from ckbbench._pkg_version import __version__  # noqa: E402 - must follow the pin above

__all__ = ["__version__"]
