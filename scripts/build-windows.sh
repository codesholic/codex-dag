#!/bin/sh
set -eu
CODEX_DAG_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$CODEX_DAG_PROJECT_DIR"
exec python3 packaging/windows/build.py "$@"
