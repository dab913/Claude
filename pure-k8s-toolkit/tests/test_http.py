import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ptk.flasharray import FlashArray
from ptk.metrics import Exporter, Snapshot

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PAGES = {
    "": {"items": [{"name": "px-a"}], "continuation_token": "p2"},
    "p2": {"items": [{"name": "px-b"}]},
}


class FakeArray(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/api_version":
            return self._json({"version": ["1.19", "2.4", "2.36", "2.9"]})
        if url.path == "/api/2.36/volumes":
            if self.headers.get("x-auth-token") != "sess":
                return self._json({}, 401)
            q = parse_qs(url.query)
            assert q["destroyed"] == ["false"]
            return self._json(PAGES[q.get("continuation_token", [""])[0]])
        self._json({}, 404)

    def do_POST(self):
        if self.path == "/api/2.36/login" and self.headers.get("api-token") == "tok":
            self.send_response(200)
            self.send_header("x-auth-token", "sess")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json({}, 401)

    def _json(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class HttpTest(unittest.TestCase):
    def test_flasharray_login_and_pagination(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeArray)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            fa = FlashArray("fa01", f"http://127.0.0.1:{server.server_port}", "tok")
            self.assertEqual(fa.api_version(), "2.36")  # numeric, not string, max
            self.assertEqual([v["name"] for v in fa.volumes()], ["px-a", "px-b"])
        finally:
            server.shutdown()
            server.server_close()

    def test_exporter_serves_metrics(self):
        exp = Exporter(0)
        server = exp.start()
        try:
            snap = Snapshot()
            snap.add("ptk_x", 1, {"a": 'q"uote'})
            exp.publish(snap, {"k": 1})
            base = f"http://127.0.0.1:{server.server_port}"
            body = DIRECT.open(base + "/metrics").read().decode()
            self.assertIn('ptk_x{a="q\\"uote"} 1', body)
            self.assertEqual(json.load(DIRECT.open(base + "/report")), {"k": 1})
        finally:
            server.shutdown()
            server.server_close()


    def test_readyz_follows_callback(self):
        state = {"ok": False}
        exp = Exporter(0, ready=lambda: state["ok"])
        server = exp.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                DIRECT.open(base + "/readyz")
            self.assertEqual(ctx.exception.code, 503)
            self.assertEqual(DIRECT.open(base + "/healthz").status, 200)  # liveness unaffected
            state["ok"] = True
            self.assertEqual(DIRECT.open(base + "/readyz").status, 200)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
