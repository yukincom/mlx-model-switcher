import Foundation
import Darwin
import CoreFoundation

struct ManagerError: Error, LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

struct ManagerConfiguration {
    let serverRoot: URL
    let stateRoot: URL
    let ggufRoot: URL
    let cacheRoot: URL
    var modelsConfig: URL { serverRoot.appendingPathComponent("models.conf") }
    var settingsURL: URL { stateRoot.appendingPathComponent("models.json") }
    var pendingURL: URL { stateRoot.appendingPathComponent("starting.json") }
    var logURL: URL { stateRoot.appendingPathComponent("mlx-server.log") }

    static func environment(_ values: [String: String] = ProcessInfo.processInfo.environment) -> Self {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        func path(_ value: String) -> URL {
            URL(fileURLWithPath: (value as NSString).expandingTildeInPath)
        }
        let state = values["MLX_MANAGER_STATE_ROOT"] ?? home + "/Library/Application Support/MLX Server Manager"
        let cache = values["HF_HUB_CACHE"]
            ?? values["HF_HOME"].map { $0 + "/hub" }
            ?? values["XDG_CACHE_HOME"].map { $0 + "/huggingface/hub" }
            ?? home + "/.cache/huggingface/hub"
        return Self(serverRoot: path(values["MLX_ACTIVATION_LAB_SERVER_ROOT"] ?? home + "/mlx-server"),
                    stateRoot: path(state), ggufRoot: path(values["MLX_MANAGER_GGUF_ROOT"] ?? state + "/gguf"),
                    cacheRoot: path(cache))
    }
}

final class OperationFileLock {
    private let descriptor: Int32
    init(_ path: URL, nonblocking: Bool = false) throws {
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        descriptor = Darwin.open(path.path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard descriptor >= 0 else { throw ManagerError("操作ロックを作成できません: \(path.lastPathComponent)") }
        guard flock(descriptor, LOCK_EX | (nonblocking ? LOCK_NB : 0)) == 0 else {
            close(descriptor)
            throw ManagerError("別のモデル操作が進行中です")
        }
    }
    deinit { flock(descriptor, LOCK_UN); close(descriptor) }
}

struct CommandResult {
    let status: Int32
    let output: String
    let error: String
}

enum SystemCommand {
    // Queries run as argument arrays, with bounded lifetime and concurrently drained pipes.
    // No shell interpolation or model-provided executable is involved in process inspection.
    static func run(_ executable: String, _ arguments: [String], timeout: TimeInterval = 5) throws -> CommandResult {
        let process = Process(), stdout = Pipe(), stderr = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdout
        process.standardError = stderr
        let completed = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in completed.signal() }
        try process.run()
        let readers = DispatchGroup(), lock = NSLock()
        var out = Data(), err = Data()
        readers.enter()
        DispatchQueue.global().async {
            let data = stdout.fileHandleForReading.readDataToEndOfFile()
            lock.lock(); out = data; lock.unlock(); readers.leave()
        }
        readers.enter()
        DispatchQueue.global().async {
            let data = stderr.fileHandleForReading.readDataToEndOfFile()
            lock.lock(); err = data; lock.unlock(); readers.leave()
        }
        guard completed.wait(timeout: .now() + timeout) == .success else {
            process.terminate()
            _ = completed.wait(timeout: .now() + 1)
            throw ManagerError("プロセスの確認が時間内に終わりませんでした")
        }
        guard readers.wait(timeout: .now() + 1) == .success else { throw ManagerError("確認結果を読み取れません") }
        lock.lock(); defer { lock.unlock() }
        return CommandResult(status: process.terminationStatus,
                             output: String(decoding: out, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines),
                             error: String(decoding: err, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))
    }
}

protocol ProcessInspecting {
    func listenerPID() throws -> Int32?
    func command(_ pid: Int32) throws -> String
    func started(_ pid: Int32) throws -> String
    func arguments(_ pid: Int32) throws -> [String]
    func allCommands() throws -> String
    func terminate(_ pid: Int32) throws
}

