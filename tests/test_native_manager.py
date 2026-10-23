"""Exercise the compiled Swift HTTP server against an isolated, instrumented worker."""
import contextlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

WORKER = '''import json, os, sys, time
from pathlib import Path
request = json.load(sys.stdin)
events = Path(__file__).resolve().parents[2] / "events"
path = request["path"]
with events.open("a") as handle:
    handle.write(path + " started\\n")
if request.get("payload", {}).get("name") == "hold":
    time.sleep(0.3)
with events.open("a") as handle:
    handle.write(path + " finished\\n")
print(json.dumps({"status": 200, "payload": {"request": request, "worker_pid": os.getpid(), "result": request.get("payload", {}), "token": "worker-cannot-set-token"}}))
'''


@unittest.skipUnless(sys.platform == "darwin", "native macOS server")
class NativeManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="mlx-manager-tests-")
        cls.binary = Path(cls.build.name) / "MLXServerManager"
        subprocess.run([
            "/usr/bin/swiftc", "-swift-version", "5", "-module-cache-path", str(Path(cls.build.name) / "module-cache"),
            "-framework", "Network", "-framework", "Security",
            str(ROOT / "native/ServerManager.swift"), "-o", str(cls.binary),
        ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    @contextlib.contextmanager
    def server(self, python=None):
        with tempfile.TemporaryDirectory(prefix="mlx-http-fixture-") as temporary:
            resources = Path(temporary)
            package = resources / "src/mlx_activation_lab"
            package.mkdir(parents=True)
            shutil.copytree(ROOT / "src/mlx_activation_lab/server_web", package / "server_web")
            (package / "manager_bridge.py").write_text(WORKER)
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            with (resources / "server.log").open("w+") as log:
                process = subprocess.Popen([
                    str(self.binary), "--port", str(port), "--resources", str(resources),
                    "--python", python or sys.executable,
                ], stdout=log, stderr=log, env={**os.environ,
                    "MLX_MANAGER_STATE_ROOT": str(resources / "state"),
                    "MLX_ACTIVATION_LAB_SERVER_ROOT": str(resources / "launcher"),
                    "MLX_MANAGER_GGUF_ROOT": str(resources / "gguf"),
                })
                try:
                    for _ in range(100):
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail(log.read())
                        try:
                            status, _, _ = self.request(port, "GET", "/api/health")
                            if status == 200:
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
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
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
        self.assertEqual(status, 200)
        result = json.loads(data)
        self.assertRegex(result["token"], r"^[0-9a-f]{64}$")
        return result["token"]

    def raw(self, port, request):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(request.encode())
            response = http.client.HTTPResponse(client)
            response.begin()
            status, data = response.status, response.read()
            return status, data

    def test_idle_socket_health_static_assets_and_native_identity(self):
        with self.server(python="/missing/python") as (port, resources, _), socket.create_connection(("127.0.0.1", port)) as idle:
            status, headers, data = self.request(port, "GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)["backend"], "swift")
            configuration = json.loads(data)["configuration"]
            self.assertEqual(Path(configuration["resources"]).resolve(), resources.resolve())
            self.assertEqual(Path(configuration["server_root"]).resolve(), resources.resolve() / "launcher")
            self.assertEqual(Path(configuration["gguf_root"]).resolve(), resources.resolve() / "gguf")
            self.assertEqual(Path(configuration["state_root"]).resolve(), resources.resolve() / "state")
            self.assertEqual(configuration["python"], "/missing/python")
            self.assertEqual(headers["Cache-Control"], "no-store")
            for path in ("/", "/app.js", "/style.css"):
                status, _, data = self.request(port, "GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(data)
                self.assertNotIn(b"__SERVER_TOKEN__", data)
            self.assertFalse((resources / "events").exists())
            self.assertEqual(self.request(port, "GET", "/api/status")[0], 503)

    def test_auth_host_origin_and_unknown_route_do_not_launch_worker(self):
        with self.server() as (port, resources, _):
            for method, path, headers, expected in [
                ("POST", "/api/start", {}, 403),
                ("POST", "/api/delete", {"X-Server-Token": "wrong"}, 403),
                ("GET", "/api/status", {"Host": f"attacker.example:{port}"}, 403),
                ("GET", "/api/status", {"Origin": "https://attacker.example"}, 403),
                ("GET", "/../manager_bridge.py", {}, 404),
                ("PUT", "/api/start", {}, 405),
            ]:
                with self.subTest(method=method, path=path, headers=headers):
                    self.assertEqual(self.request(port, method, path, headers=headers)[0], expected)
            self.assertFalse((resources / "events").exists())

    def test_framing_and_payload_rejected_before_worker(self):
        with self.server() as (port, resources, _):
            token = self.token(port)
            (resources / "events").unlink()
            prefix = f"POST /api/start HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Server-Token: {token}\r\n"
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
            self.assertFalse((resources / "events").exists())

    def test_valid_requests_forward_values_and_workers_exit(self):
        with self.server() as (port, _, _):
            token = self.token(port)
            for path, payload in [
                ("/api/start", {"alias": "日本語モデル", "thinking": False}),
                ("/api/stop", {"pid": 12345, "command": "expected command"}),
                ("/api/delete", {"aliases": ["a", "b"], "body": True, "fingerprint": "proof"}),
            ]:
                status, _, data = self.request(port, "POST", path, payload, token,
                                               {"Origin": f"http://127.0.0.1:{port}"})
                self.assertEqual(status, 200)
                result = json.loads(data)
                self.assertEqual(result["request"]["payload"], payload)
                with self.assertRaises(ProcessLookupError):
                    os.kill(result["worker_pid"], 0)

    def test_operations_serialized_while_health_stays_responsive(self):
        with self.server() as (port, resources, process):
            token = self.token(port)
            events = resources / "events"
            events.unlink()
            results = []
            writer = threading.Thread(target=lambda: results.append(self.request(
                port, "POST", "/api/rename", {"name": "hold"}, token)))
            writer.start()
            for _ in range(100):
                if events.exists() and "started" in events.read_text():
                    break
                time.sleep(0.01)
            self.assertTrue(events.exists())
            self.assertEqual(self.request(port, "GET", "/api/health")[0], 200)
            self.assertEqual(self.request(port, "GET", "/api/status")[0], 200)
            writer.join(timeout=3)
            self.assertEqual(results[0][0], 200)
            self.assertEqual(events.read_text().splitlines(), [
                "/api/rename started", "/api/rename finished", "/api/status started", "/api/status finished"])


    def test_shutdown_finishes_an_accepted_mutation(self):
        with self.server() as (port, resources, process):
            token = self.token(port)
            events = resources / "events"
            events.unlink()
            body = json.dumps({"name": "hold"}).encode()
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                head = (f"POST /api/rename HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                        f"X-Server-Token: {token}\r\nContent-Length: {len(body)}\r\n\r\n").encode()
                client.sendall(head + body)
                for _ in range(100):
                    if events.exists() and "started" in events.read_text():
                        break
                    time.sleep(0.01)
                self.assertTrue(events.exists())
                process.terminate()
                process.wait(timeout=3)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(events.read_text().splitlines(), ["/api/rename started", "/api/rename finished"])


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
                self.assertEqual(self.request(port, "POST", "/api/default", {"alias": "a"}, token)[0], 200)
                self.changed(response)
                # A subscriber does not cause any status worker to run on its own.
                self.assertEqual((resources / "events").read_text().splitlines(), [
                    "/api/status started", "/api/status finished", "/api/default started", "/api/default finished"])

    def test_process_exit_notifies_ui_even_when_stop_is_external(self):
        with self.server() as (port, _, _):
            token = self.token(port)
            child = subprocess.Popen(["/bin/sleep", "30"])
            try:
                with self.events(port) as response:
                    self.assertEqual(self.request(port, "POST", "/api/start", {"pid": child.pid}, token)[0], 200)
                    self.changed(response)
                    child.terminate()
                    child.wait(timeout=2)
                    self.changed(response)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=2)

    def test_event_disconnect_releases_connection_and_no_worker_is_started(self):
        with self.server() as (port, resources, _):
            for _ in range(40):
                with self.events(port):
                    pass
            self.assertEqual(self.request(port, "GET", "/api/health")[0], 200)
            self.assertFalse((resources / "events").exists())


if __name__ == "__main__":
    unittest.main()
