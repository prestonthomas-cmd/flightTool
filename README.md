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

cp .env.example .env                   # then fill in SMTP details
$EDITOR watches.yaml                   # replace the example trips with yours

flighttracker validate      # check the watchlist, see what each run will do
flighttracker run --dry-run # fetch real prices, print the digest, store nothing
flighttracker run           # the real thing
```

Without installing, every command also works as `python -m flighttracker ...`.

## The watchlist

`watches.yaml` is the only file you edit. It ships with the three example
watches so the scheduled workflows do something on their first run — replace
them with trips you care about, since every watch is scraped on every run.
`watches.example.yaml` keeps a pristine annotated copy. This is the shape:

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
| `exclude_basic_economy` | defaults to **true** — see below |
| `carry_on_bags`, `checked_bags` | bag fees included in the priced total |
| `hide_separate_and_self_transfer` | drop separate-ticket itineraries |
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
- **Below the percentile** — in the cheapest `percentile`% of its own history,
  **and** far enough below its median to be worth an email.

That second condition is not decoration. Being under the percentile threshold
is not on its own evidence of anything: the 20th percentile of a price that
never moves *is* that price, and a history carrying one old outlier puts the
threshold on the modal price. In both cases a completely ordinary price clears
the threshold and alerts on every run. Replaying a stable $268 fare through the
rule without the guard produced **6.8 alerts a month about a price that had not
moved**; with it, 0.4. The all-time-low rule needs no such guard — it already
demands a strict improvement on everything seen before.

That bar **scales with the watch's own volatility** (`min_discount` is its
floor, `max_discount` its ceiling). "Wait" is only advice worth giving if the
price actually moves: a watch that swings 15% should not alert on the same 2%
dip that means something on a stable fare. Volatility is measured as a median
absolute deviation, so one scraped outlier cannot make a steady fare look
wild. Set `adaptive_discount: false` for a flat bar.

A consequence worth knowing: on a genuinely stable fare, any price low enough
to clear the bar is by then also an all-time low, so that is what gets
reported. "In the cheapest 20%" carries no information when the price does not
move, and the tool stops pretending otherwise.

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

## Where the price is heading

A buy signal says a price is low *today*. These three readings say what it has
been doing and what prices like it usually do next. They appear under each
watch in the digest and on the dashboard.

**They annotate; they never gate.** Nothing here can suppress a buy signal.
With one person's watchlist the horizon curve stays thin for months, and a weak
forecast quietly swallowing a genuine all-time low would be the worst thing
this tool could do. Every reading states its own sample size, and confidence
never goes above "medium".

**1. This flight's own history.** A Theil–Sen slope — the median of all
pairwise slopes — gives the drift in dollars per week without one scraped
outlier dragging it around. Because a robust slope is *designed* to ignore a
few odd points, a price that sat flat for two months and then fell off a cliff
still reads as "steady", so a step change is measured separately: the newest
runs against the stretch before them. When there is a real step, it speaks for
the watch instead of the drift.

**2. The booking-horizon curve.** Every observation knows how many days before
departure it was taken. Pool them by that distance and you get the shape of the
booking window: dear far out, a trough somewhere in the middle, a sharp climb
at the end. Each price is divided by its own watch's median first, so a $260
Austin–Denver hop and a $1,400 long-haul can share a curve without one drowning
the other — the result is an index where 100 means "what this trip normally
costs".

The bucket edges sit on the advance-purchase boundaries airlines actually
write fare rules against — 21, 14, 7 and 3 days — so a step in price lands
between buckets instead of being smeared across one.

Once the curve is good enough it also **re-bases the history before judging
it**: past prices are scaled by the ratio of the curve at today's horizon to
the curve at the horizon they were taken at, so a percentile is not computed
against months-old prices collected at a systematically different distance from
departure. When that happens the digest and dashboard say
`horizon-adjusted baseline` — it changes the judgement, so it is never silent.
Set `horizon_adjusted_baseline: false` to switch it off, and use
`backtest --raw-baseline` to measure what it is worth on your own data.

A bucket only counts once it holds observations from **at least two different
watches**. That gate matters: a single watch's price against days-to-departure
*is* its price over time, so one watch alone cannot tell a horizon effect from
the calendar. Until the gate is met the tool says so rather than drawing a line
through noise:

```
- Booking-horizon curve: not enough data yet — it needs several watches with
  overlapping histories (have 1).
