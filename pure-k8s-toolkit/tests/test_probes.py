import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ptk import probes


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 401 if self.path == "/version" else 200
        self.send_response(code)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"pong")

    def log_message(self, *a):
        pass


class ProbeTest(unittest.TestCase):
    def test_http_status_counts_as_reachable(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            r = probes.https(f"http://127.0.0.1:{server.server_port}/version", timeout=2)
            self.assertTrue(r["reachable"])
            self.assertEqual(r["status"], 401)
            r = probes.https(f"http://127.0.0.1:{server.server_port}/ping", timeout=2)
            self.assertEqual((r["status"], r["body"]), (200, "pong"))
        finally:
            server.shutdown()
            server.server_close()

    def test_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = probes.tcp("127.0.0.1", port, timeout=1)
        self.assertFalse(r["reachable"])
        self.assertEqual(r["error"], "connection refused")
        r = probes.https(f"https://127.0.0.1:{port}/", timeout=1)
        self.assertFalse(r["reachable"])
        self.assertEqual(r["error"], "connection refused")

    def test_timeout(self):
        # A listening socket that never accepts: connect succeeds via backlog, the TLS read times out.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            r = probes.https(f"https://127.0.0.1:{s.getsockname()[1]}/", timeout=0.5)
            self.assertFalse(r["reachable"])
            self.assertEqual(r["error"], "timed out")
        finally:
            s.close()

    def test_dns(self):
        self.assertTrue(probes.dns("localhost")["reachable"])
        self.assertFalse(probes.dns("does-not-exist.invalid", timeout=2)["reachable"])


if __name__ == "__main__":
    unittest.main()
