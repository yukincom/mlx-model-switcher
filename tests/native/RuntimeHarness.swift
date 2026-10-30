import Foundation
import Darwin

// Process-controller tests supply every OS observation. No fake PID is ever
// signalled, and no test path can start an inference executable.
final class FixtureInspector: ProcessInspecting {
    let fixture: [String: Any]
    var terminated: [Int32] = []
    var startedHook: ((Int32) throws -> String)?
    var commandHook: ((Int32) throws -> String)?
    private var startedIndex = 0
    private var listenerIndex = 0
    private var argumentsIndex = 0
    private var ownerIndex = 0

    init(_ fixture: [String: Any]) { self.fixture = fixture }
    private var pid: Int32 { (fixture["pid"] as? NSNumber)?.int32Value ?? 424242 }

    func listenerPID() throws -> Int32? {
        if fixture["listener_error"] as? Bool == true { throw ManagerError("fixture listener unavailable") }
        if let sequence = fixture["listener_sequence"] as? [Any], !sequence.isEmpty {
            defer { listenerIndex += 1 }
            return (sequence[min(listenerIndex, sequence.count - 1)] as? NSNumber)?.int32Value
        }
        return (fixture["listener"] as? NSNumber)?.int32Value
    }
    func command(_ candidate: Int32) throws -> String {
        if let commandHook { return try commandHook(candidate) }
        return candidate == pid ? fixture["command"] as? String ?? "" : ""
    }
    func started(_ candidate: Int32) throws -> String {
        if let startedHook { return try startedHook(candidate) }
        guard candidate == pid else { return "" }
        if let sequence = fixture["started_sequence"] as? [String], !sequence.isEmpty {
            defer { startedIndex += 1 }
            return sequence[min(startedIndex, sequence.count - 1)]
        }
        return fixture["started"] as? String ?? ""
    }
    func arguments(_ candidate: Int32) throws -> [String] {
        if fixture["arguments_error"] as? Bool == true { throw ManagerError("fixture argv denied") }
        guard candidate == pid else { return [] }
        if let sequence = fixture["arguments_sequence"] as? [[String]], !sequence.isEmpty {
            defer { argumentsIndex += 1 }
            return sequence[min(argumentsIndex, sequence.count - 1)]
        }
        return fixture["arguments"] as? [String] ?? []
    }
    func ownerUID(_ candidate: Int32) throws -> uid_t? {
        guard candidate == pid else { return nil }
        if let sequence = fixture["uid_sequence"] as? [UInt32], !sequence.isEmpty {
            defer { ownerIndex += 1 }
            return sequence[min(ownerIndex, sequence.count - 1)]
        }
        return (fixture["uid"] as? NSNumber)?.uint32Value ?? geteuid()
    }
    func allCommands() throws -> String { fixture["all_commands"] as? String ?? "fixture" }
    func terminate(_ candidate: Int32) throws {
        terminated.append(candidate)
        throw ManagerError("fixture refuses all signals")
    }
}

