import Foundation
import Darwin
import CryptoKit

// Catalog and mutation policy live here; callers serialize operations and supply
// a fresh process view. No model code is imported while inspecting its files.
final class ModelStore {
    let configuration: ManagerConfiguration
    var currentServer: () throws -> [String: Any] = { ["running": false] }
    var listenerPID: () throws -> Int32? = { nil }
    var ensureLabIdle: () throws -> Void = {}
    private let fm = FileManager.default

    init(configuration: ManagerConfiguration) { self.configuration = configuration }

    func aliases() throws -> [[String: String]] {
        guard exists(configuration.modelsConfig) else { return [] }
        return try String(contentsOf: configuration.modelsConfig, encoding: .utf8)
            .components(separatedBy: .newlines).compactMap { raw in
                let line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !line.isEmpty, !line.hasPrefix("#"), let equal = line.firstIndex(of: "=") else { return nil }
                let alias = String(line[..<equal]).trimmingCharacters(in: .whitespaces)
                let model = String(line[line.index(after: equal)...]).trimmingCharacters(in: .whitespaces)
                return alias.isEmpty || model.isEmpty ? nil : ["alias": alias, "model": model]
            }
    }

    func settings() throws -> [String: Any] {
        guard exists(configuration.settingsURL) else { return [:] }
        let value = try object(configuration.settingsURL)
        if let names = value["display_names"], !(names is [String: String]) {
            throw ManagerError("表示名の設定を確認できません")
        }
        if let hidden = value["hidden_gguf_aliases"], !(hidden is [String]) {
            throw ManagerError("GGUFの登録設定を確認できません")
        }
        return value
    }

    func namesVersion() -> Int64 {
        guard let value = try? fileStat(configuration.settingsURL, follow: false) else { return 0 }
        return modifiedNanoseconds(value)
    }

    private func updateSettings(_ change: (inout [String: Any]) throws -> Void) throws {
        try fm.createDirectory(at: configuration.stateRoot, withIntermediateDirectories: true)
        let lock = configuration.stateRoot.appendingPathComponent("models.lock")
        let fd = open(lock.path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw ManagerError("モデル設定のロックを取得できません") }
        defer { close(fd) }
        guard flock(fd, LOCK_EX) == 0 else { throw ManagerError("モデル設定のロックを取得できません") }
        defer { _ = flock(fd, LOCK_UN) }
        var value = try settings()
        try change(&value)
        try writeJSON(value, to: configuration.settingsURL)
    }

    func displayName(_ model: String) throws -> String {
        if let name = (try settings()["display_names"] as? [String: String])?[model], !name.isEmpty { return name }
        if let profile = try ggufProfiles().first(where: { $0["model"] as? String == model }),
           let name = profile["name"] as? String { return name }
        return shortName(model)
    }

    private func shortName(_ model: String) -> String {
        let name = model.components(separatedBy: "/").last ?? model
        if name.contains("Flash-Next") { return "Qwen3.8-Flash-Next-125B" }
        var result: String
        if let capture = matches("gemma[-_ ]?(\\d+)[-_ ](E\\d+B|\\d+B)", in: name, insensitive: true).first {
            result = "Gemma-\(capture[1])-\(capture[2].uppercased())"
        } else if let capture = matches("Qwen(\\d+(?:\\.\\d+)?)[-_ ](\\d+B)", in: name, insensitive: true).first {
            result = "Qwen\(capture[1])-\(capture[2].uppercased())"
        } else {
            let pattern = try! NSRegularExpression(pattern: "[-_](?:it|mlx|qat|bf16|fp16|\\d+bit)(?=[-_]|$)", options: .caseInsensitive)
            let stripped = pattern.stringByReplacingMatches(in: name, range: NSRange(name.startIndex..., in: name), withTemplate: "")
            result = String(stripped.replacingOccurrences(of: "_", with: "-").prefix(60))
        }
        for variant in ["Huihui", "Josiefied", "AEON"] where name.localizedCaseInsensitiveContains(variant) {
            result += "-" + variant
            break
        }
        return result
    }

