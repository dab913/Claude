"""Minimal Prometheus text-format registry and HTTP server (stdlib only, so the
image builds without a PyPI mirror in an air-gapped environment)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Snapshot:
    """One scrape's worth of metrics. Build a new one each cycle, then publish it."""

    def __init__(self) -> None:
        self._meta: Dict[str, Tuple[str, str]] = {}
        self._samples: Dict[str, List[Tuple[Dict[str, str], float]]] = {}

    def add(self, name: str, value: float, labels: Optional[Dict[str, str]] = None,
            help: str = "", type: str = "gauge") -> None:
        if name not in self._meta:
            self._meta[name] = (help, type)
            self._samples[name] = []
        self._samples[name].append((dict(labels or {}), float(value)))

    def render(self) -> str:
        out = []
        for name, (help_text, mtype) in self._meta.items():
            if help_text:
                out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {mtype}")
            for labels, value in self._samples[name]:
                if labels:
                    body = ",".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()))
                    out.append(f"{name}{{{body}}} {value:g}")
                else:
                    out.append(f"{name} {value:g}")
        return "\n".join(out) + "\n"


class Exporter:
    """Serves /metrics, /healthz and /report (latest JSON report)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._lock = threading.Lock()
        self._metrics = "# no data yet\n"
        self._report: object = {}

    def publish(self, snapshot: Snapshot, report: object) -> None:
        text = snapshot.render()
        with self._lock:
            self._metrics = text
            self._report = report

    def start(self) -> ThreadingHTTPServer:
        exporter = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                with exporter._lock:
                    metrics, report = exporter._metrics, exporter._report
                if self.path == "/metrics":
                    self._send(200, "text/plain; version=0.0.4", metrics)
                elif self.path == "/report":
                    self._send(200, "application/json", json.dumps(report, indent=2))
                elif self.path in ("/healthz", "/readyz"):
                    self._send(200, "text/plain", "ok\n")
                else:
                    self._send(404, "text/plain", "not found\n")

            def _send(self, code: int, ctype: str, body: str) -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
