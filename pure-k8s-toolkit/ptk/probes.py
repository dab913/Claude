"""Network probes shared by node-check and the cluster health checker (stdlib only).

Each probe separates "cannot reach it" from "reached it but something about the
answer is wrong", because they point to different teams: a timeout to
10.43.0.1:443 is a dataplane (Cilium) problem, a 401 from it is not.
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Dict, Optional


def _opener(ctx: ssl.SSLContext) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPSHandler(context=ctx))


def https(url: str, timeout: float = 5, ca_file: Optional[str] = None,
          cert_file: Optional[str] = None, key_file: Optional[str] = None,
          token: str = "", verify: bool = True, method: str = "GET",
          data: Optional[bytes] = None) -> Dict[str, object]:
    """Returns {reachable, status, body, latency_ms, error, tls_untrusted}.

    reachable is True for any HTTP response, including 4xx/5xx. When verification
    fails, the request is retried without verification so the result still says
    whether the endpoint is up (tls_untrusted=True)."""
    if verify:
        ctx = ssl.create_default_context(cafile=ca_file)
    else:
        ctx = ssl._create_unverified_context()
    if cert_file:
        ctx.load_cert_chain(cert_file, key_file)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    started = time.monotonic()
    result: Dict[str, object] = {"reachable": False, "status": 0, "body": "", "latency_ms": 0.0,
                                 "error": "", "tls_untrusted": False}
    try:
        with _opener(ctx).open(req, timeout=timeout) as resp:
            result.update(reachable=True, status=resp.status, body=resp.read(65536).decode(errors="replace"))
    except urllib.error.HTTPError as exc:
        result.update(reachable=True, status=exc.code, body=exc.read(4096).decode(errors="replace"))
    except urllib.error.URLError as exc:
        reason = exc.reason
        if verify and isinstance(reason, ssl.SSLCertVerificationError):
            retry = https(url, timeout, None, cert_file, key_file, token, False, method, data)
            retry["tls_untrusted"] = True
            retry["error"] = f"certificate not trusted: {reason.verify_message}"
            return retry
        result["error"] = _describe(reason)
    except (socket.timeout, TimeoutError):
        result["error"] = "timed out"
    except (OSError, ssl.SSLError) as exc:
        result["error"] = _describe(exc)
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def tcp(host: str, port: int, timeout: float = 3) -> Dict[str, object]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"reachable": True, "error": "", "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except OSError as exc:
        return {"reachable": False, "error": _describe(exc), "latency_ms": round((time.monotonic() - started) * 1000, 1)}


def dns(name: str, timeout: float = 3) -> Dict[str, object]:
    """Resolves name with the system resolver (CoreDNS inside a pod). The resolver
    has no timeout argument, so a hung lookup is bounded by the socket default."""
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    started = time.monotonic()
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)})
        return {"reachable": True, "addresses": addrs, "error": "",
                "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except OSError as exc:
        return {"reachable": False, "addresses": [], "error": _describe(exc),
                "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    finally:
        socket.setdefaulttimeout(old)


def _describe(exc: object) -> str:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror.lower()
    return str(exc) or type(exc).__name__
