# Flight price tracker

Watches a hand-written list of **specific flights** — not just routes, but
origin, destination and date windows — records what each one costs over time,
and emails you when a price looks good *against that flight's own history*.

```
$ flighttracker run
NYC to Tokyo, December: 30 search(es)
  2026-12-10 to 2026-12-24 (14n): USD 1,043
  2026-12-11 to 2026-12-25 (14n): USD 986
  ...

BUY SIGNALS (1)
========================================
NYC to Tokyo, December  (JFK <-> HND, economy)
  USD 812   2026-12-13 -> 2026-12-28
  * USD 812 is the lowest seen in 34 runs — under the previous best of USD 869
  history: 34 runs, low USD 869, median USD 1,062, high USD 1,410
  https://www.google.com/travel/flights?q=Flights+from+JFK+to+HND+on+2026-12-13...

Digest sent to you@example.com.
```

## Why it is built this way

Amadeus shut down its free self-service tier in July 2026, and no other free
API gives structured flight prices at hobby scale. So this scrapes Google
Flights through [`fast-flights`](https://pypi.org/project/fast-flights/).
That is the whole trade: free, and fragile. See
[Constraints](#constraints-worth-knowing-before-you-rely-on-this).

The "is this a good price" part is **statistics, not prediction**. There is no
model to train and nothing to be wrong about: a price is interesting when it is
low compared to what *this* flight has cost on previous checks.

## Getting started

```bash
git clone <your repo> && cd flight-price-tracker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp watches.example.yaml watches.yaml   # then edit it
cp .env.example .env                   # then fill in SMTP details

flighttracker validate      # check the watchlist, see what each run will do
flighttracker run --dry-run # fetch real prices, print the digest, store nothing
flighttracker run           # the real thing
```

Without installing, every command also works as `python -m flighttracker ...`.

## The watchlist

`watches.yaml` is the only file you edit. `watches.example.yaml` is a commented
starting point; this is the shape:

```yaml
settings:
  db_path: data/prices.db
  currency: USD
  min_observations: 20     # runs of history before statistics can fire
  percentile: 20           # "cheap" means the bottom 20% of this watch's history
  max_combinations: 60     # refuse a watch that would explode into more searches

watches:
  - id: nyc-to-tokyo-dec
    label: NYC to Tokyo, December
    origin: JFK
    destination: HND
    depart_date_range: [2026-12-10, 2026-12-20]
    return_date_range: [2026-12-24, 2027-01-03]
    trip_length_nights: [14, 16]
    cabin: economy
    max_price_alert: 900
```

| Key | |
| --- | --- |
| `id` | required, unique, used as the database key — renaming it starts the history over |
| `origin`, `destination` | IATA airport codes |
| `depart_date_range` | one date, or `[first, last]` — every date in between is searched |
| `return_date_range` | same; **leave it out for a one-way** |
| `trip_length_nights` | `14` or `[14, 16]` — keeps two wide ranges from multiplying out |
| `cabin` | `economy`, `premium-economy`, `business`, `first` |
| `max_price_alert` | optional hard ceiling; alerts on its own, with no history needed |
| `passengers` | a number of adults, or `{adults, children, infants_in_seat, infants_on_lap}` |
| `max_stops` | `0` for nonstop only |
| `min_observations`, `percentile` | per-watch overrides of the global settings |

**Date ranges multiply.** An 11-day departure window against an 11-day return
window is 121 separate searches, on every run. `trip_length_nights` cuts that
to the trip lengths you would actually book (30, above), and
`max_combinations` refuses anything larger rather than quietly hammering Google
for twenty minutes. `flighttracker validate` prints the count and an estimate
of how long a run will take before you commit to it.

## What counts as a buy signal

Each run stores one row per date combination. A watch's price *for that run* is
the cheapest of those rows, and that is what gets judged — against the same
figure from every previous run.

A watch is flagged when any of these hold:

- **Under your ceiling** — the price is at or below `max_price_alert`. This is
  your own judgement, so it fires from the very first run.
- **All-time low** — cheaper than every previous run for this watch.
- **Below the percentile** — in the cheapest `percentile`% of its own history.

The last two stay silent until the watch has `min_observations` runs behind it.
Early on, "the cheapest price ever seen" is just "the only price ever seen";
the default of 20 runs is about ten days at twice a day. Until then the digest
says so:

```
ALSO TRACKED
NYC to Tokyo, December (JFK <-> HND): USD 1,043
  [building history — 6 of 20 runs needed before comparisons mean anything]
```

**Repeat alerts are held back.** A price sitting at its all-time low satisfies
the rule on every single run, which would mean the same email twice a day
forever. After an alert, that watch stays quiet for `alert_cooldown_hours`
unless the price drops a further `alert_improvement` (3% by default). An alert
is only recorded once the email is actually sent, so a failed send is retried
on the next run rather than being silently swallowed.

## Email

One digest per run, listing every flagged watch — never one email per flight.
By default nothing is sent when nothing is flagged; `--always-email` overrides
that. The digest also carries the watches that were *not* flagged and any
lookups that failed, so silence is never ambiguous.

Any SMTP server works. For Gmail you need an **app password** (2-step
verification, then `myaccount.google.com/apppasswords`) — a normal account
password will be rejected. SendGrid and Mailgun work the same way with their
own SMTP credentials. Settings go in `.env` (see `.env.example`); real
environment variables always take priority, so the same checkout works locally
and in CI.

`flighttracker test-email` sends one digest immediately to confirm the setup.

## Running it on a schedule

Every 6–12 hours is plenty. Prices move on the order of days.

**Cron, on a machine you control** — the more reliable option, see below:

```cron
0 7,19 * * * cd /path/to/flight-price-tracker && .venv/bin/flighttracker run --quiet >> data/run.log 2>&1
```

**GitHub Actions** — `.github/workflows/track.yml` is ready to go. Add
`EMAIL_TO`, `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`,
`SMTP_USERNAME` and `SMTP_PASSWORD` as repository secrets. The workflow commits
`data/prices.db` back to the repo after each run — that is how the history
survives, since the runner is thrown away, and it doubles as the repository
activity that stops GitHub disabling the schedule after 60 days.

Both paths need `watches.yaml` committed, so **keep the repo private** unless
you are happy publishing your travel plans. Nothing secret should ever go in
that file; credentials belong in `.env` (git-ignored) or in repository secrets.

## Constraints worth knowing before you rely on this

- **Scraping breaks.** Google Flights has no SLA and no contract with you. A
  layout change can stop this working with no warning. Everything that touches
  `fast_flights` is confined to `flighttracker/fetch.py`, so a fix is a small
  one, but it will occasionally need one.
- **Data-centre IPs get blocked far more than home ones.** This is the main
  reason to prefer cron on your own machine or a small VPS over GitHub Actions.
  If runs start failing in CI but work locally, that is what has happened.
  `flighttracker run --proxy http://...` routes the scraper elsewhere.
- **A failed lookup is never a price.** It is recorded in `fetch_errors`,
  reported in the digest, and left out of the statistics entirely. A run where
  everything failed exits non-zero and stores no prices, so an outage cannot
  quietly poison a watch's history with junk.
- **Prices are a snapshot, not a quote.** Airlines price dynamically and by
  session. What you see at booking can differ.
- **History belongs to the watch id.** Change a watch's dates, cabin or
  passengers and you are comparing against prices for a different trip. Give it
  a new `id` when the trip itself changes.

## Looking at the data

```bash
flighttracker history                    # every watch, run by run
flighttracker history nyc-to-tokyo-dec   # just one
flighttracker signals                    # re-judge the latest stored run, no fetching
```

`signals` is the one to use when tuning `percentile` or `min_observations` —
it re-runs the whole decision against what is already stored, without touching
the network.

The database is plain SQLite, so `sqlite3 data/prices.db` works too:

```sql
-- what one watch has cost, run by run
SELECT timestamp, MIN(price) FROM price_history
WHERE watch_id = 'nyc-to-tokyo-dec' GROUP BY timestamp ORDER BY timestamp;

-- which departure date has been cheapest on average
SELECT depart_date, ROUND(AVG(price)) FROM price_history
WHERE watch_id = 'nyc-to-tokyo-dec' GROUP BY depart_date ORDER BY 2;
```

| Table | |
| --- | --- |
| `price_history` | one row per watch, per run, per date combination |
| `fetch_errors` | lookups that failed, so gaps in the history are explainable |
| `alerts` | what was emailed and when — this is what the cooldown reads |

## Layout

```
flighttracker/
  config.py    watchlist parsing and validation
  dates.py     date ranges -> the searches to actually run
  fetch.py     the only module that knows about Google Flights
  store.py     SQLite schema and queries
  signals.py   percentiles, all-time lows, ceilings, the cooldown
  digest.py    the email, text and HTML
  run.py       one run: fetch, store, judge
  cli.py       argument parsing and the subcommands
```

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

117 tests, under a second, no network and no dependencies beyond PyYAML — the
suite drives a stub fetcher, so it never touches Google Flights. That is
deliberate: the scraper is the part most likely to break, and a test suite that
depended on it would be useless exactly when you needed it.

## Not in v1

- **Award/points pricing.** Live award availability needs backend access that
  services like point.me and ExpertFlyer pay for; it is not reliably
  scrapeable. Use point.me by hand (free with Amex, or a $5 day pass) when you
  are close to booking.
- **Transfer bonus tracking** (Amex/Chase/Cap One/Bilt). Genuinely buildable by
  scraping the free tracker sites, but it is its own project rather than part
  of a cash-price core.
- **Points balances.** Would be a small manually-maintained YAML. Nothing in the
  cash-tracking loop needs it.

One note for whenever points tracking does get built: cash and award prices
move together fairly closely on Delta, United, JetBlue and Southwest, and do
not on American or most alliance partners. Irrelevant while this is cash-only.
