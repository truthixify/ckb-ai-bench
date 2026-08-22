#!/usr/bin/env bash
# Production matrix launch wrapper.
#
# Runs the full benchmark grid via ckbbench.matrix.launch. Requires the agent venv
# (agent/.venv) with the harness editable-installed (see scripts/test.sh bootstrap).
#
# Environment (all optional; see ckbbench/config.py and .env.example):
#   CKBBENCH_PYTHON          Python interpreter (default: agent/.venv/bin/python)
#   CKBBENCH_OPENROUTER_API_KEY / CKBBENCH_CKBUILDERS_API_KEY
#                            Provider-specific credentials selected by the model profile.
#   CKBBENCH_LLM_API_KEY     Development/legacy credential fallback.
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
#   scripts/run-matrix.sh --suite suites/ckb-v1 --profile openrouter-gpt-5.6-luna
#   scripts/run-matrix.sh --suite suites/ckb-v1 --profile ckbuilders-gpt-5.6-sol --keep
#
# --models is development/dry-run only. A real run of the phase-one suite is refused without
# --profile, so every accepted row names one reviewed provider/model configuration:
#   scripts/run-matrix.sh --suite suites/ckb-v1 --models m1 --dry-run
#
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:+${PYTHONPATH}:}agent"
PY="${CKBBENCH_PYTHON:-agent/.venv/bin/python}"
exec "$PY" -m ckbbench.matrix.launch "$@"
