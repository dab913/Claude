"""In-cluster Kubernetes API reads using the pod's service account (stdlib only)."""

from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import List

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class Kube:
    def __init__(self) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise RuntimeError("not running in a cluster (KUBERNETES_SERVICE_HOST unset)")
        if ":" in host:
            host = f"[{host}]"
        self.base = f"https://{host}:{port}"
        with open(os.path.join(SA_DIR, "token")) as f:
            self._token = f.read().strip()
        self._ctx = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))

    def list(self, path: str) -> List[dict]:
        items: List[dict] = []
        cont = ""
        while True:
            url = f"{self.base}{path}?limit=500" + (f"&continue={cont}" if cont else "")
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                body = json.load(resp)
            items.extend(body.get("items", []))
            cont = body.get("metadata", {}).get("continue", "")
            if not cont:
                return items
