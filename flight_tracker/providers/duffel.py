"""Duffel (https://duffel.com) provider: live search, re-pricing and ticketing.

Uses a Duffel access token (``DUFFEL_ACCESS_TOKEN``). Test-mode tokens
(``duffel_test_...``) search and "book" against Duffel's sandbox airline with no
money moving; live tokens buy real tickets paid from your Duffel balance.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime

from ..models import Booking, Offer, Tracker
from .base import ProviderError

API_BASE = "https://api.duffel.com"
API_VERSION = "v2"
PASSENGER_FIELDS = ("title", "gender", "given_name", "family_name", "born_on", "email", "phone_number")


class DuffelProvider:
    name = "duffel"

    def __init__(self, access_token: str, timeout: float = 60.0):
        if not access_token:
            raise ProviderError("DUFFEL_ACCESS_TOKEN is not set")
        self.token = access_token
        self.timeout = timeout

    @property
    def is_test_mode(self) -> bool:
        return self.token.startswith("duffel_test_")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            API_BASE + path,
            method=method,
            data=json.dumps({"data": body}).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Duffel-Version": API_VERSION,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())["data"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                errors = json.loads(detail).get("errors", [])
                detail = "; ".join(err.get("message", "") for err in errors) or detail
            except ValueError:
                pass
            raise ProviderError(f"Duffel {method} {path} failed ({e.code}): {detail}") from e
        except urllib.error.URLError as e:
            raise ProviderError(f"Duffel {method} {path} failed: {e.reason}") from e

    @staticmethod
    def _parse_offer(data: dict) -> Offer:
        slices = []
        for s in data.get("slices", []):
            segs = s.get("segments", [])
            if segs:
                hops = [segs[0]["origin"]["iata_code"]] + [seg["destination"]["iata_code"] for seg in segs]
                slices.append(f"{'-'.join(hops)} dep {segs[0].get('departing_at', '?')}")
        expires = data.get("expires_at")
        return Offer(
            offer_id=data["id"],
            price=float(data["total_amount"]),
            currency=data["total_currency"],
            carrier=(data.get("owner") or {}).get("name", "?"),
            summary=" | ".join(slices),
            expires_at=datetime.fromisoformat(expires.replace("Z", "+00:00")) if expires else None,
            passenger_ids=[p["id"] for p in data.get("passengers", [])],
        )

    def search(self, tracker: Tracker) -> list[Offer]:
        slices = [{"origin": tracker.origin, "destination": tracker.destination, "departure_date": tracker.depart_date}]
        if tracker.return_date:
            slices.append({"origin": tracker.destination, "destination": tracker.origin, "departure_date": tracker.return_date})
        body = {
            "slices": slices,
            "passengers": [{"type": "adult"} for _ in range(tracker.adults)],
            "cabin_class": tracker.cabin,
        }
        if tracker.max_connections is not None:
            body["max_connections"] = tracker.max_connections
        data = self._request("POST", "/air/offer_requests?return_offers=true&supplier_timeout=20000", body)
        return [self._parse_offer(o) for o in data.get("offers", [])]

    def refresh_offer(self, offer: Offer) -> Offer:
        return self._parse_offer(self._request("GET", f"/air/offers/{offer.offer_id}"))

    def book(self, offer: Offer, passengers: list[dict]) -> Booking:
        if len(passengers) != len(offer.passenger_ids):
            raise ProviderError(f"offer needs {len(offer.passenger_ids)} passengers, got {len(passengers)}")
        pax = []
        for pid, p in zip(offer.passenger_ids, passengers):
            missing = [f for f in PASSENGER_FIELDS if not p.get(f)]
            if missing:
                raise ProviderError(f"passenger {p.get('given_name', '?')} missing fields: {', '.join(missing)}")
            pax.append({"id": pid, **{f: p[f] for f in PASSENGER_FIELDS}})
        data = self._request("POST", "/air/orders", {
            "type": "instant",
            "selected_offers": [offer.offer_id],
            "payments": [{"type": "balance", "currency": offer.currency, "amount": f"{offer.price:.2f}"}],
            "passengers": pax,
        })
        return Booking(data["id"], data["booking_reference"], float(data["total_amount"]), data["total_currency"])
