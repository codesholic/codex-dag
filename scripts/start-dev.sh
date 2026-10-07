#!/bin/sh
set -eu
CODEX_DAG_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CODEX_DAG_PYTHON=${CODEX_DAG_PYTHON:-python3}
CODEX_DAG_DATA_DIR=${CODEX_DAG_DATA_DIR:-"$CODEX_DAG_PROJECT_DIR/.local/dev-data"}
CODEX_DAG_SELECTED_PORT=${1:-${CODEX_DAG_DEV_PORT:-58957}}
# Development keeps a stable loopback URL and requires no bootstrap token.
if [ -n "${CODEX_DAG_SOURCE_DIR:-}" ]; then
  "$CODEX_DAG_PYTHON" "$CODEX_DAG_PROJECT_DIR/scripts/bootstrap-dev.py" \
    --data-dir "$CODEX_DAG_DATA_DIR" --codex-dir "$CODEX_DAG_SOURCE_DIR" --shared-journal
  exec "$CODEX_DAG_PYTHON" "$CODEX_DAG_PROJECT_DIR/src/monitor/monitor.py" \
    --data-dir "$CODEX_DAG_DATA_DIR" --codex-dir "$CODEX_DAG_SOURCE_DIR" --shared-journal \
    serve --port "$CODEX_DAG_SELECTED_PORT" --dev-no-auth
fi
"$CODEX_DAG_PYTHON" "$CODEX_DAG_PROJECT_DIR/scripts/bootstrap-dev.py" --data-dir "$CODEX_DAG_DATA_DIR" --shared-journal
exec "$CODEX_DAG_PYTHON" "$CODEX_DAG_PROJECT_DIR/src/monitor/monitor.py" \
  --data-dir "$CODEX_DAG_DATA_DIR" --shared-journal serve --port "$CODEX_DAG_SELECTED_PORT" --dev-no-auth
