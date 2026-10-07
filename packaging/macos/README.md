# macOS app and DMG build

Build host: Apple Silicon macOS with Xcode Command Line Tools. Target: Apple Silicon, macOS 15 or later.
Users of the app do not need Python or Homebrew.

```sh
python3 packaging/macos/prepare-runtime.py
./scripts/build-macos.sh
```

`prepare-runtime.py` downloads the pinned python.org 3.13.5 installer package into `.local/`, checks its
SHA-256 and PSF Developer ID signature, extracts it locally (nothing is installed system-wide), rewrites the
framework load paths to be relative and creates a build virtual environment with the pinned tools in
`requirements-build.txt`.

`build-macos.sh` produces `dist/Codex DAG.app` and `dist/Codex DAG.dmg`. Only the Swift sources, the backend
code and `index.html` are packaged. The build bundles Python with PyInstaller (onedir), checks every Mach-O
file for arm64, a minimum macOS of 15 or lower and no external non-system libraries, signs inside-out and
refuses to package local settings, journals, `.local/` or Codex data. `build/package-audit.json` is build
evidence only.

## Verify the bundled collector

```sh
python3 packaging/macos/verify-bundle.py 'dist/Codex DAG.app/Contents/Resources/backend/codex-dag-backend' \
  --evidence .local/verification/bundle.json
```

It copies the collector outside the repository and checks the service lifecycle, SSE, authentication,
duplicate instances, port conflicts and history preservation with a throwaway HOME, Codex folder and data.
The native menu bar and window are checked separately (see [macos](../../macos/README.md)).

## Signing and notarization

The default is ad-hoc signing (`--identity -`), which is fine for local use but is not trusted by Gatekeeper
for distribution. For notarized releases use a Developer ID identity and a notarytool Keychain profile:

```sh
./scripts/build-macos.sh --identity 'Developer ID Application: YOUR IDENTITY'
./packaging/macos/notarize.sh YOUR_KEYCHAIN_PROFILE
```

Certificates, passwords and build output never belong in Git.
