"""SQLite persistence for trackers, observed prices, purchases and events."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Booking, Quote, Status, Tracker

SCHEMA = """
CREATE TABLE IF NOT EXISTS trackers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    adults INTEGER NOT NULL,
    cabin TEXT NOT NULL,
    currency TEXT NOT NULL,
    max_price REAL NOT NULL,
    target_price REAL,
    check_every_hours REAL NOT NULL,
    auto_buy INTEGER NOT NULL,
    passengers_file TEXT,
    max_connections INTEGER,
    created_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL,
    last_checked_at TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id INTEGER NOT NULL REFERENCES trackers(id),
    observed_at TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    offer_id TEXT NOT NULL,
    carrier TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id INTEGER NOT NULL UNIQUE REFERENCES trackers(id),
    order_id TEXT NOT NULL,
    booking_reference TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    purchased_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id INTEGER NOT NULL REFERENCES trackers(id),
    at TEXT NOT NULL,
    message TEXT NOT NULL
);
"""

_TRACKER_COLUMNS = (
    "origin", "destination", "depart_date", "return_date", "adults", "cabin", "currency",
    "max_price", "target_price", "check_every_hours", "auto_buy", "passengers_file",
    "max_connections", "created_at", "deadline", "status", "last_checked_at",
)


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: str | Path = "flight_tracker.db"):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- trackers -------------------------------------------------------
    def add_tracker(self, t: Tracker) -> Tracker:
        values = self._tracker_values(t)
        cur = self.conn.execute(
            f"INSERT INTO trackers ({', '.join(_TRACKER_COLUMNS)}) VALUES ({', '.join('?' * len(values))})",
            values,
        )
        self.conn.commit()
        t.id = cur.lastrowid
        return t

    def get_tracker(self, tracker_id: int) -> Tracker | None:
        row = self.conn.execute("SELECT * FROM trackers WHERE id = ?", (tracker_id,)).fetchone()
        return self._row_to_tracker(row) if row else None

    def list_trackers(self, status: Status | None = None) -> list[Tracker]:
        if status is None:
            rows = self.conn.execute("SELECT * FROM trackers ORDER BY id").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM trackers WHERE status = ? ORDER BY id", (status.value,)).fetchall()
        return [self._row_to_tracker(r) for r in rows]

    def set_status(self, tracker_id: int, status: Status) -> None:
        self.conn.execute("UPDATE trackers SET status = ? WHERE id = ?", (status.value, tracker_id))
        self.conn.commit()

    def claim_for_purchase(self, tracker_id: int) -> bool:
        """Atomically move TRACKING -> PURCHASING. Returns False if another process got there first."""
        cur = self.conn.execute(
            "UPDATE trackers SET status = ? WHERE id = ? AND status = ?",
            (Status.PURCHASING.value, tracker_id, Status.TRACKING.value),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def mark_checked(self, tracker_id: int, at: datetime) -> None:
        self.conn.execute("UPDATE trackers SET last_checked_at = ? WHERE id = ?", (_ts(at), tracker_id))
        self.conn.commit()

    # --- quotes / purchases / events -----------------------------------
    def add_quote(self, q: Quote) -> None:
        self.conn.execute(
            "INSERT INTO quotes (tracker_id, observed_at, price, currency, offer_id, carrier) VALUES (?, ?, ?, ?, ?, ?)",
            (q.tracker_id, _ts(q.observed_at), q.price, q.currency, q.offer_id, q.carrier),
        )
        self.conn.commit()

    def quotes(self, tracker_id: int) -> list[Quote]:
        rows = self.conn.execute("SELECT * FROM quotes WHERE tracker_id = ? ORDER BY observed_at, id", (tracker_id,)).fetchall()
        return [
            Quote(r["tracker_id"], _dt(r["observed_at"]), r["price"], r["currency"], r["offer_id"], r["carrier"])
            for r in rows
        ]

    def add_purchase(self, tracker_id: int, booking: Booking, at: datetime) -> None:
        self.conn.execute(
            "INSERT INTO purchases (tracker_id, order_id, booking_reference, price, currency, purchased_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tracker_id, booking.order_id, booking.booking_reference, booking.price, booking.currency, _ts(at)),
        )
        self.conn.commit()

    def purchase(self, tracker_id: int) -> Booking | None:
        r = self.conn.execute("SELECT * FROM purchases WHERE tracker_id = ?", (tracker_id,)).fetchone()
        return Booking(r["order_id"], r["booking_reference"], r["price"], r["currency"]) if r else None

    def log(self, tracker_id: int, at: datetime, message: str) -> None:
        self.conn.execute("INSERT INTO events (tracker_id, at, message) VALUES (?, ?, ?)", (tracker_id, _ts(at), message))
        self.conn.commit()

    def events(self, tracker_id: int) -> list[tuple[datetime, str]]:
        rows = self.conn.execute("SELECT at, message FROM events WHERE tracker_id = ? ORDER BY id", (tracker_id,)).fetchall()
        return [(_dt(r["at"]), r["message"]) for r in rows]

    # --- helpers --------------------------------------------------------
    @staticmethod
    def _tracker_values(t: Tracker) -> tuple:
        return (
            t.origin, t.destination, t.depart_date, t.return_date, t.adults, t.cabin, t.currency,
            t.max_price, t.target_price, t.check_every_hours, int(t.auto_buy), t.passengers_file,
            t.max_connections, _ts(t.created_at), _ts(t.deadline), t.status.value, _ts(t.last_checked_at),
        )

    @staticmethod
    def _row_to_tracker(r: sqlite3.Row) -> Tracker:
        return Tracker(
            id=r["id"], origin=r["origin"], destination=r["destination"], depart_date=r["depart_date"],
            return_date=r["return_date"], adults=r["adults"], cabin=r["cabin"], currency=r["currency"],
            max_price=r["max_price"], target_price=r["target_price"], check_every_hours=r["check_every_hours"],
            auto_buy=bool(r["auto_buy"]), passengers_file=r["passengers_file"], max_connections=r["max_connections"],
            created_at=_dt(r["created_at"]), deadline=_dt(r["deadline"]), status=Status(r["status"]),
            last_checked_at=_dt(r["last_checked_at"]),
        )
