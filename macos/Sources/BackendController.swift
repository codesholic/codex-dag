import AppKit
import Foundation

final class AppLog {
    let directory: URL
    let file: URL
    private let lock = NSLock()

    init() {
        directory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Codex DAG", isDirectory: true)
        file = directory.appendingPathComponent("backend.log")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    func write(_ text: String) {
        lock.lock()
        defer { lock.unlock() }
        let fm = FileManager.default
        if let size = (try? fm.attributesOfItem(atPath: file.path)[.size]) as? NSNumber, size.intValue > 5_000_000 {
            let old = directory.appendingPathComponent("backend.log.1")
            try? fm.removeItem(at: old)
            try? fm.moveItem(at: file, to: old)
        }
        let line = ISO8601DateFormatter().string(from: Date()) + " " + text + (text.hasSuffix("\n") ? "" : "\n")
        guard let data = line.data(using: .utf8) else { return }
        if !fm.fileExists(atPath: file.path) { fm.createFile(atPath: file.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
        guard let handle = try? FileHandle(forWritingTo: file) else { return }
        defer { try? handle.close() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: data)
    }
}

struct BackendEndpoint {
    let url: URL
    let token: String
    let pid: Int32
    let startedAt: String

    func owns(_ candidate: URL) -> Bool {
        candidate.scheme == "http" && candidate.host == "127.0.0.1" && candidate.port == url.port && candidate.user == nil && candidate.password == nil
    }

    var webURL: URL {
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        components.path = "/"
        components.queryItems = [URLQueryItem(name: "token", value: token)]
        return components.url!
    }
}

enum BackendPhase: String { case stopped, starting, ready, stopping, failed }

final class BackendController: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private(set) var phase: BackendPhase = .stopped
    private(set) var endpoint: BackendEndpoint?
    private(set) var error: String?
    private(set) var runningAgents: Int?
    private(set) var unknownAgents: Int?
    private(set) var workflowTasks: Int = 0
    private(set) var project: String = "선택 안 됨"
    private(set) var sessionName: String = "선택 안 됨"
    private(set) var summary: [String: Any]?
    let log = AppLog()
    var onChange: (() -> Void)?
    var onReady: ((BackendEndpoint) -> Void)?
    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var readinessTimer: Timer?
    private var pollTimer: Timer?
    private var readinessDeadline = Date.distantPast
    private var pendingReadiness = false
    private var pendingSummary = false
    private var stopCallbacks: [() -> Void] = []
    private var restartSettings: AppSettings?
    private var stdoutBuffer = Data()
    private var parsedStartup = false
    private var generation = UUID()
    private lazy var http: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3
        config.timeoutIntervalForResource = 5
        config.httpShouldSetCookies = false
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()

    var ownsRunningProcess: Bool { process?.isRunning == true }

    func start(settings: AppSettings) {
        guard process == nil, phase != .starting, phase != .stopping else { return }
        generation = UUID()
        let current = generation
        phase = .starting
        error = nil
        runningAgents = nil
        unknownAgents = nil
        project = "선택 안 됨"
        sessionName = "선택 안 됨"
        summary = nil
        workflowTasks = 0
        endpoint = nil
        pendingReadiness = false
        pendingSummary = false
        stdoutBuffer = Data()
        parsedStartup = false
        onChange?()
        do {
            let paths = try AppSettings.validateStorage(dataDirectory: settings.dataDirectory, codexDirectory: settings.codexDirectory)
            let data = paths.data
            let codex = paths.codex
            guard let resources = Bundle.main.resourceURL else { throw NativeError.message("앱 리소스 폴더를 찾을 수 없습니다.") }
            let executable = resources.appendingPathComponent("backend/codex-dag-backend")
            guard FileManager.default.isExecutableFile(atPath: executable.path) else {
                throw NativeError.message("번들 수집기가 없습니다. 앱을 다시 설치하세요.")
            }
            try FileManager.default.createDirectory(atPath: data, withIntermediateDirectories: true)
            let child = Process()
            child.executableURL = executable
            child.currentDirectoryURL = resources
            child.arguments = ["--data-dir", data, "--codex-dir", codex, "--shared-journal", "serve", "--port", "0", "--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONUNBUFFERED"] = "1"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            if let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String {
                environment["CODEX_DAG_APP_VERSION"] = version
            }
            child.environment = environment
            let output = Pipe()
            let errors = Pipe()
            child.standardOutput = output
            child.standardError = errors
            stdoutPipe = output
            stderrPipe = errors
            process = child
            output.fileHandleForReading.readabilityHandler = { [weak self, weak child] handle in
                let data = handle.availableData
                guard !data.isEmpty else { handle.readabilityHandler = nil; return }
                DispatchQueue.main.async { [weak self, weak child] in
                    guard let self, let child else { return }
                    self.consumeOutput(data, child: child, generation: current)
                }
            }
            errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                guard !data.isEmpty else { handle.readabilityHandler = nil; return }
                if let text = String(data: data, encoding: .utf8) { self?.log.write(text) }
            }
            child.terminationHandler = { [weak self] child in
                DispatchQueue.main.async { self?.didExit(child, generation: current) }
            }
            try child.run()
            log.write("Started owned backend pid=\(child.processIdentifier)")
            readinessDeadline = Date().addingTimeInterval(15)
            readinessTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.checkReadiness(generation: current) }
        } catch {
            if process?.isRunning != true { cleanupHandles(); process = nil }
            fail(error.localizedDescription)
        }
    }

