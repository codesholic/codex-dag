#!/bin/sh
set -eu
NATIVE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUTPUT=${1:-"$NATIVE_DIR/../build/native/Codex DAG"}
mkdir -p "$(dirname "$OUTPUT")"
xcrun swiftc -O -target arm64-apple-macosx15.0 -framework AppKit -framework WebKit -framework ServiceManagement "$NATIVE_DIR"/Sources/*.swift -o "$OUTPUT"
printf '%s\n' "$OUTPUT"