    func rename(alias: String, name: String) throws {
        let item = try findModel(alias)
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.unicodeScalars.count <= 60,
              !name.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }) else {
            throw ManagerError("表示名は1〜60文字で、改行せずに入力してください")
        }
        try updateSettings { value in
            var names = value["display_names"] as? [String: String] ?? [:]
            names[item["model"] as! String] = trimmed
            value["display_names"] = names
        }
    }

    func defaultAlias(_ items: [[String: Any]]) throws -> String {
        if let chosen = try settings()["default_alias"] as? String,
           items.contains(where: { $0["alias"] as? String == chosen }) { return chosen }
        return items.first?["alias"] as? String ?? ""
    }

    func setDefault(alias: String) throws {
        _ = try findModel(alias)
        try updateSettings { $0["default_alias"] = alias }
    }

    func restoreGGUF() throws {
        guard !(try ggufProfiles()).isEmpty else { throw ManagerError("GGUFの起動設定が見つかりません") }
        try updateSettings { $0["hidden_gguf_aliases"] = [String]() }
    }

    func register(alias: String?, model: String) throws {
        guard !model.contains("\n"), !model.contains("\r") else {
            throw ManagerError("モデルのIDまたはパスに改行は使えません")
        }
        let model = model.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !model.isEmpty else { throw ManagerError("モデルのIDまたはパスが空です") }
        let entries = try aliases()
        let chosen: String
        if let alias, !alias.isEmpty {
            chosen = sanitizedAlias(alias)
            guard !chosen.isEmpty else { throw ManagerError("エイリアス名が空です") }
        } else {
            guard try cachedModels().contains(model) else {
                throw ManagerError("保存済みモデルが見つかりません。一覧を更新してください")
            }
            if entries.contains(where: { $0["model"] == model }) { return }
            let base = sanitizedAlias(model.components(separatedBy: "/").last ?? "")
            let stem = base.isEmpty ? "model" : base
            let used = Set(entries.compactMap { $0["alias"] })
            var value = stem, suffix = 2
            while used.contains(value) { value = "\(stem)-\(suffix)"; suffix += 1 }
            chosen = value
        }
        if entries.contains(where: { $0["alias"] == chosen && $0["model"] != model }) {
            throw ManagerError("別のモデルが同じエイリアスを使っています: \(chosen)")
        }
        if entries.contains(where: { $0["alias"] == chosen && $0["model"] == model }) { return }
        guard exists(configuration.modelsConfig) else {
            throw ManagerError("モデル設定が見つかりません: \(configuration.modelsConfig.path)")
        }
        let original = try String(contentsOf: configuration.modelsConfig, encoding: .utf8)
        try Data((original + "\n\(chosen)=\(model)\n").utf8).write(to: configuration.modelsConfig, options: .atomic)
    }

    private func sanitizedAlias(_ text: String) -> String {
        let pattern = try! NSRegularExpression(pattern: "[^A-Za-z0-9._-]+")
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return pattern.stringByReplacingMatches(in: trimmed, range: NSRange(trimmed.startIndex..., in: trimmed), withTemplate: "-")
            .trimmingCharacters(in: CharacterSet(charactersIn: "-."))
    }

    func ggufConfiguration() throws -> [String: Any]? {
        let path = configuration.ggufRoot.appendingPathComponent("profiles.json")
        guard exists(path) else { return nil }
        let value = try object(path)
        guard let root = value["models_root"] as? String,
              root.hasPrefix("/") || root.hasPrefix("~/"), !hasTraversal(root),
              let profiles = value["profiles"] as? [[String: Any]] else {
            throw ManagerError("GGUFの起動設定を確認できません")
        }
        var aliases = Set<String>(), targets = Set<String>()
        for profile in profiles {
            for key in ["target", "alias", "model", "name", "manifest"] {
                guard let text = profile[key] as? String, !text.isEmpty,
                      !text.unicodeScalars.contains(where: { $0.value < 32 }) else {
                    throw ManagerError("GGUFの起動設定を確認できません")
                }
            }
            guard aliases.insert(profile["alias"] as! String).inserted,
                  targets.insert(profile["target"] as! String).inserted,
                  safeRelative(profile["manifest"] as! String) else {
                throw ManagerError("GGUFの起動設定を確認できません")
            }
        }
        return value
    }

    func ggufProfiles() throws -> [[String: Any]] {
        try ggufConfiguration()?["profiles"] as? [[String: Any]] ?? []
    }

    func ggufModelsRoot() throws -> URL {
        guard let root = try ggufConfiguration()?["models_root"] as? String else {
            throw ManagerError("GGUFの保存先を確認できません")
        }
        return lexicalURL(URL(fileURLWithPath: (root as NSString).expandingTildeInPath))
    }

    private func ggufProfile(_ alias: String) throws -> [String: Any] {
        guard let item = try ggufProfiles().first(where: { $0["alias"] as? String == alias }) else {
            throw ManagerError("GGUFモデルの起動設定が見つかりません")
        }
        return item
    }

    private func ggufManifest(_ alias: String) throws -> URL {
        let profile = try ggufProfile(alias)
        return configuration.ggufRoot.appendingPathComponent(profile["manifest"] as! String)
    }

    func ggufFiles(alias: String) throws -> [URL] {
        let manifest = try ggufManifest(alias)
        guard exists(manifest) else { return [] }
        guard let entries = try object(manifest)["files"] as? [[String: Any]] else {
            throw ManagerError("GGUFモデルのmanifestを確認できません")
        }
        let root = try ggufModelsRoot()
        var result: [URL] = [], seen = Set<String>()
        for entry in entries {
            guard let directory = entry["directory"] as? String, let file = entry["file"] as? String,
                  (directory.isEmpty || directory == "." || safeRelative(directory)),
                  safeRelative(file), file.lowercased().hasSuffix(".gguf") else {
                throw ManagerError("GGUFの保存先を確認できません")
            }
            let path = lexicalURL(root.appendingPathComponent(directory).appendingPathComponent(file))
            guard isInside(path, root) else { throw ManagerError("GGUFの保存先を確認できません") }
            try rejectSymlinkAncestors(path)
            if exists(path) {
                let info = try fileStat(path)
                guard isRegular(info) else { throw ManagerError("GGUFの保存先を確認できません") }
                if seen.insert(path.path).inserted { result.append(path) }
            }
        }
        return result
    }

    private func ggufMainFile(_ profile: [String: Any]) throws -> URL? {
        let files = try ggufFiles(alias: profile["alias"] as! String)
        guard let name = profile["model_file"] as? String else { return files.first }
        let matches = files.filter { $0.lastPathComponent == name }
        guard matches.count == 1 else { throw ManagerError("GGUFの起動ファイルを確認できません") }
        return matches[0]
    }

    func models() throws -> [[String: Any]] {
        let repositories = try cacheRepositories()
        let byID = Dictionary(uniqueKeysWithValues: repositories.map { ($0.id, $0) })
        let value = try settings()
        let names = value["display_names"] as? [String: String] ?? [:]
        var result: [[String: Any]] = []
        for entry in try aliases() {
            let model = entry["model"]!
            let repository = byID[model]
            let path = repository?.root ?? modelPath(model)
            let stat = path.flatMap { try? fileStat($0) }
            result.append(["alias": entry["alias"]!, "model": model, "format": "MLX",
                           "name": names[model] ?? shortName(model), "order": result.count,
                           "size_bytes": repository.map { $0.bytes } as Any? ?? NSNull(),
                           "acquired_at": stat.map { birthTime($0) } as Any? ?? NSNull()])
        }
        let hidden = Set(value["hidden_gguf_aliases"] as? [String] ?? [])
        for profile in try ggufProfiles() {
            let alias = profile["alias"] as! String, model = profile["model"] as! String
            guard !hidden.contains(alias), exists(try ggufManifest(alias)) else { continue }
            let stats = try ggufFiles(alias: alias).map { try fileStat($0) }
            result.append(["alias": alias, "model": model, "format": "GGUF",
                           "name": names[model] ?? profile["name"]!, "order": result.count,
                           "size_bytes": stats.reduce(Int64(0)) { $0 + $1.st_size },
                           "acquired_at": stats.map { birthTime($0) }.max() as Any? ?? NSNull()])
        }
        return result
    }

    private func findModel(_ alias: String) throws -> [String: Any] {
        guard let item = try models().first(where: { $0["alias"] as? String == alias }) else {
            throw ManagerError("モデルが未登録です。一覧を更新してください")
        }
        return item
    }

    func cachedModels() throws -> [String] {
        var result: [String] = []
        for repo in try cacheRepositories() {
            let revision: URL?
            let main = repo.root.appendingPathComponent("refs/main")
            if exists(main) {
                let name = (try? String(contentsOf: main, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                revision = repo.revisions.first { $0.lastPathComponent == name }
            } else { revision = repo.revisions.count == 1 ? repo.revisions[0] : nil }
            guard let revision,
                  let config = try? object(revision.appendingPathComponent("config.json")) else { continue }
            if let architectures = config["architectures"] {
                guard let names = architectures as? [String], names.isEmpty || names.contains(where: {
                    $0.hasSuffix("ForCausalLM") || $0.hasSuffix("ForConditionalGeneration")
                }) else { continue }
            }
            let weights = try directory(revision).contains { path in
                path.pathExtension == "safetensors" && (try? fileStat(path)).map { isRegular($0) && $0.st_size > 0 } == true
            }
            if weights { result.append(repo.id) }
        }
        return result.sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
    }

    private struct CacheRepository {
        let id: String
        let root: URL
        let revisions: [URL]
        let resolvedFiles: Set<String>
        let referencedPaths: Set<String>
        let bytes: Int64
    }

    private func cacheRepositories() throws -> [CacheRepository] {
        guard exists(configuration.cacheRoot) else { return [] }
        var result: [CacheRepository] = []
        for root in try directory(configuration.cacheRoot) where root.lastPathComponent.hasPrefix("models--") {
            let name = String(root.lastPathComponent.dropFirst("models--".count))
            let components = name.components(separatedBy: "--")
            guard !components.isEmpty, components.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }),
                  (try? fileStat(root, follow: false)).map({ isDirectory($0) }) == true else { continue }
            let snapshots = root.appendingPathComponent("snapshots")
            var revisions: [URL] = [], files = Set<String>(), references = Set<String>(), bytes: Int64 = 0
            if exists(snapshots) {
                guard (try? fileStat(snapshots, follow: false)).map({ isDirectory($0) }) == true else {
                    throw ManagerError("共有キャッシュのsnapshotを確認できません")
                }
                for revision in try directory(snapshots) {
                    let stat = try fileStat(revision, follow: false)
                    guard isDirectory(stat) else { throw ManagerError("共有キャッシュのrevisionを確認できません") }
                    revisions.append(revision)
                    let found = try treeEntries(revision)
                    for path in found {
                        let stat = try fileStat(path, follow: false)
                        guard isRegular(stat) || isSymlink(stat) else { continue }
                        if isSymlink(stat), let target = try? fileStat(path), isDirectory(target) {
                            throw ManagerError("共有キャッシュ内のディレクトリリンクを確認できません")
                        }
                        references.formUnion(try linkChain(path))
                        let resolved = path.resolvingSymlinksInPath()
                        guard let target = try? fileStat(resolved), isRegular(target) else { continue }
                        if files.insert(resolved.path).inserted { bytes += target.st_size }
                    }
                }
            }
            result.append(CacheRepository(id: components.joined(separator: "/"), root: root,
                                          revisions: revisions.sorted { $0.path < $1.path },
                                          resolvedFiles: files, referencedPaths: references, bytes: bytes))
        }
        return result
    }

    // A complete preview includes identities of both links and targets. Replacing a
    // snapshot link must invalidate confirmation even when the blob size is unchanged.
    private func cacheContent(_ repo: CacheRepository, all: [CacheRepository]) throws -> [String: Any] {
        try rejectSymlinkAncestors(repo.root)
        let roots = ["snapshots", "refs", "blobs", ".no_exist"].map { repo.root.appendingPathComponent($0) }
        var tree: [[Any]] = [], ownedFiles: [URL] = []
        for root in roots where exists(root) {
            guard isDirectory(try fileStat(root, follow: false)) else {
                throw ManagerError("共有キャッシュの保存先を確認できません")
            }
            tree.append(try identity(root))
            for path in try treeEntries(root) {
                let info = try fileStat(path, follow: false)
                guard isRegular(info) || isDirectory(info) || isSymlink(info) else {
                    throw ManagerError("共有キャッシュに不明なファイルがあります")
                }
                tree.append(try identity(path))
                if isRegular(info) { ownedFiles.append(path) }
            }
        }
        let shared = all.filter { $0.root != repo.root }.flatMap { other in
            other.referencedPaths.filter { isInside(URL(fileURLWithPath: $0), repo.root) }
                .map { [other.id, $0] }
        }.sorted { $0.joined(separator: "\u{0}") < $1.joined(separator: "\u{0}") }
        let protected = Set(all.filter { $0.root != repo.root }.flatMap { $0.referencedPaths })
        let files = try ownedFiles.filter { !protected.contains($0.path) }.sorted { $0.path < $1.path }.map { try identity($0) }
        return ["files": files, "cache_tree": tree.sorted { String(describing: $0[0]) < String(describing: $1[0]) },
                "shared_references": shared, "revisions": repo.revisions.map { $0.lastPathComponent }]
    }

    private func modelPath(_ reference: String) -> URL? {
        let expanded = (reference as NSString).expandingTildeInPath
        if expanded.hasPrefix("/") { return URL(fileURLWithPath: expanded).resolvingSymlinksInPath() }
        let local = configuration.serverRoot.appendingPathComponent(reference)
        if reference.hasPrefix("./") || reference.hasPrefix("../") || exists(local) {
            return local.resolvingSymlinksInPath()
        }
        return nil
    }

    private func modelKey(_ reference: String, repos: [CacheRepository]) -> String {
        let path = modelPath(reference)
        for repo in repos where reference == repo.id || path.map({ isInside($0, repo.root.resolvingSymlinksInPath(), equal: true) }) == true {
            return "cache:" + repo.id
        }
        return path.map { "path:" + $0.path } ?? "model:" + reference
    }

    private func runningPath(_ reference: String) throws -> URL? {
        let path = modelPath(reference)
        if path != nil && !(reference as NSString).expandingTildeInPath.hasPrefix("/") {
            throw ManagerError("起動中モデルの相対パスを確認できないため、削除しません")
        }
        return path
    }

    private func bodyFiles(_ item: [String: Any], repos: [CacheRepository]) throws -> Set<String> {
        if item["format"] as? String == "GGUF" {
            return Set(try ggufFiles(alias: item["alias"] as! String).map { $0.resolvingSymlinksInPath().path })
        }
        guard let repo = repos.first(where: { $0.id == item["model"] as? String }) else { return [] }
        // Include unreferenced blobs too: an external server can still open a
        // cache file directly even when no snapshot points to it anymore.
        let owned = try cacheContent(repo, all: repos)["files"] as? [[Any]] ?? []
        return repo.resolvedFiles.union(owned.compactMap { $0.first as? String }.map {
            URL(fileURLWithPath: $0).resolvingSymlinksInPath().path
        })
    }

    private func runningFiles(_ current: [String: Any], repos: [CacheRepository]) throws -> Set<String> {
        let active = current["active_model"] as! String
        let command = current["command"] as? String ?? ""
        let profiles = try ggufProfiles()
        var references = [active], files = Set<String>()
        let flags = Set(["--model", "--draft-model", "--model-draft", "--adapter-path", "--mmproj", "-m", "-md"])
        if let arguments = current["arguments"] as? [String], !arguments.isEmpty {
            for index in arguments.indices {
                let parts = arguments[index].split(separator: "=", maxSplits: 1).map(String.init)
                guard let flag = parts.first, flags.contains(flag), flag != "-m" || command.contains("llama-server") else { continue }
                if parts.count == 2 { references.append(parts[1]) }
                else if index + 1 < arguments.count { references.append(arguments[index + 1]) }
                else { throw ManagerError("起動中モデルの補助ファイルを確認できないため、削除しません") }
            }
        } else {
            let pattern = "(?:^|\\s)(--model|--draft-model|--model-draft|--adapter-path|--mmproj|-m|-md)(?:=|\\s+)(.+?)(?=\\s+--?[A-Za-z]|$)"
            for capture in matches(pattern, in: command) {
                if capture[1] == "-m" && !command.contains("llama-server") { continue }
                references.append(capture[2].trimmingCharacters(in: CharacterSet(charactersIn: "'\"")))
            }
        }
        for reference in references {
            if let profile = profiles.first(where: { $0["model"] as? String == reference }) {
                files.formUnion(try ggufFiles(alias: profile["alias"] as! String).map { $0.resolvingSymlinksInPath().path })
                continue
            }
            if let repo = repos.first(where: { $0.id == reference }) {
                files.formUnion(repo.resolvedFiles)
                continue
            }
            guard let path = try runningPath(reference) else {
                if reference != active { throw ManagerError("起動中モデルの補助ファイルを確認できないため、削除しません") }
                continue
            }
            guard exists(path) else { throw ManagerError("起動中モデルのファイルを確認できないため、削除しません") }
            if isDirectory(try fileStat(path)) {
                for child in try treeEntries(path) {
                    let own = try fileStat(child, follow: false)
                    guard let target = try? fileStat(child) else { throw ManagerError("起動中モデルのファイルを確認できないため、削除しません") }
                    if isSymlink(own) && isDirectory(target) {
                        throw ManagerError("起動中モデルのディレクトリリンクを確認できないため、削除しません")
                    }
                    if isRegular(target) { files.insert(child.resolvingSymlinksInPath().path) }
                }
            } else {
                files.insert(path.resolvingSymlinksInPath().path)
                if let shard = matches("^(.+)-[0-9]{5}-of-([0-9]{5})\\.gguf$", in: path.lastPathComponent).first {
                    let expression = "^" + NSRegularExpression.escapedPattern(for: shard[1]) + "-[0-9]{5}-of-" + shard[2] + "\\.gguf$"
                    for part in try directory(path.deletingLastPathComponent()) where !matches(expression, in: part.lastPathComponent).isEmpty {
                        files.insert(part.resolvingSymlinksInPath().path)
                    }
                }
            }
        }
        return files
    }

    private func guardDeletion(_ item: [String: Any], body: Bool) throws {
        let current = try currentServer(), listener = try listenerPID()
        let running = current["running"] as? Bool == true || current["loading"] as? Bool == true
        if let listener, !running || (current["pid"] as? NSNumber)?.int32Value != listener {
            throw ManagerError("8080のプロセスが変わったため、削除内容を確認し直してください")
        }
        if running {
            guard let active = current["active_model"] as? String, !active.isEmpty else {
                throw ManagerError("起動中のモデルを確認できないため、削除しません")
            }
            let repos = try cacheRepositories(), key = modelKey(active, repos: repos)
            if modelKey(item["model"] as! String, repos: repos) == key {
                throw ManagerError("起動中のモデルは削除できません")
            }
            var path = try runningPath(active)
            let profile = try ggufProfiles().first { $0["model"] as? String == active }
            if let profile, let main = try ggufMainFile(profile) { path = main.resolvingSymlinksInPath() }
            if item["format"] as? String == "GGUF", let main = try ggufMainFile(ggufProfile(item["alias"] as! String)),
               main.resolvingSymlinksInPath() == path { throw ManagerError("起動中のモデルは削除できません") }
            let registered = try models().contains { modelKey($0["model"] as! String, repos: repos) == key }
            let known = profile != nil || key.hasPrefix("cache:") || path.map { exists($0) } == true || registered
            guard known else { throw ManagerError("起動中のモデルを確認できないため、削除しません") }
            if body, !(try bodyFiles(item, repos: repos)).isDisjoint(with: try runningFiles(current, repos: repos)) {
                throw ManagerError("起動中のモデルと本体を共有しているため、削除できません")
            }
        }
        if body { try ensureLabIdle() }
    }

    private func preview(_ alias: String, body: Bool) throws -> [String: Any] {
        let item = try findModel(alias)
        try guardDeletion(item, body: body)
        var content: [String: Any] = ["model": item["model"]!, "body": body, "files": [[Any]](), "revisions": [String]()]
        var location = "", files: [[Any]] = []
        if body {
            if item["format"] as? String == "GGUF" {
                files = try ggufFiles(alias: alias).map { try identity($0) }
                location = try ggufModelsRoot().path
                content["manifest"] = try identity(ggufManifest(alias))
                content["profile"] = try ggufProfile(alias)
                content["models_root"] = location
                content["files"] = files
            } else {
                let repos = try cacheRepositories()
                guard let repo = repos.first(where: { $0.id == item["model"] as? String }) else {
                    throw ManagerError("本体削除に対応する共有キャッシュがありません。登録だけ外せます")
                }
                content.merge(try cacheContent(repo, all: repos)) { _, new in new }
                files = content["files"] as! [[Any]]
                location = repo.root.path
            }
        }
        let snapshot: [String: Any] = ["alias": alias, "content": content,
            "config": try String(contentsOf: configuration.modelsConfig, encoding: .utf8), "settings": try settings()]
        return ["alias": alias, "name": item["name"]!, "body": body,
                "bytes": files.reduce(Int64(0)) { $0 + (($1[1] as? NSNumber)?.int64Value ?? 0) },
                "location": location, "file_count": files.count,
                "fingerprint": try digest(snapshot), "content_fingerprint": try digest(content)]
    }

    func deletionPreview(aliases: [String], body: Bool, single: Bool) throws -> [String: Any] {
        guard (1...100).contains(aliases.count), aliases.allSatisfy({ !$0.isEmpty }), Set(aliases).count == aliases.count,
              !single || aliases.count == 1 else { throw ManagerError("対象モデルを選択してください") }
        if single { return try preview(aliases[0], body: body) }
        var items: [[String: Any]] = [], seen = Set<String>()
        for alias in aliases {
            let item = try findModel(alias)
            let key = body ? item["model"] as! String : alias
            guard seen.insert(key).inserted else { continue }
            items.append(try preview(alias, body: body))
        }
        let snapshot: [String: Any] = ["aliases": aliases, "body": body, "items": items]
        return snapshot.merging(["fingerprint": try digest(snapshot),
                                 "bytes": items.reduce(Int64(0)) { $0 + (($1["bytes"] as? NSNumber)?.int64Value ?? 0) }]) { _, new in new }
    }

    func deleteModels(aliases: [String], body: Bool, fingerprint: String, single: Bool) throws -> [String: Any] {
        let confirmed = try deletionPreview(aliases: aliases, body: body, single: single)
        guard !fingerprint.isEmpty, confirmed["fingerprint"] as? String == fingerprint else {
            throw ManagerError("確認後にモデルや設定が変わりました。削除内容を確認し直してください")
        }
        let items = single ? [confirmed] : confirmed["items"] as! [[String: Any]]
        var completed: [String] = []
        for expected in items {
            do {
                let alias = expected["alias"] as! String
                let fresh = try preview(alias, body: body)
                guard fresh["content_fingerprint"] as? String == expected["content_fingerprint"] as? String else {
                    throw ManagerError("確認後にモデル本体が変わりました")
                }
                try deleteItem(findModel(alias), body: body)
                completed.append(expected["name"] as! String)
            } catch {
                if single { throw error }
                throw ManagerError("削除を中断しました。完了: \(completed.isEmpty ? "なし" : completed.joined(separator: "、"))。未完了: \(expected["name"] as! String) 以降。\(error.localizedDescription)")
            }
        }
        return confirmed
    }

    private func deleteItem(_ item: [String: Any], body: Bool) throws {
        let alias = item["alias"] as! String, model = item["model"] as! String
        var removed = Set([alias])
        if body && item["format"] as? String == "MLX" {
            removed.formUnion(try aliases().filter { $0["model"] == model }.compactMap { $0["alias"] })
        }
        if body {
            // A bulk operation may have spent time deleting an earlier item.
            // Repeat the live process guard directly before touching this item.
            try guardDeletion(item, body: true)
            if item["format"] as? String == "GGUF" {
                for path in try ggufFiles(alias: alias) {
                    try rejectSymlinkAncestors(path)
                    guard unlink(path.path) == 0 else { throw ManagerError("モデル本体を削除できません: \(path.lastPathComponent)") }
                }
            } else { try deleteCache(model) }
        }
        if item["format"] as? String == "GGUF" {
            try updateSettings { value in
                var hidden = Set(value["hidden_gguf_aliases"] as? [String] ?? [])
                hidden.insert(alias)
                value["hidden_gguf_aliases"] = hidden.sorted()
            }
        } else {
            let original = try String(contentsOf: configuration.modelsConfig, encoding: .utf8)
            var kept = ""
            original.enumerateSubstrings(in: original.startIndex..., options: [.byLines, .substringNotRequired]) { _, range, enclosing, _ in
                let line = String(original[range])
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                var remove = false
                if !trimmed.isEmpty, !trimmed.hasPrefix("#"), let equal = line.firstIndex(of: "=") {
                    let name = String(line[..<equal]).trimmingCharacters(in: .whitespaces)
                    let reference = String(line[line.index(after: equal)...]).trimmingCharacters(in: .whitespaces)
                    remove = name == alias || body && reference == model
                }
                if !remove { kept += String(original[enclosing]) }
            }
            try Data(kept.utf8).write(to: configuration.modelsConfig, options: .atomic)
        }
        try updateSettings { value in
            if let chosen = value["default_alias"] as? String, removed.contains(chosen) { value.removeValue(forKey: "default_alias") }
        }
    }

    private func deleteCache(_ model: String) throws {
        let repos = try cacheRepositories()
        guard let repo = repos.first(where: { $0.id == model }) else {
            throw ManagerError("共有キャッシュが確認後に変わりました")
        }
        _ = try cacheContent(repo, all: repos)
        let protected = Set(repos.filter { $0.root != repo.root }.flatMap { $0.referencedPaths })
        // Remove only the selected repository's links/files. Never traverse a
        // symlink while deleting, and retain blobs referenced by another repo.
        for name in ["snapshots", "refs", ".no_exist", "blobs"] {
            let root = repo.root.appendingPathComponent(name)
            guard exists(root) else { continue }
            let entries = try treeEntries(root).sorted { $0.pathComponents.count > $1.pathComponents.count }
            for path in entries {
                let info = try fileStat(path, follow: false)
                if isDirectory(info) {
                    if try directory(path).isEmpty { guard rmdir(path.path) == 0 else { throw ManagerError("キャッシュのディレクトリを削除できません") } }
                } else {
                    if protected.contains(lexicalURL(path).path) { continue }
                    try rejectSymlinkAncestors(path.deletingLastPathComponent())
                    guard unlink(path.path) == 0 else { throw ManagerError("キャッシュのファイルを削除できません") }
                }
            }
            if try directory(root).isEmpty { guard rmdir(root.path) == 0 else { throw ManagerError("キャッシュのディレクトリを削除できません") } }
        }
        if try directory(repo.root).isEmpty { guard rmdir(repo.root.path) == 0 else { throw ManagerError("キャッシュのディレクトリを削除できません") } }
    }

    private func object(_ path: URL) throws -> [String: Any] {
        guard let value = try JSONSerialization.jsonObject(with: Data(contentsOf: path)) as? [String: Any] else {
            throw ManagerError("JSON objectを確認できません: \(path.lastPathComponent)")
        }
        return value
    }
    private func writeJSON(_ value: [String: Any], to path: URL) throws {
        var data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .prettyPrinted, .withoutEscapingSlashes])
        data.append(10)
        try data.write(to: path, options: .atomic)
    }
    private func digest(_ value: [String: Any]) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes])
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
    private func exists(_ path: URL) -> Bool { (try? fileStat(path, follow: false)) != nil }
    private func fileStat(_ path: URL, follow: Bool = true) throws -> stat {
        var value = stat()
        let filesystemPath: String = path.path
        let result: Int32 = filesystemPath.withCString { pointer in
            if follow { return stat(pointer, &value) }
            return lstat(pointer, &value)
        }
        guard result == 0 else { throw ManagerError("ファイルの状態を確認できません: \(path.path)") }
        return value
    }
    private func isRegular(_ value: stat) -> Bool { value.st_mode & S_IFMT == S_IFREG }
    private func isDirectory(_ value: stat) -> Bool { value.st_mode & S_IFMT == S_IFDIR }
    private func isSymlink(_ value: stat) -> Bool { value.st_mode & S_IFMT == S_IFLNK }
    private func modifiedNanoseconds(_ value: stat) -> Int64 { Int64(value.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(value.st_mtimespec.tv_nsec) }
    private func birthTime(_ value: stat) -> Double { Double(value.st_birthtimespec.tv_sec) + Double(value.st_birthtimespec.tv_nsec) / 1_000_000_000 }
    private func identity(_ path: URL) throws -> [Any] {
        let info = try fileStat(path, follow: false)
        var result: [Any] = [path.path, info.st_size, modifiedNanoseconds(info), UInt64(info.st_ino), UInt64(info.st_dev), UInt64(info.st_mode)]
        if isSymlink(info) {
            result.append(try fm.destinationOfSymbolicLink(atPath: path.path))
            let target = path.resolvingSymlinksInPath()
            if let targetInfo = try? fileStat(target) {
                result.append([target.path, targetInfo.st_size, modifiedNanoseconds(targetInfo), UInt64(targetInfo.st_ino), UInt64(targetInfo.st_dev)])
            } else { result.append(NSNull()) }
        }
        return result
    }
    private func directory(_ root: URL) throws -> [URL] {
        try fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil).sorted { $0.path < $1.path }
    }
    private func treeEntries(_ root: URL) throws -> [URL] {
        var result: [URL] = []
        for path in try directory(root) {
            result.append(path)
            if isDirectory(try fileStat(path, follow: false)) { result += try treeEntries(path) }
        }
        return result
    }
    private func linkChain(_ path: URL) throws -> Set<String> {
        var current = lexicalURL(path), visited = Set<String>(), references = Set<String>()
        for _ in 0..<40 {
            guard visited.insert(current.path).inserted else {
                throw ManagerError("共有キャッシュの循環リンクを確認できません")
            }
            references.insert(current.path)
            let components = current.path.split(separator: "/")
            var next: URL?
            for index in components.indices {
                let prefix = URL(fileURLWithPath: "/" + components[...index].joined(separator: "/"))
                guard let info = try? fileStat(prefix, follow: false), isSymlink(info) else { continue }
                references.insert(prefix.path)
                let target = try fm.destinationOfSymbolicLink(atPath: prefix.path)
                let destination = target.hasPrefix("/") ? URL(fileURLWithPath: target)
                    : prefix.deletingLastPathComponent().appendingPathComponent(target)
                next = lexicalURL(destination.appendingPathComponent(components.dropFirst(index + 1).joined(separator: "/")))
                break
            }
            guard let next else { return references }
            current = next
        }
        throw ManagerError("共有キャッシュのリンクを確認できません")
    }
    // Foundation's standardizedFileURL may rewrite /private/var to /var on
    // macOS. Keep lexical normalization separate from symlink resolution so a
    // checked physical path cannot acquire a new symlink during validation.
    private func lexicalURL(_ path: URL) -> URL {
        var components: [Substring] = []
        for part in path.path.split(separator: "/") {
            if part == "." { continue }
            if part == ".." { if !components.isEmpty { components.removeLast() }; continue }
            components.append(part)
        }
        return URL(fileURLWithPath: "/" + components.joined(separator: "/"))
    }
    private func rejectSymlinkAncestors(_ path: URL) throws {
        var components = lexicalURL(path).path.split(separator: "/")
        while !components.isEmpty {
            let current = URL(fileURLWithPath: "/" + components.joined(separator: "/"))
            if let info = try? fileStat(current, follow: false), isSymlink(info) {
                throw ManagerError("リンク先のモデル本体は削除対象にできません")
            }
            components.removeLast()
        }
    }
    private func hasTraversal(_ path: String) -> Bool { path.components(separatedBy: "/").contains("..") || path.contains("\u{0}") }
    private func safeRelative(_ path: String) -> Bool { !path.isEmpty && !path.hasPrefix("/") && !path.hasPrefix("~") && !hasTraversal(path) }
    private func isInside(_ path: URL, _ root: URL, equal: Bool = false) -> Bool {
        let child = lexicalURL(path).path, parent = lexicalURL(root).path
        return (equal && child == parent) || child.hasPrefix(parent.hasSuffix("/") ? parent : parent + "/")
    }
    private func matches(_ pattern: String, in text: String, insensitive: Bool = false) -> [[String]] {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: insensitive ? .caseInsensitive : []) else { return [] }
        return regex.matches(in: text, range: NSRange(text.startIndex..., in: text)).map { match in
            (0..<match.numberOfRanges).map { index in Range(match.range(at: index), in: text).map { String(text[$0]) } ?? "" }
        }
    }
}
