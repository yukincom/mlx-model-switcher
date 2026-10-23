import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from mlx_activation_lab import manager_bridge as bridge


class ManagerBridgeTests(unittest.TestCase):
    def call(self, raw, operation):
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(bridge.manager_operations, "LOG_ROOT", Path(temporary)), \
             patch.object(bridge.manager_operations, "dispatch", side_effect=operation) as dispatch, \
             patch.object(bridge.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            bridge.main()
        return json.loads(output.getvalue()), errors.getvalue(), dispatch

    def test_launcher_stdout_does_not_corrupt_response(self):
        def noisy_start(*args):
            print('{"status": "loading", "pid": 42}')
            return 200, {"ok": True, "result": {"pid": 42}}
        result, errors, operation = self.call(
            b'{"method":"POST","path":"/api/start","payload":{"thinking":true}}', noisy_start)
        self.assertEqual(result, {"status": 200, "payload": {"ok": True, "result": {"pid": 42}}})
        self.assertIn('"loading"', errors)
        operation.assert_called_once_with("POST", "/api/start", {"thinking": True})

    def test_exception_returns_one_error_envelope(self):
        result, errors, _ = self.call(b'{"method":"POST","path":"/api/delete"}', ValueError("changed preview"))
        self.assertEqual(result["status"], 400)
        self.assertIn("changed preview", result["payload"]["error"])
        self.assertIn("ValueError", errors)

    def test_oversize_request_never_dispatches(self):
        result, _, operation = self.call(b"x" * 65537, None)
        self.assertEqual(result["status"], 400)
        operation.assert_not_called()
