"""Run the Swift catalog and deletion guards against disposable filesystem fixtures.

No production Python module, inference process, real model cache or listening port
is used. The harness injects process state into the same ModelStore used by the app.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REV_A, REV_B = "a" * 40, "b" * 40


@unittest.skipUnless(sys.platform == "darwin", "native macOS model management")
class NativeModelStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build = tempfile.TemporaryDirectory(prefix="mlx-swift-store-build-")
        cls.addClassCleanup(build.cleanup)
        cls.binary = Path(build.name) / "ModelStoreHarness"
        result = subprocess.run([
            "/usr/bin/swiftc", "-swift-version", "5",
            "-target", f"{platform.machine()}-apple-macos14.0",
            "-module-cache-path", str(Path(build.name) / "module-cache"),
            str(ROOT / "native/ManagerRuntime.swift"),
            str(ROOT / "native/ManagerModelStore.swift"),
            str(ROOT / "tests/native/ModelStoreHarness.swift"),
            "-o", str(cls.binary),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError("Swift fixture build failed:\n" + result.stdout + result.stderr)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mlx-swift-store-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ("server", "state", "cache", "gguf", "weights"):
            (self.root / name).mkdir()
        self.config = self.root / "server/models.conf"
        self.config.write_text("# retained comment\na=owner/model-a\nb=owner/model-b\n")
        self.profiles = []

    def run_actions(self, *actions):
        result = subprocess.run([str(self.binary)], input=json.dumps({"root": str(self.root), "actions": actions}),
                                capture_output=True, text=True, timeout=15,
                                env={**os.environ, "HF_HUB_OFFLINE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "", result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(len(value), len(actions))
        return value

    def ok(self, result):
        self.assertTrue(result["ok"], result.get("error"))
        return result["value"]

    def rejected(self, result, text=None):
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["error"])
        if text is not None:
            self.assertIn(text, result["error"])
        return result["error"]

    def snapshot(self, model="owner/model-a", revision=REV_A, *, complete=True,
                 architecture="ExampleForCausalLM", weight=b"fixture weights", ref=False):
        repo = self.root / "cache" / ("models--" + model.replace("/", "--"))
        snapshot = repo / "snapshots" / revision
        snapshot.mkdir(parents=True, exist_ok=True)
        blobs = repo / "blobs"
        blobs.mkdir(exist_ok=True)
        config = json.dumps({"architectures": [architecture]}).encode()
        for name, content in [("config.json", config)] + ([("model.safetensors", weight)] if complete else []):
            blob = blobs / hashlib.sha256(content).hexdigest()
            blob.write_bytes(content)
            link = snapshot / name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(os.path.relpath(blob, snapshot))
        if ref:
            (repo / "refs").mkdir(exist_ok=True)
            (repo / "refs/main").write_text(revision)
        return repo, snapshot

    def gguf(self, target="one", files=None):
        files = files or [(target, "main.gguf"), (target, "draft.gguf"), (target, "projector.gguf")]
        manifest = []
        for directory, name in files:
            path = self.root / "weights" / directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b"fixture-" + target.encode())
            manifest.append({"directory": directory, "file": name, "bytes": path.stat().st_size})
        profile = {"target": target, "alias": f"gguf:{target}", "model": f"model-{target}-GGUF",
                   "name": f"Model {target}", "manifest": f"{target}.json"}
        self.profiles.append(profile)
        self.write_profiles()
        (self.root / "gguf" / profile["manifest"]).write_text(json.dumps({"files": manifest}))
        return [self.root / "weights" / entry["directory"] / entry["file"] for entry in manifest]

    def write_profiles(self, **overrides):
        value = {"models_root": str(self.root / "weights"), "binary": "runtime/bin/llama-server",
                 "profiles": self.profiles, **overrides}
        (self.root / "gguf/profiles.json").write_text(json.dumps(value))

    @staticmethod
    def preview(*aliases, body=False, single=True, save="p"):
        return {"op": "preview", "aliases": list(aliases), "body": body, "single": single, "save": save}

    @staticmethod
    def delete(*aliases, body=False, single=True, use="p"):
        return {"op": "delete", "aliases": list(aliases), "body": body, "single": single, "use": use}

    @staticmethod
    def server(model, *, loading=False, command="", pid=123, arguments=None):
        value = {"running": True, "loading": loading, "pid": pid,
                 "active_model": model, "command": command}
        if arguments is not None:
            value["arguments"] = arguments
        return {"op": "set-server", "value": value}

    def test_cache_uses_complete_main_revision_without_falling_back(self):
        repo, _ = self.snapshot(ref=True)
        self.snapshot(revision=REV_B, complete=False)
        result = self.run_actions({"op": "cached"})
        self.assertEqual(self.ok(result[0]), ["owner/model-a"])
        (repo / "refs/main").write_text(REV_B)
        self.assertEqual(self.ok(self.run_actions({"op": "cached"})[0]), [])
        (repo / "refs/main").write_text("c" * 40)
        self.assertEqual(self.ok(self.run_actions({"op": "cached"})[0]), [])

    def test_cache_rejects_ambiguous_revisions_incomplete_and_zero_byte_weights(self):
        self.snapshot()
        self.assertEqual(self.ok(self.run_actions({"op": "cached"})[0]), ["owner/model-a"])
        self.snapshot(revision=REV_B)
        self.snapshot("owner/incomplete", complete=False)
        self.snapshot("owner/empty", weight=b"")
        self.assertEqual(self.ok(self.run_actions({"op": "cached"})[0]), [])

    def test_cache_generation_architecture_and_config_validation(self):
        self.snapshot("owner/encoder", architecture="BertModel")
        self.snapshot("owner/vision", architecture="ExampleForConditionalGeneration")
        _, invalid = self.snapshot("owner/invalid")
        (invalid / "config.json").unlink()
        (invalid / "config.json").write_text("[]")
        self.assertEqual(self.ok(self.run_actions({"op": "cached"})[0]), ["owner/vision"])

    def test_register_uses_cache_and_unique_internal_aliases(self):
        self.snapshot("owner/new-model")
        self.snapshot("other/new-model")
        result = self.run_actions(
            {"op": "register", "model": "owner/new-model"},
            {"op": "register", "model": "other/new-model"},
            {"op": "register", "model": "owner/new-model"},
            {"op": "register", "model": "owner/missing"},
            {"op": "aliases"})
        for entry in result[:3]: self.ok(entry)
        self.rejected(result[3])
        registered = self.ok(result[4])
        self.assertEqual([item["alias"] for item in registered], ["a", "b", "new-model", "new-model-2"])
        self.assertIn("# retained comment", self.config.read_text())

    def test_explicit_registration_rejects_invalid_lines_and_alias_collision(self):
        before = self.config.read_text()
        result = self.run_actions(
            {"op": "register", "alias": "a", "model": "different/model"},
            {"op": "register", "alias": "new", "model": "owner/model\nextra=owner/extra"},
            {"op": "register", "alias": "new", "model": ""},
            {"op": "register", "alias": "...", "model": "owner/model"})
        for entry in result: self.rejected(entry)
        self.assertEqual(self.config.read_text(), before)

    def test_names_defaults_and_settings_persist_without_process_operations(self):
        result = self.run_actions({"op": "set-listener", "error": "process lookup must not be used"},
            {"op": "rename", "alias": "b", "name": "名前を変更"},
            {"op": "set-default", "alias": "b"}, {"op": "models"}, {"op": "default"})
        for entry in result: self.ok(entry)
        self.assertEqual(next(item for item in result[3]["value"] if item["alias"] == "b")["name"], "名前を変更")
        self.assertEqual(result[4]["value"], "b")
        settings = json.loads((self.root / "state/models.json").read_text())
        self.assertEqual(settings["default_alias"], "b")
        self.assertEqual(settings["display_names"]["owner/model-b"], "名前を変更")
        self.assertEqual(self.ok(self.run_actions({"op": "default"})[0]), "b")

    def test_invalid_display_names_and_missing_default_do_not_write_settings(self):
        result = self.run_actions(*({"op": "rename", "alias": "a", "name": name}
                                  for name in ("", " ", "a\nb", "x" * 61)),
                                  {"op": "set-default", "alias": "missing"})
        for entry in result: self.rejected(entry)
        self.assertFalse((self.root / "state/models.json").exists())

    def test_unregister_preserves_weights_comments_and_other_model(self):
        _, snapshot = self.snapshot()
        weights = (snapshot / "model.safetensors").resolve()
        result = self.run_actions(self.preview("a"), self.delete("a"), {"op": "aliases"})
        for entry in result: self.ok(entry)
        self.assertTrue(weights.is_file())
        self.assertEqual([item["alias"] for item in result[2]["value"]], ["b"])
        self.assertIn("# retained comment", self.config.read_text())

    def test_active_model_and_duplicate_alias_cannot_be_unregistered_or_deleted(self):
        self.snapshot()
        self.config.write_text(self.config.read_text() + "duplicate=owner/model-a\n")
        before = self.config.read_text()
        actions = [self.server("owner/model-a"), {"op": "set-listener", "pid": 123}]
        for body in (False, True):
            for alias in ("a", "duplicate"):
                actions.append(self.preview(alias, body=body))
        for result in self.run_actions(*actions)[2:]: self.rejected(result, "起動中")
        self.assertEqual(self.config.read_text(), before)

    def test_loading_model_is_protected_before_it_has_a_listener(self):
        self.snapshot()
        result = self.run_actions(self.server("owner/model-a", loading=True),
                                  self.preview("a", body=True), self.preview("a"))
        for entry in result[1:]: self.rejected(entry, "起動中")

    def test_strict_listener_error_or_changed_pid_blocks_deletion(self):
        result = self.run_actions({"op": "set-listener", "error": "fixture cannot inspect listener"},
            self.preview("a"), {"op": "set-listener", "pid": 123}, self.preview("a"),
            self.server("owner/model-b", pid=456), self.preview("a"))
        for index in (1, 3, 5): self.rejected(result[index])

    def test_unknown_active_model_and_unknown_relative_path_fail_closed(self):
        self.snapshot()
        result = self.run_actions(self.server("unregistered/unknown-model"), self.preview("a", body=True),
            self.server(""), self.preview("a"), self.server("./weights/unknown"), self.preview("a", body=True))
        for index in (1, 3, 5): self.rejected(result[index])

    def test_lab_busy_blocks_body_deletion_but_allows_unregister(self):
        self.snapshot()
        result = self.run_actions({"op": "set-lab-busy", "value": True},
                                  self.preview("a", body=True), self.preview("a"), self.delete("a"))
        self.rejected(result[1], "実験ラボ")
        self.ok(result[2]); self.ok(result[3])

    def test_default_can_be_removed_while_another_model_runs(self):
        result = self.run_actions({"op": "set-default", "alias": "a"}, self.server("owner/model-b"),
            {"op": "set-listener", "pid": 123}, self.preview("a"), self.delete("a"),
            {"op": "settings"}, {"op": "default"})
        for entry in result: self.ok(entry)
        self.assertNotIn("default_alias", result[5]["value"])
        self.assertEqual(result[6]["value"], "b")

    def test_gguf_delete_only_selected_manifest_files_and_preserve_other_model(self):
        selected = self.gguf("one")
        other = self.gguf("two")
        extra = self.root / "weights/one/keep.gguf"
        extra.write_bytes(b"not in manifest")
        result = self.run_actions(self.preview("gguf:one", body=True), self.delete("gguf:one", body=True),
            {"op": "models"}, {"op": "restore-gguf"}, {"op": "models"})
        for entry in result: self.ok(entry)
        self.assertEqual(result[0]["value"]["file_count"], 3)
        self.assertFalse(any(path.exists() for path in selected))
        self.assertTrue(all(path.exists() for path in other))
        self.assertEqual(extra.read_bytes(), b"not in manifest")
        self.assertNotIn("gguf:one", [item["alias"] for item in result[2]["value"]])
        self.assertIn("gguf:two", [item["alias"] for item in result[2]["value"]])
        self.assertIn("gguf:one", [item["alias"] for item in result[4]["value"]])

    def test_gguf_unregister_hides_profile_without_deleting_weights(self):
        files = self.gguf()
        result = self.run_actions({"op": "set-default", "alias": "gguf:one"},
            self.preview("gguf:one"), self.delete("gguf:one"), {"op": "settings"})
        for entry in result: self.ok(entry)
        self.assertTrue(all(path.is_file() for path in files))
        self.assertIn("gguf:one", result[3]["value"]["hidden_gguf_aliases"])
        self.assertNotIn("default_alias", result[3]["value"])

    def test_gguf_traversal_symlink_file_and_symlink_ancestor_are_rejected(self):
        files = self.gguf()
        keep = self.root / "outside.gguf"
        keep.write_bytes(b"keep")
        manifest = self.root / "gguf/one.json"
        original = manifest.read_text()
        manifest.write_text(json.dumps({"files": [{"directory": "..", "file": "outside.gguf", "bytes": 4}]}))
        self.rejected(self.run_actions(self.preview("gguf:one", body=True))[0])
        manifest.write_text(original)
        files[0].unlink(); files[0].symlink_to(keep)
        self.rejected(self.run_actions(self.preview("gguf:one", body=True))[0], "リンク")
        files[0].unlink(); files[0].write_bytes(b"fixture-one")
        folder = self.root / "weights/one"
        renamed = self.root / "real-one"
        folder.rename(renamed); folder.symlink_to(renamed, target_is_directory=True)
        self.rejected(self.run_actions(self.preview("gguf:one", body=True))[0], "リンク")
        self.assertEqual(keep.read_bytes(), b"keep")

    def test_invalid_gguf_root_or_profiles_fail_closed_instead_of_disappearing(self):
        self.gguf()
        for root in ("relative/models", str(self.root / "weights/../weights")):
            with self.subTest(root=root):
                self.write_profiles(models_root=root)
                self.rejected(self.run_actions({"op": "models"})[0])
        (self.root / "gguf/profiles.json").write_text("{broken")
        self.rejected(self.run_actions({"op": "models"})[0])

    def test_gguf_profiles_sharing_the_active_main_file_cannot_be_removed(self):
        files = [("shared", "main.gguf")]
        self.gguf("one", files)
        self.gguf("two", files)
        result = self.run_actions(self.server("model-two-GGUF"),
                                  self.preview("gguf:one"), self.preview("gguf:one", body=True))
        for entry in result[1:]: self.rejected(entry, "起動中")

    def test_external_projector_and_draft_files_are_protected(self):
        files = self.gguf()
        active = self.root / "external-main.gguf"
        active.write_bytes(b"active model")
        for flag, referenced in (("--mmproj", files[2]), ("-md", files[1])):
            with self.subTest(flag=flag):
                command = f"llama-server -m {active} {flag} {referenced} --port 8080"
                result = self.run_actions(self.server(str(active), command=command),
                    self.preview("gguf:one"), self.preview("gguf:one", body=True))
                self.ok(result[1])
                self.rejected(result[2], "共有")
        self.assertTrue(all(path.is_file() for path in files))

    def test_explicit_gguf_main_file_is_protected_when_not_first_in_manifest(self):
        files = self.gguf("one", [("one", "draft.gguf"), ("one", "main.gguf")])
        self.profiles[0]["model_file"] = "main.gguf"
        self.write_profiles()
        result = self.run_actions(self.server(str(files[1])),
                                  self.preview("gguf:one"), self.preview("gguf:one", body=True))
        for entry in result[1:]:
            self.rejected(entry, "起動中")
        self.assertTrue(all(path.is_file() for path in files))

    def test_exact_arguments_protect_paths_with_spaces_and_option_like_names(self):
        files = self.gguf("one", [("shared files", "main.gguf"),
                                  ("shared files", "projector --port.gguf")])
        self.gguf("two")
        active = self.root / "external --model name.gguf"
        active.write_bytes(b"active model")
        for option in (["--mmproj", str(files[1])], [f"--mmproj={files[1]}"]):
            with self.subTest(option=option):
                arguments = ["llama-server", "-m", str(active), *option, "--port", "8080"]
                result = self.run_actions(self.server(str(active), command=" ".join(arguments),
                                                     arguments=arguments),
                    self.preview("gguf:one", body=True), self.preview("gguf:two", body=True))
                self.rejected(result[1], "共有")
                self.ok(result[2])

    def test_split_gguf_sibling_is_protected_for_external_main(self):
        first = self.root / "weights/split/model-00001-of-00002.gguf"
        first.parent.mkdir()
        first.write_bytes(b"first shard")
        sibling = self.gguf("one", [("split", "model-00002-of-00002.gguf")])[0]
        result = self.run_actions(self.server(str(first), command=f"llama-server -m {first} --port 8080"),
                                  self.preview("gguf:one", body=True))
        self.rejected(result[1], "共有")
        self.assertTrue(sibling.is_file())

    def test_active_hf_snapshot_path_resolves_to_same_repository(self):
        _, snapshot = self.snapshot()
        result = self.run_actions(self.server(str(snapshot), command=f"python -m mlx_lm.server --model {snapshot}"),
                                  self.preview("a"), self.preview("a", body=True))
        for entry in result[1:]: self.rejected(entry, "起動中")

    def test_cached_main_does_not_hide_draft_and_adapter_references(self):
        self.snapshot("owner/model-a")
        self.snapshot("owner/model-b")
        for flag in ("--draft-model", "--model-draft", "--adapter-path"):
            with self.subTest(flag=flag):
                command = f"python -m mlx_lm.server --model owner/model-b {flag} owner/model-a --port 8080"
                result = self.run_actions(self.server("owner/model-b", command=command), self.preview("a", body=True))
                self.rejected(result[1], "共有")
        result = self.run_actions(self.server("owner/model-b", command="python -m mlx_lm.server --model owner/model-b --adapter-path unknown/adapter"),
                                  self.preview("a", body=True))
        self.rejected(result[1])

    def test_shared_hf_blob_used_by_active_repository_cannot_be_deleted(self):
        _, first = self.snapshot("owner/model-a")
        _, second = self.snapshot("owner/model-b", weight=b"other weights")
        blob = (first / "model.safetensors").resolve()
        (second / "model.safetensors").unlink()
        (second / "model.safetensors").symlink_to(os.path.relpath(blob, second))
        result = self.run_actions(self.server("owner/model-b"), self.preview("a", body=True))
        self.rejected(result[1], "共有")
        self.assertTrue(blob.is_file())

    def test_unreferenced_hf_blob_used_as_external_projector_is_protected(self):
        repo, _ = self.snapshot()
        orphan = repo / "blobs/orphan-projector.gguf"
        orphan.write_bytes(b"projector in use")
        active = self.root / "external-main.gguf"
        active.write_bytes(b"active main")
        arguments = ["llama-server", "-m", str(active), "--mmproj", str(orphan)]
        result = self.run_actions(self.server(str(active), command=" ".join(arguments), arguments=arguments),
                                  self.preview("a", body=True))
        self.rejected(result[1], "共有")
        self.assertEqual(orphan.read_bytes(), b"projector in use")

    def test_hf_deletion_preserves_blob_referenced_by_another_repository(self):
        first_repo, first = self.snapshot("owner/model-a", ref=True)
        self.snapshot("owner/model-a", revision=REV_B, weight=b"second revision")
        _, other = self.snapshot("owner/model-b", weight=b"other weights", ref=True)
        shared = (first / "model.safetensors").resolve()
        original_other = (other / "model.safetensors").resolve()
        (other / "model.safetensors").unlink()
        (other / "model.safetensors").symlink_to(os.path.relpath(shared, other))
        result = self.run_actions(self.preview("a", body=True), self.delete("a", body=True),
                                  {"op": "aliases"}, {"op": "cached"})
        for entry in result: self.ok(entry)
        self.assertEqual((other / "model.safetensors").read_bytes(), b"fixture weights")
        self.assertTrue(shared.is_file())
        self.assertTrue(original_other.is_file())
        self.assertFalse((first_repo / "snapshots" / REV_A).exists())
        self.assertFalse((first_repo / "snapshots" / REV_B).exists())
        self.assertFalse((first_repo / "refs/main").exists())
        self.assertEqual([item["alias"] for item in result[2]["value"]], ["b"])
        self.assertEqual(result[3]["value"], ["owner/model-b"])

    def test_hf_external_blob_target_is_never_deleted(self):
        _, snapshot = self.snapshot()
        outside = self.root / "outside.safetensors"
        outside.write_bytes(b"external data")
        (snapshot / "model.safetensors").unlink()
        (snapshot / "model.safetensors").symlink_to(outside)
        results = self.run_actions(self.preview("a", body=True), self.delete("a", body=True))
        # Implementations may reject the malformed repository at preview, or unlink
        # its local references. Neither policy may follow and delete external data.
        self.assertTrue(any(not item["ok"] for item in results) or not snapshot.exists())
        self.assertEqual(outside.read_bytes(), b"external data")

    def test_hf_blob_reached_through_another_repository_directory_link_is_preserved(self):
        first_repo, first = self.snapshot("owner/model-a")
        other_repo, other = self.snapshot("owner/model-b", weight=b"other weights")
        shared = (first / "model.safetensors").resolve()
        directory_link = other_repo / "blobs/shared-directory"
        directory_link.symlink_to(first_repo / "blobs", target_is_directory=True)
        (other / "model.safetensors").unlink()
        (other / "model.safetensors").symlink_to(os.path.relpath(directory_link / shared.name, other))
        result = self.run_actions(self.preview("a", body=True), self.delete("a", body=True), {"op": "cached"})
        for entry in result:
            self.ok(entry)
        self.assertTrue(shared.is_file())
        self.assertEqual((other / "model.safetensors").read_bytes(), b"fixture weights")
        self.assertEqual(result[2]["value"], ["owner/model-b"])

    def test_preview_detects_config_settings_and_same_size_inode_replacement(self):
        self.gguf()
        for mutation in (
            {"op": "write", "path": "server/models.conf", "text": self.config.read_text() + "# changed\n"},
            {"op": "rename", "alias": "gguf:one", "name": "Changed"},
            {"op": "replace", "path": "weights/one/main.gguf", "text": "changed-one"},
        ):
            with self.subTest(mutation=mutation):
                result = self.run_actions(self.preview("gguf:one", body=True), mutation,
                                          self.delete("gguf:one", body=True))
                self.ok(result[0]); self.ok(result[1]); self.rejected(result[2], "確認後")
                self.assertTrue((self.root / "weights/one/main.gguf").is_file())

    def test_preview_detects_revision_and_ref_changes(self):
        repo, _ = self.snapshot(ref=True)
        self.snapshot(revision=REV_B)
        result = self.run_actions(self.preview("a", body=True),
            {"op": "write", "path": str((repo / "refs/main").relative_to(self.root)), "text": REV_B},
            self.delete("a", body=True))
        self.ok(result[0]); self.ok(result[1]); self.rejected(result[2], "確認後")
        self.assertTrue((repo / "snapshots" / REV_A).is_dir())
        self.assertTrue((repo / "snapshots" / REV_B).is_dir())

    def test_model_becoming_active_after_preview_is_not_deleted(self):
        files = self.gguf()
        result = self.run_actions(self.preview("gguf:one", body=True), self.server("model-one-GGUF"),
                                  self.delete("gguf:one", body=True))
        self.ok(result[0]); self.rejected(result[2], "起動中")
        self.assertTrue(all(path.is_file() for path in files))

    def test_empty_or_forged_preview_fingerprint_never_deletes(self):
        files = self.gguf()
        for fingerprint in ("", "0" * 64):
            with self.subTest(fingerprint=fingerprint):
                action = self.delete("gguf:one", body=True)
                action["fingerprint"] = fingerprint
                self.rejected(self.run_actions(action)[0])
        self.assertTrue(all(path.is_file() for path in files))

    def test_bulk_validates_all_targets_before_any_mutation(self):
        before = self.config.read_text()
        result = self.run_actions(self.preview("a", "missing", single=False),
                                  self.delete("a", "missing", single=False))
        for entry in result: self.rejected(entry)
        self.assertEqual(self.config.read_text(), before)
        for aliases in ([], ["a", "a"], ["a"] * 101):
            self.rejected(self.run_actions(self.preview(*aliases, single=False))[0])

    def test_bulk_stale_second_target_prevents_first_target_deletion(self):
        first = self.gguf("one")
        second = self.gguf("two")
        result = self.run_actions(self.preview("gguf:one", "gguf:two", body=True, single=False),
            {"op": "replace", "path": "weights/two/main.gguf", "text": "changed-two"},
            self.delete("gguf:one", "gguf:two", body=True, single=False))
        self.ok(result[0]); self.rejected(result[2], "確認後")
        self.assertTrue(all(path.is_file() for path in first + second))

    def test_bulk_body_deduplicates_shared_model_aliases_and_clears_default(self):
        repo, _ = self.snapshot()
        self.config.write_text(self.config.read_text() + "duplicate=owner/model-a\n")
        result = self.run_actions({"op": "set-default", "alias": "duplicate"},
            self.preview("a", "duplicate", body=True, single=False),
            self.delete("a", "duplicate", body=True, single=False), {"op": "aliases"}, {"op": "settings"})
        for entry in result: self.ok(entry)
        self.assertEqual(len(result[1]["value"]["items"]), 1)
        self.assertEqual([item["alias"] for item in result[3]["value"]], ["b"])
        self.assertNotIn("default_alias", result[4]["value"])
        self.assertFalse((repo / "snapshots" / REV_A).exists())

    def test_bulk_reports_completed_and_remaining_when_state_changes_between_deletions(self):
        first = self.gguf("one")
        second = self.gguf("two")
        result = self.run_actions({"op": "lab-busy-after-missing", "path": "weights/one/main.gguf"},
            self.preview("gguf:one", "gguf:two", body=True, single=False),
            self.delete("gguf:one", "gguf:two", body=True, single=False))
        self.ok(result[1])
        error = self.rejected(result[2])
        self.assertIn("Model one", error)
        self.assertIn("Model two", error)
        self.assertFalse(any(path.exists() for path in first))
        self.assertTrue(all(path.is_file() for path in second))


if __name__ == "__main__":
    unittest.main()
