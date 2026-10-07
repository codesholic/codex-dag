#!/bin/sh
set -eu
CODEX_DAG_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CODEX_DAG_BUILD_PYTHON=${CODEX_DAG_BUILD_PYTHON:-"$CODEX_DAG_ROOT/.local/build-venv/bin/python"}
if [ ! -x "$CODEX_DAG_BUILD_PYTHON" ]; then
  echo 'Prepare the isolated PSF build runtime: python3 packaging/macos/prepare-runtime.py' >&2
  exit 1
fi
exec "$CODEX_DAG_BUILD_PYTHON" "$CODEX_DAG_ROOT/packaging/macos/build.py" "$@"
