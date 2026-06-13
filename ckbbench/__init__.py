"""CKB AI Bench harness (v1).

A versioned benchmark that measures whether the CKB AI MCP server measurably improves an AI
coding agent at Nervos CKB development. The headline result is the ``C - B`` delta on the
condition ladder (see docs/RECOMMENDATION.md and docs/adr/).

Package layout (filled in across build phases):

    ckbbench/
      config.py        run-time constants + live-infra references (single source of truth)
      suite/           Suite model, registry, composer, freeze, run-params (Phase 1)
      verify/          per-task verifier framework: on-chain + code task (Phase 2)
      run/             the run orchestrator: arms, preflight, agent driver, metrics (Phase 4)
      matrix/          matrix driver + ladder metrics + reporting (Phase 5)

Containers (agent image, verifier image, devnet sidecar, egress proxy) live under
``containers/`` and the v1 task registry under ``suites/`` at the repo root.
"""

from __future__ import annotations

__version__ = "1.0.0"