final class SystemProcessInspector: ProcessInspecting {
    func listenerPID() throws -> Int32? {
        let result = try SystemCommand.run("/usr/sbin/lsof", ["-nP", "-iTCP:8080", "-sTCP:LISTEN", "-t"])
        let lines = Set(result.output.split(whereSeparator: \.isNewline).map(String.init))
        guard [0, 1].contains(result.status), result.error.isEmpty, lines.count <= 1,
              lines.allSatisfy({ Int32($0).map { $0 > 0 } == true }) else {
            throw ManagerError("8080の使用状況を確認できないため、操作しません")
        }
        return lines.first.flatMap(Int32.init)
    }
    func command(_ pid: Int32) throws -> String {
        let result = try SystemCommand.run("/bin/ps", ["-ww", "-p", String(pid), "-o", "command="])
        guard [0, 1].contains(result.status), result.error.isEmpty else { throw ManagerError("起動コマンドを確認できません") }
        return result.output
    }
    func started(_ pid: Int32) throws -> String {
        let result = try SystemCommand.run("/bin/ps", ["-p", String(pid), "-o", "lstart="])
        guard [0, 1].contains(result.status), result.error.isEmpty else { throw ManagerError("プロセスの開始時刻を確認できません") }
        return result.output
    }
    func arguments(_ pid: Int32) throws -> [String] {
        var mib = [CTL_KERN, KERN_PROCARGS2, pid]
        var length = 0
        guard sysctl(&mib, UInt32(mib.count), nil, &length, nil, 0) == 0, length > 4, length < 4_194_304 else {
            if errno == ESRCH { return [] }
            throw ManagerError("プロセスの引数を確認できません")
        }
        var bytes = [UInt8](repeating: 0, count: length)
        guard sysctl(&mib, UInt32(mib.count), &bytes, &length, nil, 0) == 0 else {
            if errno == ESRCH { return [] }
            throw ManagerError("プロセスの引数を確認できません")
        }
        let count = bytes.withUnsafeBytes { $0.loadUnaligned(as: Int32.self) }
        guard count > 0, count < 65536 else { throw ManagerError("プロセスの引数が不正です") }
        var offset = MemoryLayout<Int32>.size
        while offset < length, bytes[offset] != 0 { offset += 1 } // kernel executable path
        while offset < length, bytes[offset] == 0 { offset += 1 }
        var values: [String] = []
        for _ in 0..<count {
            let begin = offset
            while offset < length, bytes[offset] != 0 { offset += 1 }
            guard offset < length, let value = String(bytes: bytes[begin..<offset], encoding: .utf8) else {
                throw ManagerError("プロセスの引数を読み取れません")
            }
            values.append(value); offset += 1
        }
        return values
    }
    func allCommands() throws -> String {
        let result = try SystemCommand.run("/bin/ps", ["-A", "-ww", "-o", "command="])
        guard result.status == 0, !result.output.isEmpty, result.error.isEmpty else {
            throw ManagerError("実験ラボの使用状況を確認できないため、本体を削除しません")
        }
        return result.output
    }
    func terminate(_ pid: Int32) throws {
        guard Darwin.kill(pid, SIGTERM) == 0 || errno == ESRCH else { throw ManagerError("対象プロセスを停止できません") }
    }
}

func strictBool(_ value: Any?) -> Bool? {
    guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
    return number.boolValue
}

func readJSONObject(_ url: URL) throws -> [String: Any] {
    guard let value = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any] else {
        throw ManagerError("JSON objectが必要です: \(url.lastPathComponent)")
    }
    return value
}

func writeJSONObject(_ value: [String: Any], to url: URL) throws {
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]).write(to: url, options: .atomic)
}

func flagValue(_ arguments: [String], _ names: Set<String>) -> String? {
    for (index, value) in arguments.enumerated() {
        if names.contains(value), index + 1 < arguments.count { return arguments[index + 1] }
        if let split = value.firstIndex(of: "="), names.contains(String(value[..<split])) {
            return String(value[value.index(after: split)...])
        }
    }
    return nil
}

final class DetachedLauncher {
    private var exits: [Int32: DispatchSourceProcess] = [:]
    private let queue = DispatchQueue(label: "mlx.manager.children")

    // An unrecorded launch must not leave an unmanaged inference server behind.
    // Only an unreaped direct child can be in this map, so its PID cannot be reused.
    func discard(_ pid: Int32) {
        queue.sync {
            guard exits[pid] != nil else { return }
            _ = Darwin.kill(pid, SIGKILL)
        }
    }

