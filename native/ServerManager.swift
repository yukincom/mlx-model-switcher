import Foundation
import Network
import Security
import Darwin

// HTTP, model operations and OS notifications run in this native process.
// Only the selected inference engine is started as a separate process.
struct HTTPFailure: Error {
    let status: Int
    let message: String
}

struct Request {
    let method: String
    let path: String
    let headers: [String: String]
    let body: Data

    static func parse(_ data: Data) throws -> Request? {
        guard let boundary = data.range(of: Data("\r\n\r\n".utf8)) else {
            if data.count > 8192 { throw HTTPFailure(status: 431, message: "headers too large") }
            return nil
        }
        guard boundary.lowerBound <= 8192,
              let head = String(data: data[..<boundary.lowerBound], encoding: .utf8) else {
            throw HTTPFailure(status: 400, message: "invalid headers")
        }
        let lines = head.components(separatedBy: "\r\n")
        let first = lines[0].split(separator: " ", omittingEmptySubsequences: false)
        guard first.count == 3, ["HTTP/1.0", "HTTP/1.1"].contains(String(first[2])),
              first[1].hasPrefix("/"), !first[1].hasPrefix("//") else {
            throw HTTPFailure(status: 400, message: "invalid request line")
        }
        var headers: [String: String] = [:]
        let validName = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~")
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else {
                throw HTTPFailure(status: 400, message: "invalid header")
            }
            let name = String(line[..<colon]).lowercased()
            let value = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty, name.unicodeScalars.allSatisfy({ validName.contains($0) }),
                  !value.contains("\r"), !value.contains("\n"), headers[name] == nil else {
                throw HTTPFailure(status: 400, message: "invalid or duplicate header")
            }
            headers[name] = value
        }
        guard headers["transfer-encoding"] == nil, headers["expect"] == nil else {
            throw HTTPFailure(status: 400, message: "unsupported request framing")
        }
        let textLength = headers["content-length"] ?? "0"
        guard !textLength.isEmpty, textLength.utf8.allSatisfy({ (48...57).contains($0) }),
              let length = Int(textLength), length <= 16_384 else {
            throw HTTPFailure(status: 413, message: "invalid or excessive content length")
        }
        let bodyStart = boundary.upperBound
        guard data.count >= bodyStart + length else { return nil }
        guard data.count == bodyStart + length else {
            throw HTTPFailure(status: 400, message: "unexpected request data")
        }
        return Request(method: String(first[0]),
                       path: String(first[1].split(separator: "?", maxSplits: 1)[0]),
                       headers: headers, body: Data(data[bodyStart...]))
    }
}

struct Response {
    let status: Int
    let mime: String
    let data: Data
    var stream = false
    static func json(_ payload: [String: Any], status: Int = 200) -> Response {
        Response(status: status, mime: "application/json; charset=utf-8",
                 data: (try? JSONSerialization.data(withJSONObject: payload)) ?? Data("{}".utf8))
    }
    static func events() -> Response {
        Response(status: 200, mime: "text/event-stream", data: Data(": connected\n\n".utf8), stream: true)
    }
    static func error(_ status: Int, _ message: String) -> Response {
        .json(["error": message], status: status)
    }
    var wire: Data {
        let labels = [200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                      405: "Method Not Allowed", 413: "Payload Too Large", 431: "Request Header Fields Too Large",
                      500: "Internal Server Error", 503: "Service Unavailable"]
        var result = Data(("HTTP/1.1 \(status) \(labels[status] ?? "Error")\r\n"
            + "Content-Type: \(mime)\r\n" + (stream ? "" : "Content-Length: \(data.count)\r\n")
            + "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
            + "Connection: close\r\n\r\n").utf8)
        result.append(data)
        return result
    }
}

// State changes come from filesystem/process notifications, not a recurring timer.
// Polling while the window is idle or closed would repeatedly scan unchanged model files.
// The short delay below only coalesces a burst of actual filesystem events.
final class StateFiles {
    let paths: [String]
    let queue: DispatchQueue
    let changed: () -> Void
    var sources: [DispatchSourceFileSystemObject] = []
    var signatures: [String: String] = [:]
    var pending: DispatchWorkItem?

