"""Backtest the buy strategy against simulated fares, without spending anything."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Store
from .engine import Engine
from .models import Quote, Status, Tracker
from .notify import Notifier
from .providers.mock import MockProvider


@dataclass
class SimResult:
    paid: float | None      # None if the tracker expired without buying
    first_price: float      # what buying immediately would have cost
    mean_price: float       # expected cost of buying at a random moment
    best_price: float       # lowest fare observed across the whole window (hindsight)


class _Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class _Silent(Notifier):
    def send(self, title: str, body: str) -> None:
        pass


def run_once(seed: int, base_price: float = 400.0, window_days: float = 5, every_hours: float = 3,
             max_price: float | None = None) -> SimResult:
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    clock = _Clock(start)
    store = Store(":memory:")
    provider = MockProvider(base_price=base_price, seed=seed, clock=clock)
    engine = Engine(store, provider, _Silent(), live=False, clock=clock)
    t = store.add_tracker(Tracker(
        origin="AAA", destination="BBB", depart_date="2030-03-01", created_at=start,
        deadline=start + timedelta(days=window_days), check_every_hours=every_hours,
        max_price=max_price if max_price is not None else base_price * 10,
    ))

    # Keep observing the full window after the buy signal so we can score it in hindsight.
    paid = None
    while clock.now < t.deadline:
        if paid is None:
            engine.run_due()
            if store.get_tracker(t.id).status is Status.RECOMMENDED:
                paid = store.quotes(t.id)[-1].price
        else:
            price = min(o.price for o in provider.search(t))
            store.add_quote(Quote(t.id, clock.now, price, t.currency, "sim", "sim"))
        clock.now += timedelta(hours=every_hours)

    prices = [q.price for q in store.quotes(t.id)]
    store.close()
    return SimResult(paid, prices[0], statistics.mean(prices), min(prices))


def summarize(runs: int = 200, **kwargs) -> str:
    results = [run_once(seed, **kwargs) for seed in range(runs)]
    bought = [r for r in results if r.paid is not None]
    if not bought:
        return f"{runs} simulated windows: nothing bought (every fare was above the cap)."
    vs_best = [(r.paid / r.best_price - 1) * 100 for r in bought]
    vs_first = [(1 - r.paid / r.first_price) * 100 for r in bought]
    vs_mean = [(1 - r.paid / r.mean_price) * 100 for r in bought]
    return "\n".join([
        f"Simulated {runs} five-day windows ({len(bought)} bought, {runs - len(bought)} expired over cap)",
        f"  paid vs. hindsight-best fare : +{statistics.mean(vs_best):.1f}% on average, "
        f"median +{statistics.median(vs_best):.1f}%",
        f"  saved vs. buying immediately : {statistics.mean(vs_first):.1f}% on average",
        f"  saved vs. buying at random   : {statistics.mean(vs_mean):.1f}% on average",
        f"  got the exact best fare      : {sum(v < 0.01 for v in vs_best) / len(bought):.0%} of windows",
    ])
