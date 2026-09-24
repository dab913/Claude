"""Ties together price checks, the buy strategy and the booking flow."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Callable

from .db import Store
from .models import Offer, Quote, Status, Tracker, utcnow
from .notify import Notifier
from .providers.base import FlightProvider, ProviderError
from .strategy import Action, Decision, decide

log = logging.getLogger("flight_tracker")

# Abort a purchase if the fare moved up more than this between search and booking.
PRICE_SLIPPAGE = 0.02


def load_passengers(path: str, expected: int) -> list[dict]:
    with open(path) as f:
        passengers = json.load(f)
    if not isinstance(passengers, list) or len(passengers) != expected:
        raise ValueError(f"{path} must be a JSON list of exactly {expected} passenger(s)")
    return passengers


class Engine:
    def __init__(self, store: Store, provider: FlightProvider, notifier: Notifier | None = None,
                 live: bool = False, clock: Callable[[], datetime] = utcnow):
        self.store = store
        self.provider = provider
        self.notifier = notifier or Notifier()
        self.live = live
        self.clock = clock

    # --- scheduling -----------------------------------------------------
    def is_due(self, t: Tracker, now: datetime) -> bool:
        if t.status is not Status.TRACKING:
            return False
        if t.last_checked_at is None or now >= t.deadline:
            return True
        # Small tolerance so a scheduler firing a minute early still counts.
        return now >= t.last_checked_at + timedelta(hours=t.check_every_hours) - timedelta(minutes=2)

    def run_due(self) -> int:
        now = self.clock()
        checked = 0
        for t in self.store.list_trackers(Status.TRACKING):
            if self.is_due(t, now):
                self.check(t)
                checked += 1
        return checked

    # --- one price check --------------------------------------------------
    def check(self, t: Tracker) -> Decision | None:
        now = self.clock()
        next_check_at = now + timedelta(hours=t.check_every_hours)
        self.store.mark_checked(t.id, now)

        try:
            offers = self.provider.search(t)
        except ProviderError as e:
            self._event(t, f"search failed: {e}")
            self._expire_if_past_deadline(t, now, "searches kept failing")
            return None

        offers = [o for o in offers if o.currency == t.currency and len(o.passenger_ids) == t.adults]
        if not offers:
            self._event(t, "no bookable offers found")
            self._expire_if_past_deadline(t, now, "no bookable offers")
            return None

        best = min(offers, key=lambda o: o.price)
        previous = [q.price for q in self.store.quotes(t.id)]
        self.store.add_quote(Quote(t.id, now, best.price, best.currency, best.offer_id, best.carrier))

        decision = decide(
            price=best.price, previous_prices=previous, created_at=t.created_at, deadline=t.deadline,
            now=now, next_check_at=next_check_at, max_price=t.max_price, target_price=t.target_price,
        )
        self._event(t, f"{best.carrier} {best.price:.2f} {best.currency} -> {decision.action.value}: {decision.reason}")

        if decision.action is Action.BUY:
            self._buy(t, best, decision, now)
        elif decision.action is Action.GIVE_UP:
            self.store.set_status(t.id, Status.EXPIRED)
            self.notifier.send(f"Tracker #{t.id} expired", f"{t.route}: {decision.reason}. Nothing was purchased.")
        return decision

    # --- purchase ---------------------------------------------------------
    def _buy(self, t: Tracker, offer: Offer, decision: Decision, now: datetime) -> None:
        headline = f"{t.route}: {offer.carrier} {offer.price:.2f} {offer.currency} ({decision.reason})"

        if not (t.auto_buy and self.live):
            mode = "auto-buy is off for this tracker" if not t.auto_buy else "running in dry-run mode (no --live)"
            self.store.set_status(t.id, Status.RECOMMENDED)
            self._event(t, f"buy signal, not purchasing: {mode}")
            self.notifier.send(f"Buy now - tracker #{t.id}", f"{headline}. Not purchased because {mode}.")
            return

        try:
            passengers = load_passengers(t.passengers_file or "", t.adults)
        except (OSError, ValueError) as e:
            self.store.set_status(t.id, Status.RECOMMENDED)
            self._event(t, f"cannot auto-buy, passenger details unusable: {e}")
            self.notifier.send(f"Buy manually - tracker #{t.id}", f"{headline}. Auto-buy failed: {e}")
            return

        if not self.store.claim_for_purchase(t.id):
            self._event(t, "purchase already in progress elsewhere; skipping")
            return

        try:
            fresh = self.provider.refresh_offer(offer)
        except ProviderError as e:
            self._release(t, now, f"could not confirm live price: {e}")
            return
        ceiling = min(t.max_price, offer.price * (1 + PRICE_SLIPPAGE))
        if fresh.price > ceiling:
            self._release(t, now, f"fare moved to {fresh.price:.2f} before booking (limit {ceiling:.2f})")
            return

        try:
            booking = self.provider.book(fresh, passengers)
        except Exception as e:  # booking state is unknown: stop and make a human look
            self.store.set_status(t.id, Status.FAILED)
            self._event(t, f"BOOKING FAILED: {e}")
            self.notifier.send(f"Booking FAILED - tracker #{t.id}",
                               f"{headline}. Error: {e}. Check your provider dashboard before retrying.")
            return

        self.store.add_purchase(t.id, booking, now)
        self.store.set_status(t.id, Status.PURCHASED)
        self._event(t, f"PURCHASED {booking.booking_reference} for {booking.price:.2f} {booking.currency}")
        self.notifier.send(f"Flight purchased - tracker #{t.id}",
                           f"{t.route}: booking ref {booking.booking_reference}, {booking.price:.2f} {booking.currency} "
                           f"({offer.carrier}). {decision.reason}.")

    def _release(self, t: Tracker, now: datetime, reason: str) -> None:
        self.store.set_status(t.id, Status.TRACKING)
        self._event(t, f"purchase aborted: {reason}")
        self._expire_if_past_deadline(t, now, reason)

    def _expire_if_past_deadline(self, t: Tracker, now: datetime, reason: str) -> None:
        if now >= t.deadline:
            self.store.set_status(t.id, Status.EXPIRED)
            self.notifier.send(f"Tracker #{t.id} expired", f"{t.route}: window ended ({reason}). Nothing was purchased.")

    def _event(self, t: Tracker, message: str) -> None:
        log.info("tracker #%s: %s", t.id, message)
        self.store.log(t.id, self.clock(), message)