```

Realistically this takes a couple of months of running two or more watches. The
watch's own route is used when that route has enough data; otherwise everything
is pooled.

**3. Did waiting pay?** The question you are actually asking. Of the past runs
priced at a comparable point in this watch's own distribution, how many were
followed by something cheaper, and by how much:

```
Historically, in 15 of 15 past run(s) priced around here, something cheaper
followed — typically 26% lower.
```

It is a frequency from your own history, not a model, and it reads the same way
when a price is high as when it is low — which is exactly when it matters most.
Runs near the end of the series are excluded: they have barely any future to
judge, and counting them would bias the answer towards "waiting never paid".

**4. Departure weekday.** Pooled across watches and normalised the same way as
the horizon curve, since weekday is one of the strongest fare drivers:

```
Friday departures run about 20% above Tuesday, the cheapest day (312
observations, all watches).
```

Same two-watch gate — a single watch whose departures all fall on one weekday
would otherwise "prove" that weekday is cheap. In practice a small watchlist
often will not clear it, and the reading simply stays quiet.

**5. This date against its own window, over time.** The within-run comparison
below says a date is cheaper than its neighbours *today*. This says whether it
is cheaper than it *usually* is relative to them, which is what separates a
date-specific opportunity from the whole market moving:

```
2026-12-10 is normally 12% below the rest of its window, but today it is 28%
below — unusually well placed against its own window (111 runs).
```

If the whole window drops 30%, this correctly reports "no better placed than
usual" rather than calling it a deal on that date.

**6. Sooner and further out.** Within a single run, the chosen date is compared
against the other departures in the same window. This one works from the very
first run, because the window is already being searched:

```
2026-12-10 is USD 367 below the 4 later one(s).
```

or, when nothing stands out:

```
every date in the window is within USD 40 of the others — this is route-wide
pricing, not one cheap date
```

## Holidays

Holiday travel demand tracks the *holiday*, not the calendar date: the days
before Thanksgiving are expensive whether that falls on 26 November or the
22nd. `flighttracker/holidays.py` computes US holidays by rule for any year —
fixed dates, nth-weekday rules for Thanksgiving and the Monday holidays, and
the Gregorian computus for Easter — so historic and future schedules both come
out right, with no data file to go stale and no dependency.

Each major holiday carries an asymmetric travel window (out before
Thanksgiving, home for several days after; Christmas reaches past New Year
because that is one trip, not two). That gets used two ways:

- **Explaining a cheap date.** If the cheapest date in a window is cheap
  because it falls outside the holiday peak, that is not a deal — it is a
  different trip, and the digest says so:

  ```
  2026-12-10 is 14d after Thanksgiving while 2 other date(s) in the window fall
  inside the Christmas peak — cheaper, but not the same trip.
  ```

- **Comparing across years.** A date's holiday position (`Thanksgiving, -2`)
  means the same thing in every year, where "23 November" does not. That is
  what makes a cross-year comparison honest.

A holiday label is only applied within three weeks of the holiday. Wider than
that and almost every date in the year gets one — at six weeks only a single
day in 2026 came back clean, which makes the label meaningless.

## The dashboard

```bash
flighttracker dashboard                       # writes site/index.html
flighttracker dashboard --out ~/flights.html  # anywhere you like
```

One self-contained HTML file — no server, no build step, no CDN, nothing
fetched. Open it from disk. Per watch it shows the current price against its
median, the buy-signal state and why, the outlook above, a price-history chart,
and the date grid for the latest run with holiday-peak dates marked. Below
that, the pooled booking-horizon curve. It follows your system light/dark
setting, and every chart has a hover readout and a table view underneath.

The tracker rebuilds it on every run and commits it to the repo, where it stays
private.

### Publishing it

The dashboard is published to GitHub Pages after every tracking run, and on
demand from Actions → *Publish the dashboard* → **Run workflow**. The URL is
`https://<you>.github.io/<repo>/`, and the workflow prints it when it finishes.

**A Pages site is public.** So is this repository — which is what makes Pages
available on a free plan at all, since publishing from a *private* repo needs a
paid one, and even then the site itself is still public. Everything on the page
is already public in the repo: the code, the price history in `data/`, and
`watches.yaml`. Worth remembering when you put real trips in it.

If the deploy fails with `Resource not accessible by integration` or
`Not Found`, set the source by hand once: Settings → Pages → Build and
deployment → Source: **GitHub Actions**. The workflow asks to enable Pages
itself, but the Actions token is not always permitted to create the site.

To stop publishing, set the repository variable `PUBLISH_DASHBOARD` to `false`
(Settings → Secrets and variables → Actions → Variables). The tracker still
commits `site/index.html` either way, so `git pull && open site/index.html`
always works without any of this.

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
  one, but it will occasionally need one. `doctor --live` and the weekly canary
  exist to make that failure loud instead of silent.
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
- **The forecasts are descriptive statistics, not a model.** They summarise the
  history in your own database. The defaults were tuned against simulated
  prices, so treat them as starting points and re-check them with `backtest`
  once you have a few weeks of real history. Early on that history is thin, and the tool
  says so rather than dressing a guess up as a prediction. Airlines also
  reprice for reasons no amount of history can see — a fare war, a schedule
  change, a competitor pulling a route.

## Looking at the data

```bash
flighttracker history                    # every watch, run by run
flighttracker history nyc-to-tokyo-dec   # just one
flighttracker signals                    # re-judge the latest stored run, no fetching
flighttracker dashboard                  # the whole thing as one HTML page
flighttracker doctor                     # is it still collecting usable prices?
flighttracker backtest --sweep           # what would this rule have done?
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

`price_history` also records `origin` and `destination`, which is what lets the
horizon curve pool by route, and `fare`, the signature of what each price
actually bought. A database written by an earlier version gains the
columns on open — the migration is additive and never drops anything.

## Layout

```
flighttracker/
  config.py     watchlist parsing and validation
  dates.py      date ranges -> the searches to actually run
  fetch.py      the only module that knows about Google Flights
  store.py      SQLite schema, queries and in-place migrations
  signals.py    percentiles, all-time lows, ceilings, the cooldown
  health.py     staleness and fare-signature checks
  backtest.py   replaying stored history through the alerting rule
  holidays.py   US holidays by rule, and travel peak windows
  forecast.py   trend, step changes, the horizon curve, neighbouring dates
  digest.py     the email, text and HTML
  charts.py     inline SVG line and bar charts, no dependencies
  dashboard.py  the self-contained HTML page
  run.py        one run: fetch, store, judge, annotate
  cli.py        argument parsing and the subcommands
```

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

282 tests, under a second, no network and no dependencies beyond PyYAML — the
suite drives a stub fetcher, so it never touches Google Flights. That is
deliberate: the scraper is the part most likely to break, and a test suite that
depended on it would be useless exactly when you needed it.

The holiday rules are checked against published calendars for past and future
years, and the horizon curve is checked by feeding it a known shape and
confirming it recovers it.

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