@main
struct RuntimeHarness {
    static func emit(_ value: Any) throws {
        FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]))
        FileHandle.standardOutput.write(Data("\n".utf8))
    }

    // Do not call waitpid while the child might still be a zombie: that would
    // hide a broken production reaper. ESRCH first proves the reaper ran; only
    // then verify that the child is no longer waitable. A failing test cleans
    // up its own direct child after collecting the failure evidence.
    static func reapingResult(_ pid: Int32) -> [String: Any] {
        let deadline = Date().addingTimeInterval(3)
        var gone = false
        repeat {
            if Darwin.kill(pid, 0) == -1 && errno == ESRCH { gone = true; break }
            Thread.sleep(forTimeInterval: 0.01)
        } while Date() < deadline
        var status: Int32 = 0
        errno = 0
        let waited = waitpid(pid, &status, WNOHANG)
        let waitError = errno
        if !gone && waited == 0 {
            // waitpid confirmed this is still our unreaped direct fixture child.
            _ = Darwin.kill(pid, SIGKILL)
            _ = waitpid(pid, &status, 0)
        }
        return ["gone_before_fixture_wait": gone, "wait_result": waited,
                "wait_errno": waitError, "echild": ECHILD]
    }

    static func detachedFixture(operation: String, base: URL) throws {
        let launcher = DetachedLauncher(), inspector = SystemProcessInspector()
        let log = base.appendingPathComponent("detached.log")
        let context = operation == "detached-context"
        let binary = URL(fileURLWithPath: context ? CommandLine.arguments[0] : "/bin/sleep")
        let arguments = context ? ["fixture-child", "two words 日本語", ""] : ["30"]
        let pid = try launcher.launch(executable: binary, arguments: arguments, cwd: base,
            environment: ["PATH": "/usr/bin:/bin", "MLX_RUNTIME_TEST_VALUE": "fixture environment 日本語"], log: log)
        // Covers thrown assertions/inspection errors without signalling any
        // process other than a child still owned by this launcher.
        defer { launcher.discard(pid) }
        var result: [String: Any]
        do {
            result = ["pid": pid, "arguments": try inspector.arguments(pid), "session": getsid(pid),
                      "process_group": getpgid(pid), "parent_session": getsid(0), "parent_pid": getpid()]
            if context {
                let deadline = Date().addingTimeInterval(3)
                var text = ""
                repeat {
                    text = (try? String(contentsOf: log, encoding: .utf8)) ?? ""
                    if text.contains("fixture stderr\n") { break }
                    Thread.sleep(forTimeInterval: 0.01)
                } while Date() < deadline
                guard let first = text.split(separator: "\n").first,
                      let data = String(first).data(using: .utf8),
                      let child = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw ManagerError("fixture child did not write its context")
                }
                result["child"] = child
                result["stderr_redirected"] = text.contains("fixture stderr\n")
            }
        } catch {
            launcher.discard(pid)
            _ = withExtendedLifetime(launcher) { reapingResult(pid) }
            throw error
        }
        launcher.discard(pid)
        result.merge(withExtendedLifetime(launcher) { reapingResult(pid) }, uniquingKeysWith: { _, new in new })
        // An already reaped child must be a harmless discard, too.
        launcher.discard(pid)
        try emit(result)
    }

    static func failedRecordFixture(configuration: ManagerConfiguration) throws {
        let inspector = FixtureInspector(["listener": NSNull()])
        let store = ModelStore(configuration: configuration)
        let controller = ProcessController(configuration: configuration, store: store, inspector: inspector, readiness: { false })
        let blockedRecord = configuration.ggufRoot.appendingPathComponent("evidence/llama-server.json")
        var launchedPID: Int32?
        inspector.startedHook = { pid in
            // This callback occurs after posix_spawn but before record writing.
            // Creating the directory earlier would fail the initial status read
            // and never exercise cleanup of an actually launched child.
            launchedPID = pid
            try FileManager.default.createDirectory(at: blockedRecord, withIntermediateDirectories: true)
            return try SystemProcessInspector().started(pid)
        }
        inspector.commandHook = { try SystemProcessInspector().command($0) }
        var failure = ""
        do {
            launchedPID = try controller.start(alias: "gguf:failure-fixture", thinking: false)
        } catch { failure = error.localizedDescription }
        guard let pid = launchedPID else { throw ManagerError("fixture failed before launching its child: \(failure)") }
        var isDirectory: ObjCBool = false
        let exists = FileManager.default.fileExists(atPath: blockedRecord.path, isDirectory: &isDirectory)
        var result: [String: Any] = ["pid": pid, "failure": failure,
            "record_is_directory": exists && isDirectory.boolValue, "terminated": inspector.terminated]
        result.merge(withExtendedLifetime(controller) { reapingResult(pid) }, uniquingKeysWith: { _, new in new })
        try emit(result)
    }

    static func main() throws {
        if CommandLine.arguments.dropFirst().first == "fixture-child" {
            try emit(["arguments": CommandLine.arguments, "cwd": FileManager.default.currentDirectoryPath,
                      "environment": ProcessInfo.processInfo.environment["MLX_RUNTIME_TEST_VALUE"] ?? "",
                      "stdin_bytes": FileHandle.standardInput.readDataToEndOfFile().count])
            FileHandle.standardError.write(Data("fixture stderr\n".utf8))
            Thread.sleep(forTimeInterval: 30)
            return
        }
        if CommandLine.arguments.dropFirst().first == "inspect-self" {
            let inspector = SystemProcessInspector()
            try emit(["arguments": inspector.arguments(getpid()), "expected": CommandLine.arguments,
                      "command": inspector.command(getpid()), "started": inspector.started(getpid())])
            return
        }
        guard let request = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as? [String: Any],
              let operation = request["operation"] as? String else { throw ManagerError("fixture request missing") }
        if operation == "strict-bools" {
            try emit((request["values"] as? [Any] ?? []).map { strictBool($0) as Any? ?? NSNull() })
            return
        }
        if operation == "flag" {
            let result = flagValue(request["arguments"] as? [String] ?? [], Set(request["names"] as? [String] ?? []))
            try emit(["value": result as Any? ?? NSNull()])
            return
        }
        guard let root = request["root"] as? String else { throw ManagerError("fixture root missing") }
        let base = URL(fileURLWithPath: root)
        if ["detached-sleep", "detached-context"].contains(operation) {
            try detachedFixture(operation: operation, base: base)
            return
        }
        let configuration = ManagerConfiguration(serverRoot: base.appendingPathComponent("launcher"),
            stateRoot: base.appendingPathComponent("state"), ggufRoot: base.appendingPathComponent("gguf"),
            cacheRoot: base.appendingPathComponent("cache"))
        if operation == "failed-record-cleanup" {
            try failedRecordFixture(configuration: configuration)
            return
        }
        if operation == "record-self" {
            try ProcessController.recordStart(configuration: configuration, pid: getpid(),
                alias: request["alias"] as? String ?? "fixture", thinking: strictBool(request["thinking"]) ?? false)
            try emit(["pid": getpid(), "record": readJSONObject(configuration.pendingURL)])
            return
        }
        let inspector = FixtureInspector(request["process"] as? [String: Any] ?? [:])
        let store = ModelStore(configuration: configuration)
        let controller = ProcessController(configuration: configuration, store: store, inspector: inspector, readiness: { false })
        do {
            switch operation {
            case "status":
                try emit(["server": controller.status(), "terminated": inspector.terminated])
            case "blocked-start":
                // The Python caller always supplies a running fixture. If the guard
                // regresses, start_server.sh does not exist in the fixture directory.
                let pid = try controller.start(alias: "fixture", thinking: false)
                try emit(["unexpected_started_pid": pid, "terminated": inspector.terminated])
            case "blocked-stop":
                try controller.stop(expectedPID: (request["expected_pid"] as? NSNumber)?.int32Value ?? 424242,
                    expectedCommand: request["expected_command"] as? String ?? "",
                    expectedStarted: request["expected_started"] as? String ?? "")
                try emit(["unexpected_stop_success": true, "terminated": inspector.terminated])
            default:
                throw ManagerError("unsupported fixture operation")
            }
        } catch {
            try emit(["error": error.localizedDescription, "terminated": inspector.terminated])
        }
    }
}
