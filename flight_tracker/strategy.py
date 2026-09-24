"""When to buy.

Nobody can know in advance which moment of a 5-day window will have the lowest
fare, so this is an optimal-stopping problem. The rule used here is a practical
variant of the classic "secretary problem" solution:

1. **Target hit** - if the fare drops to the user's target price, buy at once.
2. **Explore** - for the first 25% of the window, only observe prices to learn
   what "cheap" looks like for this route.
3. **Exploit** - afterwards, buy as soon as the fare is among the cheapest seen
   so far. At first only a new low qualifies; the bar relaxes steadily to the
   30th percentile of observed fares by the deadline, because waiting gets
   riskier as the window runs out (fares tend to climb toward departure).
4. **Deadline** - on the last check before the window closes, buy the current
   best fare so the trip is never left unbought.

A hard ``max_price`` cap overrides everything: the system never buys above it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

# Tuned by backtesting (see ``flight-tracker simulate``). The textbook secretary
# answer is 1/e ~ 0.37, but that maximises the odds of the single best fare;
# a shorter look-only phase gives a lower *average* price paid.
EXPLORE_FRACTION = 0.25
FINAL_QUANTILE = 0.30     # by the deadline, accept any fare in the cheapest 30% seen


class Action(str, Enum):
    BUY = "buy"
    WAIT = "wait"
    GIVE_UP = "give_up"


@dataclass
class Decision:
    action: Action
    reason: str
    threshold: float | None = None


def decide(
    *,
    price: float,
    previous_prices: list[float],
    created_at: datetime,
    deadline: datetime,
    now: datetime,
    next_check_at: datetime,
    max_price: float,
    target_price: float | None = None,
    explore_fraction: float = EXPLORE_FRACTION,
    final_quantile: float = FINAL_QUANTILE,
) -> Decision:
    """Decide whether to buy at ``price`` given the prices observed before it."""
    window = (deadline - created_at).total_seconds()
    progress = 1.0 if window <= 0 else min(max((now - created_at).total_seconds() / window, 0.0), 1.0)
    last_chance = next_check_at >= deadline

    if price > max_price:
        if last_chance:
            return Decision(Action.GIVE_UP, f"window closing and fare {price:.2f} is above cap {max_price:.2f}")
        return Decision(Action.WAIT, f"fare {price:.2f} above cap {max_price:.2f}")

    if target_price is not None and price <= target_price:
        return Decision(Action.BUY, f"fare {price:.2f} hit target {target_price:.2f}", target_price)

    if last_chance:
        return Decision(Action.BUY, f"last check before deadline; buying best available fare {price:.2f}")

    if progress < explore_fraction or not previous_prices:
        return Decision(Action.WAIT, f"observing prices ({progress:.0%} of window elapsed)")

    best_seen = min(previous_prices)
    exploit_progress = (progress - explore_fraction) / (1.0 - explore_fraction)
    threshold = _quantile(previous_prices, final_quantile * exploit_progress)
    if price <= threshold:
        return Decision(Action.BUY, f"fare {price:.2f} at or below threshold {threshold:.2f} (best seen {best_seen:.2f})", threshold)
    return Decision(Action.WAIT, f"fare {price:.2f} above threshold {threshold:.2f} (best seen {best_seen:.2f})", threshold)


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(q * len(ordered)), len(ordered) - 1)]
