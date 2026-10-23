import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "native macOS filesystem notifications")
class NativeStateEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="mlx-state-tests-")
        folder = Path(cls.temporary.name)
        source = (ROOT / 'native/ServerManager.swift').read_text().split('\ndo {\n    let args =')[0]
        source += '''
let queue = DispatchQueue(label: "fixture")
let watcher = StateFiles(paths: [CommandLine.arguments[1]], queue: queue) {
    print("changed"); fflush(stdout)
}
print("ready"); fflush(stdout)
withExtendedLifetime(watcher) { dispatchMain() }
'''
        (folder / 'main.swift').write_text(source)
        cls.binary = folder / 'observer'
        subprocess.run(['/usr/bin/swiftc', '-swift-version', '5', '-module-cache-path', str(folder / 'cache'),
                        '-framework', 'Network', '-framework', 'Security', str(folder / 'main.swift'),
                        '-o', str(cls.binary)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def line(self, process, timeout=2):
        self.assertTrue(select.select([process.stdout], [], [], timeout)[0], 'notification did not arrive')
        return process.stdout.readline()

    def test_same_size_rewrite_and_atomic_replacement_are_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('aaaa')
            process = subprocess.Popen([str(self.binary), str(path)], stdout=subprocess.PIPE)
            try:
                self.assertEqual(self.line(process), b'ready\n')
                path.write_text('bbbb')
                self.assertEqual(self.line(process), b'changed\n')
                path.write_text('cccc')
                self.assertEqual(self.line(process), b'changed\n')
                replacement = path.with_suffix('.tmp')
                replacement.write_text('dddd')
                replacement.replace(path)
                self.assertEqual(self.line(process), b'changed\n')
                path.write_text('eeee')
                self.assertEqual(self.line(process), b'changed\n')
            finally:
                process.terminate(); process.wait(timeout=2); process.stdout.close()

    def test_continuous_events_are_delivered_before_writer_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server.log'
            path.write_text('')
            process = subprocess.Popen([str(self.binary), str(path)], stdout=subprocess.PIPE)
            stop = threading.Event()
            def write():
                while not stop.is_set():
                    with path.open('a') as handle:
                        handle.write('new log line\n')
                    time.sleep(0.02)
            writer = threading.Thread(target=write)
            try:
                self.assertEqual(self.line(process), b'ready\n')
                writer.start()
                self.assertEqual(self.line(process, timeout=1), b'changed\n')
                self.assertTrue(writer.is_alive())
            finally:
                stop.set(); writer.join(timeout=2)
                process.terminate(); process.wait(timeout=2); process.stdout.close()
