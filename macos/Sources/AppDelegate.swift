import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let backend = BackendController()
    private var settings = AppSettings.load()
    private var statusItem: NSStatusItem!
    private let menu = NSMenu()
    private let backendStatus = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let agentStatus = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let unknownStatus = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let projectStatus = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let sessionStatus = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let workflowStatus = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let errorStatus = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private var startItem: NSMenuItem!
    private var stopItem: NSMenuItem!
    private var restartItem: NSMenuItem!
    private var monitor: MonitorWindowController!
    private var settingsWindow: SettingsWindowController?
    private var terminating = false
    private var smoke: NativeSmokeTest?
    private var lastPhase: BackendPhase?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let bundleIdentifier = Bundle.main.bundleIdentifier ?? "io.github.codesholic.codex-dag"
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
            .filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
        if let existing = others.first {
            existing.activate(options: [])
            NSApp.terminate(nil)
            return
        }
        NSApp.setActivationPolicy(.accessory)
        monitor = MonitorWindowController()
        setupMenu()
        setupApplicationMenu()
        backend.onChange = { [weak self] in self?.refreshStatus() }
        backend.onReady = { [weak self] endpoint in self?.monitor.load(endpoint) }
        refreshStatus()
        if CommandLine.arguments.contains("--smoke-test") {
            smoke = NativeSmokeTest(backend: backend, monitor: monitor, settings: settings)
            smoke?.start()
        } else if settings.startOnLaunch {
            backend.start(settings: settings)
        }
        if settings.openWindowOnLaunch || smoke != nil { monitor.open() }
    }

    private func setupMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted", accessibilityDescription: "Codex DAG")
        statusItem.button?.imagePosition = .imageLeading
        statusItem.button?.setAccessibilityLabel("Codex DAG 메뉴")
        statusItem.button?.setAccessibilityIdentifier("codex-dag-status-item")
        statusItem.menu = menu
        menu.delegate = self
        menu.autoenablesItems = false
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "개발"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0"
        let versionItem = NSMenuItem(title: "Codex DAG \(version) (\(build)) · Apple Silicon", action: nil, keyEquivalent: "")
        versionItem.isEnabled = false
        menu.addItem(versionItem)
        menu.addItem(.separator())
        for item in [backendStatus, agentStatus, unknownStatus, projectStatus, sessionStatus, workflowStatus, errorStatus] {
            item.isEnabled = false
            menu.addItem(item)
        }
        menu.addItem(.separator())
        add("모니터 창 열기", #selector(openMonitor), "o")
        startItem = add("수집기 시작", #selector(startMonitor), "")
        stopItem = add("수집기 중지", #selector(stopMonitor), "")
        restartItem = add("수집기 재시작", #selector(restartMonitor), "r")
        menu.addItem(.separator())
        add("설정…", #selector(openSettings), ",")
        add("로그 폴더 열기", #selector(openLogs), "l")
        add("Codex DAG 정보", #selector(about), "")
        menu.addItem(.separator())
        add("Codex DAG 종료", #selector(quit), "q")
    }

    private func setupApplicationMenu() {
        let bar = NSMenu()
        let app = NSMenuItem(title: "Codex DAG", action: nil, keyEquivalent: "")
        let appMenu = NSMenu(title: "Codex DAG")
        for (title, action, shortcut) in [
            ("Codex DAG 정보", #selector(about), ""),
            ("설정…", #selector(openSettings), ","),
            ("Codex DAG 종료", #selector(quit), "q")
        ] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: shortcut)
            item.target = self
            appMenu.addItem(item)
        }
        // Share the actual status-item menu with the application menu so the
        // same controls are reachable through keyboard/assistive navigation.
        let controls = NSMenuItem(title: "모니터 상태 및 제어", action: nil, keyEquivalent: "")
        controls.submenu = menu
        appMenu.insertItem(controls, at: 2)
        app.submenu = appMenu
        bar.addItem(app)
        let edit = NSMenuItem(title: "편집", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "편집")
        for (title, action, shortcut) in [
            ("실행 취소", Selector(("undo:")), "z"),
            ("잘라내기", #selector(NSText.cut(_:)), "x"),
            ("복사", #selector(NSText.copy(_:)), "c"),
            ("붙여넣기", #selector(NSText.paste(_:)), "v"),
            ("모두 선택", #selector(NSText.selectAll(_:)), "a")
        ] { editMenu.addItem(withTitle: title, action: action, keyEquivalent: shortcut) }
        edit.submenu = editMenu
        bar.addItem(edit)
        let window = NSMenuItem(title: "창", action: nil, keyEquivalent: "")
        let windowMenu = NSMenu(title: "창")
        let open = NSMenuItem(title: "모니터 창 열기", action: #selector(openMonitor), keyEquivalent: "o")
        open.target = self
        windowMenu.addItem(open)
        windowMenu.addItem(withTitle: "창 닫기", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowMenu.addItem(withTitle: "최소화", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        window.submenu = windowMenu
        bar.addItem(window)
        NSApp.mainMenu = bar
        NSApp.windowsMenu = windowMenu
    }

    @discardableResult private func add(_ title: String, _ action: Selector, _ shortcut: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: shortcut)
        item.target = self
        menu.addItem(item)
        return item
    }

    private func refreshStatus() {
        guard statusItem != nil else { return }
        let phaseText: String
        switch backend.phase {
        case .stopped: phaseText = "중지됨"
        case .starting: phaseText = "시작 중"
        case .ready: phaseText = "실행 중"
        case .stopping: phaseText = "종료 중"
        case .failed: phaseText = "오류"
        }
        backendStatus.title = "수집기: " + phaseText
        let count = backend.runningAgents.map(String.init) ?? "?"
        statusItem.button?.title = backend.phase == .ready ? "DAG \(count)" : "DAG " + (backend.phase == .failed ? "!" : "·")
        agentStatus.title = "실제 실행 에이전트: " + (backend.runningAgents.map { "\($0)명" } ?? "미확인")
        unknownStatus.title = "실행 상태 미확인: " + (backend.unknownAgents.map { "\($0)명" } ?? "근거 없음")
        projectStatus.title = "프로젝트: " + abbreviate(backend.project)
        projectStatus.toolTip = backend.project
        sessionStatus.title = "세션: " + abbreviate(backend.sessionName)
        sessionStatus.toolTip = backend.sessionName
        workflowStatus.title = "등록 워크플로우 작업: \(backend.workflowTasks)개"
        errorStatus.title = "상태: " + abbreviate(backend.error ?? "수집 오류 없음")
        errorStatus.toolTip = backend.error
        errorStatus.isHidden = backend.error == nil
        statusItem.button?.toolTip = "Codex DAG · 수집기 \(phaseText)\n\(agentStatus.title)\n\(projectStatus.title)\n\(sessionStatus.title)" + (backend.error.map { "\n" + $0 } ?? "")
        startItem.isEnabled = !backend.ownsRunningProcess && [.stopped, .failed].contains(backend.phase)
        stopItem.isEnabled = backend.ownsRunningProcess && backend.phase != .stopping
        restartItem.isEnabled = backend.phase != .stopping
        if backend.phase != lastPhase {
            switch backend.phase {
            case .stopped: monitor?.displayMessage(title: "수집기 중지됨", detail: "메뉴바의 ‘수집기 시작’을 선택하세요.")
            case .starting: monitor?.displayMessage(title: "수집기 시작 중", detail: "실행 기록을 읽을 준비를 하고 있습니다.")
            case .stopping: monitor?.displayMessage(title: "수집기 종료 중", detail: "Codex DAG가 시작한 수집기를 정리하고 있습니다.")
            case .failed: monitor?.displayMessage(title: "수집기 오류", detail: backend.error ?? "로그를 확인하고 수집기를 다시 시작하세요.")
            case .ready: break
            }
            lastPhase = backend.phase
        }
        if backend.phase == .ready {
            let name = backend.project == "선택 안 됨" ? "" : URL(fileURLWithPath: backend.project).lastPathComponent
            monitor.window?.title = name.isEmpty ? "Codex DAG" : "Codex DAG · " + name
        }
    }

    private func abbreviate(_ value: String) -> String { value.count <= 75 ? value : String(value.prefix(72)) + "…" }
    func menuWillOpen(_ menu: NSMenu) { refreshStatus() }

    @objc private func openMonitor() { monitor.open() }
    @objc private func startMonitor() { backend.start(settings: settings) }
    @objc private func stopMonitor() { backend.stop() }
    @objc private func restartMonitor() { backend.restart(settings: settings) }
    @objc private func openLogs() { NSWorkspace.shared.open(backend.log.directory) }
    @objc private func about() {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "개발"
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "Codex DAG", .applicationVersion: version,
                                                    .credits: NSAttributedString(string: "Codex 실행 기록과 에이전트 협업 관찰\nApple Silicon · macOS 15 이상")])
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc private func openSettings() {
        let controller = SettingsWindowController(settings: settings)
        controller.onSave = { [weak self] updated in
            guard let self else { return }
            let pathsChanged = self.settings.dataDirectory != updated.dataDirectory || self.settings.codexDirectory != updated.codexDirectory
            self.settings = updated
            if pathsChanged, self.backend.ownsRunningProcess || self.backend.phase == .starting { self.backend.restart(settings: updated) }
        }
        settingsWindow?.close()
        settingsWindow = controller
        controller.showWindow(nil)
        controller.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc private func quit() { NSApp.terminate(nil) }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        monitor?.open()
        return true
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminating { return .terminateLater }
        guard backend.ownsRunningProcess else { return .terminateNow }
        terminating = true
        backend.stop { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

// Explicit command-line smoke mode checks the installed app's real backend and WKWebView transport.
// It does not claim to verify menu clicks, graph dragging, provenance expansion, or login-item approval.
final class NativeSmokeTest {
    private let backend: BackendController
    private let monitor: MonitorWindowController
    private let settings: AppSettings
    private var timer: Timer?
    private var stage = 0
    private var deadline = Date().addingTimeInterval(45)
    private var firstPID: Int32?
    private var checkingWeb = false
    private var completed = false

    init(backend: BackendController, monitor: MonitorWindowController, settings: AppSettings) {
        self.backend = backend
        self.monitor = monitor
        self.settings = settings
    }
    func start() {
        backend.start(settings: settings)
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.step() }
    }
    private func emit(_ payload: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]), let text = String(data: data, encoding: .utf8) else { return }
        print(text)
        fflush(stdout)
    }
    private func step() {
        guard !completed else { return }
        if Date() > deadline { finish(error: "Native smoke deadline exceeded at stage \(stage)"); return }
        if backend.phase == .failed { finish(error: backend.error ?? "Backend failed"); return }
        if stage == 0, backend.phase == .ready, backend.summary != nil, let endpoint = backend.endpoint {
            firstPID = endpoint.pid
            emit(["native_smoke": "ready", "pid": endpoint.pid, "url": endpoint.url.absoluteString, "actual_running_agents": backend.runningAgents.map { $0 as Any } ?? NSNull()])
            stage = 1
        }
        if stage == 1, !checkingWeb, monitor.webView.url?.host == "127.0.0.1", !monitor.webView.isLoading {
            checkingWeb = true
            let script = """
            return await new Promise((resolve,reject)=>{
              const stream=new EventSource('/api/stream');
              const timeout=setTimeout(()=>{stream.close();reject(new Error('SSE snapshot timeout'));},8000);
              stream.onmessage=event=>{clearTimeout(timeout);stream.close();const data=JSON.parse(event.data);resolve({nodes:data.nodes.length,events:data.events.length,documentTitle:document.title});};
              stream.onerror=()=>{clearTimeout(timeout);stream.close();reject(new Error('SSE transport failed'));};
            });
            """
            monitor.webView.callAsyncJavaScript(script, arguments: [:], in: nil, in: .page) { [weak self] result in
                guard let self, !self.completed else { return }
                switch result {
                case .failure(let error): self.finish(error: error.localizedDescription)
                case .success(let payload):
                    self.emit(["native_smoke": "webview_sse_snapshot", "payload": payload])
                    self.stage = 2
                    self.backend.stop {
                        guard let firstPID = self.firstPID, kill(firstPID, 0) != 0, errno == ESRCH else {
                            self.finish(error: "Owned child remains after stop"); return
                        }
                        self.emit(["native_smoke": "owned_process_stopped", "pid": firstPID])
                        self.stage = 3
                        self.backend.start(settings: self.settings)
                    }
                }
            }
        }
        if stage == 3, backend.phase == .ready, backend.summary != nil, let endpoint = backend.endpoint {
            emit(["native_smoke": "restarted", "pid": endpoint.pid, "url": endpoint.url.absoluteString])
            stage = 4
            backend.stop {
                guard kill(endpoint.pid, 0) != 0, errno == ESRCH else { self.finish(error: "Restarted child remains after stop"); return }
                self.finish(error: nil)
            }
        }
    }
    private func finish(error: String?) {
        guard !completed else { return }
        completed = true
        timer?.invalidate()
        timer = nil
        emit(["native_smoke": error == nil ? "passed" : "failed", "detail": error ?? "Readiness, authenticated summary, WKWebView SSE, stop, restart, and owned-child cleanup passed."])
        backend.stop {
            fflush(stdout)
            exit(error == nil ? 0 : 1)
        }
    }
}
