"""Read-only FlashArray REST 2.x client (stdlib only).

Use an API token for a read-only array user:
    pureadmin create --role readonly ptk-audit
    pureadmin create --api-token ptk-audit
"""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from typing import Dict, Iterator, List, Optional


class FlashArray:
    def __init__(self, name: str, endpoint: str, api_token: str,
                 ca_file: Optional[str] = None, insecure: bool = False, timeout: float = 30) -> None:
        self.name = name
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.startswith("http"):
            self.endpoint = "https://" + self.endpoint
        self._api_token = api_token
        self._timeout = timeout
        if insecure:
            self._ctx = ssl._create_unverified_context()
        else:
            self._ctx = ssl.create_default_context(cafile=ca_file)
        self._session: Optional[str] = None
        self._version: Optional[str] = None
        # Array management traffic stays on the management network; ignore any
        # HTTP(S)_PROXY that happens to be set in the pod environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                   urllib.request.HTTPSHandler(context=self._ctx))

    def _request(self, method: str, path: str, headers: Dict[str, str],
                 params: Optional[Dict[str, str]] = None):
        url = self.endpoint + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, headers=headers,
                                     data=b"" if method == "POST" else None)
        return self._opener.open(req, timeout=self._timeout)

    def api_version(self) -> str:
        if self._version is None:
            with self._request("GET", "/api/api_version", {}) as resp:
                versions = json.load(resp).get("version", [])
            v2 = [v for v in versions if v.startswith("2.")]
            if not v2:
                raise RuntimeError(f"{self.name}: array does not offer REST 2.x (got {versions})")
            self._version = max(v2, key=lambda v: tuple(int(x) for x in v.split(".")))
        return self._version

    def login(self) -> None:
        with self._request("POST", f"/api/{self.api_version()}/login", {"api-token": self._api_token}) as resp:
            self._session = resp.headers.get("x-auth-token")
        if not self._session:
            raise RuntimeError(f"{self.name}: login returned no x-auth-token")

    def _get_items(self, resource: str, params: Dict[str, str]) -> Iterator[dict]:
        if not self._session:
            self.login()
        params = dict(params, limit="1000")
        while True:
            with self._request("GET", f"/api/{self.api_version()}/{resource}",
                               {"x-auth-token": self._session or ""}, params) as resp:
                body = json.load(resp)
            yield from body.get("items", [])
            token = body.get("continuation_token")
            if not token:
                return
            params["continuation_token"] = token

    def volumes(self) -> List[dict]:
        return list(self._get_items("volumes", {"destroyed": "false"}))

    def destroyed_volumes(self) -> List[dict]:
        return list(self._get_items("volumes", {"destroyed": "true"}))


def wwid_for_serial(serial: str) -> str:
    """The SCSI WWID Linux shows for a FlashArray volume (multipath -ll, /sys/block/dm-*/dm/uuid)."""
    return "3624a9370" + serial.lower()
