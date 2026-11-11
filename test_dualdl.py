import hashlib
import http.server
import os
import re
import tempfile
import threading
import unittest

import dualdl

PAYLOAD = os.urandom(3 * dualdl.CHUNK_SIZE + 12345)
SOURCES = ["127.0.0.1", "127.0.0.2"]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_body(self, status, body, headers=()):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        srv = self.server
        with srv.lock:
            srv.clients.add(self.client_address[0])
            srv.requests += 1
            n = srv.requests
        if self.path == "/redirect":
            return self.send_body(302, b"", [("Location", "/file")])
        if self.path == "/norange":
            return self.send_body(200, PAYLOAD)
        if srv.fail_every and n > 1 and n % srv.fail_every == 0:
            return self.send_body(500, b"fail")
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers["Range"]).groups())
        if srv.truncate_first and n == 2:
            self.send_response(206)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
            self.end_headers()
            self.wfile.write(PAYLOAD[start:start + 10])
            self.close_connection = True
            return
        self.send_body(206, PAYLOAD[start:end + 1], [
            ("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}"),
            ("ETag", '"v1"'),
        ])


class DualDlTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.server.daemon_threads = True
        self.server.lock = threading.Lock()
        self.server.clients = set()
        self.server.requests = 0
        self.server.fail_every = 0
        self.server.truncate_first = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.dir = tempfile.TemporaryDirectory()
        self.output = os.path.join(self.dir.name, "out.bin")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def assert_downloaded(self):
        with open(self.output, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(PAYLOAD).digest())
        self.assertFalse(os.path.exists(self.output + ".part"))

    def test_uses_both_sources_and_preserves_content(self):
        size, _, received = dualdl.download(f"{self.base}/file", self.output, SOURCES)
        self.assert_downloaded()
        self.assertEqual(self.server.clients, set(SOURCES))
        self.assertEqual(sum(received.values()), size)

    def test_follows_redirect(self):
        dualdl.download(f"{self.base}/redirect", self.output, SOURCES)
        self.assert_downloaded()

    def test_retries_server_errors(self):
        self.server.fail_every = 3
        dualdl.download(f"{self.base}/file", self.output, SOURCES)
        self.assert_downloaded()

    def test_retries_truncated_response(self):
        self.server.truncate_first = True
        _, _, received = dualdl.download(f"{self.base}/file", self.output, SOURCES)
        self.assert_downloaded()
        self.assertEqual(sum(received.values()), len(PAYLOAD))

    def test_rejects_server_without_range_support(self):
        with self.assertRaises(dualdl.DownloadError):
            dualdl.download(f"{self.base}/norange", self.output, SOURCES)
        self.assertFalse(os.path.exists(self.output))

    def test_refuses_to_overwrite_without_force(self):
        with open(self.output, "wb") as f:
            f.write(b"keep")
        with self.assertRaises(dualdl.DownloadError):
            dualdl.download(f"{self.base}/file", self.output, SOURCES)
        with open(self.output, "rb") as f:
            self.assertEqual(f.read(), b"keep")

    def test_fails_when_every_path_is_down(self):
        self.server.fail_every = 1
        with self.assertRaises(dualdl.DownloadError):
            dualdl.download(f"{self.base}/file", self.output, SOURCES, streams=1)
        self.assertFalse(os.path.exists(self.output + ".part"))


if __name__ == "__main__":
    unittest.main()
