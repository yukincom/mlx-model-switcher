import Foundation

// This executable only operates on the temporary fixture supplied by the tests.
// Process state is injected, so no real process lookup, signal or model launch is used.
@main
struct ModelStoreHarness {
    static func main() {
        do {
            guard let request = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as? [String: Any],
                  let rootPath = request["root"] as? String,
                  let actions = request["actions"] as? [[String: Any]] else {
                throw ManagerError("invalid fixture request")
            }
            // Foundation rewrites existing /private/var paths to the /var alias.
            // Keep the canonical fixture spelling supplied by Python so the store
            // can inspect each original ancestor without introducing a symlink.
            let root = URL(fileURLWithPath: rootPath)
            guard root.lastPathComponent.hasPrefix("mlx-swift-store-fixture-") else {
                throw ManagerError("fixture root required")
            }
            let config = ManagerConfiguration(
                serverRoot: root.appendingPathComponent("server"),
                stateRoot: root.appendingPathComponent("state"),
                ggufRoot: root.appendingPathComponent("gguf"),
                cacheRoot: root.appendingPathComponent("cache")
            )
            let store = ModelStore(configuration: config)
            var server: [String: Any] = ["running": false]
            var listener: Int32?
            var listenerError: String?
            var labBusy = false
            var labBusyAfterMissing: URL?
            store.currentServer = { server }
            store.listenerPID = {
                if let error = listenerError { throw ManagerError(error) }
                return listener
            }
            store.ensureLabIdle = {
                if labBusy || (labBusyAfterMissing.map { !FileManager.default.fileExists(atPath: $0.path) } ?? false) {
                    throw ManagerError("fixture: 実験ラボが使用中です")
                }
            }
            func fixturePath(_ relative: String) throws -> URL {
                guard !relative.hasPrefix("/"), !relative.split(separator: "/").contains("..") else {
                    throw ManagerError("fixture relative path required")
                }
                let path = root.appendingPathComponent(relative)
                let resolved = path.resolvingSymlinksInPath()
                guard resolved.path.hasPrefix(root.resolvingSymlinksInPath().path + "/") else {
                    throw ManagerError("fixture mutation escaped root")
                }
                return path
            }
            var previews: [String: [String: Any]] = [:]
            var results: [[String: Any]] = []
            for action in actions {
                do {
                    let op = action["op"] as? String ?? ""
                    var value: Any = NSNull()
                    switch op {
                    case "models": value = try store.models()
                    case "aliases": value = try store.aliases()
                    case "cached": value = try store.cachedModels()
                    case "settings": value = try store.settings()
                    case "name": value = try store.displayName(action["model"] as? String ?? "")
                    case "default": value = try store.defaultAlias(store.models())
                    case "rename":
                        try store.rename(alias: action["alias"] as? String ?? "", name: action["name"] as? String ?? "")
                    case "set-default": try store.setDefault(alias: action["alias"] as? String ?? "")
                    case "restore-gguf": try store.restoreGGUF()
                    case "register":
                        try store.register(alias: action["alias"] as? String, model: action["model"] as? String ?? "")
                        value = try store.aliases()
                    case "gguf-files": value = try store.ggufFiles(alias: action["alias"] as? String ?? "").map(\.path)
                    case "preview":
                        let result = try store.deletionPreview(aliases: action["aliases"] as? [String] ?? [],
                            body: action["body"] as? Bool ?? false, single: action["single"] as? Bool ?? false)
                        if let name = action["save"] as? String { previews[name] = result }
                        value = result
                    case "delete":
                        let saved = previews[action["use"] as? String ?? ""]
                        let fingerprint = action["fingerprint"] as? String ?? saved?["fingerprint"] as? String ?? ""
                        value = try store.deleteModels(aliases: action["aliases"] as? [String] ?? [],
                            body: action["body"] as? Bool ?? false, fingerprint: fingerprint,
                            single: action["single"] as? Bool ?? false)
                    case "set-server": server = action["value"] as? [String: Any] ?? ["running": false]
                    case "set-listener":
                        listener = (action["pid"] as? NSNumber).map { $0.int32Value }
                        listenerError = action["error"] as? String
                    case "set-lab-busy": labBusy = action["value"] as? Bool ?? false
                    case "lab-busy-after-missing":
                        labBusyAfterMissing = try fixturePath(action["path"] as? String ?? "")
                    case "write", "replace":
                        let target = try fixturePath(action["path"] as? String ?? "")
                        try Data((action["text"] as? String ?? "").utf8).write(to: target,
                            options: op == "replace" ? .atomic : [])
                    case "remove":
                        try FileManager.default.removeItem(at: fixturePath(action["path"] as? String ?? ""))
                    default: throw ManagerError("unknown fixture operation: \(op)")
                    }
                    results.append(["ok": true, "value": value])
                } catch {
                    results.append(["ok": false, "error": error.localizedDescription])
                }
            }
            let data = try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys])
            FileHandle.standardOutput.write(data)
        } catch {
            FileHandle.standardError.write(Data("\(error)\n".utf8))
            exit(1)
        }
    }
}
