# Codex DAG

**See what your Codex agents are doing — as a live graph.**

Codex DAG is a small, local, read-only monitor for [OpenAI Codex](https://openai.com/codex/) sessions.
It turns each request you give Codex, and every sub-agent it spawns, into a live DAG: who delegated
what to whom, which agent is still running, what each one reported back, and when.

[한국어 문서](README.ko.md) · [MIT License](LICENSE)

## Download

| | Download | Requirements |
| --- | --- | --- |
| **macOS** | [**Codex-DAG-macOS-arm64.dmg**](https://github.com/codesholic/codex-dag/releases/latest/download/Codex-DAG-macOS-arm64.dmg) | macOS 15+ on Apple Silicon |
| **Windows** | [**Codex-DAG-Windows-x64-Setup.exe**](https://github.com/codesholic/codex-dag/releases/latest/download/Codex-DAG-Windows-x64-Setup.exe) | Windows 10/11 x64 |

Always the latest release. Checksums (`SHA256SUMS.txt`) and older versions are on the
[Releases](https://github.com/codesholic/codex-dag/releases) page. First launch of an unsigned build: see [Install](#install).

![Codex DAG overview (dark)](assets/screenshots/overview-dark.png)

## Why this exists

Codex DAG was born from a moment of envy. Watching [OmO](https://omo.dev) lay out multi-wave agent
work as a DAG — parallel agents fanning out, results flowing back — made it obvious how much easier
multi-agent work is to follow when you can *see* the graph instead of scrolling through logs.
I wanted that same clarity for my everyday Codex sessions, so I built a small monitor that draws it
from the records Codex already keeps on your machine.

Huge thanks to the OmO team for the inspiration. And honestly, I can't wait for an **OmO desktop app** —
the day it ships, I'd love to use it.

## Features

- **Request timeline** — every request in a session, with its progress. Click one to jump the graph there.
  Large sessions open on the latest request at a readable zoom instead of a tiny overview.
- **Live agent graph** — one card per actual agent run, with delegation, assignment, progress-report and
  result lines (arrows point from sender to receiver). Select a line to read the exact messages and the
  evidence used to connect them.
- **Live overview** — running agents, what is happening right now, last activity and a status chip per agent.
- **Collaboration feed and execution log** — public progress reports, final reports and tool activity,
  with every original source record one click away.
- **Menu bar / tray app** — start, stop and restart the collector, open the monitor window, change the
  Codex data folder, open logs.
- **Light and dark themes** that follow your system.

The interface text is Korean today.

![Codex DAG overview (light)](assets/screenshots/overview-light.png)

## How it works

```
~/.codex (read-only) ──► Python collector ──► 127.0.0.1 server ──SSE──► monitor window
optional public journal ─┘   (stdlib only)      (per-run token)        (WKWebView / Electron)
```

- The collector reads Codex's local session records (`~/.codex/sessions/**/*.jsonl`) incrementally and
  never writes to the Codex folder. Session names come from a temporary copy of Codex's app metadata.
- Agent-to-agent message bodies are encrypted in Codex's local records. Agents can optionally record the
  exact public text they send in a local **public journal** (`scripts/journal-public.py`); Codex DAG links
  those entries to the real deliveries and always labels inferred links as such.
- The server listens on `127.0.0.1` only, uses a fresh token per run and serves live updates over SSE.
- Private reasoning is never collected and encrypted content is never decrypted.

## Install

| Platform | Package | How |
| --- | --- | --- |
| macOS 15+ on Apple Silicon | [`Codex-DAG-macOS-arm64.dmg`](https://github.com/codesholic/codex-dag/releases/latest/download/Codex-DAG-macOS-arm64.dmg) | Open the DMG and drag `Codex DAG.app` into Applications. It lives in the menu bar. |
| Windows 10/11 x64 | [`Codex-DAG-Windows-x64-Setup.exe`](https://github.com/codesholic/codex-dag/releases/latest/download/Codex-DAG-Windows-x64-Setup.exe) | Run the installer (per user, no admin rights). It lives in the system tray. |

Both packages bundle their own Python runtime, so nothing else needs to be installed.

The builds are **not signed or notarized**:

- **macOS** — on first launch, right-click the app and choose *Open*, or run
  `xattr -dr com.apple.quarantine "/Applications/Codex DAG.app"`.
- **Windows** — if SmartScreen appears, choose *More info → Run anyway*.

Then open the monitor from the menu bar or tray, connect your project folder (it is pre-filled from the
project selected in the Codex app) and pick a session.

App data lives in `~/Library/Application Support/Codex DAG` (macOS) or `%LOCALAPPDATA%\Codex DAG` (Windows).

## Build from source

The backend uses only the Python standard library (Python 3.10+).

```sh
./scripts/test.sh                 # backend tests
npm test --prefix windows         # Windows host tests (after npm ci --prefix windows)
./scripts/start-dev.sh            # dev monitor at http://127.0.0.1:58957/ (source mode, no token)
```

**macOS app and DMG** (Apple Silicon, Xcode Command Line Tools):

```sh
python3 packaging/macos/prepare-runtime.py   # pinned python.org runtime + build tools in .local/
./scripts/build-macos.sh                     # → dist/Codex DAG.app, dist/Codex DAG.dmg
```

**Windows installer** (on Windows: `python packaging/windows/build.py`; on macOS: Node.js/npm and Rosetta 2):

```sh
./scripts/build-windows.sh                   # → dist/Codex-DAG-<version>-Setup-x64.exe
```

`./scripts/prepare-release.sh` copies both installers to the stable release asset names used by the
download links above and writes `SHA256SUMS.txt` into `dist/release/`.

More detail: [macOS app](macos/README.md) · [macOS packaging](packaging/macos/README.md) ·
[Windows host](windows/README.md) · [Windows packaging](packaging/windows/README.md).

### Recording public messages (optional)

To show the real text of agent hand-offs, have the sending agent record it right before sending:

```sh
python3 scripts/journal-public.py --project /path/to/project --session "$CODEX_THREAD_ID" \
  --from-agent /root --to-agent /root/reviewer --kind assignment --role Reviewer \
  --message-file message.txt
```

`--collaboration-call-id` takes only a real Codex call id; `--assignment-id` is your own task id.

## Project layout

```
src/monitor/      collector, journal, loopback server and the monitor page (index.html)
tests/            backend tests (unittest)
macos/            AppKit menu bar app with a WKWebView monitor window
windows/          Electron tray app for Windows
packaging/        reproducible macOS (app, DMG) and Windows (NSIS) builds
scripts/          test, dev server, build and journal helpers
```

## Limitations

- Codex's local record formats are not a public API and may change with Codex updates.
- Release builds are ad-hoc signed (macOS) or unsigned (Windows); macOS Intel is not supported.
- The Windows build is cross-built and has had less real-device testing than the macOS app.

## Disclaimer

Codex DAG is an independent, unofficial project. It is not affiliated with, endorsed by or sponsored by
OpenAI or the OmO project. "Codex" and "OmO" are names of their respective owners.

## License

[MIT](LICENSE) © 2026 codesholic
