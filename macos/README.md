# Codex DAG for macOS

An AppKit menu bar app for Apple Silicon Macs running **macOS 15 or later**. The Swift sources use only
AppKit, WebKit and ServiceManagement. The bundled backend is the Python collector from `src/monitor`
packaged with PyInstaller (see [packaging/macos](../packaging/macos/README.md)).

## Build the native executable

```sh
./macos/build-native.sh           # → build/native/Codex DAG
```

This compiles only the native host. The full app bundle and DMG are assembled by
`./scripts/build-macos.sh`; a native executable alone does not include the collector.

## Bundle layout

- Bundle ID: `io.github.codesholic.codex-dag`
- Executable: `Contents/MacOS/Codex DAG`
- Collector: `Contents/Resources/backend/codex-dag-backend`
- Data: `~/Library/Application Support/Codex DAG/`
- Codex input (read-only): `~/.codex/` by default
- Log: `~/Library/Logs/Codex DAG/backend.log` (rotated once above 5 MB)

The app starts the collector as `--data-dir PATH --codex-dir PATH --shared-journal serve --port 0 --parent-pid PID`.
It validates the first JSON line (PID, loopback URL, token), waits up to 15 s for an authenticated
`/api/health`, then opens the monitor. The startup line with the token is never logged, and authenticated
native requests never follow redirects.

## Behaviour

- The menu shows collector state and the number of actually running agents. It polls only the authenticated
  `/api/summary` every 1.5 s; the full state reaches the window over SSE. If a request fails, counts show as
  unknown instead of reusing old values.
- Menu: open monitor, start/stop/restart collector, settings, open log folder, about, quit.
- While the monitor window is open the app is a regular app (Dock, Cmd+Tab). Closing the window hides it and
  returns to menu-bar-only mode; the WebView, SSE and collector keep running. Minimized windows are restored on reopen.
- Quitting stops only the collector this app started (SIGTERM, then SIGKILL after 3 s). If another instance
  with the same bundle ID is running, the new one activates it and exits without starting a collector.
- Settings (data folder, Codex folder, launch behaviour) are stored in UserDefaults. Changing a path restarts
  the owned collector. Data folders inside the Codex folder or the app bundle are rejected, including
  symlinked and not-yet-created paths. Launch at login uses `SMAppService.mainApp` and is registered only on request.
- The monitor window and its waiting page follow the system light/dark appearance. WKWebView uses an in-memory
  cookie store, navigates only to its own `http://127.0.0.1:<port>` origin and refuses new windows.

## Checks

Read-only storage boundary check of a built executable (exit 0 when valid, 2 when rejected):

```sh
"build/native/Codex DAG" --validate-storage /tmp/codex-dag-data ~/.codex
python3 macos/verify-storage.py "build/native/Codex DAG"
```

Smoke mode for an assembled app, with throwaway data and an empty Codex folder (overrides are not saved):

```sh
"/Applications/Codex DAG.app/Contents/MacOS/Codex DAG" \
  --smoke-test --data-dir /tmp/codex-dag-smoke/data --codex-dir /tmp/codex-dag-smoke/codex
```

It checks readiness, the authenticated summary, the WKWebView SSE snapshot, stop/restart and that owned
processes exit, then prints JSON and quits. It does not replace checking the real menu and window by hand.
Web Inspector is enabled only with `--inspect-webview`.

Accessibility identifiers: `codex-dag-status-item`, `codex-dag-monitor`, `codex-dag-webview`,
`codex-dag-settings`, `data-directory`, `codex-directory`, `save-settings`.
