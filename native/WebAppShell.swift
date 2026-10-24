import AppKit
import Foundation
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private var backendLog: FileHandle?
    private var backendOutput: Pipe?
    private var incompatibleBackend = false
    private let configuration = Bundle.main.infoDictionary ?? [:]

    private var appName: String { configuration["CFBundleDisplayName"] as? String ?? "MLX 8080" }
    private var port: Int { configuration["MLXBackendPort"] as? Int ?? 8766 }
    private var expectedApp: String { "mlx-server-manager" }
    private var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    func applicationDidFinishLaunching(_ notification: Notification) {
        makeMenu()
        NSApp.setActivationPolicy(.regular)

        let width: CGFloat = 800
        let height: CGFloat = 840
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        // AppDelegate retains this window for Dock reopen events.
        // AppKit must not release it a second time when the close button is used.
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.title = appName
        window.minSize = NSSize(width: 620, height: 540)
        window.center()

        let webConfiguration = WKWebViewConfiguration()
        webConfiguration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: webConfiguration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        showMessage("起動しています…")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.ensureBackend()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        updateManagerVisibility()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let backend, backend.isRunning { backend.terminate() }
        backendOutput?.fileHandleForReading.readabilityHandler = nil
        try? backendLog?.close()
    }

    // Red close, minimize, hide and full occlusion must disconnect UI updates.
    // The inference server has its own lifetime and continues after this window closes.
    private func updateManagerVisibility(_ forced: Bool? = nil) {
        guard webView != nil else { return }
        let visible = forced ?? (window.isVisible && !window.isMiniaturized && !NSApp.isHidden
            && window.occlusionState.contains(.visible))
        webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('mlx-visibility', {detail: \(visible ? "true" : "false")}));", completionHandler: nil)
    }
    func windowWillClose(_ notification: Notification) { updateManagerVisibility(false) }
    func windowDidMiniaturize(_ notification: Notification) { updateManagerVisibility(false) }
    func windowDidDeminiaturize(_ notification: Notification) { updateManagerVisibility() }
    func windowDidChangeOcclusionState(_ notification: Notification) { updateManagerVisibility() }
    func applicationDidHide(_ notification: Notification) { updateManagerVisibility(false) }
    func applicationDidUnhide(_ notification: Notification) { updateManagerVisibility() }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { updateManagerVisibility() }

    private func makeMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "\(appName) を終了", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        mainMenu.addItem(appItem)

        // Standard responder-chain actions also reach NSTextField editors in alert sheets.
        let editItem = NSMenuItem(title: "編集", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "編集")
        for (title, action, key) in [
            ("切り取り", "cut:", "x"),
            ("コピー", "copy:", "c"),
            ("ペースト", "paste:", "v"),
            ("すべて選択", "selectAll:", "a")
        ] {
            let item = NSMenuItem(title: title, action: NSSelectorFromString(action), keyEquivalent: key)
            item.keyEquivalentModifierMask = .command
            editMenu.addItem(item)
        }
        editItem.submenu = editMenu
        mainMenu.addItem(editItem)
        NSApp.mainMenu = mainMenu
    }

    private func showMessage(_ message: String) {
        DispatchQueue.main.async { [weak self] in
            let escaped = message
                .replacingOccurrences(of: "&", with: "&amp;")
                .replacingOccurrences(of: "<", with: "&lt;")
                .replacingOccurrences(of: ">", with: "&gt;")
            self?.webView.loadHTMLString("""
                <html><meta charset="utf-8"><body style="margin:0;background:#192780;color:#8bfff6;font:19px system-ui;display:grid;place-items:center;height:100vh"><p>\(escaped)</p></body></html>
                """, baseURL: nil)
        }
    }

    private var expectedBackendConfiguration: [String: String] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let state = ProcessInfo.processInfo.environment["MLX_MANAGER_STATE_ROOT"]
            ?? home.appendingPathComponent("Library/Application Support/MLX Server Manager").path
        func canonical(_ path: String) -> String {
            URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath().path
        }
        let environment = ProcessInfo.processInfo.environment
        let cache = environment["HF_HUB_CACHE"] ?? environment["HF_HOME"].map { $0 + "/hub" }
            ?? environment["XDG_CACHE_HOME"].map { $0 + "/huggingface/hub" }
            ?? home.appendingPathComponent(".cache/huggingface/hub").path
        return ["resources": canonical(Bundle.main.resourceURL!.path),
                "cache_root": canonical(cache),
                "state_root": canonical(state),
                "server_root": canonical(configuration["MLXServerRoot"] as? String
                    ?? home.appendingPathComponent("mlx-server").path),
                "gguf_root": canonical(configuration["MLXGGUFRoot"] as? String
                    ?? URL(fileURLWithPath: state).appendingPathComponent("gguf").path)]
    }

    private func backendIsReady() -> Bool {
        let url = baseURL.appendingPathComponent("api/health")
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        let semaphore = DispatchSemaphore(value: 0)
        var ready = false
        URLSession.shared.dataTask(with: request) { data, response, _ in
            if let data,
               (response as? HTTPURLResponse)?.statusCode == 200,
               let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               payload["app"] as? String == self.expectedApp {
                ready = payload["backend"] as? String == "swift" && payload["protocol"] as? Int == 2
                    && payload["configuration"] as? [String: String] == self.expectedBackendConfiguration
                self.incompatibleBackend = !ready
            }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 2)
        return ready
    }

    private func ensureBackend() {
        if backendIsReady() {
            openPage()
            return
        }
        if incompatibleBackend {
            showMessage("別の構成または以前の8080管理アプリが動いています。そちらを終了してから、このアプリを開き直してください。")
            return
        }
        guard let resources = Bundle.main.resourceURL else {
            showMessage("アプリのリソースが見つかりません。")
            return
        }

        let process = Process()
        process.executableURL = Bundle.main.executableURL?.deletingLastPathComponent()
            .appendingPathComponent("MLXServerManager")
        process.arguments = ["--port", String(port), "--resources", resources.path]
        process.currentDirectoryURL = resources
        var environment = ProcessInfo.processInfo.environment
        if let root = configuration["MLXServerRoot"] as? String {
            environment["MLX_ACTIVATION_LAB_SERVER_ROOT"] = root
        }
        if let root = configuration["MLXGGUFRoot"] as? String {
            environment["MLX_MANAGER_GGUF_ROOT"] = root
        } else {
            environment.removeValue(forKey: "MLX_MANAGER_GGUF_ROOT")
        }
        process.environment = environment

        let logPath = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/\(expectedApp)-backend.log")
        _ = FileManager.default.createFile(atPath: logPath.path, contents: nil)
        if let log = try? FileHandle(forWritingTo: logPath) {
            _ = try? log.seekToEnd()
            process.standardOutput = log
            process.standardError = log
            backendLog = log
        }
        // The native child reports readiness once. Do not poll its health endpoint:
        // first launch may be delayed by macOS executable checks before it can bind.
        let readiness = DispatchSemaphore(value: 0)
        let readinessLock = NSLock()
        var received = Data()
        var nativeReady = false
        do {
            let output = Pipe()
            backendOutput = output
            process.standardOutput = output
            output.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                guard !data.isEmpty else { readiness.signal(); return }
                readinessLock.lock()
                received.append(data)
                let complete = received.range(of: Data("MLX_MANAGER_READY\n".utf8)) != nil
                if complete { nativeReady = true }
                readinessLock.unlock()
                if complete { readiness.signal() }
            }
            process.terminationHandler = { _ in readiness.signal() }
        }
        do {
            try process.run()
            backend = process
            try? backendOutput?.fileHandleForWriting.close()
        } catch {
            backendOutput?.fileHandleForReading.readabilityHandler = nil
            showMessage("管理プロセスを起動できません: \(error.localizedDescription)")
            return
        }
        _ = readiness.wait(timeout: .now() + 120)
        backendOutput?.fileHandleForReading.readabilityHandler = nil
        readinessLock.lock()
        let ready = nativeReady
        readinessLock.unlock()
        if ready && process.isRunning { openPage() }
        else { showMessage("管理プロセスの起動を確認できません。ログ: \(logPath.path)") }
    }

    private func openPage() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.webView.load(URLRequest(url: self.baseURL, cachePolicy: .reloadIgnoringLocalCacheData))
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.scheme == "about" || (url.host == "127.0.0.1" && url.port == port) {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "実行")
        alert.addButton(withTitle: "キャンセル")
        alert.beginSheetModal(for: window) { response in
            completionHandler(response == .alertFirstButtonReturn)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "キャンセル")
        let field = NSTextField(string: defaultText ?? "")
        field.frame = NSRect(x: 0, y: 0, width: 360, height: 26)
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        alert.beginSheetModal(for: window) { response in
            completionHandler(response == .alertFirstButtonReturn ? field.stringValue : nil)
        }
        field.selectText(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
