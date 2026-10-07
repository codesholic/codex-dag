# Codex DAG for Windows

An Electron tray app for Windows x64. It runs the bundled Python collector and shows the same monitor page in
its own window. The renderer has no Node.js access; only the local settings window gets a narrow IPC bridge.
The server URL and token are taken from the owned child's startup line and confirmed with the health PID;
the token is never written to logs. Build and install details: [packaging/windows](../packaging/windows/README.md).

## Develop

Keep test data apart from your real app data and Codex folder. PowerShell example:

```powershell
npm ci --prefix windows
$env:CODEX_DAG_JOURNAL_DIR = 'C:\Temp\dag-test-journal'
npm start --prefix windows -- --data-dir C:\Temp\dag-test-data --codex-dir C:\Temp\codex-fixture --python C:\Python314\python.exe
Remove-Item Env:CODEX_DAG_JOURNAL_DIR
```

On macOS the same host can be run for UI checks with absolute `--data-dir`, `--codex-dir` and `--python`
paths; this does not replace testing on Windows. Never write fixtures or test messages into a real Codex folder.

The tray polls only the authenticated `/api/summary` every 3 s; the window receives the full state over SSE.

## Test

```sh
npm test --prefix windows     # host tests
./scripts/test.sh             # shared backend tests
```
