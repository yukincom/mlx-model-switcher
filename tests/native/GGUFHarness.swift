import Foundation
import Darwin

// The same executable becomes the fixture llama-server after the tests copy it
// into their temporary runtime directory. It records only test-related values,
// never opens a socket, and has a deadline if its parent unexpectedly exits.
private func runFixtureServer() throws {
    let environment = ProcessInfo.processInfo.environment
    guard let capture = environment["MLX_GGUF_FIXTURE_CAPTURE"],
          capture.contains("/mlx-native-gguf-fixture-") else {
        throw ManagerError("fixture capture path required")
    }
    let keys = ["GGML_ALLOC_FREE_DELAY", "LLAMA_GRAPH_CACHE", "LLAMA_SPEC_MTP_MERGE",
                "LLAMA_SPEC_STOP", "LLAMA_SPEC_STOP_PARAMS", "MLX_GGUF_FIXTURE_SENTINEL"]
    let selected = Dictionary(uniqueKeysWithValues: keys.compactMap { key in environment[key].map { (key, $0) } })
    try writeJSONObject(["argv": CommandLine.arguments, "environment": selected,
                         "cwd": FileManager.default.currentDirectoryPath, "pid": getpid(), "ppid": getppid()],
                        to: URL(fileURLWithPath: capture))
    FileHandle.standardOutput.write(Data("fixture llama-server ready\n".utf8))
    Thread.sleep(forTimeInterval: 15)
}

// Port discovery is deliberately injected as empty. Every remaining observation
// and signal is restricted to a direct child running this fixture executable.
private final class ChildOnlyInspector: ProcessInspecting {
    let binary: URL
    private let system = SystemProcessInspector()
    private(set) var owned = Set<Int32>()
    private(set) var terminated: [Int32] = []
    private(set) var listenerQueries = 0
    var externalPID: Int32?

    init(binary: URL) { self.binary = binary }

    private func isOwnChild(_ pid: Int32) throws -> Bool {
        guard pid > 1 else { return false }
        let parent = try SystemCommand.run("/bin/ps", ["-p", String(pid), "-o", "ppid="])
        guard parent.status == 0, parent.error.isEmpty, Int32(parent.output) == getpid() else { return false }
        let arguments = try system.arguments(pid)
        guard arguments.first == binary.path else { return false }
        owned.insert(pid)
        return true
    }
    func listenerPID() throws -> Int32? {
        listenerQueries += 1
        if let externalPID, try isOwnChild(externalPID) { return externalPID }
        return nil
    }
    func command(_ pid: Int32) throws -> String { try isOwnChild(pid) ? system.command(pid) : "" }
    func started(_ pid: Int32) throws -> String { try isOwnChild(pid) ? system.started(pid) : "" }
    func arguments(_ pid: Int32) throws -> [String] { try isOwnChild(pid) ? system.arguments(pid) : [] }
    func ownerUID(_ pid: Int32) throws -> uid_t? { try isOwnChild(pid) ? system.ownerUID(pid) : nil }
    func allCommands() throws -> String { throw ManagerError("fixture does not inspect unrelated processes") }
    func terminate(_ pid: Int32) throws {
        guard owned.contains(pid), try isOwnChild(pid) else { throw ManagerError("fixture refuses unrelated process") }
        try system.terminate(pid)
        terminated.append(pid)
    }
    func cleanup() {
        for pid in owned where (try? isOwnChild(pid)) == true { try? system.terminate(pid) }
    }
}

@main
struct GGUFHarness {
    static func emit(_ value: [String: Any]) throws {
        FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]))
        FileHandle.standardOutput.write(Data("\n".utf8))
    }

    static func main() throws {
        if URL(fileURLWithPath: CommandLine.arguments[0]).lastPathComponent == "llama-server" {
            try runFixtureServer()
            return
        }
        guard let request = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as? [String: Any],
              let rootPath = request["root"] as? String, let alias = request["alias"] as? String,
              let thinking = strictBool(request["thinking"]) else { throw ManagerError("fixture request required") }
        let root = URL(fileURLWithPath: rootPath)
        guard root.lastPathComponent.hasPrefix("mlx-native-gguf-fixture-") else { throw ManagerError("temporary fixture required") }
        let configuration = ManagerConfiguration(serverRoot: root.appendingPathComponent("server"),
            stateRoot: root.appendingPathComponent("state"), ggufRoot: root.appendingPathComponent("gguf"),
            cacheRoot: root.appendingPathComponent("cache"))
        let fixture = try readJSONObject(configuration.ggufRoot.appendingPathComponent("profiles.json"))
        guard fixture["binary"] as? String == "runtime/bin/llama-server",
              let weights = fixture["models_root"] as? String, weights.hasPrefix(root.path + "/") else {
            throw ManagerError("fixture runtime and weight paths required")
        }
        let captureURL = root.appendingPathComponent("captured.json")
        guard ProcessInfo.processInfo.environment["MLX_GGUF_FIXTURE_CAPTURE"] == captureURL.path else {
            throw ManagerError("fixture capture environment required")
        }
        let binary = configuration.ggufRoot.appendingPathComponent("runtime/bin/llama-server")
        let inspector = ChildOnlyInspector(binary: binary)
        defer { inspector.cleanup() }
        let store = ModelStore(configuration: configuration)
        let controller = ProcessController(configuration: configuration, store: store, inspector: inspector, readiness: { false })
        do {
            let pid = try controller.start(alias: alias, thinking: thinking)
            let deadline = Date().addingTimeInterval(3)
            while !FileManager.default.fileExists(atPath: captureURL.path), Date() < deadline {
                Thread.sleep(forTimeInterval: 0.01)
            }
            let captured = try readJSONObject(captureURL)
            let record = try readJSONObject(configuration.ggufRoot.appendingPathComponent("evidence/llama-server.json"))
            let managed = try readJSONObject(configuration.ggufRoot.appendingPathComponent("evidence/managed-server.json"))
            if request["external"] as? Bool == true {
                // Listener lookup and readiness are injected; all identity reads,
                // SIGTERM and exit observation target this real fixture child.
                inspector.externalPID = pid
                try Data("{broken".utf8).write(to: configuration.pendingURL)
                try writeJSONObject(["pid": pid + 1], to: configuration.ggufRoot.appendingPathComponent("evidence/llama-server.json"))
            }
            let running = try controller.status()
            try controller.stop(expectedPID: pid, expectedCommand: running["command"] as! String,
                                expectedStarted: running["started"] as! String)
            let stopped = try controller.status()
            try emit(["ok": true, "pid": pid, "harness_pid": getpid(), "capture": captured,
                      "running": running, "stopped": stopped, "record": record, "managed_record": managed,
                      "terminated": inspector.terminated, "owned": inspector.owned.sorted(),
                      "listener_queries": inspector.listenerQueries])
        } catch {
            try emit(["ok": false, "error": error.localizedDescription,
                      "terminated": inspector.terminated, "owned": inspector.owned.sorted(),
                      "captured": FileManager.default.fileExists(atPath: captureURL.path)])
        }
    }
}
