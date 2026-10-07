import AppKit
import WebKit

final class MonitorWindowController: NSWindowController, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {
    let webView: WKWebView
    private(set) var endpoint: BackendEndpoint?
    var onDocumentReady: (() -> Void)?

    init() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        webView = WKWebView(frame: .zero, configuration: configuration)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 850),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Codex DAG"
        window.identifier = NSUserInterfaceItemIdentifier("codex-dag-monitor")
        window.isReleasedWhenClosed = false
        window.hidesOnDeactivate = false
        window.minSize = NSSize(width: 960, height: 620)
        window.setFrameAutosaveName("CodexDAGMonitorWindow")
        window.center()
        window.contentView = webView
        window.delegate = self
        // The monitor page follows the system light/dark appearance.
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setAccessibilityLabel("Codex DAG 실시간 모니터")
        webView.setAccessibilityIdentifier("codex-dag-webview")
        if #available(macOS 13.3, *) { webView.isInspectable = CommandLine.arguments.contains("--inspect-webview") }
        displayMessage(title: "Codex DAG", detail: "메뉴바에서 수집기를 시작하세요. 프로젝트와 세션은 준비된 모니터에서 선택할 수 있습니다.")
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is unsupported") }

    func open() {
        // A visible monitor is a normal app window, including Dock and Cmd+Tab.
        NSApp.setActivationPolicy(.regular)
        if window?.isMiniaturized == true { window?.deminiaturize(nil) }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func load(_ endpoint: BackendEndpoint) {
        if self.endpoint?.pid == endpoint.pid, self.endpoint?.url == endpoint.url { return }
        self.endpoint = endpoint
        webView.load(URLRequest(url: endpoint.webURL, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    func displayMessage(title: String, detail: String) {
        endpoint = nil
        webView.stopLoading()
        func escape(_ text: String) -> String {
            text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
        }
        webView.loadHTMLString("""
        <!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta name="color-scheme" content="light dark"><title>Codex DAG</title>
        <style>:root{color-scheme:light dark;--bg:#f1eee7;--card:#fbfaf6;--line:#e2dccf;--text:#1b2220;--muted:#6c7672;--accent:#0d7a69}@media(prefers-color-scheme:dark){:root{--bg:#0b0f11;--card:#12191c;--line:#222b30;--text:#e7edeb;--muted:#8b9894;--accent:#45c7ab}}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font-family:"Apple SD Gothic Neo",-apple-system,BlinkMacSystemFont,sans-serif}main{max-width:560px;margin:24px;padding:32px 36px;border:1px solid var(--line);border-radius:18px;background:var(--card)}small{color:var(--accent);font-weight:700}h1{margin:10px 0 8px;font:600 28px/1.3 "Avenir Next","Apple SD Gothic Neo",sans-serif}p{margin:0;font-size:15px;line-height:1.75;color:var(--muted);white-space:pre-wrap}</style></head>
        <body><main><small>Codex DAG</small><h1>\(escape(title))</h1><p>\(escape(detail))</p></main></body></html>
        """, baseURL: nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.absoluteString == "about:blank", endpoint == nil { decisionHandler(.allow); return }
        guard let endpoint, endpoint.owns(url), navigationAction.targetFrame != nil else { decisionHandler(.cancel); return }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard let url = navigationResponse.response.url else { decisionHandler(.cancel); return }
        if url.absoluteString == "about:blank", endpoint == nil { decisionHandler(.allow); return }
        decisionHandler(endpoint?.owns(url) == true ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if let url = webView.url, endpoint?.owns(url) == true { onDocumentReady?() }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        displayMessage(title: "모니터 페이지를 열지 못했습니다", detail: error.localizedDescription + "\n메뉴바에서 수집기를 다시 시작하세요.")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if let endpoint { webView.load(URLRequest(url: endpoint.webURL)) }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? { nil }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        guard let window else { completionHandler(); return }
        let alert = NSAlert()
        alert.messageText = "Codex DAG"
        alert.informativeText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    // Closing returns to the menu bar, preserving this window, its WebView and the owned collector.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        NSApp.setActivationPolicy(.accessory)
        return false
    }
}