    func launch(executable: URL, arguments: [String], cwd: URL, environment: [String: String], log: URL) throws -> Int32 {
        try FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
        var actions: posix_spawn_file_actions_t?, attributes: posix_spawnattr_t?
        guard posix_spawn_file_actions_init(&actions) == 0 else { throw ManagerError("起動設定を作成できません") }
        defer { posix_spawn_file_actions_destroy(&actions) }
        guard posix_spawnattr_init(&attributes) == 0 else { throw ManagerError("起動属性を作成できません") }
        defer { posix_spawnattr_destroy(&attributes) }
        func checked(_ result: Int32) throws { if result != 0 { throw ManagerError("起動設定に失敗しました: \(String(cString: strerror(result)))") } }
        try checked(posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETSID | POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK)))
        var defaults = sigset_t(), mask = sigset_t()
        sigemptyset(&defaults); sigaddset(&defaults, SIGTERM); sigaddset(&defaults, SIGINT); sigaddset(&defaults, SIGHUP)
        sigemptyset(&mask)
        try checked(posix_spawnattr_setsigdefault(&attributes, &defaults))
        try checked(posix_spawnattr_setsigmask(&attributes, &mask))
        try checked(posix_spawn_file_actions_addchdir_np(&actions, cwd.path))
        try checked(posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0))
        try checked(posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, log.path, O_CREAT | O_WRONLY | O_APPEND, 0o600))
        try checked(posix_spawn_file_actions_adddup2(&actions, STDOUT_FILENO, STDERR_FILENO))
        var argv = ([executable.path] + arguments).map { strdup($0) } + [nil]
        var env = environment.sorted(by: { $0.key < $1.key }).map { strdup($0.key + "=" + $0.value) } + [nil]
        defer { argv.forEach { free($0) }; env.forEach { free($0) } }
        var pid: pid_t = 0
        try checked(posix_spawn(&pid, executable.path, &actions, &attributes, &argv, &env))
        // Reap only our detached children. They continue if the manager exits first.
        queue.sync {
            let source = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: queue)
            source.setEventHandler { [weak self] in
                var status: Int32 = 0
                _ = waitpid(pid, &status, WNOHANG)
                self?.exits.removeValue(forKey: pid)?.cancel()
            }
            exits[pid] = source
            source.resume()
        }
        return pid
    }
}

final class ProcessController {
    let configuration: ManagerConfiguration
    let store: ModelStore
    let inspector: ProcessInspecting
    private let launcher = DetachedLauncher()
    let executable: String

    init(configuration: ManagerConfiguration, store: ModelStore,
         inspector: ProcessInspecting = SystemProcessInspector(), executable: String = CommandLine.arguments[0]) {
        self.configuration = configuration
        self.store = store
        self.inspector = inspector
        self.executable = executable
    }

    static func recordStart(configuration: ManagerConfiguration, pid: Int32, alias: String, thinking: Bool) throws {
        guard pid > 1, !alias.isEmpty else { throw ManagerError("起動記録のPIDとaliasを確認してください") }
        let started = try SystemProcessInspector().started(pid)
        guard !started.isEmpty else { throw ManagerError("起動プロセスの記録を確認できません") }
        try writeJSONObject(["pid": pid, "started": started, "alias": alias, "thinking": thinking], to: configuration.pendingURL)
    }

