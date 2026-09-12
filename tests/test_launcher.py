"""Exercise the real launcher with isolated model/cache and server fixtures."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "start_server.sh"


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mlx launcher ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "models.conf"
        self.capture = self.root / "invocation.json"
        self.cache = self.root / "hub"
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("MLX_")
        }
        self.env.update(
            MLX_SERVER_DIR=str(self.root),
            HF_HUB_CACHE=str(self.cache),
            MLX_TEST_CAPTURE=str(self.capture),
        )
        self.model = self.make_model(self.root / "models" / "sample model")
        self.config.write_text("sample=./models/sample model\n", encoding="utf-8")

    def make_model(self, path):
        path.mkdir(parents=True)
        (path / "config.json").write_text("{}", encoding="utf-8")
        (path / "weights.safetensors").write_bytes(b"fixture; not a real model")
        return path

    def install_fake_server(self):
        binary_dir = self.root / ".venv" / "bin"
        binary_dir.mkdir(parents=True)
        (binary_dir / "activate").write_text(
            f'export PATH={shlex.quote(str(binary_dir))}:"$PATH"\n',
            encoding="utf-8",
        )
        server = binary_dir / "mlx_lm.server"
        server.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['MLX_TEST_CAPTURE']).write_text(json.dumps({\n"
            "    'args': sys.argv[1:],\n"
            "    'offline': [os.environ.get('HF_HUB_OFFLINE'),\n"
            "                os.environ.get('TRANSFORMERS_OFFLINE')]\n"
            "}))\n",
            encoding="utf-8",
        )
        server.chmod(0o755)

    def run_launcher(self, *args, overrides=None):
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *args],
            cwd=self.root,
            env={**self.env, **(overrides or {})},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def assert_launched(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        invocation = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(invocation["offline"], ["1", "1"])
        arguments = invocation["args"]
        self.assertEqual(len(arguments) % 2, 0)
        return dict(zip(arguments[::2], arguments[1::2]))

    def assert_rejected(self, result, code=1):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertFalse(self.capture.exists(), "Invalid input reached the server")

    def test_help_needs_no_config_or_environment(self):
        self.config.unlink()
        result = self.run_launcher("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--help", result.stdout)

    def test_list_handles_comments_whitespace_and_missing_final_newline(self):
        self.config.write_text(
            " # comment\n\ninvalid\n =empty\nempty= \n"
            " first = org/one \nlast=org/two", encoding="utf-8"
        )
        result = self.run_launcher("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("first", result.stdout)
        self.assertIn("org/one", result.stdout)
        self.assertIn("last", result.stdout)
        self.assertIn("org/two", result.stdout)
        self.assertNotIn("empty", result.stdout)
        self.assertNotIn("invalid", result.stdout)

    def test_noninteractive_requires_explicit_alias(self):
        self.assert_rejected(self.run_launcher(), code=2)

    def test_extra_arguments_are_rejected(self):
        self.assert_rejected(self.run_launcher("sample", "extra"), code=2)

    def test_unknown_alias_is_rejected(self):
        self.assert_rejected(self.run_launcher("unknown"))

    def test_missing_config_is_rejected(self):
        self.config.unlink()
        self.assert_rejected(self.run_launcher("sample"))

    def test_local_model_path_with_spaces_and_offline_environment(self):
        self.install_fake_server()
        args = self.assert_launched(self.run_launcher("sample"))
        self.assertEqual(Path(args["--model"]).resolve(), self.model.resolve())
        self.assertEqual(args["--host"], "127.0.0.1")
        self.assertEqual(args["--port"], "8080")

    def test_environment_overrides_reach_server(self):
        self.install_fake_server()
        args = self.assert_launched(self.run_launcher("sample", overrides={
            "MLX_PORT": "8181", "MLX_MAX_TOKENS": "1024",
            "MLX_DECODE_CONCURRENCY": "3", "MLX_PROMPT_CONCURRENCY": "2",
            "MLX_PREFILL_STEP_SIZE": "256", "MLX_PROMPT_CACHE_BYTES": "2GB",
            "MLX_ALLOWED_ORIGINS": "http://localhost:4321",
        }))
        self.assertEqual(args["--port"], "8181")
        self.assertEqual(args["--max-tokens"], "1024")
        self.assertEqual(args["--decode-concurrency"], "3")
        self.assertEqual(args["--prompt-concurrency"], "2")
        self.assertEqual(args["--prefill-step-size"], "256")
        self.assertEqual(args["--prompt-cache-bytes"], "2GB")
        self.assertEqual(args["--allowed-origins"], "http://localhost:4321")

    def test_invalid_numeric_settings_never_launch_server(self):
        self.install_fake_server()
        for name in ("MLX_PORT", "MLX_MAX_TOKENS", "MLX_CLIENT_CONTEXT_TARGET",
                     "MLX_DECODE_CONCURRENCY", "MLX_PROMPT_CONCURRENCY",
                     "MLX_PREFILL_STEP_SIZE"):
            for value in ("0", "-1", "nope"):
                with self.subTest(name=name, value=value):
                    self.assert_rejected(self.run_launcher(
                        "sample", overrides={name: value}))

    def test_remote_bind_requires_explicit_opt_in(self):
        self.install_fake_server()
        self.assert_rejected(self.run_launcher(
            "sample", overrides={"MLX_HOST": "0.0.0.0"}))
        args = self.assert_launched(self.run_launcher("sample", overrides={
            "MLX_HOST": "0.0.0.0", "MLX_ALLOW_REMOTE": "1",
        }))
        self.assertEqual(args["--host"], "0.0.0.0")

    def test_incomplete_models_never_launch_server(self):
        self.install_fake_server()
        config = self.model / "config.json"
        config.unlink()
        self.assert_rejected(self.run_launcher("sample"))
        config.write_text("{}", encoding="utf-8")
        weights = self.model / "weights.safetensors"
        weights.write_bytes(b"")
        self.assert_rejected(self.run_launcher("sample"))
        weights.unlink()
        self.assert_rejected(self.run_launcher("sample"))

    def hf_model(self):
        self.config.write_text("sample=org/model\n", encoding="utf-8")
        return self.cache / "models--org--model"

    def test_hf_main_selects_valid_snapshot_and_preserves_model_id(self):
        self.install_fake_server()
        root = self.hf_model()
        self.make_model(root / "snapshots" / "abcdef012345")
        # The other snapshot is intentionally incomplete; refs/main must win.
        (root / "snapshots" / "111111111111").mkdir()
        (root / "refs").mkdir()
        (root / "refs" / "main").write_text("abcdef012345", encoding="utf-8")
        args = self.assert_launched(self.run_launcher("sample"))
        self.assertEqual(args["--model"], "org/model")

    def test_hf_single_snapshot_fallback(self):
        self.install_fake_server()
        root = self.hf_model()
        self.make_model(root / "snapshots" / "abcdef012345")
        args = self.assert_launched(self.run_launcher("sample"))
        self.assertEqual(args["--model"], "org/model")

    def test_hf_missing_or_ambiguous_cache_is_rejected(self):
        self.install_fake_server()
        root = self.hf_model()
        self.assert_rejected(self.run_launcher("sample"))
        self.make_model(root / "snapshots" / "abcdef012345")
        self.make_model(root / "snapshots" / "111111111111")
        self.assert_rejected(self.run_launcher("sample"))

    def test_broken_hf_main_does_not_silently_use_other_snapshot(self):
        self.install_fake_server()
        root = self.hf_model()
        self.make_model(root / "snapshots" / "abcdef012345")
        (root / "refs").mkdir()
        (root / "refs" / "main").write_text("deadbeef", encoding="utf-8")
        self.assert_rejected(self.run_launcher("sample"))


if __name__ == "__main__":
    unittest.main()