    init(paths: [String], queue: DispatchQueue, changed: @escaping () -> Void) {
        self.paths = paths
        self.queue = queue
        self.changed = changed
        signatures = snapshot()
        rebuild()
    }
    func snapshot() -> [String: String] {
        Dictionary(uniqueKeysWithValues: paths.map { path in
            let attributes = try? FileManager.default.attributesOfItem(atPath: path)
            let modified = (attributes?[.modificationDate] as? Date)?.timeIntervalSince1970
            return (path, "\(String(describing: attributes?[.systemFileNumber])):\(String(describing: attributes?[.size])):\(String(describing: modified))")
        })
    }
    func rebuild() {
        sources.forEach { $0.cancel() }
        sources.removeAll()
        var watched = Set<String>()
        for path in paths {
            var parent = URL(fileURLWithPath: path).deletingLastPathComponent()
            while !FileManager.default.fileExists(atPath: parent.path), parent.path != "/" {
                parent.deleteLastPathComponent()
            }
            watched.insert(parent.path)
            if FileManager.default.fileExists(atPath: path) { watched.insert(path) }
        }
        for path in watched {
            let descriptor = open(path, O_EVTONLY)
            guard descriptor >= 0 else { continue }
            let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: descriptor,
                eventMask: [.write, .rename, .delete, .attrib, .extend], queue: queue)
            source.setEventHandler { [weak self] in self?.receivedEvent() }
            source.setCancelHandler { close(descriptor) }
            source.resume()
            sources.append(source)
        }
    }
    func receivedEvent() {
        guard pending == nil else { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pending = nil
            let latest = self.snapshot()
            if latest != self.signatures {
                self.signatures = latest
                self.changed()
            }
            // Atomic replacement changes the inode; observe the new file as well.
            self.rebuild()
        }
        pending = work
        queue.asyncAfter(deadline: .now() + .milliseconds(150), execute: work)
    }
    deinit { pending?.cancel(); sources.forEach { $0.cancel() } }
}


final class Manager {
    let port: UInt16
    let resources: URL
    let modelOperations: ModelOperations
    let token: String
    let operations = DispatchQueue(label: "mlx.manager.operations")
    let io = DispatchQueue(label: "mlx.manager.connections")
    var listener: NWListener!
    var clients: [UUID: Client] = [:]
    var signals: [DispatchSourceSignal] = []
    var streams = Set<UUID>()
    var stateFiles: StateFiles?
    var processSource: DispatchSourceProcess?
    var observedPID: Int32?
    var pendingChange: DispatchWorkItem?

