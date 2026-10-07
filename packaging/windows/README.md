# Windows x64 installer build

The Windows app is an Electron tray host plus the shared Python collector and monitor page.

## Install (users)

Run `Codex-DAG-Windows-x64-Setup.exe` from the [latest release](https://github.com/codesholic/codex-dag/releases/latest).
It installs for the current user without admin rights and adds Start menu and desktop shortcuts.
Python, Node.js and WebView2 are bundled. The installer is **unsigned**, so SmartScreen may ask you to
confirm (*More info → Run anyway*).

- Program files: `%LOCALAPPDATA%\Programs\Codex DAG`
- Settings and public journal: `%LOCALAPPDATA%\Codex DAG`
- Electron profile and cache: `electron` under the data folder; logs: `logs` under the data folder
- Codex input: `%USERPROFILE%\.codex` by default (changeable in the tray settings)

A fresh install starts without any project or history. Connect a project folder and pick a session in the
monitor window. The tray offers open monitor, start/stop/restart collector, settings, logs and quit. Closing
the window only hides it. Uninstall from Windows *Installed apps* or the Start menu shortcut; settings and
the public journal are kept. A running app is asked to quit through its single-instance channel; other
processes are never killed.

## Build

On Windows (PowerShell):

```powershell
python packaging/windows/build.py
```

On macOS (cross build; needs Python 3.10+, Node.js/npm and Rosetta 2 for the x86_64 NSIS compiler):

```sh
./scripts/build-windows.sh                 # runs npm ci first
./scripts/build-windows.sh --skip-install  # reuse existing pinned node_modules
```

`prepare.py` downloads the official Python 3.14.8 embeddable package and NSIS 3.0.4.1 and checks them against
the SHA-256 values in `runtime-lock.json`; Electron and electron-builder are pinned in `windows/package-lock.json`.
Only the seven backend source files are copied, and the embedded Python path file references only the standard
library, the runtime and the backend. `verify.py` checks the PE x64 binaries, runtime hashes, the ASAR file
list and that no private data is included.

Output:

- `dist/Codex-DAG-<version>-Setup-x64.exe` and `.sha256`
- `dist/windows/package-audit.json`
- `dist/windows/win-unpacked/` (unpacked app)

`.github/workflows/windows.yml` is a manually triggered workflow that runs the Win32 platform tests and builds
the installer on a Windows runner.

## Checks

```sh
./scripts/test.sh
npm run test --prefix windows
python3 packaging/windows/verify.py dist/Codex-DAG-<version>-Setup-x64.exe
```

Real Win32 API tests are skipped on macOS and run on Windows. A full Windows check means installing and
running on Windows: settings, a real Codex session, SSE, tray, window reopen, quit, update, uninstall and
non-ASCII paths.
