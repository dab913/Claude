"""Interface every flight data / booking provider implements."""

from __future__ import annotations

from typing import Protocol

from ..models import Booking, Offer, Tracker


class ProviderError(Exception):
    pass


class FlightProvider(Protocol):
    name: str

    def search(self, tracker: Tracker) -> list[Offer]:
        """Return currently bookable offers for the tracker's trip."""

    def refresh_offer(self, offer: Offer) -> Offer:
        """Re-fetch an offer right before booking to confirm its live price."""

    def book(self, offer: Offer, passengers: list[dict]) -> Booking:
        """Buy the offer. Passengers are dicts as described in the README."""
