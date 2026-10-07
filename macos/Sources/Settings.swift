import AppKit
import ServiceManagement

struct AppSettings {
    var dataDirectory: String
    var codexDirectory: String
    var startOnLaunch: Bool
    var openWindowOnLaunch: Bool

    static func load(arguments: [String] = CommandLine.arguments) -> AppSettings {
        let defaults = UserDefaults.standard
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        func value(_ flag: String) -> String? {
            guard let index = arguments.firstIndex(of: flag), index + 1 < arguments.count else { return nil }
            return arguments[index + 1]
        }
        return AppSettings(
            dataDirectory: value("--data-dir") ?? defaults.string(forKey: "dataDirectory") ?? home + "/Library/Application Support/Codex DAG",
            codexDirectory: value("--codex-dir") ?? defaults.string(forKey: "codexDirectory") ?? home + "/.codex",
            startOnLaunch: defaults.object(forKey: "startOnLaunch") as? Bool ?? true,
            openWindowOnLaunch: defaults.object(forKey: "openWindowOnLaunch") as? Bool ?? true
        )
    }

    func persist() {
        let defaults = UserDefaults.standard
        defaults.set(dataDirectory, forKey: "dataDirectory")
        defaults.set(codexDirectory, forKey: "codexDirectory")
        defaults.set(startOnLaunch, forKey: "startOnLaunch")
        defaults.set(openWindowOnLaunch, forKey: "openWindowOnLaunch")
    }

    static func validateStorage(dataDirectory: String, codexDirectory: String) throws -> (data: String, codex: String) {
        let data = try directory(dataDirectory)
        let codex = try directory(codexDirectory)
        let resolvedData = canonicalPath(data)
        let resolvedCodex = canonicalPath(codex)
        if resolvedData == resolvedCodex || resolvedData.hasPrefix(resolvedCodex.hasSuffix("/") ? resolvedCodex : resolvedCodex + "/") {
            throw NativeError.message("데이터 폴더는 읽기 전용 Codex 폴더 안에 둘 수 없습니다. 다른 사용자 폴더를 선택하세요.")
        }
        let bundled = [Bundle.main.bundleURL, Bundle.main.resourceURL].compactMap { $0 }
        for location in bundled {
            let path = location.resolvingSymlinksInPath().path
            if resolvedData == path || resolvedData.hasPrefix(path + "/") {
                throw NativeError.message("앱 번들 또는 리소스 안에는 데이터를 저장할 수 없습니다. 사용자 폴더를 선택하세요.")
            }
        }
        for path in [data, codex] {
            var isDirectory: ObjCBool = false
            if FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory), !isDirectory.boolValue {
                throw NativeError.message("폴더 경로에 파일이 있습니다: " + path)
            }
        }
        return (data, codex)
    }

    // Foundation may skip resolving a symlink when the final leaf does not exist.
    // Resolve the nearest existing ancestor first, then append the untouched new components.
    static func canonicalPath(_ input: String) -> String {
        var ancestor = URL(fileURLWithPath: input, isDirectory: true).standardizedFileURL
        var missing: [String] = []
        while !FileManager.default.fileExists(atPath: ancestor.path), ancestor.path != "/" {
            missing.append(ancestor.lastPathComponent)
            ancestor.deleteLastPathComponent()
        }
        var result = ancestor.resolvingSymlinksInPath()
        for component in missing.reversed() { result.appendPathComponent(component, isDirectory: true) }
        return result.standardizedFileURL.path
    }

    static func directory(_ input: String) throws -> String {
        let expanded = (input.trimmingCharacters(in: .whitespacesAndNewlines) as NSString).expandingTildeInPath
        guard expanded.hasPrefix("/"), !expanded.contains("\0") else {
            throw NativeError.message("폴더 경로는 / 또는 ~/로 시작해야 합니다.")
        }
        return URL(fileURLWithPath: expanded, isDirectory: true).standardizedFileURL.path
    }
}

enum NativeError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let text): return text }
    }
}

final class SettingsWindowController: NSWindowController {
    private let dataField = NSTextField()
    private let codexField = NSTextField()
    private let autoStart = NSButton(checkboxWithTitle: "앱 실행 시 수집기 시작", target: nil, action: nil)
    private let openWindow = NSButton(checkboxWithTitle: "앱 실행 시 모니터 창 열기", target: nil, action: nil)
    private let login = NSButton(checkboxWithTitle: "로그인 시 Codex DAG 실행", target: nil, action: nil)
    private let loginNote = NSTextField(wrappingLabelWithString: "")
    var onSave: ((AppSettings) -> Void)?

