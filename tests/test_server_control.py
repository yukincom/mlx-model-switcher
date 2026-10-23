import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from mlx_activation_lab import server_control, manager_operations

class ServerManagerRouteTests(unittest.TestCase):
    def test_stop_passes_exact_displayed_process_identity(self):
        with patch.object(server_control, "stop") as stop, patch.object(server_control, "status", return_value={"running": False}):
            result = manager_operations.dispatch("POST", "/api/stop", {"pid": 123, "command": "mlx_lm.server --port 8080"})
        stop.assert_called_once_with(expected_pid=123, expected_command="mlx_lm.server --port 8080")
        self.assertEqual(result, (200, {"ok": True, "result": {"running": False}}))

    def test_stop_without_process_identity_does_not_signal(self):
        with patch.object(server_control, "stop") as stop:
            with self.assertRaises(ValueError):
                manager_operations.dispatch("POST", "/api/stop", {"pid": 123})
        stop.assert_not_called()

    def test_model_config_rejects_newlines(self):
        with self.assertRaisesRegex(ValueError, "改行"):
            server_control.register_alias("test", "owner/model\nother=owner/other")

class ServerModelRegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / "models.conf"
        self.config.write_text("# existing models\n", encoding="utf-8")
        mocked = patch.object(server_control, "MODELS_CONFIG", self.config)
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_model_only_registration_keeps_full_huggingface_id(self):
        alias = server_control.register_model("owner/shared-model")
        self.assertEqual(server_control.aliases(), [{"alias": alias, "model": "owner/shared-model"}])

    def test_same_model_reuses_existing_launcher_key_without_rewriting(self):
        self.config.write_text("custom-name=owner/model\n", encoding="utf-8")
        before = self.config.read_bytes()
        self.assertEqual(server_control.register_model("owner/model"), "custom-name")
        self.assertEqual(self.config.read_bytes(), before)

    def test_same_model_name_from_different_owners_does_not_overwrite(self):
        first = server_control.register_model("one/model")
        second = server_control.register_model("two/model")
        self.assertNotEqual(first, second)
        self.assertEqual([item["model"] for item in server_control.aliases()], ["one/model", "two/model"])

    def test_local_path_with_spaces_and_japanese_is_preserved(self):
        path = "/example/モデル/会話 用"
        alias = server_control.register_model(path)
        self.assertRegex(alias, r"^[A-Za-z0-9._-]+$")
        self.assertEqual(server_control.aliases()[0]["model"], path)

    def test_invalid_reference_does_not_modify_config(self):
        before = self.config.read_bytes()
        for model in ("", "owner/model\nother=owner/other"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                server_control.register_model(model)
        self.assertEqual(self.config.read_bytes(), before)

    def test_manager_accepts_registration_without_alias(self):
        with patch.object(server_control, "cached_models", return_value=["owner/model"]):
            status, _ = manager_operations.dispatch("POST", "/api/register", {"model": "owner/model"})
        self.assertEqual(status, 200)
        self.assertEqual(server_control.aliases()[0]["model"], "owner/model")

    def test_missing_cached_model_cannot_be_registered_from_picker(self):
        with patch.object(server_control, "cached_models", return_value=[]):
            with self.assertRaises(ValueError):
                manager_operations.dispatch("POST", "/api/register", {"model": "owner/missing"})
        self.assertEqual(server_control.aliases(), [])

class CachedServerModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def snapshot(self, revision="a" * 40, complete=True):
        folder = self.root / "models--owner--model" / "snapshots" / revision
        folder.mkdir(parents=True)
        (folder / "config.json").write_text("{}", encoding="utf-8")
        if complete:
            (folder / "model.safetensors").write_bytes(b"fixture")
        return folder

    def main_revision(self, revision):
        ref = self.root / "models--owner--model" / "refs/main"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(revision, encoding="utf-8")

    def test_complete_single_snapshot_is_available_without_download(self):
        self.snapshot()
        with patch("huggingface_hub.snapshot_download") as download:
            self.assertEqual(server_control.cached_models(self.root), ["owner/model"])
        download.assert_not_called()

    def test_main_revision_wins_over_incomplete_other_revision(self):
        self.snapshot()
        self.snapshot("b" * 40, complete=False)
        self.main_revision("a" * 40)
        self.assertEqual(server_control.cached_models(self.root), ["owner/model"])

    def test_incomplete_main_does_not_fall_back_to_other_revision(self):
        self.snapshot()
        self.snapshot("b" * 40, complete=False)
        self.main_revision("b" * 40)
        self.assertEqual(server_control.cached_models(self.root), [])

    def test_encoder_models_are_not_offered_as_generation_models(self):
        snapshot = self.snapshot()
        for architecture, expected in (("BertModel", []), ("ModernBertModel", []),
                                       ("Qwen3ForCausalLM", ["owner/model"]),
                                       ("Qwen3_5ForConditionalGeneration", ["owner/model"])):
            with self.subTest(architecture=architecture):
                (snapshot / "config.json").write_text(
                    '{"architectures":["' + architecture + '"]}', encoding="utf-8"
                )
                self.assertEqual(server_control.cached_models(self.root), expected)

    def test_ambiguous_revisions_and_missing_cache_are_not_offered(self):
        self.snapshot()
        self.snapshot("b" * 40)
        self.assertEqual(server_control.cached_models(self.root), [])
        self.assertEqual(server_control.cached_models(self.root / "missing"), [])

class ServerStopTests(unittest.TestCase):
    def test_changed_external_listener_is_not_stopped(self):
        current = {"running": True, "pid": 123, "command": "mlx_lm.server --port 8080", "trusted": True}
        with patch.object(server_control, "status", return_value=current), patch.object(server_control.os, "kill") as terminate:
            with self.assertRaisesRegex(RuntimeError, "切り替わった"):
                server_control.stop(expected_pid=122, expected_command=current["command"])
            with self.assertRaisesRegex(RuntimeError, "切り替わった"):
                server_control.stop(expected_pid=123, expected_command="別の起動コマンド")
        terminate.assert_not_called()

    def test_matching_listener_receives_only_sigterm(self):
        current = {"running": True, "pid": 123, "command": "mlx_lm.server --port 8080", "trusted": True}
        with patch.object(server_control, "status", return_value=current), patch.object(server_control.os, "kill") as terminate, patch.object(
            server_control, "_listener_pid", return_value=None,
        ), patch.object(server_control, "_command", return_value=""):
            server_control.stop(expected_pid=123, expected_command=current["command"])
        terminate.assert_called_once_with(123, server_control.signal.SIGTERM)