    init(port: UInt16, resources: URL) throws {
        self.port = port
        self.resources = resources
        self.modelOperations = ModelOperations(configuration: .environment())
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw HTTPFailure(status: 500, message: "cannot generate token")
        }
        token = bytes.map { String(format: "%02x", $0) }.joined()
    }

    var configurationIdentity: [String: String] {
        let configuration = modelOperations.configuration
        func canonical(_ path: URL) -> String { path.standardizedFileURL.resolvingSymlinksInPath().path }
        return ["resources": canonical(resources), "state_root": canonical(configuration.stateRoot),
                "server_root": canonical(configuration.serverRoot), "gguf_root": canonical(configuration.ggufRoot),
                "cache_root": canonical(configuration.cacheRoot)]
    }

    func run() throws {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!)
        parameters.allowLocalEndpointReuse = true
        listener = try NWListener(using: parameters)
        listener.newConnectionHandler = { [weak self] connection in
            guard let self, self.clients.count < 32 else { connection.cancel(); return }
            let client = Client(connection: connection, manager: self)
            self.clients[client.id] = client
            client.start()
        }
        listener.stateUpdateHandler = { state in
            switch state {
            case .ready:
                print("MLX_MANAGER_READY")
                fflush(stdout)
            case .failed(let error):
                fputs("Listener failed: \(error)\n", stderr)
                exit(1)
            default: break
            }
        }
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: io)
            source.setEventHandler { [weak self] in
                guard let self else { return }
                self.listener.cancel()
                // Finish accepted mutations before exit. Detached inference is not terminated.
                self.operations.async { exit(0) }
            }
            source.resume()
            signals.append(source)
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        let environment = ProcessInfo.processInfo.environment
        let state = URL(fileURLWithPath: environment["MLX_MANAGER_STATE_ROOT"]
            ?? home.appendingPathComponent("Library/Application Support/MLX Server Manager").path)
        let server = URL(fileURLWithPath: environment["MLX_ACTIVATION_LAB_SERVER_ROOT"]
            ?? home.appendingPathComponent("mlx-server").path)
        let runtime = URL(fileURLWithPath: environment["MLX_MANAGER_GGUF_ROOT"]
            ?? state.appendingPathComponent("gguf").path)
        stateFiles = StateFiles(paths: [
            server.appendingPathComponent("models.conf").path,
            state.appendingPathComponent("models.json").path,
            state.appendingPathComponent("starting.json").path,
            state.appendingPathComponent("mlx-server.log").path,
            runtime.appendingPathComponent("evidence/managed-server.json").path,
            runtime.appendingPathComponent("evidence/llama-server.json").path,
            runtime.appendingPathComponent("logs/llama-server.log").path,
            runtime.appendingPathComponent("profiles.json").path,
        ], queue: io) { [weak self] in self?.stateChanged() }
        listener.start(queue: io)
    }

    func stateChanged() {
        // Coalesce notifications from one operation (record, log and API response).
        // There is no periodic task or background model scan when the UI is disconnected.
        guard pendingChange == nil, !streams.isEmpty else { return }
        let event = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pendingChange = nil
            for identifier in self.streams { self.clients[identifier]?.sendEvent() }
        }
        pendingChange = event
        io.asyncAfter(deadline: .now() + .milliseconds(150), execute: event)
    }

    func observeProcess(_ pid: Int32?) {
        guard pid != observedPID else { return }
        processSource?.cancel()
        processSource = nil
        observedPID = pid
        guard let pid, pid > 0 else { return }
        let source = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: io)
        source.setEventHandler { [weak self] in
            self?.observeProcess(nil)
            self?.stateChanged()
        }
        source.resume()
        processSource = source
    }

    func authorized(_ candidate: String) -> Bool {
        let left = Array(candidate.utf8), right = Array(token.utf8)
        guard left.count == right.count else { return false }
        var difference: UInt8 = 0
        for index in left.indices { difference |= left[index] ^ right[index] }
        return difference == 0
    }

    func handle(_ request: Request, completion: @escaping (Response) -> Void) {
        let hosts = ["127.0.0.1:\(port)", "localhost:\(port)"]
        guard hosts.contains(request.headers["host"] ?? "") else {
            completion(.error(403, "invalid host")); return
        }
        if let origin = request.headers["origin"], !hosts.map({ "http://" + $0 }).contains(origin) {
            completion(.error(403, "invalid origin")); return
        }
        guard request.method == "GET" || request.method == "POST" else {
            completion(.error(405, "method not allowed")); return
        }
        if request.method == "POST", !authorized(request.headers["x-server-token"] ?? "") {
            completion(.error(403, "invalid token")); return
        }
        if request.method == "GET" {
            guard request.body.isEmpty else { completion(.error(400, "unexpected body")); return }
            if request.path == "/api/health" {
                completion(.json(["app": "mlx-server-manager", "backend": "swift", "protocol": 3,
                                  "configuration": configurationIdentity])); return
            }
            if request.path == "/api/events" { completion(.events()); return }
            let pages = ["/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                         "/style.css": ("style.css", "text/css")]
            if let (name, mime) = pages[request.path] {
                do {
                    let path = resources.appendingPathComponent("server_web/" + name)
                    var data = try Data(contentsOf: path)
                    if name == "index.html" {
                        data = Data(String(decoding: data, as: UTF8.self)
                            .replacingOccurrences(of: "__SERVER_TOKEN__", with: token).utf8)
                    }
                    completion(Response(status: 200, mime: mime + "; charset=utf-8", data: data))
                } catch { completion(.error(500, "画面のファイルを読み込めません")) }
                return
            }
        }
        let getPaths = ["/api/status", "/api/cached-models"]
        let postPaths = ["/api/start", "/api/stop", "/api/rename", "/api/default", "/api/gguf/register",
                         "/api/delete-preview", "/api/delete", "/api/register"]
        guard (request.method == "GET" ? getPaths : postPaths).contains(request.path) else {
            completion(.error(404, "not found")); return
        }
        let payload: [String: Any]
        do {
            if request.body.isEmpty { payload = [:] }
            else if let object = try JSONSerialization.jsonObject(with: request.body) as? [String: Any] { payload = object }
            else { throw HTTPFailure(status: 400, message: "JSON objectを送ってください") }
        } catch { completion(.error(400, "JSON objectを送ってください")); return }
        operations.async {
            let result = self.execute(method: request.method, path: request.path, payload: payload)
            self.io.async { completion(result) }
        }
    }

    func execute(method: String, path: String, payload: [String: Any]) -> Response {
        defer {
            // Partial failures may already have changed files or started a child.
            if method == "POST", path != "/api/delete-preview" { io.async { self.stateChanged() } }
        }
        do {
            var result = try modelOperations.dispatch(method: method, path: path, payload: payload)
            if path == "/api/status" {
                result["token"] = token
                let server = result["server"] as? [String: Any]
                let pid = (server?["running"] as? Bool == true) ? (server?["pid"] as? NSNumber)?.int32Value : nil
                io.async { self.observeProcess(pid) }
            }
            if path == "/api/start", let pid = (result["result"] as? [String: Any])?["pid"] as? NSNumber {
                io.async { self.observeProcess(pid.int32Value) }
            }
            return .json(result)
        } catch {
            return .error(400, error.localizedDescription)
        }
    }

}

