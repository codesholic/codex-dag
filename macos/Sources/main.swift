import AppKit

// A read-only boundary check for packager/integration tests. It performs no directory creation or app launch.
if let index = CommandLine.arguments.firstIndex(of: "--validate-storage") {
    guard index + 2 < CommandLine.arguments.count else {
        fputs("usage: Codex DAG --validate-storage DATA_DIR CODEX_DIR\n", stderr)
        exit(2)
    }
    do {
        let paths = try AppSettings.validateStorage(dataDirectory: CommandLine.arguments[index + 1], codexDirectory: CommandLine.arguments[index + 2])
        let payload = ["storage": "valid", "data_dir": paths.data, "codex_dir": paths.codex]
        let json = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        print(String(data: json, encoding: .utf8)!)
        exit(0)
    } catch {
        fputs(error.localizedDescription + "\n", stderr)
        exit(2)
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.run()
