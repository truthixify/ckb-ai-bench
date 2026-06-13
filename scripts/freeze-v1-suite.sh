#!/usr/bin/env bash
# Regenerate suites/ckb-v1/suite.freeze.json from the live registry (ADR-0008).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${CKBBENCH_PYTHON:-agent/.venv/bin/python}"
exec "$PY" -c "
from pathlib import Path
from ckbbench.suite.registry import load_suite
from ckbbench.suite.freeze import freeze, write_freeze
root = Path('suites/ckb-v1')
path = write_freeze(freeze(load_suite(root), root), root)
print(f'Wrote {path}')
"