"""Deterministic fake provider for testing, demos and strategy backtests."""

from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime
from typing import Callable

from ..models import Booking, Offer, Tracker, utcnow


class MockProvider:
    """Fares follow a random walk plus daily seasonality, reproducible per seed.

    ``clock`` lets simulations drive time forward without waiting.
    """

    name = "mock"

    def __init__(self, base_price: float = 400.0, seed: int = 0, volatility: float = 0.04,
                 clock: Callable[[], datetime] = utcnow):
        self.base_price = base_price
        self.seed = seed
        self.volatility = volatility
        self.clock = clock
        self.bookings: list[Booking] = []
        self._walk: dict[str, tuple[datetime, float]] = {}

    def _price(self, tracker: Tracker, now: datetime) -> float:
        # Mean-reverting fare level (fares bounce around a slowly rising trend),
        # plus short-lived dips/spikes as airlines open and close fare buckets.
        key = tracker.route
        start, level = self._walk.get(key, (now, 0.0))
        digest = hashlib.sha256(f"{self.seed}|{key}|{now.isoformat()}".encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        level = 0.85 * level + rng.gauss(0, self.volatility)
        self._walk[key] = (start, level)
        days = (now - start).total_seconds() / 86400
        trend = 0.01 * days
        seasonal = 0.02 * math.sin(2 * math.pi * days)
        bucket = rng.choice([0.0] * 8 + [-0.08, 0.10])
        return round(self.base_price * tracker.adults * (1 + level + trend + seasonal + bucket), 2)

    def search(self, tracker: Tracker) -> list[Offer]:
        now = self.clock()
        price = self._price(tracker, now)
        stamp = int(now.timestamp())
        return [
            Offer(f"mock_{stamp}_a", price, tracker.currency, "Mock Air", passenger_ids=[f"pas_{i}" for i in range(tracker.adults)]),
            Offer(f"mock_{stamp}_b", round(price * 1.12, 2), tracker.currency, "Fake Airways",
                  passenger_ids=[f"pas_{i}" for i in range(tracker.adults)]),
        ]

    def refresh_offer(self, offer: Offer) -> Offer:
        return offer

    def book(self, offer: Offer, passengers: list[dict]) -> Booking:
        booking = Booking(f"ord_{offer.offer_id}", f"MOCK{len(self.bookings) + 1:03d}", offer.price, offer.currency)
        self.bookings.append(booking)
        return booking
