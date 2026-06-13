"""CKB AI Bench harness (v1).

A versioned benchmark that measures whether the CKB AI MCP server measurably improves an AI
coding agent at Nervos CKB development. The headline result is the ``C - B`` delta on the
condition ladder.

Sources of truth: ``docs/RECOMMENDATION.md`` (architecture) and ``docs/adr/`` (decisions).
Package subdivisions (suite / verify / run / matrix) are documented in their own ``__init__``.
"""

from __future__ import annotations

from ckbbench._pkg_version import __version__  # single source; pyproject reads the same attr

__all__ = ["__version__"]