    // Only the startup announcement is interpreted. It is never written to logs because it contains the token.
    private func consumeOutput(_ data: Data, child: Process, generation current: UUID) {
        guard generation == current, process === child, !parsedStartup else { return }
        stdoutBuffer.append(data)
        guard stdoutBuffer.count <= 65_536 else {
            DispatchQueue.main.async { [weak self] in
                guard self?.generation == current else { return }
                self?.fail("수집기 시작 응답이 너무 큽니다.")
            }
            parsedStartup = true
            return
        }
        guard let newline = stdoutBuffer.firstIndex(of: 10) else { return }
        let line = stdoutBuffer[..<newline]
        parsedStartup = true
        do {
            guard let info = try JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                  let address = info["url"] as? String, let url = URL(string: address),
                  url.scheme == "http", url.host == "127.0.0.1", let port = url.port, (1...65_535).contains(port),
                  url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
                  let token = info["token"] as? String, !token.isEmpty,
                  let pid = info["pid"] as? Int, pid == Int(child.processIdentifier),
                  let startedAt = info["started_at"] as? String else {
                throw NativeError.message("수집기 시작 응답의 주소 또는 소유 PID를 확인하지 못했습니다.")
            }
            let result = BackendEndpoint(url: url, token: token, pid: Int32(pid), startedAt: startedAt)
            DispatchQueue.main.async { [weak self] in
                guard let self, self.generation == current, self.phase == .starting else { return }
                self.endpoint = result
                self.checkReadiness(generation: current)
            }
        } catch {
            DispatchQueue.main.async { [weak self] in
                guard self?.generation == current else { return }
                self?.fail("수집기 시작 응답 오류: " + error.localizedDescription)
            }
        }
    }

