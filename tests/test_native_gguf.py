"""Exercise native GGUF launches with tiny files and a socket-free child fixture.

The real declarative profiles supply the configuration under test. All manifests,
verification records, weights and executables are built inside temporary roots;
neither real model data nor the machine's 8080 listener is inspected or changed.
"""
import copy
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "native macOS GGUF launch lifecycle")
class NativeGGUFTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build = tempfile.TemporaryDirectory(prefix="mlx-native-gguf-build-")
        cls.addClassCleanup(build.cleanup)
        cls.binary = Path(build.name) / "GGUFHarness"
        # Overrides support independent implementation worktrees; CI uses this repo.
        runtime = Path(os.environ.get("MLX_NATIVE_RUNTIME_SOURCE", ROOT / "native/ManagerRuntime.swift"))
        store = Path(os.environ.get("MLX_NATIVE_MODEL_STORE_SOURCE", ROOT / "native/ManagerModelStore.swift"))
        cls.profile_source = Path(os.environ.get("MLX_NATIVE_GGUF_PROFILES_SOURCE", ROOT / "gguf/profiles.json"))
        cls.source_bytes = cls.profile_source.read_bytes()
        cls.source = json.loads(cls.source_bytes)
        result = subprocess.run([
            "/usr/bin/swiftc", "-swift-version", "5", "-target", f"{platform.machine()}-apple-macos14.0",
            "-module-cache-path", str(Path(build.name) / "module-cache"),
            str(runtime), str(store), str(ROOT / "tests/native/GGUFHarness.swift"), "-o", str(cls.binary),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError("Swift GGUF fixture build failed:\n" + result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        if cls.profile_source.read_bytes() != cls.source_bytes:
            raise AssertionError("source profiles changed during fixture tests")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mlx-native-gguf-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.weights = self.root / "weights with spaces"
        for directory in ("server", "state", "cache", "gguf", self.weights.name):
            (self.root / directory).mkdir()
        (self.root / "server/models.conf").write_text("# fixture only\n")
        self.config = copy.deepcopy(self.source)
        self.config["models_root"] = str(self.weights)
        self.assertEqual(self.config["binary"], "runtime/bin/llama-server")
        self.profiles = {item["target"]: item for item in self.config["profiles"]}
        self.assertEqual(set(self.profiles), {"125b", "gemma-gguf"})
        self.files = {}
        for target, profile in self.profiles.items():
            names = [profile["model_file"]]
            split = re.fullmatch(r"(.+)-[0-9]{5}-of-([0-9]{5})\.gguf", names[0])
            if split:
                names = [f"{split[1]}-{index:05d}-of-{split[2]}.gguf" for index in range(1, int(split[2]) + 1)]
            names += [match[1] for value in profile["arguments"] if (match := re.fullmatch(r"\{model:(.+)\}", value))]
            entries = []
            for name in dict.fromkeys(names):
                self.assertEqual(Path(name).name, name)
                path = self.weights / target / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(("fixture: " + target + "/" + name).encode())
                self.files[(target, name)] = path
                entries.append({"directory": target, "file": name, "bytes": path.stat().st_size})
            self.fixture_path(profile["manifest"]).write_text(json.dumps({"files": entries}))
            for relative in profile["required_verifications"]:
                path = self.fixture_path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"fixture": true}\n')
        self.executable = self.fixture_path(self.config["binary"])
        self.executable.parent.mkdir(parents=True)
        shutil.copyfile(self.binary, self.executable)
        self.executable.chmod(0o755)
        self.write_profiles()

    def fixture_path(self, relative):
        path = Path(relative)
        self.assertFalse(path.is_absolute())
        self.assertNotIn("..", path.parts)
        return self.root / "gguf" / path

    def write_profiles(self):
        (self.root / "gguf/profiles.json").write_text(json.dumps(self.config))

    def launch(self, target, thinking=False):
        capture = self.root / "captured.json"
        capture.unlink(missing_ok=True)
        environment = {**os.environ, "MLX_GGUF_FIXTURE_CAPTURE": str(capture),
                       "MLX_GGUF_FIXTURE_SENTINEL": "keep-me", "GGML_ALLOC_FREE_DELAY": "inherited-delay",
                       "LLAMA_GRAPH_CACHE": "inherited-cache", "LLAMA_SPEC_MTP_MERGE": "inherited-merge",
                       "LLAMA_SPEC_STOP": "inherited-stop", "LLAMA_SPEC_STOP_PARAMS": "inherited-params"}
        request = {"root": str(self.root), "alias": self.profiles[target]["alias"], "thinking": thinking}
        result = subprocess.run([str(self.binary)], input=json.dumps(request), capture_output=True,
                                text=True, timeout=25, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def assert_rejected_before_spawn(self, result, message):
        self.assertFalse(result["ok"], result)
        self.assertIn(message, result["error"])
        self.assertEqual(result["owned"], [])
        self.assertEqual(result["terminated"], [])
        self.assertFalse(result["captured"])
        for record in ("llama-server.json", "managed-server.json"):
            self.assertFalse(self.fixture_path("evidence/" + record).exists())

    def assert_lifecycle(self, result, target, thinking, expected_arguments):
        self.assertTrue(result["ok"], result.get("error"))
        pid = result["pid"]
        self.assertGreater(pid, 1)
        self.assertNotEqual(pid, result["harness_pid"])
        self.assertEqual(result["owned"], [pid])
        self.assertEqual(result["terminated"], [pid])
        captured = result["capture"]
        self.assertEqual(captured["pid"], pid)
        self.assertEqual(captured["ppid"], result["harness_pid"])
        self.assertEqual(Path(captured["cwd"]).resolve(), self.root / "gguf")
        tail = ["--reasoning", "on" if thinking else "off", "--host", "127.0.0.1", "--port", "8080", "--alias", "default_model"]
        self.assertEqual(captured["argv"], [str(self.executable), *expected_arguments, *tail])
        profile = self.profiles[target]
        record = result["record"]
        self.assertEqual(result["managed_record"], record)
        self.assertEqual(record["pid"], pid)
        self.assertEqual(record["argv"], captured["argv"])
        for key in ("target", "model", "alias"):
            self.assertEqual(record[key], profile[key])
        self.assertIs(record["thinking"], thinking)
        self.assertTrue(record["identity"])
        running = result["running"]
        self.assertIs(running["running"], True)
        self.assertIs(running["trusted"], True)
        self.assertIs(running["loading"], True)  # The fixture never listens on 8080.
        self.assertIs(running["thinking"], thinking)
        self.assertEqual(running["active_model"], profile["model"])
        self.assertEqual(running["arguments"], captured["argv"])
        self.assertIs(result["stopped"]["running"], False)
        self.assertIn("fixture llama-server ready", self.fixture_path("logs/llama-server.log").read_text())
        environment = captured["environment"]
        self.assertEqual(environment["MLX_GGUF_FIXTURE_SENTINEL"], "keep-me")
        self.assertEqual(environment["GGML_ALLOC_FREE_DELAY"], "4")
        self.assertEqual(environment["LLAMA_GRAPH_CACHE"], "16")
        return environment

    def test_qwen_thinking_modes_preserve_split_main_draft_and_context(self):
        target = "125b"
        main = self.files[(target, "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf")]
        draft = self.files[(target, "mtp-Qwen3.8-Flash-Next-Q8_0.gguf")]
        arguments = ["-m", str(main), "-md", str(draft), "--spec-type", "draft-mtp", "--spec-draft-n-max", "7",
                     "-ngl", "99", "-c", "16384", "-np", "1"]
        for thinking in (False, True):
            with self.subTest(thinking=thinking):
                environment = self.assert_lifecycle(self.launch(target, thinking), target, thinking, arguments)
                self.assertEqual(environment["LLAMA_SPEC_MTP_MERGE"], "1")
                self.assertEqual(environment["LLAMA_SPEC_STOP"], "1")
                self.assertEqual(environment["LLAMA_SPEC_STOP_PARAMS"], "inherited-params")

    def test_gemma_thinking_modes_preserve_projector_draft_batch_context_and_unset_environment(self):
        target = "gemma-gguf"
        main = self.files[(target, "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf")]
        projector = self.files[(target, "mmproj-F16.gguf")]
        draft = self.files[(target, "mtp-gemma-4-26B-A4B-it.gguf")]
        arguments = ["-m", str(main), "--mmproj", str(projector), "-md", str(draft), "--spec-type", "draft-mtp",
                     "--spec-draft-n-max", "2", "-ngld", "99", "-b", "2048", "-ub", "2048", "-ngl", "99",
                     "-c", "65536", "-np", "1"]
        for thinking in (False, True):
            with self.subTest(thinking=thinking):
                environment = self.assert_lifecycle(self.launch(target, thinking), target, thinking, arguments)
                for key in ("LLAMA_SPEC_MTP_MERGE", "LLAMA_SPEC_STOP", "LLAMA_SPEC_STOP_PARAMS"):
                    self.assertNotIn(key, environment)

    def test_every_required_verification_blocks_launch_when_missing(self):
        for target, profile in self.profiles.items():
            for relative in profile["required_verifications"]:
                with self.subTest(target=target, verification=relative):
                    path = self.fixture_path(relative)
                    content = path.read_bytes()
                    path.unlink()
                    try:
                        self.assert_rejected_before_spawn(self.launch(target), "検証記録")
                    finally:
                        path.write_bytes(content)

    def test_every_manifest_weight_including_second_shard_blocks_launch_when_missing(self):
        for (target, name), path in self.files.items():
            with self.subTest(target=target, file=name):
                content = path.read_bytes()
                path.unlink()
                try:
                    self.assert_rejected_before_spawn(self.launch(target), "不足")
                finally:
                    path.write_bytes(content)

    def test_every_manifest_weight_blocks_launch_when_size_is_wrong(self):
        for (target, name), path in self.files.items():
            with self.subTest(target=target, file=name):
                content = path.read_bytes()
                path.write_bytes(content[:-1])
                try:
                    self.assert_rejected_before_spawn(self.launch(target), "サイズ")
                finally:
                    path.write_bytes(content)

    def test_another_process_holding_switch_lock_blocks_both_profiles_before_spawn(self):
        import fcntl
        with self.fixture_path("evidence/switch.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for target in self.profiles:
                with self.subTest(target=target):
                    self.assert_rejected_before_spawn(self.launch(target), "別のモデル操作")

    def test_nonexecutable_runtime_is_rejected_before_spawn(self):
        self.executable.chmod(0o600)
        for target in self.profiles:
            with self.subTest(target=target):
                self.assert_rejected_before_spawn(self.launch(target), "実行ファイル")

    def test_unknown_model_token_is_rejected_before_spawn(self):
        for target, profile in self.profiles.items():
            with self.subTest(target=target):
                original = profile["arguments"]
                profile["arguments"] = [*original, "--mmproj", "{model:missing-projector.gguf}"]
                self.write_profiles()
                try:
                    self.assert_rejected_before_spawn(self.launch(target), "一意")
                finally:
                    profile["arguments"] = original
                    self.write_profiles()

    def test_profile_cannot_override_managed_launch_arguments(self):
        profile = self.profiles["125b"]
        original = profile["arguments"]
        for argument in ("--port=9999", "--host=0.0.0.0", "--alias=other", "--reasoning=on", "--model=other", "-m"):
            with self.subTest(argument=argument):
                profile["arguments"] = [*original, argument]
                self.write_profiles()
                self.assert_rejected_before_spawn(self.launch("125b"), "上書き")
        profile["arguments"] = original
        self.write_profiles()


if __name__ == "__main__":
    unittest.main()
