import Foundation
import Darwin

// Process-controller tests supply every OS observation. No fake PID is ever
// signalled, and no test path can start an inference executable.
final class FixtureInspector: ProcessInspecting {
    let fixture: [String: Any]
    var terminated: [Int32] = []
    private var startedIndex = 0

    init(_ fixture: [String: Any]) { self.fixture = fixture }
    private var pid: Int32 { (fixture["pid"] as? NSNumber)?.int32Value ?? 424242 }

    func listenerPID() throws -> Int32? {
        if fixture["listener_error"] as? Bool == true { throw ManagerError("fixture listener unavailable") }
        return (fixture["listener"] as? NSNumber)?.int32Value
    }
    func command(_ candidate: Int32) throws -> String {
        candidate == pid ? fixture["command"] as? String ?? "" : ""
    }
    func started(_ candidate: Int32) throws -> String {
        guard candidate == pid else { return "" }
        if let sequence = fixture["started_sequence"] as? [String], !sequence.isEmpty {
            defer { startedIndex += 1 }
            return sequence[min(startedIndex, sequence.count - 1)]
        }
        return fixture["started"] as? String ?? ""
    }
    func arguments(_ candidate: Int32) throws -> [String] {
        candidate == pid ? fixture["arguments"] as? [String] ?? [] : []
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

    static func main() throws {
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
        let configuration = ManagerConfiguration(serverRoot: base.appendingPathComponent("launcher"),
            stateRoot: base.appendingPathComponent("state"), ggufRoot: base.appendingPathComponent("gguf"),
            cacheRoot: base.appendingPathComponent("cache"))
        if operation == "record-self" {
            try ProcessController.recordStart(configuration: configuration, pid: getpid(),
                alias: request["alias"] as? String ?? "fixture", thinking: strictBool(request["thinking"]) ?? false)
            try emit(["pid": getpid(), "record": readJSONObject(configuration.pendingURL)])
            return
        }
        let inspector = FixtureInspector(request["process"] as? [String: Any] ?? [:])
        let store = ModelStore(configuration: configuration)
        let controller = ProcessController(configuration: configuration, store: store, inspector: inspector)
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
                    expectedCommand: request["expected_command"] as? String ?? "")
                try emit(["unexpected_stop_success": true, "terminated": inspector.terminated])
            default:
                throw ManagerError("unsupported fixture operation")
            }
        } catch {
            try emit(["error": error.localizedDescription, "terminated": inspector.terminated])
        }
    }
}
