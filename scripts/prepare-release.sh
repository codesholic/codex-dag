#!/bin/sh
# Copy the built installers to the stable asset names used by the README's
# "latest release" download links, and write SHA256SUMS.txt next to them.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
VERSION=$(python3 -c "import json;print(json.load(open('windows/package.json'))['version'])")
APP_VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "dist/Codex DAG.app/Contents/Info.plist")
if [ "$APP_VERSION" != "$VERSION" ]; then
  echo "Version mismatch: macOS app $APP_VERSION, Windows installer $VERSION" >&2
  exit 1
fi
OUT=dist/release
mkdir -p "$OUT"
cp "dist/Codex DAG.dmg" "$OUT/Codex-DAG-macOS-arm64.dmg"
cp "dist/Codex-DAG-$VERSION-Setup-x64.exe" "$OUT/Codex-DAG-Windows-x64-Setup.exe"
(cd "$OUT" && shasum -a 256 Codex-DAG-macOS-arm64.dmg Codex-DAG-Windows-x64-Setup.exe > SHA256SUMS.txt)
echo "Release v$VERSION assets in $OUT:"
cat "$OUT/SHA256SUMS.txt"
