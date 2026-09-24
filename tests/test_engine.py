import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from flight_tracker.db import Store
from flight_tracker.engine import Engine
from flight_tracker.models import Booking, Offer, Status, Tracker
from flight_tracker.notify import Notifier
from flight_tracker.providers.base import ProviderError

START = datetime(2030, 1, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, title, body):
        self.sent.append((title, body))


class ScriptedProvider:
    """Returns prices from a list, one per search."""

    name = "scripted"

    def __init__(self, prices, refresh_delta=0.0, book_error=None):
        self.prices = list(prices)
        self.refresh_delta = refresh_delta
        self.book_error = book_error
        self.booked = []
        self.searches = 0

    def search(self, t):
        price = self.prices[min(self.searches, len(self.prices) - 1)]
        self.searches += 1
        return [Offer(f"off_{self.searches}", price, "USD", "Test Air", passenger_ids=["pas_1"]),
                Offer(f"eur_{self.searches}", price / 2, "EUR", "Other", passenger_ids=["pas_1"])]

    def refresh_offer(self, offer):
        return Offer(offer.offer_id, offer.price + self.refresh_delta, offer.currency, offer.carrier,
                     passenger_ids=offer.passenger_ids)

    def book(self, offer, passengers):
        if self.book_error:
            raise self.book_error
        self.booked.append((offer, passengers))
        return Booking("ord_1", "ABC123", offer.price, offer.currency)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:")
        self.notifier = RecordingNotifier()
        fd, self.pax_file = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump([{"title": "mr", "gender": "m", "given_name": "Test", "family_name": "User",
                        "born_on": "1990-01-01", "email": "t@example.com", "phone_number": "+15555550100"}], f)

    def tearDown(self):
        self.store.close()
        os.unlink(self.pax_file)

    def tracker(self, **kw):
        defaults = dict(origin="JFK", destination="LHR", depart_date="2030-02-01", max_price=600.0,
                        created_at=START, deadline=START + timedelta(days=5), check_every_hours=3,
                        auto_buy=True, passengers_file=self.pax_file)
        defaults.update(kw)
        return self.store.add_tracker(Tracker(**defaults))

    def run_window(self, provider, live=True, **kw):
        t = self.tracker(**kw)
        engine = Engine(self.store, provider, self.notifier, live=live, clock=self.clock)
        while self.clock.now <= t.deadline + timedelta(hours=6):
            engine.run_due()
            self.clock.now += timedelta(hours=1)
        return self.store.get_tracker(t.id)

    def test_buys_exactly_once_at_a_dip_after_exploring(self):
        prices = [500, 480, 510, 490, 505, 495, 500, 520, 515, 485, 530, 505, 470]  # dip at hour 36, after exploring
        provider = ScriptedProvider(prices + [520] * 40)
        t = self.run_window(provider)
        self.assertIs(t.status, Status.PURCHASED)
        self.assertEqual(len(provider.booked), 1)
        self.assertEqual(provider.booked[0][0].price, 470)
        self.assertEqual(provider.booked[0][1][0]["given_name"], "Test")
        self.assertEqual(self.store.purchase(t.id).booking_reference, "ABC123")

    def test_ignores_offers_in_other_currencies(self):
        provider = ScriptedProvider([500])
        t = self.tracker()
        Engine(self.store, provider, self.notifier, live=True, clock=self.clock).check(t)
        self.assertEqual(self.store.quotes(t.id)[0].currency, "USD")

    def test_dry_run_only_recommends(self):
        provider = ScriptedProvider([300])
        t = self.run_window(provider, live=False, target_price=350)
        self.assertIs(t.status, Status.RECOMMENDED)
        self.assertEqual(provider.booked, [])
        self.assertTrue(any("Buy now" in title for title, _ in self.notifier.sent))

    def test_alert_only_tracker_never_books_even_live(self):
        provider = ScriptedProvider([300])
        t = self.run_window(provider, auto_buy=False, target_price=350)
        self.assertIs(t.status, Status.RECOMMENDED)
        self.assertEqual(provider.booked, [])

    def test_buys_at_deadline_if_nothing_better(self):
        provider = ScriptedProvider([400 + i for i in range(60)])  # steadily rising
        t = self.run_window(provider)
        self.assertIs(t.status, Status.PURCHASED)
        self.assertLessEqual(self.clock.now - timedelta(hours=6), t.deadline + timedelta(hours=1))

    def test_expires_when_always_above_cap(self):
        provider = ScriptedProvider([700])
        t = self.run_window(provider)
        self.assertIs(t.status, Status.EXPIRED)
        self.assertEqual(provider.booked, [])

    def test_price_jump_before_booking_aborts_then_retries(self):
        provider = ScriptedProvider([300], refresh_delta=50)
        t = self.tracker(target_price=350)
        engine = Engine(self.store, provider, self.notifier, live=True, clock=self.clock)
        engine.check(t)
        self.assertIs(self.store.get_tracker(t.id).status, Status.TRACKING)
        self.assertEqual(provider.booked, [])
        provider.refresh_delta = 0
        self.clock.now += timedelta(hours=3)
        engine.run_due()
        self.assertIs(self.store.get_tracker(t.id).status, Status.PURCHASED)

    def test_refreshed_price_above_cap_is_not_bought(self):
        provider = ScriptedProvider([595], refresh_delta=10)
        t = self.run_window(provider)
        self.assertEqual(provider.booked, [])
        self.assertIs(t.status, Status.EXPIRED)

    def test_booking_error_marks_failed_and_stops(self):
        provider = ScriptedProvider([300], book_error=ProviderError("card declined"))
        t = self.run_window(provider, target_price=350)
        self.assertIs(t.status, Status.FAILED)
        self.assertEqual(provider.searches, 1)
        self.assertTrue(any("FAILED" in title for title, _ in self.notifier.sent))

    def test_claim_prevents_double_purchase(self):
        t = self.tracker()
        self.assertTrue(self.store.claim_for_purchase(t.id))
        self.assertFalse(self.store.claim_for_purchase(t.id))

    def test_bad_passenger_file_falls_back_to_alert(self):
        provider = ScriptedProvider([300])
        t = self.run_window(provider, target_price=350, passengers_file="/nonexistent.json")
        self.assertIs(t.status, Status.RECOMMENDED)
        self.assertEqual(provider.booked, [])

    def test_search_errors_are_retried(self):
        class Flaky(ScriptedProvider):
            def search(self, t):
                if self.searches == 0:
                    self.searches += 1
                    raise ProviderError("timeout")
                return super().search(t)

        provider = Flaky([300])
        t = self.run_window(provider, target_price=350)
        self.assertIs(t.status, Status.PURCHASED)


if __name__ == "__main__":
    unittest.main()