    private func record(_ url: URL) throws -> [String: Any]? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return try readJSONObject(url)
    }

    private func sameProcess(_ record: [String: Any], pid: Int32, command: String) throws -> Bool {
        guard (record["pid"] as? NSNumber)?.int32Value == pid else { return false }
        let started = try inspector.started(pid)
        if let identity = record["identity"] as? String {
            return !started.isEmpty && identity == started + " " + command
        }
        return !started.isEmpty && record["started"] as? String == started
    }

    private func looksLikeMLX(_ arguments: [String]) -> Bool {
        let root = configuration.serverRoot.path
        let names = arguments.map { URL(fileURLWithPath: $0).lastPathComponent }
        return flagValue(arguments, ["--port"]) == "8080" &&
            (arguments.contains("mlx_lm.server") || names.contains("mlx_lm.server") ||
             arguments.contains(root + "/local_server_entry") || names.contains("mlx_priority_server.py"))
    }

    private func thinking(_ arguments: [String]) -> Any {
        if let value = flagValue(arguments, ["--reasoning", "-rea"]), ["on", "off"].contains(value) { return value == "on" }
        if let text = flagValue(arguments, ["--chat-template-args"]), let data = text.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let value = strictBool(object["enable_thinking"]) { return value }
        return NSNull()
    }

    private func ggufBinary() throws -> URL? {
        guard let config = try store.ggufConfiguration(), let relative = config["binary"] as? String,
              !relative.isEmpty, !relative.hasPrefix("/"), !relative.split(separator: "/").contains("..") else { return nil }
        return configuration.ggufRoot.appendingPathComponent(relative)
    }

    func status() throws -> [String: Any] {
        let listener = try inspector.listenerPID()
        let ggufRecord = try record(configuration.ggufRoot.appendingPathComponent("evidence/llama-server.json"))
        let mlxRecord = try record(configuration.pendingURL)
        var pid = listener
        if pid == nil {
            for candidate in [ggufRecord, mlxRecord].compactMap({ $0 }) {
                guard let number = candidate["pid"] as? NSNumber, number.int32Value > 1 else { continue }
                let command = try inspector.command(number.int32Value)
                if !command.isEmpty, try sameProcess(candidate, pid: number.int32Value, command: command) {
                    pid = number.int32Value; break
                }
            }
        }
        var value: [String: Any] = ["running": false, "pid": NSNull(), "trusted": false,
                                     "command": "", "active_model": "", "thinking": NSNull()]
        if let pid {
            let command = try inspector.command(pid)
            let arguments = try inspector.arguments(pid)
            // A process exiting between snapshots is stopped, never an inferred trusted PID.
            if !command.isEmpty, !arguments.isEmpty {
                let llama = URL(fileURLWithPath: arguments[0]).lastPathComponent == "llama-server"
                var model = flagValue(arguments, llama ? ["-m", "--model"] : ["--model"]) ?? ""
                var trusted = false
                var launchThinking = thinking(arguments)
                if llama, let ggufRecord, try sameProcess(ggufRecord, pid: pid, command: command),
                   let binary = try ggufBinary(), arguments[0] == binary.path,
                   let target = ggufRecord["target"] as? String,
                   let profile = try store.ggufProfiles().first(where: { $0["target"] as? String == target }),
                   (ggufRecord["argv"] as? [String]) == arguments {
                    model = profile["model"] as? String ?? model
                    trusted = true
                    launchThinking = strictBool(ggufRecord["thinking"]) as Any? ?? launchThinking
                } else if let mlxRecord, try sameProcess(mlxRecord, pid: pid, command: command),
                          let alias = mlxRecord["alias"] as? String,
                          let item = try store.aliases().first(where: { $0["alias"] == alias }) {
                    let bootingScript = arguments.contains(configuration.serverRoot.appendingPathComponent("start_server.sh").path)
                    if looksLikeMLX(arguments) || bootingScript {
                        trusted = true
                        if model.isEmpty { model = item["model"] ?? "" }
                        launchThinking = strictBool(mlxRecord["thinking"]) as Any? ?? launchThinking
                    }
                }
                value = ["running": true, "pid": pid, "trusted": trusted, "command": command,
                         "arguments": arguments, "started": try inspector.started(pid),
                         "active_model": model, "format": llama ? "GGUF" : "MLX", "thinking": launchThinking]
                if listener == nil || (llama && !inferenceReady()) { value["loading"] = true }
            }
        }
        value["names_version"] = store.namesVersion()
        value["active_name"] = try (value["active_model"] as? String).flatMap { $0.isEmpty ? nil : $0 }.map(store.displayName) ?? ""
        return value
    }

    private func inferenceReady() -> Bool {
        // One health check per actual state snapshot; events trigger this, never a timer.
        var request = URLRequest(url: URL(string: "http://127.0.0.1:8080/health")!)
        request.timeoutInterval = 1
        let completed = DispatchSemaphore(value: 0), lock = NSLock()
        var ready = false
        let task = URLSession.shared.dataTask(with: request) { _, response, _ in
            lock.lock(); ready = (response as? HTTPURLResponse)?.statusCode == 200; lock.unlock()
            completed.signal()
        }
        task.resume()
        if completed.wait(timeout: .now() + 1.2) == .timedOut { task.cancel(); return false }
        lock.lock(); defer { lock.unlock() }; return ready
    }

    func ensureLabIdle() throws {
        let commands = try inspector.allCommands()
        let expressions = [#"(?:^|\s)-m\s+mlx_activation_lab(?:\.webapp(?:\s|$)|\s+(?:app|run|capture|inspect|steer|wizard)(?:\s|$))"#,
                           #"(?:^|[\s/])mlx-activation-lab\s+(?:app|run|capture|inspect|steer|wizard)(?:\s|$)"#]
        if expressions.contains(where: { commands.range(of: $0, options: .regularExpression) != nil }) {
            throw ManagerError("実験ラボと実行中の実験を終了してから、本体を削除してください")
        }
    }

    func start(alias: String, thinking: Bool) throws -> Int32 {
        guard try status()["running"] as? Bool != true, try inspector.listenerPID() == nil else {
            throw ManagerError("8080はすでに稼働中です。切り替える場合は先に停止してください")
        }
        if let profile = try store.ggufProfiles().first(where: { $0["alias"] as? String == alias }) {
            return try startGGUF(profile, thinking: thinking)
        }
        guard try store.aliases().contains(where: { $0["alias"] == alias }) else { throw ManagerError("未登録のエイリアスです: \(alias)") }
        var environment = ProcessInfo.processInfo.environment
        environment.merge(["MLX_THINKING": thinking ? "on" : "off", "MLX_PORT": "8080", "MLX_HOST": "127.0.0.1",
                           "MLX_ALLOW_REMOTE": "0", "MLX_SERVER_DIR": configuration.serverRoot.path,
                           "MLX_MODELS_CONFIG": configuration.modelsConfig.path,
                           "MLX_MANAGER_STATE_ROOT": configuration.stateRoot.path,
                           "MLX_MANAGER_EXECUTABLE": URL(fileURLWithPath: executable).standardizedFileURL.path,
                           "MLX_MANAGER_LOGGED": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"], uniquingKeysWith: { _, new in new })
        let pid = try launcher.launch(executable: configuration.serverRoot.appendingPathComponent("start_server.sh"),
                                      arguments: [alias], cwd: configuration.serverRoot, environment: environment, log: configuration.logURL)
        // Record immediately as well as the exec-preserving CLI hook, so the pending
        // child is visible even while Bash validates a large local cache.
        do { try Self.recordStart(configuration: configuration, pid: pid, alias: alias, thinking: thinking) }
        catch { launcher.discard(pid); throw error }
        return pid
    }

    private func startGGUF(_ profile: [String: Any], thinking: Bool) throws -> Int32 {
        let lock = try OperationFileLock(configuration.ggufRoot.appendingPathComponent("evidence/switch.lock"), nonblocking: true)
        defer { withExtendedLifetime(lock) {} }
        guard let alias = profile["alias"] as? String,
              try store.models().contains(where: { $0["alias"] as? String == alias }),
              let manifest = profile["manifest"] as? String,
              let binary = try ggufBinary(), FileManager.default.isExecutableFile(atPath: binary.path),
              let target = profile["target"] as? String, let model = profile["model"] as? String,
              let main = profile["model_file"] as? String,
              let configuredArguments = profile["arguments"] as? [String],
              let verifications = profile["required_verifications"] as? [String] else {
            throw ManagerError("GGUFの起動設定または実行ファイルを確認できません")
        }
        for relative in verifications {
            guard !relative.hasPrefix("/"), !relative.split(separator: "/").contains(".."),
                  FileManager.default.fileExists(atPath: configuration.ggufRoot.appendingPathComponent(relative).path) else {
                throw ManagerError("GGUFの重み・画像・先読みモデルの検証記録を確認できません")
            }
        }
        let files = try store.ggufFiles(alias: alias)
        let entries = try readJSONObject(configuration.ggufRoot.appendingPathComponent(manifest))["files"] as? [[String: Any]] ?? []
        guard !entries.isEmpty, entries.count == files.count else { throw ManagerError("GGUFのモデルファイルが不足しています") }
        let root = try store.ggufModelsRoot()
        for item in entries {
            guard let directory = item["directory"] as? String, let file = item["file"] as? String,
                  let bytes = item["bytes"] as? NSNumber else { throw ManagerError("GGUF manifestを確認できません") }
            let path = root.appendingPathComponent(directory).appendingPathComponent(file)
            let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
            guard (attributes[.size] as? NSNumber)?.int64Value == bytes.int64Value else { throw ManagerError("モデルファイルのサイズが一致しません: \(file)") }
        }
        func modelFile(_ name: String) throws -> String {
            let matches = files.filter { $0.lastPathComponent == name }
            guard matches.count == 1 else { throw ManagerError("GGUFの起動ファイルが一意に決まりません: \(name)") }
            return matches[0].path
        }
        var arguments = ["-m", try modelFile(main)]
        let reserved = Set(["-m", "--model", "--host", "--port", "--alias", "--reasoning", "-rea"])
        for value in configuredArguments {
            guard !reserved.contains(value.components(separatedBy: "=")[0]) else { throw ManagerError("GGUFの管理用起動引数は上書きできません") }
            if value.hasPrefix("{model:"), value.hasSuffix("}") {
                arguments.append(try modelFile(String(value.dropFirst(7).dropLast())))
            } else {
                guard !value.contains("{model:") else { throw ManagerError("GGUFのモデル引数を確認できません") }
                arguments.append(value)
            }
        }
        arguments += ["--reasoning", thinking ? "on" : "off", "--host", "127.0.0.1", "--port", "8080", "--alias", "default_model"]
        var environment = ProcessInfo.processInfo.environment
        for name in profile["unset_environment"] as? [String] ?? [] { environment.removeValue(forKey: name) }
        for (key, value) in profile["environment"] as? [String: String] ?? [:] { environment[key] = value }
        guard try inspector.listenerPID() == nil, try status()["running"] as? Bool != true else { throw ManagerError("別のモデルが起動中です") }
        let pid = try launcher.launch(executable: binary, arguments: arguments, cwd: configuration.ggufRoot,
                                      environment: environment, log: configuration.ggufRoot.appendingPathComponent("logs/llama-server.log"))
        do {
            let started = try inspector.started(pid), command = try inspector.command(pid)
            guard !started.isEmpty, !command.isEmpty else { throw ManagerError("GGUFサーバーが起動直後に終了しました。起動ログを確認してください") }
            let record: [String: Any] = ["pid": pid, "identity": started + " " + command, "argv": [binary.path] + arguments,
                                         "target": target, "alias": alias, "model": model, "thinking": thinking]
            try writeJSONObject(record, to: configuration.ggufRoot.appendingPathComponent("evidence/llama-server.json"))
            try writeJSONObject(record, to: configuration.ggufRoot.appendingPathComponent("evidence/managed-server.json"))
        } catch { launcher.discard(pid); throw error }
        return pid
    }

    func stop(expectedPID: Int32, expectedCommand: String) throws {
        let current = try status()
        guard current["running"] as? Bool == true else { return }
        guard (current["pid"] as? NSNumber)?.int32Value == expectedPID, current["command"] as? String == expectedCommand else {
            throw ManagerError("8080のプロセスが表示後に切り替わったため、停止しません")
        }
        guard current["trusted"] as? Bool == true, let started = current["started"] as? String, !started.isEmpty else {
            throw ManagerError("既知の起動記録と一致しないため、停止しません")
        }
        let listener = try inspector.listenerPID()
        guard (listener == nil || listener == expectedPID), try inspector.started(expectedPID) == started,
              try inspector.command(expectedPID) == expectedCommand else { throw ManagerError("停止直前に対象プロセスが変わりました") }
        let completion = DispatchSemaphore(value: 0)
        let exited = DispatchSource.makeProcessSource(identifier: expectedPID, eventMask: .exit, queue: .global())
        exited.setEventHandler { completion.signal() }
        exited.resume()
        defer { exited.cancel() }
        try inspector.terminate(expectedPID)
        guard completion.wait(timeout: .now() + 20) == .success else {
            throw ManagerError("サーバーはまだ終了中です。強制終了は行いません")
        }
    }
}

final class ModelOperations {
    let configuration: ManagerConfiguration
    let store: ModelStore
    let processes: ProcessController
    init(configuration: ManagerConfiguration, executable: String = CommandLine.arguments[0]) {
        self.configuration = configuration
        store = ModelStore(configuration: configuration)
        processes = ProcessController(configuration: configuration, store: store, executable: executable)
        store.currentServer = { [unowned processes] in try processes.status() }
        store.listenerPID = { [unowned processes] in try processes.inspector.listenerPID() }
        store.ensureLabIdle = { [unowned processes] in try processes.ensureLabIdle() }
    }

    func recentLog(_ server: [String: Any]) throws -> [String] {
        let path = server["format"] as? String == "GGUF"
            ? configuration.ggufRoot.appendingPathComponent("logs/llama-server.log") : configuration.logURL
        guard FileManager.default.fileExists(atPath: path.path) else { return [] }
        let handle = try FileHandle(forReadingFrom: path)
        defer { try? handle.close() }
        let size = try handle.seekToEnd()
        try handle.seek(toOffset: size > 16384 ? size - 16384 : 0)
        return String(decoding: try handle.readToEnd() ?? Data(), as: UTF8.self).split(whereSeparator: \.isNewline).suffix(35).map(String.init)
    }

    func dispatch(method: String, path: String, payload: [String: Any]) throws -> [String: Any] {
        let lock = try OperationFileLock(configuration.stateRoot.appendingPathComponent("operations.lock"))
        defer { withExtendedLifetime(lock) {} }
        if method == "GET", path == "/api/cached-models" {
            let models = try store.cachedModels()
            return ["models": models, "names": try Dictionary(uniqueKeysWithValues: models.map { ($0, try store.displayName($0)) })]
        }
        if method == "GET", path == "/api/status" {
            let items = try store.models(), server = try processes.status(), profiles = try store.ggufProfiles()
            let visible = Set(items.compactMap { $0["alias"] as? String })
            return ["app": "mlx-server-manager", "server": server, "aliases": try store.aliases(), "models": items,
                    "display_names": try store.settings()["display_names"] ?? [:], "default_alias": try store.defaultAlias(items),
                    "gguf_available": try store.ggufConfiguration() != nil,
                    "gguf_restore_available": profiles.contains { !visible.contains($0["alias"] as? String ?? "") &&
                        FileManager.default.fileExists(atPath: configuration.ggufRoot.appendingPathComponent($0["manifest"] as? String ?? "").path) },
                    "log": try recentLog(server)]
        }
        var result: [String: Any] = [:]
        switch path {
        case "/api/start":
            guard let thinking = payload["thinking"] == nil ? false : strictBool(payload["thinking"]) else { throw ManagerError("ThinkingはON/OFFで指定してください") }
            result = ["pid": try processes.start(alias: payload["alias"] as? String ?? "", thinking: thinking)]
        case "/api/stop":
            guard let pid = payload["pid"] as? NSNumber, CFGetTypeID(pid) != CFBooleanGetTypeID(),
                  pid.doubleValue == Double(pid.int32Value), pid.int32Value > 1,
                  let command = payload["command"] as? String, !command.isEmpty else { throw ManagerError("停止対象のPIDと起動コマンドを指定してください") }
            try processes.stop(expectedPID: pid.int32Value, expectedCommand: command)
            result = try processes.status()
        case "/api/rename": try store.rename(alias: payload["alias"] as? String ?? "", name: payload["name"] as? String ?? "")
        case "/api/default":
            try store.setDefault(alias: payload["alias"] as? String ?? "")
            result = ["default_alias": try store.defaultAlias(store.models())]
        case "/api/gguf/register": try store.restoreGGUF()
        case "/api/register":
            try store.register(alias: payload["alias"] as? String, model: payload["model"] as? String ?? "")
            result = ["aliases": try store.aliases()]
        case "/api/delete-preview", "/api/delete":
            guard let body = payload["body"] == nil ? false : strictBool(payload["body"]) else { throw ManagerError("削除方法を確認してください") }
            let single = payload["aliases"] == nil
            let aliases: [String]
            if single { aliases = [payload["alias"] as? String ?? ""] }
            else { guard let list = payload["aliases"] as? [String] else { throw ManagerError("対象モデルを選択してください") }; aliases = list }
            result = try path == "/api/delete-preview"
                ? store.deletionPreview(aliases: aliases, body: body, single: single)
                : store.deleteModels(aliases: aliases, body: body, fingerprint: payload["fingerprint"] as? String ?? "", single: single)
        default: throw ManagerError("未対応のモデル操作です")
        }
        return ["ok": true, "result": result]
    }
}
