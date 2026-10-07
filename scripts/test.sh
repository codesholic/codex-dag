#!/bin/sh
set -eu
CODEX_DAG_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CODEX_DAG_PYTHON=${CODEX_DAG_PYTHON:-python3}
cd "$CODEX_DAG_PROJECT_DIR"
PYTHONPATH="$CODEX_DAG_PROJECT_DIR/src/monitor${PYTHONPATH:+:$PYTHONPATH}" \
  "$CODEX_DAG_PYTHON" -m unittest discover -s tests -p 'test_*.py' -v
