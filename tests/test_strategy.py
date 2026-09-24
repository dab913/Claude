import unittest
from datetime import datetime, timedelta, timezone

from flight_tracker.strategy import Action, decide

START = datetime(2030, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(days=5)


def at(hours, price, previous, max_price=1000.0, target=None, every=3):
    now = START + timedelta(hours=hours)
    return decide(price=price, previous_prices=previous, created_at=START, deadline=END, now=now,
                  next_check_at=now + timedelta(hours=every), max_price=max_price, target_price=target)


class StrategyTest(unittest.TestCase):
    def test_waits_during_explore_phase_even_at_a_low(self):
        self.assertIs(at(6, 100, [500, 400]).action, Action.WAIT)

    def test_target_price_buys_immediately(self):
        d = at(0, 250, [], target=260)
        self.assertIs(d.action, Action.BUY)
        self.assertIn("target", d.reason)

    def test_new_low_after_explore_phase_buys(self):
        self.assertIs(at(60, 380, [400, 420, 390, 410]).action, Action.BUY)

    def test_expensive_fare_after_explore_phase_waits(self):
        self.assertIs(at(60, 450, [400, 420, 390, 410]).action, Action.WAIT)

    def test_threshold_relaxes_toward_deadline(self):
        history = [400, 420, 390, 410, 395, 430, 405, 415, 399, 425]
        self.assertIs(at(35, 396, history).action, Action.WAIT)
        self.assertIs(at(110, 396, history).action, Action.BUY)

    def test_last_check_buys_whatever_is_available(self):
        d = at(118, 600, [400, 420, 390])
        self.assertIs(d.action, Action.BUY)
        self.assertIn("last check", d.reason)

    def test_never_buys_above_cap(self):
        self.assertIs(at(60, 510, [600, 700], max_price=500).action, Action.WAIT)
        self.assertIs(at(118, 510, [600, 700], max_price=500).action, Action.GIVE_UP)
        self.assertIs(at(0, 510, [], max_price=500, target=600).action, Action.WAIT)


if __name__ == "__main__":
    unittest.main()
