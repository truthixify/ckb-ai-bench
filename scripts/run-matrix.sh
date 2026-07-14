#!/usr/bin/env bash
# Production matrix launch wrapper.
#
# Runs the full benchmark grid via ckbbench.matrix.launch. Requires the agent venv
# (agent/.venv) with the harness editable-installed (see scripts/test.sh bootstrap).
#
# Environment (all optional; see ckbbench/config.py and .env.example):
#   CKBBENCH_PYTHON          Python interpreter (default: agent/.venv/bin/python)
#   CKBBENCH_LLM_API_BASE    LLM proxy base URL
#   CKBBENCH_LLM_API_KEY     LLM proxy API key (no-auth placeholder by default)
#   CKBBENCH_MCP_URL         MCP server endpoint
#   CKBBENCH_MCP_VERSION     Pinned MCP server version for preflight
#   CKBBENCH_DEVNET_RPC      DevNet RPC URL (harness host view)
#   CKBBENCH_TESTNET_RPC     TestNet RPC URL
#   CKBBENCH_DOCKER          Set to 1 to wire docker runner + proxy violation check
#   CKBBENCH_ALLOWLIST_FILE  Per-arm egress allowlist for violation_check (docker path)
#   CKBBENCH_KEEP            Set to 1 to keep docker volumes/containers and host run dirs
#                            after a run (default: delete). Same as --keep.
#
# Usage:
#   scripts/run-matrix.sh --suite suites/ckb-v1 --models model1,model2
#   scripts/run-matrix.sh --suite suites/ckb-v1 --models m1 --dry-run
#   scripts/run-matrix.sh --suite suites/ckb-v1 --models m1 --keep
#
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:+${PYTHONPATH}:}agent"
PY="${CKBBENCH_PYTHON:-agent/.venv/bin/python}"
exec "$PY" -m ckbbench.matrix.launch "$@"