    init(settings: AppSettings) {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 700, height: 390),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Codex DAG 설정"
        window.identifier = NSUserInterfaceItemIdentifier("codex-dag-settings")
        window.isReleasedWhenClosed = false
        window.center()
        dataField.identifier = NSUserInterfaceItemIdentifier("data-directory")
        codexField.identifier = NSUserInterfaceItemIdentifier("codex-directory")
        dataField.setAccessibilityLabel("데이터 폴더 경로")
        codexField.setAccessibilityLabel("Codex 폴더 경로")
        dataField.stringValue = settings.dataDirectory
        codexField.stringValue = settings.codexDirectory
        autoStart.state = settings.startOnLaunch ? .on : .off
        openWindow.state = settings.openWindowOnLaunch ? .on : .off
        refreshLoginState()

        let dataBrowse = NSButton(title: "선택…", target: self, action: #selector(browseData))
        let codexBrowse = NSButton(title: "선택…", target: self, action: #selector(browseCodex))
        dataBrowse.setAccessibilityLabel("데이터 폴더 선택")
        codexBrowse.setAccessibilityLabel("Codex 폴더 선택")
        let grid = NSGridView(views: [
            [NSTextField(labelWithString: "데이터 폴더"), dataField, dataBrowse],
            [NSTextField(labelWithString: "Codex 폴더"), codexField, codexBrowse]
        ])
        grid.rowSpacing = 14
        grid.columnSpacing = 12
        grid.column(at: 0).width = 85
        grid.column(at: 1).width = 435
        let hint = NSTextField(wrappingLabelWithString: "데이터 폴더에 설정과 워크플로우 기록을 저장합니다. Codex 폴더의 실행 기록은 읽기만 합니다. 프로젝트와 세션은 모니터 창에서 선택하세요.")
        hint.textColor = .secondaryLabelColor
        hint.font = .systemFont(ofSize: 12)
        loginNote.textColor = .secondaryLabelColor
        loginNote.font = .systemFont(ofSize: 12)
        let loginSettings = NSButton(title: "로그인 항목 설정 열기", target: self, action: #selector(openLoginSettings))
        let save = NSButton(title: "저장", target: self, action: #selector(saveSettings))
        save.keyEquivalent = "\r"
        save.identifier = NSUserInterfaceItemIdentifier("save-settings")
        let cancel = NSButton(title: "취소", target: self, action: #selector(cancelSettings))
        cancel.keyEquivalent = "\u{1b}"
        let buttons = NSStackView(views: [loginSettings, NSView(), cancel, save])
        buttons.orientation = .horizontal
        buttons.distribution = .fill
        let stack = NSStackView(views: [grid, hint, autoStart, openWindow, login, loginNote, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24),
            hint.widthAnchor.constraint(equalTo: stack.widthAnchor),
            loginNote.widthAnchor.constraint(equalTo: stack.widthAnchor),
            buttons.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is unsupported") }

    private func refreshLoginState() {
        let status = SMAppService.mainApp.status
        login.state = status == .enabled || status == .requiresApproval ? .on : .off
        loginNote.stringValue = status == .requiresApproval
            ? "로그인 자동 실행을 사용하려면 시스템 설정에서 허용하세요."
            : "로그인 자동 실행은 이 설정을 선택한 경우에만 등록됩니다."
    }

    private func browse(_ field: NSTextField) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "폴더 선택"
        panel.directoryURL = URL(fileURLWithPath: (field.stringValue as NSString).expandingTildeInPath)
        guard let window else { return }
        panel.beginSheetModal(for: window) { response in
            if response == .OK, let url = panel.url { field.stringValue = url.path }
        }
    }

    @objc private func browseData() { browse(dataField) }
    @objc private func browseCodex() { browse(codexField) }
    @objc private func openLoginSettings() { SMAppService.openSystemSettingsLoginItems() }
    @objc private func cancelSettings() { close() }

    @objc private func saveSettings() {
        do {
            let paths = try AppSettings.validateStorage(dataDirectory: dataField.stringValue, codexDirectory: codexField.stringValue)
            let data = paths.data
            let codex = paths.codex
            try FileManager.default.createDirectory(atPath: data, withIntermediateDirectories: true)
            let requestedLogin = login.state == .on
            let service = SMAppService.mainApp
            let registered = service.status == .enabled || service.status == .requiresApproval
            if requestedLogin != registered {
                if requestedLogin { try service.register() }
                else { try service.unregister() }
            }
            let settings = AppSettings(dataDirectory: data, codexDirectory: codex,
                                       startOnLaunch: autoStart.state == .on,
                                       openWindowOnLaunch: openWindow.state == .on)
            settings.persist()
            onSave?(settings)
            close()
        } catch {
            refreshLoginState()
            let alert = NSAlert()
            alert.messageText = "설정을 저장하지 못했습니다"
            alert.informativeText = error.localizedDescription
            if let window { alert.beginSheetModal(for: window) }
        }
    }
}