    private func checkReadiness(generation current: UUID) {
        guard generation == current, phase == .starting else { return }
        if Date() >= readinessDeadline {
            fail("수집기가 15초 안에 준비되지 않았습니다. 로그를 확인하세요.")
            return
        }
        guard let endpoint, !pendingReadiness else { return }
        pendingReadiness = true
        request("/api/health", endpoint: endpoint) { [weak self] result in
            guard let self, self.generation == current, self.phase == .starting else { return }
            self.pendingReadiness = false
            guard case .success(let payload) = result,
                  payload["status"] as? String == "ok" || payload["status"] as? String == "ready" || payload["ready"] as? Bool == true else { return }
            if let pid = payload["pid"] as? Int, pid != Int(endpoint.pid) { self.fail("수집기 health PID가 소유 프로세스와 다릅니다."); return }
            self.phase = .ready
            self.error = nil
            self.readinessTimer?.invalidate()
            self.readinessTimer = nil
            self.log.write("Owned backend ready pid=\(endpoint.pid)")
            self.onChange?()
            self.onReady?(endpoint)
            self.fetchSummary()
            self.pollTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in self?.fetchSummary() }
        }
    }

    private func request(_ path: String, endpoint: BackendEndpoint, completion: @escaping (Result<[String: Any], Error>) -> Void) {
        let url = endpoint.url.appendingPathComponent(path.trimmingCharacters(in: CharacterSet(charactersIn: "/")))
        var request = URLRequest(url: url)
        request.setValue("Bearer " + endpoint.token, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        http.dataTask(with: request) { data, response, error in
            let result: Result<[String: Any], Error>
            do {
                if let error { throw error }
                guard let response = response as? HTTPURLResponse, response.statusCode == 200,
                      let data, let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw NativeError.message("수집기 응답을 확인하지 못했습니다.")
                }
                result = .success(payload)
            } catch { result = .failure(error) }
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }

    // The menu needs only the summary projection; the full state stays with the WebView's SSE.
    private func fetchSummary() {
        guard phase == .ready, let endpoint, !pendingSummary else { return }
        let current = generation
        pendingSummary = true
        request("/api/summary", endpoint: endpoint) { [weak self] result in
            guard let self, self.generation == current, self.phase == .ready else { return }
            self.pendingSummary = false
            switch result {
            case .success(let payload):
                self.summary = payload
                let live = payload["live"] as? [String: Any]
                self.runningAgents = live?["running_agents"] as? Int
                self.unknownAgents = live?["unknown_agents"] as? Int
                let selectedProject = payload["project_path"] as? String ?? ""
                self.project = selectedProject.isEmpty ? "선택 안 됨" : selectedProject
                let selectedSession = payload["root_session_id"] as? String ?? ""
                let selectedName = payload["root_session_name"] as? String ?? ""
                self.sessionName = selectedSession.isEmpty ? "선택 안 됨" : (selectedName.isEmpty ? selectedSession : selectedName)
                let workflow = payload["registered_workflow"] as? [String: Any]
                self.workflowTasks = workflow?["node_count"] as? Int ?? 0
                let collection = payload["collection"] as? [String: Any]
                self.error = collection?["error"] as? String
            case .failure(let error):
                self.runningAgents = nil
                self.unknownAgents = nil
                self.error = "수집 상태 미확인: " + error.localizedDescription
            }
            self.onChange?()
        }
    }

    private func fail(_ message: String) {
        log.write(message)
        error = message
        phase = .failed
        runningAgents = nil
        unknownAgents = nil
        readinessTimer?.invalidate()
        readinessTimer = nil
        pollTimer?.invalidate()
        pollTimer = nil
        onChange?()
        if let process, process.isRunning {
            process.terminate()
            forceStopAfterDeadline(process)
        }
    }

    func stop(completion: (() -> Void)? = nil) {
        restartSettings = nil
        stopOwned(completion: completion)
    }

    private func stopOwned(completion: (() -> Void)? = nil) {
        if let completion { stopCallbacks.append(completion) }
        readinessTimer?.invalidate()
        readinessTimer = nil
        pollTimer?.invalidate()
        pollTimer = nil
        endpoint = nil
        runningAgents = nil
        unknownAgents = nil
        summary = nil
        error = nil
        guard let process, process.isRunning else {
            self.process = nil
            cleanupHandles()
            phase = .stopped
            onChange?()
            finishStop()
            return
        }
        phase = .stopping
        onChange?()
        process.terminate()
        forceStopAfterDeadline(process)
    }

    private func forceStopAfterDeadline(_ child: Process) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self, weak child] in
            guard let self, let child, self.process === child, child.isRunning else { return }
            self.log.write("SIGTERM deadline exceeded; SIGKILL owned backend pid=\(child.processIdentifier)")
            kill(child.processIdentifier, SIGKILL)
        }
    }

    func restart(settings: AppSettings) {
        restartSettings = settings
        guard phase != .stopping else { return }
        stopOwned { [weak self] in
            guard let self, let latest = self.restartSettings else { return }
            self.restartSettings = nil
            self.start(settings: latest)
        }
    }

    private func didExit(_ child: Process, generation current: UUID) {
        guard process === child, generation == current else { return }
        let intendedStop = phase == .stopping
        log.write("Owned backend exited pid=\(child.processIdentifier) status=\(child.terminationStatus)")
        process = nil
        endpoint = nil
        cleanupHandles()
        readinessTimer?.invalidate()
        readinessTimer = nil
        pollTimer?.invalidate()
        pollTimer = nil
        runningAgents = nil
        unknownAgents = nil
        if intendedStop { phase = .stopped }
        else if phase != .failed {
            phase = .failed
            error = "수집기가 종료되었습니다 (코드 \(child.terminationStatus)). 로그를 확인하거나 다시 시작하세요."
        }
        onChange?()
        finishStop()
    }

    private func finishStop() {
        let callbacks = stopCallbacks
        stopCallbacks.removeAll()
        callbacks.forEach { $0() }
    }

    private func cleanupHandles() {
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        try? stdoutPipe?.fileHandleForReading.close()
        try? stderrPipe?.fileHandleForReading.close()
        stdoutPipe = nil
        stderrPipe = nil
    }

    // Authenticated control requests never follow redirects or disclose the bearer token to another origin.
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
