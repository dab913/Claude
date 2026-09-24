"""Core data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Status(str, Enum):
    TRACKING = "tracking"        # actively watching prices
    PURCHASING = "purchasing"    # booking in flight; guards against double purchase
    PURCHASED = "purchased"      # ticket bought
    RECOMMENDED = "recommended"  # strategy said buy, but auto-buy/live mode was off
    EXPIRED = "expired"          # window ended without an acceptable price
    FAILED = "failed"            # booking attempt errored; needs a human to check
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self is Status.TRACKING


@dataclass
class Tracker:
    origin: str
    destination: str
    depart_date: str                 # YYYY-MM-DD
    max_price: float                 # hard cap: never buy above this
    deadline: datetime               # must have bought by this time
    created_at: datetime
    return_date: str | None = None
    adults: int = 1
    cabin: str = "economy"
    currency: str = "USD"
    target_price: float | None = None  # buy immediately at or below this
    check_every_hours: float = 3.0
    auto_buy: bool = False
    passengers_file: str | None = None
    max_connections: int | None = None
    status: Status = Status.TRACKING
    last_checked_at: datetime | None = None
    id: int | None = None

    @property
    def route(self) -> str:
        trip = f"{self.origin}->{self.destination} {self.depart_date}"
        if self.return_date:
            trip += f" / return {self.return_date}"
        return trip


@dataclass
class Offer:
    offer_id: str
    price: float
    currency: str
    carrier: str
    summary: str = ""
    expires_at: datetime | None = None
    passenger_ids: list[str] = field(default_factory=list)


@dataclass
class Quote:
    tracker_id: int
    observed_at: datetime
    price: float
    currency: str
    offer_id: str
    carrier: str


@dataclass
class Booking:
    order_id: str
    booking_reference: str
    price: float
    currency: str
