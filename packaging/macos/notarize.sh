#!/bin/sh
# Credentials stay in a preconfigured Keychain profile, never in this repository.
set -eu
CODEX_DAG_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
CODEX_DAG_PROFILE=${1:?Usage: notarize.sh KEYCHAIN_PROFILE}
CODEX_DAG_APP="$CODEX_DAG_ROOT/dist/Codex DAG.app"
CODEX_DAG_DMG="$CODEX_DAG_ROOT/dist/Codex DAG.dmg"
codesign --verify --deep --strict "$CODEX_DAG_APP"
codesign -dv "$CODEX_DAG_APP" 2>&1 | /usr/bin/grep 'Authority=Developer ID Application:' >/dev/null
xcrun notarytool submit "$CODEX_DAG_DMG" --keychain-profile "$CODEX_DAG_PROFILE" --wait
xcrun stapler staple "$CODEX_DAG_DMG"
xcrun stapler validate "$CODEX_DAG_DMG"
spctl --assess --type open --context context:primary-signature --verbose "$CODEX_DAG_DMG"
