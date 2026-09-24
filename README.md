# Flight Tracker with auto-purchase

This tool watches a flight's price over a set window (5 days by default) and buys the ticket at the best time it can find. It never goes over a price cap you set, and it always buys before the window closes.

```
add tracker ──► check price every 3h ──► strategy: wait / buy / give up ──► re-confirm price ──► book ──► notify
```

## How it decides when to buy

Nobody can know ahead of time which moment in the next 5 days will have the lowest fare. That makes this an *optimal stopping* problem. The rules are in [`flight_tracker/strategy.py`](flight_tracker/strategy.py):

1. **Price cap.** It never buys above `--max-price`. If the fare is still over the cap when the window ends, the tracker expires and nothing is bought.
2. **Target price.** If the fare drops to `--target-price`, it buys immediately.
3. **Look-only phase.** During the first 25% of the window it only records prices, to learn what "cheap" means on this route.
4. **Buying phase.** After that it buys when the fare is among the cheapest seen so far. At first only a new low qualifies. The bar then relaxes steadily until, near the deadline, any fare in the cheapest 30% seen qualifies. Waiting gets riskier as the window runs out.
5. **Deadline.** On the last check before the window closes, it buys the best fare available.

You can backtest the strategy on simulated fares without spending anything:

```
$ flight-tracker simulate --runs 300
Simulated 300 five-day windows (300 bought, 0 expired over cap)
  paid vs. hindsight-best fare : +8.9% on average, median +7.1%
  saved vs. buying immediately : 5.4% on average
  saved vs. buying at random   : 8.1% on average
  got the exact best fare      : 15% of windows
```

These numbers come from a fare model, not real airline data. Real savings depend on how volatile the route is.

## Safety: how it avoids buying the wrong ticket

Buying a ticket spends real money, so a purchase only happens when **all** of these are true:

- The tracker was created with `--auto-buy` and a `--passengers` file.
- The process runs with `--live`. Without it, the tracker only **recommends** (a "Buy now" alert) and books nothing.
- The fare is re-checked with the airline right before booking. The purchase is aborted if the fare rose more than 2% or went over the cap.
- The tracker is claimed atomically in the database first, so two processes can never buy the same trip.
- If a booking call errors, the tracker is marked `failed` and never retried automatically. Check your Duffel dashboard to see whether the order went through.
- The purchase deadline is always at least one day before departure.

## Setup

Requires Python 3.10+ and has no third-party dependencies.

```bash
pip install -e .
```

Live search and booking use [Duffel](https://duffel.com), a flight booking API that sells tickets from 300+ airlines:

1. Create a Duffel account and copy an access token.
   - A **test token** (`duffel_test_...`) searches and books against Duffel's sandbox airline. No money moves, so start with this one.
   - A **live token** buys real tickets, paid from your Duffel balance. You need to top up the balance first.
2. `export DUFFEL_ACCESS_TOKEN=duffel_test_...`

To try it without any account, add `--provider mock` to any command.

### Passenger file (required for auto-buy)

Save the travellers as JSON, one object per adult, in the same order as `--adults`. Keep this file private: it's listed in `.gitignore`.

```json
[
  {"title": "mr", "gender": "m", "given_name": "Jane", "family_name": "Doe",
   "born_on": "1985-04-12", "email": "you@example.com", "phone_number": "+15555550100"}
]
```

Names must match the passport exactly.

## Usage

```bash
# Track JFK→LHR for 5 days. Never pay more than $650; buy instantly at $420 or less.
flight-tracker add --from JFK --to LHR --depart 2026-11-15 --return 2026-11-22 \
    --max-price 650 --target-price 420 --auto-buy --passengers passengers.json

# Alert-only tracker (never buys, just tells you when to buy)
flight-tracker add --from SFO --to NRT --depart 2026-12-01 --max-price 900

flight-tracker list          # status, latest and lowest price for each tracker
flight-tracker show 1        # full price and decision history
flight-tracker cancel 2
```

Keep it running with one of these:

```bash
flight-tracker --live run                    # long-running process; checks each tracker when it's due

# ...or from cron, every 15 minutes (each tracker is still only checked every --every-hours):
*/15 * * * *  cd /path/to/app && DUFFEL_ACCESS_TOKEN=... flight-tracker --live check
```

Drop `--live` to dry-run: you get the same decisions and alerts, and nothing is bought.

### Notifications

Every alert (buy signal, purchase, expiry, failure) prints to the log. To also get alerts on your phone or in chat, set `--webhook URL` or `FLIGHT_TRACKER_WEBHOOK`. The tool sends a JSON POST with `text`, `title` and `body` fields, which Slack incoming webhooks and ntfy.sh/Zapier/IFTTT all accept.

### Options for `add`

| option | default | meaning |
|---|---|---|
| `--window-days` | 5 | how long to track before it must buy |
| `--every-hours` | 3 | time between price checks (5 days ÷ 3h = 40 checks) |
| `--max-price` | required | hard cap on the total price for all passengers |
| `--target-price` | none | buy immediately at or below this price |
| `--adults` | 1 | number of passengers |
| `--cabin` | economy | `economy`, `premium_economy`, `business`, `first` |
| `--max-connections` | any | e.g. `0` for nonstop only |
| `--currency` | USD | must match the currency your Duffel account is billed in |

Global options: `--db PATH` (default `flight_tracker.db`), `--provider duffel|mock`, `--live`, `--webhook URL`, `-v`.

## Code layout

```
flight_tracker/
  strategy.py        when to buy (pure function, easy to test and tune)
  engine.py          check → decide → re-confirm → book, with the safety checks
  db.py              SQLite: trackers, price history, purchases, event log
  providers/duffel.py  real search, re-pricing and ticketing via the Duffel API
  providers/mock.py    simulated fares for demos, tests and backtests
  simulate.py        strategy backtest
  cli.py             command-line interface
tests/               python -m unittest discover -s tests
```

To add another booking source, implement `search`, `refresh_offer` and `book` from `providers/base.py`.
