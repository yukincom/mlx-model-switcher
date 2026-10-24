"""Native process identity and lifecycle guards; never start a real model or use 8080."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PID = 424242
STARTED = "Sat Oct 24 09:00:00 2026"


@unittest.skipUnless(sys.platform == "darwin", "native macOS process inspection")
class NativeRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="mlx-runtime-build-")
        cls.binary = Path(cls.build.name) / "RuntimeHarness"
        # Optional source overrides let independent test worktrees exercise the
        # in-progress native implementation; integrated/CI runs use this checkout.
        runtime = Path(os.environ.get("MLX_NATIVE_RUNTIME_SOURCE", ROOT / "native/ManagerRuntime.swift"))
        store = Path(os.environ.get("MLX_NATIVE_MODEL_STORE_SOURCE", ROOT / "native/ManagerModelStore.swift"))
        result = subprocess.run([
            "/usr/bin/swiftc", "-swift-version", "5", "-module-cache-path", str(Path(cls.build.name) / "module-cache"),
            str(runtime), str(store), str(ROOT / "tests/native/RuntimeHarness.swift"), "-o", str(cls.binary),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            cls.build.cleanup()
            raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mlx-runtime-fixture-")
        self.addCleanup(self.temporary.cleanup)
        # /var and /tmp are macOS symlinks; fixtures use their actual directory.
        self.root = Path(self.temporary.name).resolve()
        for directory in ("launcher", "state", "gguf", "cache"):
            (self.root / directory).mkdir()
        (self.root / "launcher/models.conf").write_text("fixture=owner/Fixture-4bit\n", encoding="utf-8")

    def request(self, operation, process=None, **values):
        payload = {"operation": operation, "root": str(self.root), **values}
        if process is not None:
            payload["process"] = process
        result = subprocess.run([str(self.binary)], input=json.dumps(payload),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def mlx_process(self, *, listener=PID, arguments=None, started=STARTED):
        arguments = arguments or ["/fixture/python", "-m", "mlx_lm.server", "--model", "owner/Fixture-4bit",
                                  "--port", "8080"]
        return {"pid": PID, "listener": listener, "command": " ".join(arguments),
                "arguments": arguments, "started": started}

    def record_mlx(self, **updates):
        record = {"pid": PID, "started": STARTED, "alias": "fixture", **updates}
        (self.root / "state/starting.json").write_text(json.dumps(record), encoding="utf-8")

    def gguf_process(self, **record_updates):
        binary = str(self.root / "gguf/runtime/bin/llama-server")
        arguments = [binary, "-m", str(self.root / "weights/model with spaces.gguf"), "--port", "8080"]
        process = self.mlx_process(listener=None, arguments=arguments)
        (self.root / "gguf/profiles.json").write_text(json.dumps({
            "models_root": str(self.root / "weights"), "binary": "runtime/bin/llama-server",
            "profiles": [{"target": "fixture-gguf", "alias": "gguf:fixture", "model": "fixture-GGUF",
                          "name": "Fixture GGUF", "manifest": "manifest.json"}],
        }), encoding="utf-8")
        (self.root / "gguf/evidence").mkdir()
        record = {"pid": PID, "identity": STARTED + " " + process["command"], "argv": arguments,
                  "target": "fixture-gguf", "thinking": False, **record_updates}
        (self.root / "gguf/evidence/llama-server.json").write_text(json.dumps(record), encoding="utf-8")
        return process

    def assertBlocked(self, result, message):
        self.assertIn(message, result.get("error", ""), result)
        self.assertEqual(result.get("terminated"), [], result)

    def test_real_sysctl_arguments_preserve_spaces_unicode_empty_and_json(self):
        arguments = ["inspect-self", "two words", "日本語のモデル", "", '{"enable_thinking":true}', "--port=8080"]
        result = subprocess.run([str(self.binary), *arguments], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["arguments"], [str(self.binary), *arguments])
        self.assertEqual(value["arguments"], value["expected"])
        self.assertTrue(value["started"])
        self.assertIn("日本語のモデル", value["command"])

    def test_record_hook_writes_identity_for_only_its_own_fixture_process(self):
        value = self.request("record-self", alias="fixture", thinking=True)
        self.assertEqual(value["record"]["pid"], value["pid"])
        self.assertEqual(value["record"]["alias"], "fixture")
        self.assertIs(value["record"]["thinking"], True)
        self.assertTrue(value["record"]["started"])

    def test_json_boolean_type_is_not_coerced_from_numbers_or_strings(self):
        values = [True, False, 1, 0, 1.0, 0.0, 2, "true", "false", None, {}, []]
        self.assertEqual(self.request("strict-bools", values=values), [True, False] + [None] * 10)

    def test_flag_values_keep_argument_boundaries_and_support_equals(self):
        value = self.request("flag", arguments=["--model", "/weights/日本語 model", "--port=8080"], names=["--model"])
        self.assertEqual(value["value"], "/weights/日本語 model")
        value = self.request("flag", arguments=["--port=8080"], names=["--port"])
        self.assertEqual(value["value"], "8080")

    def test_empty_port_and_absent_record_are_stopped(self):
        server = self.request("status", self.mlx_process(listener=None))["server"]
        self.assertIs(server["running"], False)
        self.assertIsNone(server["pid"])
        self.assertIs(server["trusted"], False)

    def test_known_module_without_own_record_is_untrusted(self):
        server = self.request("status", self.mlx_process())["server"]
        self.assertIs(server["running"], True)
        self.assertIs(server["trusted"], False)

    def test_matching_record_and_port_trust_the_mlx_process(self):
        self.record_mlx(thinking=False)
        server = self.request("status", self.mlx_process())["server"]
        self.assertIs(server["trusted"], True)
        self.assertIs(server["thinking"], False)
        self.assertEqual(server["active_model"], "owner/Fixture-4bit")
        self.assertNotIn("loading", server)

    def test_record_does_not_trust_a_different_pid_or_wrong_port(self):
        self.record_mlx(pid=PID + 1)
        self.assertIs(self.request("status", self.mlx_process())["server"]["trusted"], False)
        self.record_mlx()
        process = self.mlx_process()
        process["arguments"][-1] = "8081"
        process["command"] = " ".join(process["arguments"])
        self.assertIs(self.request("status", process)["server"]["trusted"], False)

    def test_reused_pid_is_untrusted_and_cannot_be_stopped(self):
        self.record_mlx()
        process = self.mlx_process(started="Sat Oct 24 10:00:00 2026")
        self.assertIs(self.request("status", process)["server"]["trusted"], False)
        self.assertBlocked(self.request("blocked-stop", process, expected_pid=PID,
                                        expected_command=process["command"]), "既知の起動記録")

    def test_reused_pending_pid_is_not_reported_as_loading(self):
        self.record_mlx()
        process = self.mlx_process(listener=None, started="Sat Oct 24 10:00:00 2026")
        self.assertIs(self.request("status", process)["server"]["running"], False)

    def test_loading_launcher_is_trusted_and_blocks_another_start(self):
        self.record_mlx(thinking=True)
        process = self.mlx_process(listener=None, arguments=["/bin/bash", str(self.root / "launcher/start_server.sh"), "fixture"])
        server = self.request("status", process)["server"]
        self.assertIs(server["running"], True)
        self.assertIs(server["loading"], True)
        self.assertIs(server["trusted"], True)
        self.assertEqual(server["active_model"], "owner/Fixture-4bit")
        self.assertBlocked(self.request("blocked-start", process), "すでに稼働中")

    def test_unknown_listener_blocks_start_and_stop(self):
        process = self.mlx_process(arguments=["/fixture/other-service", "--port", "8080"])
        self.assertBlocked(self.request("blocked-start", process), "すでに稼働中")
        self.assertBlocked(self.request("blocked-stop", process, expected_pid=PID,
                                        expected_command=process["command"]), "既知の起動記録")

    def test_listener_inspection_error_blocks_start(self):
        process = {**self.mlx_process(listener=None), "listener_error": True}
        self.assertBlocked(self.request("blocked-start", process), "fixture listener unavailable")

    def test_changed_displayed_pid_and_command_never_signal(self):
        self.record_mlx()
        process = self.mlx_process()
        for pid, command in ((PID + 1, process["command"]), (PID, process["command"] + " changed")):
            with self.subTest(pid=pid, command=command):
                self.assertBlocked(self.request("blocked-stop", process, expected_pid=pid,
                                                expected_command=command), "表示後に切り替わった")

    def test_identity_change_immediately_before_stop_never_signals(self):
        self.record_mlx()
        process = {**self.mlx_process(), "started_sequence": [STARTED, STARTED, "Sat Oct 24 11:00:00 2026"]}
        self.assertBlocked(self.request("blocked-stop", process, expected_pid=PID,
                                        expected_command=process["command"]), "停止直前に対象プロセスが変わりました")

    def test_thinking_comes_from_exact_json_boolean_or_record(self):
        self.record_mlx()
        for value, expected in ((True, True), (False, False), (1, None), ("true", None)):
            with self.subTest(value=value):
                process = self.mlx_process()
                process["arguments"] += ["--chat-template-args", json.dumps({"enable_thinking": value})]
                process["command"] = " ".join(process["arguments"])
                self.assertIs(self.request("status", process)["server"]["thinking"], expected)
        self.record_mlx(thinking=False)
        process = self.mlx_process()
        process["arguments"] += ["--chat-template-args", '{"enable_thinking":true}']
        process["command"] = " ".join(process["arguments"])
        self.assertIs(self.request("status", process)["server"]["thinking"], False)

    def test_gguf_pending_record_requires_exact_argv_and_target(self):
        process = self.gguf_process()
        server = self.request("status", process)["server"]
        self.assertIs(server["trusted"], True)
        self.assertIs(server["loading"], True)
        self.assertEqual(server["format"], "GGUF")
        self.assertEqual(server["active_model"], "fixture-GGUF")
        self.assertBlocked(self.request("blocked-start", process), "すでに稼働中")
        record_file = self.root / "gguf/evidence/llama-server.json"
        record = json.loads(record_file.read_text())
        original = dict(record)
        for change in ({"argv": record["argv"] + ["--different"]}, {"target": "unknown"}):
            with self.subTest(change=change):
                record_file.write_text(json.dumps({**original, **change}))
                self.assertIs(self.request("status", process)["server"]["trusted"], False)


if __name__ == "__main__":
    unittest.main()
