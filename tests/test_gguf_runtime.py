"""誤停止・起動中モデルの重複・切替競合を、実サービスに触れず検証する。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import Mock, patch
import fcntl
import json
import importlib.util

_spec = importlib.util.spec_from_file_location("gguf_launcher_under_test", Path(__file__).resolve().parents[1] / "gguf/server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


class SwitchTests(TestCase):
    def test_gemma_requires_verified_and_complete_projector_and_draft(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = server.GGUF_PROFILES["gemma-gguf"]
            binary = root / "llama-server"
            binary.touch()
            verified = root / profile["verified"]
            verified.parent.mkdir(parents=True)
            verified.write_text("{}")
            model_dir = root / "separate-cache/gemma"
            model_dir.mkdir(parents=True)
            (model_dir / "qat.gguf").write_bytes(b"test")
            items = [{"directory": "gemma", "file": name, "bytes": 4}
                     for name in ("qat.gguf", "mmproj-F16.gguf", profile["draft"])]
            (root / profile["manifest"]).write_text(json.dumps({"files": items}))
            with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "separate-cache"), patch.object(server, "BINARY", binary):
                with self.assertRaisesRegex(RuntimeError, "画像用ファイルの検証"):
                    server.validated_gguf_files("gemma-gguf")
                (root / profile["vision_verified"]).write_text("{}")
                with self.assertRaisesRegex(RuntimeError, "先読みモデルの検証"):
                    server.validated_gguf_files("gemma-gguf")
                (root / profile["draft_verified"]).write_text("{}")
                with self.assertRaisesRegex(RuntimeError, "モデルファイル"):
                    server.validated_gguf_files("gemma-gguf")
                (model_dir / "mmproj-F16.gguf").write_bytes(b"test")
                with self.assertRaisesRegex(RuntimeError, "モデルファイル"):
                    server.validated_gguf_files("gemma-gguf")
                (model_dir / profile["draft"]).write_bytes(b"test")
                self.assertEqual(server.validated_gguf_files("gemma-gguf"), items)

    def test_unprepared_gemma_does_not_stop_current_model(self):
        with TemporaryDirectory() as directory, patch.object(server, "ROOT", Path(directory)), patch.object(server, "stop") as stop:
            with self.assertRaisesRegex(RuntimeError, "まだ完了"):
                server.start("gemma-gguf")
            stop.assert_not_called()

    def test_unprepared_125b_does_not_stop_current_model(self):
        with TemporaryDirectory() as directory, patch.object(server, "ROOT", Path(directory)), patch.object(server, "stop") as stop:
            with self.assertRaisesRegex(RuntimeError, "まだ完了"):
                server.start("125b")
            stop.assert_not_called()

    def test_unknown_listener_is_never_stopped(self):
        current = {"running": True, "trusted": False, "pid": 123, "command": "unrelated-server"}
        with patch.object(server, "api", return_value={"server": current}), patch.object(server, "is_our_llama", return_value=False), patch.object(server.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "管理対象と一致"):
                server.stop()
            kill.assert_not_called()

    def test_identity_change_during_wait_aborts_stop(self):
        before = {"running": True, "trusted": True, "pid": 123, "command": "known mlx"}
        after = dict(before, pid=456)
        with patch.object(server, "api", side_effect=[{"server": before}, {"server": after}]) as api, patch.object(server, "process_identity", return_value="same"), patch.object(server, "wait_idle"), patch.object(server.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "変わりました"):
                server.stop()
            self.assertEqual(api.call_count, 2)
            kill.assert_not_called()

    def test_tracked_loading_process_is_stopped_before_listener_exists(self):
        with TemporaryDirectory() as directory:
            record = Path(directory) / "managed.json"
            record.write_text(json.dumps({"pid": 123, "identity": "our-loading-process"}))
            with patch.object(server, "MANAGED_RECORD", record), patch.object(server, "api", return_value={"server": {"running": False}}), patch.object(server, "process_identity", side_effect=["our-loading-process", ""]), patch.object(server.os, "kill") as kill:
                server.stop()
                kill.assert_called_once_with(123, server.signal.SIGTERM)

    def test_reused_pid_is_never_stopped(self):
        with TemporaryDirectory() as directory:
            record = Path(directory) / "managed.json"
            record.write_text(json.dumps({"pid": 123, "identity": "old-process"}))
            with patch.object(server, "MANAGED_RECORD", record), patch.object(server, "api", return_value={"server": {"running": False}}), patch.object(server, "process_identity", return_value="new-process"), patch.object(server.os, "kill") as kill:
                server.stop()
                kill.assert_not_called()

    def test_parallel_switch_is_rejected_before_api_call(self):
        # APIもモデルも用意せず、ロックだけで起動が止まることを確認する。
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence").mkdir()
            with (root / "evidence/switch.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "models"), patch.object(server, "api") as api, patch.object(server, "validated_125b_files") as validate, patch.object(server.socket, "socket") as socket, patch.object(server, "_launch_125b") as launch:
                    with self.assertRaisesRegex(RuntimeError, "別のモデル切替"):
                        server.start_gguf_if_idle("125b", thinking=True)
                    api.assert_not_called()
                    validate.assert_not_called()
                    socket.assert_not_called()
                    launch.assert_not_called()


class ManagerEntryTests(TestCase):
    def test_manager_forwards_thinking_for_each_profile(self):
        for target, launcher, validator in (("125b", "_launch_125b", "validated_125b_files"),
                                             ("gemma-gguf", "_launch_gguf", "validated_gguf_files")):
            for thinking in (False, True):
                with self.subTest(target=target, thinking=thinking), TemporaryDirectory() as directory:
                    root = Path(directory)
                    items = ["validated"]
                    with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "models"), patch.object(server, "MANAGED_RECORD", root / "missing.json"), patch.object(server, validator, return_value=items), patch.object(server.socket, "socket"), patch.object(server, launcher, return_value=432) as launch, patch.object(server, "api") as api, patch.object(server, "stop") as stop:
                        self.assertEqual(server.start_gguf_if_idle(target, thinking=thinking), 432)
                        if target == "125b":
                            launch.assert_called_once_with(items, thinking=thinking)
                        else:
                            launch.assert_called_once_with(target, items, thinking=thinking)
                        api.assert_not_called()
                        stop.assert_not_called()

    def test_125b_wrappers_forward_thinking(self):
        for thinking in (False, True):
            with self.subTest(thinking=thinking), patch.object(server, "start_gguf_if_idle", return_value=321) as start, patch.object(server, "_launch_gguf", return_value=432) as launch:
                self.assertEqual(server.start_125b_if_idle(thinking=thinking), 321)
                start.assert_called_once_with("125b", thinking=thinking)
                self.assertEqual(server._launch_125b(["validated"], thinking=thinking), 432)
                launch.assert_called_once_with("125b", ["validated"], thinking=thinking)

    def test_thinking_must_be_bool_before_any_start_side_effect(self):
        for entry in (server.start_gguf_if_idle, server._launch_gguf):
            for target in server.GGUF_PROFILES:
                for invalid in (None, 0, 1, "false", "true", [], {}):
                    with self.subTest(entry=entry.__name__, target=target, value=invalid), TemporaryDirectory() as directory:
                        root = Path(directory)
                        args = (target,) if entry is server.start_gguf_if_idle else (target, [])
                        with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "models"), patch.object(server, "validated_125b_files") as validate_125b, patch.object(server, "validated_gguf_files") as validate_gguf, patch.object(server.socket, "socket") as socket, patch.object(server.subprocess, "Popen") as spawn, patch.object(server, "api") as api, patch.object(server, "stop") as stop:
                            with self.assertRaisesRegex(ValueError, "thinking.*真偽値"):
                                entry(*args, thinking=invalid)
                            self.assertEqual(list(root.iterdir()), [])
                            validate_125b.assert_not_called()
                            validate_gguf.assert_not_called()
                            socket.assert_not_called()
                            spawn.assert_not_called()
                            api.assert_not_called()
                            stop.assert_not_called()

    def test_launch_reasoning_arguments_and_records_for_each_profile(self):
        for target in server.GGUF_PROFILES:
            for thinking in (False, True):
                with self.subTest(target=target, thinking=thinking), TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / "models-manifest.json").write_text(json.dumps({"source_revision": "fixture"}))
                    profile = server.GGUF_PROFILES[target]
                    items = [{"directory": target, "file": name} for name in (
                        "model.gguf", profile.get("mmproj", "second-shard.gguf"),
                        profile.get("draft", "draft.gguf"))]
                    record = root / "record.json"
                    managed_record = root / "managed.json"
                    process = Mock(pid=432)
                    process.poll.return_value = None
                    with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "models"), patch.object(server, "RECORD", record), patch.object(server, "MANAGED_RECORD", managed_record), patch.object(server, "process_identity", return_value="same"), patch.object(server.time, "sleep"), patch.object(server.subprocess, "Popen", return_value=process) as spawn, patch("builtins.print"):
                        self.assertEqual(server._launch_gguf(target, items, thinking=thinking), 432)
                    args = spawn.call_args.args[0]
                    self.assertEqual(args.count("--reasoning"), 1)
                    self.assertEqual(args[args.index("--reasoning") + 1], "on" if thinking else "off")
                    self.assertEqual(args[args.index("-c") + 1], str(profile["context"]))
                    for path in (record, managed_record):
                        saved = json.loads(path.read_text())
                        self.assertIs(saved["thinking"], thinking)
                        self.assertEqual(saved["target"], target)
                        self.assertEqual(saved["argv"], args)

    def test_gemma_manager_launch_uses_selected_target_without_api_or_stop(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "models"), patch.object(server, "MANAGED_RECORD", root / "missing.json"), patch.object(server, "validated_gguf_files", return_value=["gemma"]) as validate, patch.object(server.socket, "socket"), patch.object(server, "_launch_gguf", return_value=432) as launch, patch.object(server, "api") as api, patch.object(server, "stop") as stop:
                self.assertEqual(server.start_gguf_if_idle("gemma-gguf"), 432)
                validate.assert_called_once_with("gemma-gguf")
                launch.assert_called_once_with("gemma-gguf", ["gemma"], thinking=False)
                api.assert_not_called()
                stop.assert_not_called()

    def test_gemma_launch_does_not_use_qwen_draft_and_records_model(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models-manifest.json").write_text(json.dumps({"source_revision": "fixture"}))
            process = Mock(pid=432)
            process.poll.return_value = None
            record = root / "record.json"
            items = [{"directory": "gemma", "file": "qat.gguf"},
                     {"directory": "gemma", "file": "mmproj-F16.gguf"},
                     {"directory": "gemma", "file": server.GGUF_PROFILES["gemma-gguf"]["draft"]}]
            with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "models"), patch.object(server, "RECORD", record), patch.object(server, "MANAGED_RECORD", root / "managed.json"), patch.object(server, "process_identity", return_value="same"), patch.object(server.time, "sleep"), patch.object(server.subprocess, "Popen", return_value=process) as spawn, patch.dict(server.os.environ, {"LLAMA_SPEC_MTP_MERGE": "1", "LLAMA_SPEC_STOP": "1", "LLAMA_SPEC_STOP_PARAMS": "qwen.params"}):
                self.assertEqual(server._launch_gguf("gemma-gguf", items), 432)
            args = spawn.call_args.args[0]
            self.assertEqual(args[args.index("-m") + 1], str(root / "models/gemma/qat.gguf"))
            self.assertEqual(args[args.index("--mmproj") + 1], str(root / "models/gemma/mmproj-F16.gguf"))
            self.assertEqual(args[args.index("--spec-type") + 1], "draft-mtp")
            self.assertEqual(args[args.index("--spec-draft-n-max") + 1], "2")
            self.assertGreaterEqual(int(args[args.index("-ub") + 1]), 1120)
            self.assertEqual(args[args.index("-c") + 1], "65536")
            self.assertEqual(args[args.index("--reasoning") + 1], "off")
            self.assertEqual(server.GGUF_PROFILES["125b"]["context"], 16384)
            self.assertEqual(args[args.index("-md") + 1], str(root / "models/gemma/mtp-gemma-4-26B-A4B-it.gguf"))
            self.assertNotIn("LLAMA_SPEC_STOP", spawn.call_args.kwargs["env"])
            self.assertNotIn("LLAMA_SPEC_MTP_MERGE", spawn.call_args.kwargs["env"])
            self.assertNotIn("LLAMA_SPEC_STOP_PARAMS", spawn.call_args.kwargs["env"])
            saved = json.loads(record.read_text())
            self.assertEqual(saved["target"], "gemma-gguf")
            self.assertIs(saved["thinking"], False)
            self.assertEqual(saved["model"], server.GGUF_PROFILES["gemma-gguf"]["model"])

    def test_manager_launch_never_calls_api_or_stops_another_model(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "models"), patch.object(server, "MANAGED_RECORD", root / "missing.json"), patch.object(server, "validated_125b_files", return_value=["validated"]), patch.object(server.socket, "socket"), patch.object(server, "_launch_125b", return_value=321) as launch, patch.object(server, "api") as api, patch.object(server, "stop") as stop:
                self.assertEqual(server.start_125b_if_idle(), 321)
                launch.assert_called_once_with(["validated"], thinking=False)
                api.assert_not_called()
                stop.assert_not_called()

    def test_manager_rejects_busy_port_without_launching_or_stopping(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "models"), patch.object(server, "MANAGED_RECORD", root / "missing.json"), patch.object(server, "validated_125b_files", return_value=[]), patch.object(server.socket, "socket") as socket, patch.object(server, "_launch_125b") as launch, patch.object(server, "stop") as stop:
                socket.return_value.__enter__.return_value.bind.side_effect=OSError("busy")
                with self.assertRaises(OSError): server.start_125b_if_idle()
                launch.assert_not_called()
                stop.assert_not_called()

    def test_manager_rejects_still_loading_previous_model(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            record=root / "managed.json"
            record.write_text(json.dumps({"pid":123, "identity":"same-process"}))
            with patch.object(server, "ROOT", root), patch.object(server, "MODELS_ROOT", root / "models"), patch.object(server, "MANAGED_RECORD", record), patch.object(server, "validated_125b_files", return_value=[]), patch.object(server, "process_identity", return_value="same-process"), patch.object(server, "_launch_125b") as launch:
                with self.assertRaisesRegex(RuntimeError, "前のモデル"): server.start_125b_if_idle()
                launch.assert_not_called()


if __name__ == "__main__":
    main()
