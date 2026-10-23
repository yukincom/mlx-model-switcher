import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mlx_activation_lab import manager_models, server_control as control
from mlx_activation_lab import manager_operations as operations


class ThinkingTests(unittest.TestCase):
    def test_api_preserves_explicit_boolean_and_defaults_off(self):
        for payload, expected in (({'thinking': True}, True), ({'thinking': False}, False), ({}, False)):
            with self.subTest(payload=payload), patch.object(control, 'start', return_value=42) as start:
                self.assertEqual(operations.dispatch('POST', '/api/start', {'alias': 'example', **payload})[0], 200)
                start.assert_called_once_with('example', operations.LOG_PATH, thinking=expected)

    def test_api_rejects_nonboolean_without_launch(self):
        for value in ('false', 'true', 0, 1, None, [], {}):
            with self.subTest(value=value), patch.object(control, 'start') as start:
                with self.assertRaises(ValueError):
                    operations.dispatch('POST', '/api/start', {'alias': 'example', 'thinking': value})
                start.assert_not_called()

    def test_mlx_spawn_overrides_inherited_thinking_and_records_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            for enabled in (True, False):
                process = Mock(pid=42)
                process.poll.return_value = None
                with self.subTest(enabled=enabled), patch.object(control, 'status', return_value={'running': False}), patch.object(control, '_listener_pid', return_value=None), patch.object(control, 'aliases', return_value=[{'alias': 'example'}]), patch.object(manager_models, 'gguf_profiles', return_value=[]), patch.object(control.subprocess, 'Popen', return_value=process) as spawn, patch.object(control.time, 'sleep'), patch.object(manager_models, 'remember_start') as remember, patch.dict(os.environ, {'MLX_THINKING': 'xhigh', 'MLX_PORT': '8181', 'MLX_HOST': '0.0.0.0', 'MLX_SERVER_DIR': '/wrong'}):
                    self.assertEqual(control.start('example', Path(directory) / 'log', thinking=enabled), 42)
                    self.assertEqual(spawn.call_args.kwargs['env']['MLX_THINKING'], 'on' if enabled else 'off')
                    environment = spawn.call_args.kwargs['env']
                    self.assertEqual(environment['MLX_PORT'], '8080')
                    self.assertEqual(environment['MLX_HOST'], '127.0.0.1')
                    self.assertEqual(environment['MLX_SERVER_DIR'], str(control.SERVER_ROOT))
                    self.assertEqual(environment['MLX_MODELS_CONFIG'], str(control.MODELS_CONFIG))
                    self.assertEqual(environment['MLX_MANAGER_LOGGED'], '1')
                    remember.assert_called_once_with(42, 'example', thinking=enabled)

    def test_control_rejects_invalid_types_before_status_or_spawn(self):
        with patch.object(control, 'status') as status, patch.object(control.subprocess, 'Popen') as spawn:
            with self.assertRaises(ValueError):
                control.start('example', '/unused', thinking='false')
            status.assert_not_called()
            spawn.assert_not_called()

    def test_running_or_loading_process_is_not_replaced(self):
        for loading in (False, True):
            with patch.object(control, 'status', return_value={'running': True, 'loading': loading}), patch.object(control.subprocess, 'Popen') as spawn, patch.object(manager_models, 'start_gguf') as gguf, patch.object(control.os, 'kill') as kill:
                with self.assertRaises(ValueError):
                    control.start('example', '/unused', thinking=True)
                spawn.assert_not_called()
                gguf.assert_not_called()
                kill.assert_not_called()

    def test_status_reads_launch_default_without_guessing(self):
        for command, expected in (
            ('llama-server --reasoning on --port 8080', True),
            ('llama-server --reasoning off --port 8080', False),
            ('llama-server -rea on', True),
            ('mlx_lm.server --chat-template-args {"enable_thinking":true,"preserve_thinking":false}', True),
            ("mlx_lm.server --chat-template-args '{\"enable_thinking\":false}' --port 8080", False),
            ('mlx_lm.server --chat-template-args {"enable_thinking":"false"}', None),
            ('mlx_lm.server --chat-template-args invalid', None),
            ('llama-server --reasoning auto', None),
            ('external-server --port 8080', None),
        ):
            with self.subTest(command=command):
                self.assertIs(control._thinking_from_command(command), expected)

    def test_loading_record_keeps_choice_before_launcher_execs(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manager_models, 'PENDING', Path(directory) / 'starting.json'), patch.object(manager_models, 'process_started', return_value='same'), patch.object(control, '_command', return_value='bash start_server.sh example'), patch.object(control, 'aliases', return_value=[{'alias': 'example', 'model': 'owner/model'}]):
            manager_models.remember_start(42, 'example', thinking=True)
            status = manager_models.pending_mlx()
            self.assertTrue(status['thinking'])
            self.assertTrue(status['loading'])

    def test_gguf_on_and_off_are_forwarded_to_existing_runtime(self):
        runtime = SimpleNamespace(start_gguf_if_idle=Mock(return_value=42))
        profile = {'alias': 'gguf:example', 'target': 'example'}
        with patch.object(control, 'status', return_value={'running': False}), patch.object(control, '_listener_pid', return_value=None), patch.object(manager_models, 'gguf_profiles', return_value=[profile]), patch.object(manager_models, 'find_model'), patch.object(manager_models, 'gguf_runtime', return_value=runtime):
            for enabled in (True, False):
                self.assertEqual(control.start('gguf:example', '/unused', thinking=enabled), 42)
                runtime.start_gguf_if_idle.assert_called_with('example', thinking=enabled)
