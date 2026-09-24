"""Notifications: always printed; optionally POSTed to a webhook (Slack, ntfy, Zapier...)."""

from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("flight_tracker")


class Notifier:
    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url

    def send(self, title: str, body: str) -> None:
        log.warning("%s: %s", title, body)
        if not self.webhook_url:
            return
        payload = json.dumps({"text": f"*{title}*\n{body}", "title": title, "body": body}).encode()
        req = urllib.request.Request(self.webhook_url, data=payload, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15).close()
        except OSError as e:  # never let a notification failure break tracking
            log.error("webhook notification failed: %s", e)
