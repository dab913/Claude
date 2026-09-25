"""Read-only Kubernetes API client (stdlib only).

Three ways to reach the API:
  * in-cluster through the kubernetes Service (default for pods);
  * in-cluster, but straight at the local API server (api="https://127.0.0.1:6443")
    for hostNetwork pods on control-plane nodes, so the check does not depend on
    the Service dataplane (Cilium) it may be diagnosing;
  * out of band with RKE2's admin client certificate, for podman on a controller.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import List, Optional

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class Kube:
    def __init__(self, api: Optional[str] = None, token_file: Optional[str] = None,
                 ca_file: Optional[str] = None, cert_file: Optional[str] = None,
                 key_file: Optional[str] = None, timeout: float = 30) -> None:
        if api is None:
            api = os.environ.get("PTK_KUBE_API")
        if api is None:
            host = os.environ.get("KUBERNETES_SERVICE_HOST")
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if not host:
                raise RuntimeError("not running in a cluster (KUBERNETES_SERVICE_HOST unset) and no API URL given")
            if ":" in host:
                host = f"[{host}]"
            api = f"https://{host}:{port}"
        self.base = api.rstrip("/")
        self.timeout = timeout

        if cert_file is None and token_file is None and os.path.exists(os.path.join(SA_DIR, "token")):
            token_file = os.path.join(SA_DIR, "token")
        if ca_file is None and os.path.exists(os.path.join(SA_DIR, "ca.crt")):
            ca_file = os.path.join(SA_DIR, "ca.crt")

        self._token = ""
        if token_file:
            with open(token_file) as f:
                self._token = f.read().strip()
        ctx = ssl.create_default_context(cafile=ca_file)
        if cert_file:
            ctx.load_cert_chain(cert_file, key_file)
        # API traffic never goes through an HTTP proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                   urllib.request.HTTPSHandler(context=ctx))

    def get(self, path: str) -> dict:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                body = json.loads(body).get("message", body)
            except ValueError:
                pass
            raise KubeError(exc.code, str(body)[:300]) from None

    def list(self, path: str) -> List[dict]:
        items: List[dict] = []
        cont = ""
        sep = "&" if "?" in path else "?"
        while True:
            body = self.get(f"{path}{sep}limit=500" + (f"&continue={cont}" if cont else ""))
            items.extend(body.get("items", []))
            cont = body.get("metadata", {}).get("continue", "")
            if not cont:
                return items
