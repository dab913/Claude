"""Command-line interface: ``flight-tracker <command>``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from .db import Store
from .engine import Engine, load_passengers
from .models import Status, Tracker, utcnow
from .notify import Notifier
from .providers import DuffelProvider, MockProvider, ProviderError


def _provider(name: str):
    if name == "mock":
        return MockProvider()
    return DuffelProvider(os.environ.get("DUFFEL_ACCESS_TOKEN", ""))


def _engine(args, store: Store) -> Engine:
    provider = _provider(args.provider)
    if args.live and isinstance(provider, DuffelProvider) and not provider.is_test_mode:
        logging.getLogger("flight_tracker").warning("LIVE MODE with a live Duffel token: real tickets will be purchased.")
    return Engine(store, provider, Notifier(args.webhook), live=args.live)


def cmd_add(args, store: Store) -> int:
    now = utcnow()
    depart = date.fromisoformat(args.depart)
    if args.return_date and date.fromisoformat(args.return_date) < depart:
        sys.exit("error: return date is before departure")
    if args.target_price is not None and args.target_price > args.max_price:
        sys.exit("error: --target-price cannot exceed --max-price")
    # Always leave at least a day between the purchase deadline and departure.
    latest = datetime.combine(depart, dtime(0), tzinfo=timezone.utc) - timedelta(days=1)
    deadline = min(now + timedelta(days=args.window_days), latest)
    if deadline <= now:
        sys.exit("error: departure is too soon to track (need at least 1 day before departure)")
    if args.auto_buy:
        if not args.passengers:
            sys.exit("error: --auto-buy requires --passengers FILE with traveller details")
        try:
            load_passengers(args.passengers, args.adults)
        except (OSError, ValueError) as e:
            sys.exit(f"error: {e}")

    t = store.add_tracker(Tracker(
        origin=args.origin.upper(), destination=args.destination.upper(), depart_date=args.depart,
        return_date=args.return_date, adults=args.adults, cabin=args.cabin, currency=args.currency.upper(),
        max_price=args.max_price, target_price=args.target_price, check_every_hours=args.every_hours,
        auto_buy=args.auto_buy, passengers_file=os.path.abspath(args.passengers) if args.passengers else None,
        max_connections=args.max_connections, created_at=now, deadline=deadline,
    ))
    print(f"Tracker #{t.id}: {t.route}, checking every {t.check_every_hours:g}h until "
          f"{t.deadline:%Y-%m-%d %H:%M} UTC, cap {t.max_price:.2f} {t.currency}, "
          f"auto-buy {'ON' if t.auto_buy else 'off (alerts only)'}")
    return 0


def cmd_list(args, store: Store) -> int:
    trackers = store.list_trackers()
    if not trackers:
        print("No trackers. Add one with: flight-tracker add --help")
    for t in trackers:
        quotes = store.quotes(t.id)
        latest = f"{quotes[-1].price:.2f}" if quotes else "-"
        low = f"{min(q.price for q in quotes):.2f}" if quotes else "-"
        print(f"#{t.id:<3} {t.status.value:<11} {t.route:<40} now {latest:>9}  low {low:>9}  "
              f"cap {t.max_price:.2f} {t.currency}  deadline {t.deadline:%m-%d %H:%M}")
    return 0


def cmd_show(args, store: Store) -> int:
    t = store.get_tracker(args.id)
    if not t:
        sys.exit(f"error: no tracker #{args.id}")
    print(f"#{t.id} {t.route} [{t.status.value}] cap {t.max_price:.2f} {t.currency}, deadline {t.deadline:%Y-%m-%d %H:%M} UTC")
    booking = store.purchase(t.id)
    if booking:
        print(f"Booked: ref {booking.booking_reference} (order {booking.order_id}) {booking.price:.2f} {booking.currency}")
    print("\nEvents:")
    for at, msg in store.events(t.id):
        print(f"  {at:%m-%d %H:%M}  {msg}")
    return 0


def cmd_cancel(args, store: Store) -> int:
    t = store.get_tracker(args.id)
    if not t:
        sys.exit(f"error: no tracker #{args.id}")
    if t.status is not Status.TRACKING:
        sys.exit(f"error: tracker #{t.id} is {t.status.value}, not tracking")
    store.set_status(t.id, Status.CANCELLED)
    print(f"Cancelled tracker #{t.id}")
    return 0


def cmd_check(args, store: Store) -> int:
    n = _engine(args, store).run_due()
    print(f"Checked {n} tracker(s).")
    return 0


def cmd_run(args, store: Store) -> int:
    engine = _engine(args, store)
    print(f"Running ({'LIVE' if args.live else 'dry-run'}); polling every {args.poll_seconds}s. Ctrl-C to stop.")
    try:
        while True:
            try:
                engine.run_due()
            except Exception:  # keep the daemon alive; the next poll retries
                logging.getLogger("flight_tracker").exception("check cycle failed")
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0


def cmd_simulate(args, store: Store) -> int:
    from .simulate import summarize
    print(summarize(runs=args.runs, base_price=args.base_price, window_days=args.window_days, every_hours=args.every_hours))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flight-tracker", description=__doc__)
    p.add_argument("--db", default=os.environ.get("FLIGHT_TRACKER_DB", "flight_tracker.db"))
    p.add_argument("--provider", choices=["duffel", "mock"], default=os.environ.get("FLIGHT_TRACKER_PROVIDER", "duffel"))
    p.add_argument("--webhook", default=os.environ.get("FLIGHT_TRACKER_WEBHOOK"), help="URL to POST notifications to")
    p.add_argument("--live", action="store_true",
                   help="actually purchase tickets for auto-buy trackers (default is dry run: alert only)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="start tracking a trip")
    a.add_argument("--from", dest="origin", required=True, help="origin IATA code, e.g. JFK")
    a.add_argument("--to", dest="destination", required=True, help="destination IATA code, e.g. LHR")
    a.add_argument("--depart", required=True, help="YYYY-MM-DD")
    a.add_argument("--return", dest="return_date", help="YYYY-MM-DD for a round trip")
    a.add_argument("--adults", type=int, default=1)
    a.add_argument("--cabin", default="economy", choices=["economy", "premium_economy", "business", "first"])
    a.add_argument("--currency", default="USD")
    a.add_argument("--max-price", type=float, required=True, help="hard cap; never buy above this total")
    a.add_argument("--target-price", type=float, help="buy immediately if the total drops to this")
    a.add_argument("--window-days", type=float, default=5, help="days to track before buying (default 5)")
    a.add_argument("--every-hours", type=float, default=3, help="hours between price checks (default 3)")
    a.add_argument("--max-connections", type=int)
    a.add_argument("--auto-buy", action="store_true", help="purchase automatically (also needs --live when running)")
    a.add_argument("--passengers", help="JSON file with traveller details (required for --auto-buy)")
    a.set_defaults(func=cmd_add)

    sub.add_parser("list", help="list trackers").set_defaults(func=cmd_list)
    s = sub.add_parser("show", help="price history and events for a tracker")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_show)
    c = sub.add_parser("cancel", help="stop tracking")
    c.add_argument("id", type=int)
    c.set_defaults(func=cmd_cancel)
    sub.add_parser("check", help="check all due trackers once (for cron)").set_defaults(func=cmd_check)
    r = sub.add_parser("run", help="run continuously, checking trackers when due")
    r.add_argument("--poll-seconds", type=int, default=300)
    r.set_defaults(func=cmd_run)
    sim = sub.add_parser("simulate", help="backtest the buying strategy on simulated fares")
    sim.add_argument("--runs", type=int, default=200)
    sim.add_argument("--base-price", type=float, default=400)
    sim.add_argument("--window-days", type=float, default=5)
    sim.add_argument("--every-hours", type=float, default=3)
    sim.set_defaults(func=cmd_simulate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    store = Store(":memory:" if args.command == "simulate" else args.db)
    try:
        return args.func(args, store)
    except ProviderError as e:
        sys.exit(f"error: {e}")
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
