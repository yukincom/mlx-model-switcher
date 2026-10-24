"""Exercise production Swift HTTP/model management using disposable catalogs.

The manager never starts or stops inference here. It receives only fixture paths,
no Python runtime on PATH, and a dynamically allocated HTTP port.
"""
import contextlib
import http.client
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

if sys.platform == "darwin":
    import fcntl

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "native macOS server")
class NativeManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build = tempfile.TemporaryDirectory(prefix="mlx-manager-tests-")
        cls.addClassCleanup(build.cleanup)
        cls.binary = Path(build.name) / "MLXServerManager"
        result = subprocess.run([
            "/usr/bin/swiftc", "-swift-version", "5",
            "-target", f"{platform.machine()}-apple-macos14.0",
            "-module-cache-path", str(Path(build.name) / "module-cache"),
            "-framework", "Network", "-framework", "Security",
            str(ROOT / "native/ManagerRuntime.swift"),
            str(ROOT / "native/ManagerModelStore.swift"),
            str(ROOT / "native/ServerManager.swift"), "-o", str(cls.binary),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError("Swift server build failed:\n" + result.stdout + result.stderr)

    @contextlib.contextmanager
    def server(self):
        with tempfile.TemporaryDirectory(prefix="mlx-http-fixture-") as temporary:
            resources = Path(temporary).resolve()
            shutil.copytree(ROOT / "src/mlx_activation_lab/server_web", resources / "server_web")
            for name in ("launcher", "state", "gguf", "cache"):
                (resources / name).mkdir()
            (resources / "launcher/models.conf").write_text("# fixture only\na=owner/model-a\nb=owner/model-b\n")
            snapshot = resources / "cache/models--owner--new-model/snapshots" / ("a" * 40)
            snapshot.mkdir(parents=True)
            (snapshot / "config.json").write_text('{"architectures":["ExampleForCausalLM"]}')
            (snapshot / "model.safetensors").write_bytes(b"fixture weights")
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            with (resources / "server.log").open("w+") as log:
                process = subprocess.Popen([
                    str(self.binary), "--port", str(port), "--resources", str(resources),
                ], stdout=log, stderr=log, env={**os.environ,
                    "PATH": "/nonexistent",
                    "MLX_MANAGER_STATE_ROOT": str(resources / "state"),
                    "MLX_ACTIVATION_LAB_SERVER_ROOT": str(resources / "launcher"),
                    "MLX_MANAGER_GGUF_ROOT": str(resources / "gguf"),
                    "HF_HUB_CACHE": str(resources / "cache"), "HF_HUB_OFFLINE": "1",
                })
                try:
                    for _ in range(100):
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail(log.read())
                        try:
                            if self.request(port, "GET", "/api/health")[0] == 200:
                                break
                        except OSError:
                            time.sleep(0.02)
                    else:
                        self.fail("native listener did not start")
                    yield port, resources, process
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        self.fail("native manager did not shut down")
                    with socket.socket() as probe:
                        self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def request(self, port, method, path, payload=None, token=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        request_headers = dict(headers or {})
        if token:
            request_headers["X-Server-Token"] = token
        body = None if payload is None else json.dumps(payload).encode()
        try:
            client.request(method, path, body=body, headers=request_headers)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def token(self, port):
        status, _, data = self.request(port, "GET", "/api/status")
        self.assertEqual(status, 200, data)
        result = json.loads(data)
        self.assertRegex(result["token"], r"^[0-9a-f]{64}$")
        return result["token"]

    def raw(self, port, request):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(request.encode())
            response = http.client.HTTPResponse(client)
            response.begin()
            return response.status, response.read()

    def assert_catalog_unchanged(self, resources):
        self.assertEqual((resources / "launcher/models.conf").read_text(),
                         "# fixture only\na=owner/model-a\nb=owner/model-b\n")
        self.assertFalse((resources / "state/models.json").exists())
        self.assertFalse((resources / "state/starting.json").exists())

    def wait_for_blocked_operation(self, process, lock):
        # The production dispatcher opens operations.lock before flock blocks.
        # Observing that descriptor establishes that the request was accepted.
        for _ in range(40):
            result = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(process.pid), "-Fn", str(lock)],
                                    capture_output=True, text=True, timeout=2)
            if any(line.startswith("n") and Path(line[1:]).resolve() == lock.resolve()
                   for line in result.stdout.splitlines()):
                return
            time.sleep(0.01)
        self.fail("accepted operation did not reach its file lock")

    def async_request(self, *args):
        result = {}
        def run():
            try:
                result["response"] = self.request(*args)
            except Exception as error:
                result["error"] = error
        thread = threading.Thread(target=run)
        thread.start()
        return thread, result

    def test_idle_socket_health_static_assets_and_native_identity(self):
        with self.server() as (port, resources, _), socket.create_connection(("127.0.0.1", port)):
            status, headers, data = self.request(port, "GET", "/api/health")
            self.assertEqual(status, 200)
            health = json.loads(data)
            self.assertEqual((health["backend"], health["protocol"]), ("swift", 2))
            configuration = health["configuration"]
            for key, directory in (("resources", ""), ("server_root", "launcher"), ("gguf_root", "gguf"),
                                   ("state_root", "state"), ("cache_root", "cache")):
                self.assertEqual(Path(configuration[key]).resolve(), resources / directory)
            self.assertNotIn("python", configuration)
            self.assertEqual(headers["Cache-Control"], "no-store")
            for path in ("/", "/app.js", "/style.css"):
                status, _, data = self.request(port, "GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(data)
                self.assertNotIn(b"__SERVER_TOKEN__", data)
            self.token(port)
            self.assert_catalog_unchanged(resources)

    def test_auth_host_origin_and_unknown_route_do_not_change_models(self):
        with self.server() as (port, resources, _):
            for method, path, headers, expected in [
                ("POST", "/api/start", {}, 403),
                ("POST", "/api/delete", {"X-Server-Token": "wrong"}, 403),
                ("GET", "/api/status", {"Host": f"attacker.example:{port}"}, 403),
                ("GET", "/api/status", {"Origin": "https://attacker.example"}, 403),
                ("GET", "/../models.conf", {}, 404),
                ("PUT", "/api/start", {}, 405),
            ]:
                with self.subTest(method=method, path=path, headers=headers):
                    self.assertEqual(self.request(port, method, path, headers=headers)[0], expected)
            self.assert_catalog_unchanged(resources)

    def test_framing_and_payload_rejected_before_model_operations(self):
        with self.server() as (port, resources, _):
            token = self.token(port)
            prefix = f"POST /api/rename HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Server-Token: {token}\r\n"
            for tail, expected in [
                ("Content-Length: 16385\r\n\r\n", 413),
                ("Content-Length: -1\r\n\r\n", 413),
                ("Content-Length: 0\r\nContent-Length: 0\r\n\r\n", 400),
                ("Transfer-Encoding: chunked\r\n\r\n", 400),
                ("Content-Length: 2\r\n\r\n[]", 400),
                ("Content-Length: 1\r\n\r\n{", 400),
            ]:
                with self.subTest(tail=tail):
                    self.assertEqual(self.raw(port, prefix + tail)[0], expected)
            self.assertEqual(self.raw(port, "GET / HTTP/1.1\r\nX-Fill: " + "x" * 8200)[0], 431)
            self.assert_catalog_unchanged(resources)

    def test_native_catalog_rename_default_and_registration_without_python(self):
        with self.server() as (port, resources, _):
            token = self.token(port)
            status, _, data = self.request(port, "GET", "/api/cached-models")
            self.assertEqual(status, 200, data)
            self.assertEqual(json.loads(data)["models"], ["owner/new-model"])
            for path, payload in [
                ("/api/rename", {"alias": "a", "name": "日本語モデル"}),
                ("/api/default", {"alias": "b"}),
                ("/api/register", {"model": "owner/new-model"}),
            ]:
                status, _, data = self.request(port, "POST", path, payload, token,
                                               {"Origin": f"http://127.0.0.1:{port}"})
                self.assertEqual(status, 200, data)
                self.assertTrue(json.loads(data)["ok"])
            status, _, data = self.request(port, "GET", "/api/status")
            self.assertEqual(status, 200, data)
            result = json.loads(data)
            self.assertEqual(result["token"], token)
            self.assertEqual(result["default_alias"], "b")
            self.assertEqual(next(item["name"] for item in result["models"] if item["alias"] == "a"), "日本語モデル")
            self.assertIn({"alias": "new-model", "model": "owner/new-model"}, result["aliases"])
            self.assertEqual(json.loads((resources / "state/models.json").read_text())["default_alias"], "b")
            self.assertFalse((resources / "state/starting.json").exists())

    def test_strict_payload_types_reject_process_and_delete_operations(self):
        with self.server() as (port, resources, _):
            token = self.token(port)
            for path, payload in [
                ("/api/start", {"alias": "a", "thinking": "false"}),
                ("/api/start", {"alias": "a", "thinking": 1}),
                ("/api/stop", {"pid": True, "command": "fixture"}),
                ("/api/stop", {"pid": 123.5, "command": "fixture"}),
                ("/api/delete", {"alias": "a", "body": "true"}),
                ("/api/delete-preview", {"aliases": "a", "body": False}),
            ]:
                with self.subTest(path=path, payload=payload):
                    status, _, data = self.request(port, "POST", path, payload, token)
                    self.assertEqual(status, 400, data)
                    self.assertTrue(json.loads(data)["error"])
            self.assert_catalog_unchanged(resources)

    def test_operations_serialized_while_health_stays_responsive(self):
        with self.server() as (port, resources, process):
            token = self.token(port)
            lock = resources / "state/operations.lock"
            with lock.open("a+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                writer, written = self.async_request(port, "POST", "/api/rename", {"alias": "a", "name": "Serialized"}, token)
                try:
                    self.wait_for_blocked_operation(process, lock)
                    reader, read = self.async_request(port, "GET", "/api/status")
                    self.assertEqual(self.request(port, "GET", "/api/health")[0], 200)
                    self.assertTrue(writer.is_alive())
                    self.assertTrue(reader.is_alive())
                    self.assert_catalog_unchanged(resources)
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    writer.join(timeout=5)
                reader.join(timeout=5)
            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
            self.assertNotIn("error", written)
            self.assertNotIn("error", read)
            self.assertEqual(written["response"][0], 200)
            self.assertEqual(read["response"][0], 200)
            models = json.loads(read["response"][2])["models"]
            self.assertEqual(next(item["name"] for item in models if item["alias"] == "a"), "Serialized")

    def test_shutdown_finishes_an_accepted_mutation(self):
        with self.server() as (port, resources, process):
            token = self.token(port)
            lock = resources / "state/operations.lock"
            with lock.open("a+") as handle, socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    body = json.dumps({"alias": "a", "name": "Shutdown 完了"}).encode()
                    head = (f"POST /api/rename HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                            f"X-Server-Token: {token}\r\nContent-Length: {len(body)}\r\n\r\n").encode()
                    client.sendall(head + body)
                    self.wait_for_blocked_operation(process, lock)
                    process.terminate()
                    time.sleep(0.05)
                    self.assertIsNone(process.poll())
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                process.wait(timeout=3)
                self.assertEqual(process.returncode, 0)
                settings = json.loads((resources / "state/models.json").read_text())
                self.assertEqual(settings["display_names"]["owner/model-a"], "Shutdown 完了")

    @contextlib.contextmanager
    def events(self, port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/api/events")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        self.assertIsNone(response.getheader("Content-Length"))
        try:
            self.assertEqual(response.fp.readline(), b": connected\n")
            self.assertEqual(response.fp.readline(), b"\n")
            yield response
        finally:
            response.close()
            connection.close()

    def changed(self, response):
        self.assertEqual(response.fp.readline(), b"event: changed\n")
        self.assertEqual(response.fp.readline(), b"data: {}\n")
        self.assertEqual(response.fp.readline(), b"\n")

    def test_external_api_operation_notifies_open_ui_without_polling(self):
        with self.server() as (port, resources, _):
            token = self.token(port)
            with self.events(port) as response:
                self.assertEqual(self.request(port, "POST", "/api/default", {"alias": "b"}, token)[0], 200)
                self.changed(response)
                self.assertEqual(json.loads((resources / "state/models.json").read_text())["default_alias"], "b")
                self.assertFalse((resources / "state/starting.json").exists())

    def test_external_settings_change_notifies_open_ui(self):
        with self.server() as (port, resources, _), self.events(port) as response:
            replacement = resources / "state/settings.tmp"
            replacement.write_text('{"default_alias":"b"}')
            replacement.replace(resources / "state/models.json")
            self.changed(response)

    def test_event_disconnect_releases_connection(self):
        with self.server() as (port, resources, _):
            for _ in range(40):
                with self.events(port):
                    pass
            self.assertEqual(self.request(port, "GET", "/api/health")[0], 200)
            self.assert_catalog_unchanged(resources)


if __name__ == "__main__":
    unittest.main()