final class Client {
    let id = UUID()
    let connection: NWConnection
    unowned let manager: Manager
    var data = Data()
    var timer: DispatchSourceTimer?
    var completed = false

    init(connection: NWConnection, manager: Manager) {
        self.connection = connection
        self.manager = manager
    }
    func start() {
        connection.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            if case .cancelled = state { self.dispose() }
            if case .failed = state { self.close() }
        }
        connection.start(queue: manager.io)
        let deadline = DispatchSource.makeTimerSource(queue: manager.io)
        deadline.schedule(deadline: .now() + 10)
        deadline.setEventHandler { [weak self] in self?.close() }
        timer = deadline
        deadline.resume()
        receive()
    }
    func receive() {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 24_580) { [weak self] part, _, end, error in
            guard let self, !self.completed else { return }
            if let part { self.data.append(part) }
            do {
                if let request = try Request.parse(self.data) {
                    self.completed = true
                    self.timer?.cancel(); self.timer = nil
                    self.manager.handle(request) { [weak self] response in self?.send(response) }
                } else if end || error != nil { self.close() }
                else { self.receive() }
            } catch let failure as HTTPFailure { self.send(.error(failure.status, failure.message)) }
            catch { self.send(.error(400, "invalid request")) }
        }
    }
    func send(_ response: Response) {
        completed = true
        timer?.cancel(); timer = nil
        if response.stream {
            manager.streams.insert(id)
            connection.send(content: response.wire, completion: .contentProcessed { [weak self] error in
                if error != nil { self?.close() }
            })
            // Detect EventSource.close()/window close without a heartbeat timer.
            connection.receive(minimumIncompleteLength: 1, maximumLength: 1) { [weak self] _, _, _, _ in self?.close() }
        } else {
            connection.send(content: response.wire, completion: .contentProcessed { [weak self] _ in self?.close() })
        }
    }
    func sendEvent() {
        connection.send(content: Data("event: changed\ndata: {}\n\n".utf8),
                        completion: .contentProcessed { [weak self] error in if error != nil { self?.close() } })
    }
    func close() { completed = true; connection.cancel(); dispose() }
    func dispose() { timer?.cancel(); timer = nil; manager.streams.remove(id); manager.clients.removeValue(forKey: id) }
}

@main
struct NativeManagerMain {
    static func main() {
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            var options: [String: String] = [:]
            guard args.count % 2 == 0 else { throw ManagerError("invalid arguments") }
            for index in stride(from: 0, to: args.count, by: 2) {
                guard ["--port", "--resources", "--record-start", "--alias", "--thinking"].contains(args[index]),
                      options[args[index]] == nil else { throw ManagerError("unknown or duplicate option") }
                options[args[index]] = args[index + 1]
            }
            if let text = options["--record-start"] {
                guard options.count == 3, let pid = Int32(text), let alias = options["--alias"],
                      let thinking = options["--thinking"], ["off", "on", "low", "medium", "xhigh"].contains(thinking) else {
                    throw ManagerError("--record-start PID --alias ALIAS --thinking MODE are required")
                }
                try ProcessController.recordStart(configuration: .environment(), pid: pid, alias: alias, thinking: thinking != "off")
                return
            }
            guard options.keys.allSatisfy({ ["--port", "--resources"].contains($0) }),
                  let port = UInt16(options["--port"] ?? "8766"), port > 0,
                  let resources = options["--resources"] else { throw ManagerError("--resources is required") }
            let manager = try Manager(port: port, resources: URL(fileURLWithPath: resources))
            try manager.run()
            withExtendedLifetime(manager) { dispatchMain() }
        } catch {
            fputs("MLXServerManager: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
}
