#!/usr/bin/env bash
# Production matrix launch wrapper.
#
# Runs the full benchmark grid via ckbbench.matrix.launch. Requires the agent venv
# (agent/.venv) with the harness editable-installed (see scripts/test.sh bootstrap).
#
# Environment (all optional; see ckbbench/config.py and .env.example):
#   CKBBENCH_PYTHON          Python interpreter (default: agent/.venv/bin/python)
#   CKBBENCH_LLM_API_BASE    LLM proxy base URL. Under --model-profile the profile's api_base
#                            wins and a conflicting exported value is refused.
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
#   scripts/run-matrix.sh --suite suites/ckb-v1 --model-profile configs/phase1-gpt.json
#   scripts/run-matrix.sh --suite suites/ckb-v1 --model-profile configs/phase1-gpt.json --keep
#
# --models is development/dry-run only. A real run of the phase-one suite is refused without
# --model-profile, so every accepted row names the same reviewed model:
#   scripts/run-matrix.sh --suite suites/ckb-v1 --models m1 --dry-run
#
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:+${PYTHONPATH}:}agent"
PY="${CKBBENCH_PYTHON:-agent/.venv/bin/python}"
exec "$PY" -m ckbbench.matrix.launch "$@"