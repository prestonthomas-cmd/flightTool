"""Evidence about where a price is heading, beyond whether it is low today.

Three independent readings, each of which states its own sample size:

1. **This flight's own trend** — a robust slope through its price history.
   What it has actually been doing.
2. **The booking-horizon curve** — pooled across watches, what prices at this
   many days from departure typically do next. What flights like this tend to
   do. Needs several watches before it says anything.
3. **Holiday position** — whether the dates sit in a holiday peak, and whether
   the cheap date in a window is cheap because it is a different trip.

None of this suppresses a buy signal. With one person's watchlist the curve
stays thin for months, and a weak forecast quietly swallowing a genuine
all-time low would be the worst thing this tool could do. It annotates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from sqlite3 import Connection
from statistics import median
from typing import Optional, Sequence

from .config import Settings, Watch
from .holidays import describe, is_peak, peak_window
from .model import Fit as PriceModel
from .model import forecast as model_forecast
from .store import (
    HorizonSample,
    RunPoint,
    date_prices,
    horizon_samples,
    parse_iso,
)

FALLING = "falling"
RISING = "rising"
FLAT = "flat"
UNKNOWN = "unknown"

# Days from departure. The edges sit on the advance-purchase boundaries
# airlines actually write fare rules against — 21, 14, 7 and 3 days — so a step
# in price lands between buckets instead of being smeared across one.
BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 2),
    (3, 6),
    (7, 13),
    (14, 20),
    (21, 29),
    (30, 44),
    (45, 59),
    (60, 89),
    (90, 119),
    (120, 179),
    (180, 730),
)

# Below this, a difference is noise rather than a move.
MEANINGFUL = 0.03


def bucket_label(low: int, high: int) -> str:
    return f"{low}d+" if high >= 730 else f"{low}-{high}d"


def bucket_for_days(days: int) -> Optional[tuple[int, int]]:
    for low, high in BUCKETS:
        if low <= days <= high:
            return (low, high)
    return None


def theil_sen(points: Sequence[tuple[float, float]]) -> Optional[float]:
    """Median of the pairwise slopes — a fit that a single odd price cannot drag.

    Ordinary least squares would be pulled around by exactly the kind of
    one-off spike that scraping produces.
    """
    if len(points) < 2:
        return None
    slopes = [
        (y2 - y1) / (x2 - x1)
        for index, (x1, y1) in enumerate(points)
        for (x2, y2) in points[index + 1 :]
        if x2 != x1
    ]
    return median(slopes) if slopes else None


@dataclass(frozen=True)
class Trend:
    """What this watch's own price has been doing."""

    per_week: float
    observations: int
    span_days: int
    latest: float

    @property
    def direction(self) -> str:
        if abs(self.per_week) < MEANINGFUL * max(self.latest, 1):
            return FLAT
        return FALLING if self.per_week < 0 else RISING

    def describe(self, currency: str) -> str:
        if self.direction == FLAT:
            return (
                f"holding steady over the last {self.span_days} days "
                f"({self.observations} runs)"
            )
        way = "down" if self.per_week < 0 else "up"
        return (
            f"drifting {way} about {currency} {abs(self.per_week):,.0f} a week over "
            f"the last {self.span_days} days ({self.observations} runs)"
        )


@dataclass(frozen=True)
class Move:
    """A step change in the recent runs, as opposed to a slow drift.

    A robust slope is deliberately hard to move, which means a price that sat
    flat for two months and then fell off a cliff still reads as "steady". That
    is the one case where the drift line is actively misleading, so the step is
    measured separately.
    """

    fraction: float
    recent_runs: int
    baseline_runs: int
    recent: float
    baseline: float

    @property
    def direction(self) -> str:
        return FALLING if self.fraction < 0 else RISING

    def describe(self, currency: str) -> str:
        way = "down" if self.fraction < 0 else "up"
        return (
            f"{way} {abs(self.fraction) * 100:.0f}% in the last "
            f"{self.recent_runs} runs ({currency} {self.baseline:,.0f} to "
            f"{currency} {self.recent:,.0f})"
        )


def recent_move(
    history: Sequence[RunPoint], settings: Settings
) -> Optional[Move]:
    """Compare the newest runs against the stretch before them."""
    recent_n = settings.move_recent_runs
    baseline_n = settings.move_baseline_runs
    if len(history) < recent_n + baseline_n:
        return None

    recent = [p.price for p in history[-recent_n:]]
    baseline = [p.price for p in history[-(recent_n + baseline_n) : -recent_n]]
    if not recent or not baseline:
        return None

    recent_median = float(median(recent))
    baseline_median = float(median(baseline))
    if baseline_median <= 0:
        return None

    fraction = (recent_median - baseline_median) / baseline_median
    if abs(fraction) < settings.move_threshold:
        return None
    return Move(
        fraction=fraction,
        recent_runs=len(recent),
        baseline_runs=len(baseline),
        recent=recent_median,
        baseline=baseline_median,
    )


@dataclass(frozen=True)
class HorizonBucket:
    low: int
    high: int
    index: float
    samples: int
    watches: int

    @property
    def label(self) -> str:
        return bucket_label(self.low, self.high)


@dataclass(frozen=True)
class HorizonCurve:
    """Relative price by days-to-departure, pooled across watches.

    Every price is divided by its own watch's median first, so a $400 domestic
    hop and a $1,200 long-haul can sit in the same curve without one drowning
    the other. The result is an index: 1.08 means "8% above what this trip
    normally costs".
    """

    buckets: tuple[HorizonBucket, ...]
    scope: str
    samples: int
    watches: int

    @property
    def usable(self) -> bool:
        # One bucket says nothing about direction — you need somewhere to go.
        return len(self.buckets) >= 2

    def bucket_for(self, days: int) -> Optional[HorizonBucket]:
        for bucket in self.buckets:
            if bucket.low <= days <= bucket.high:
                return bucket
        return None

    def ahead_of(self, days: int) -> list[HorizonBucket]:
        """Buckets closer to departure than `days` — where the price is going."""
        return [b for b in self.buckets if b.high < days]


def build_curve(
    samples: Sequence[HorizonSample],
    settings: Settings,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
) -> HorizonCurve:
    """Pool observations into a booking-horizon curve.

    A bucket is only kept once it holds enough observations from enough
    *distinct* watches. That second condition matters: a single watch's price
    against days-to-departure is the same series as its price over time, so one
    watch alone cannot tell a horizon effect from the calendar.
    """
    scope = "all watches"
    if origin and destination:
        routed = [s for s in samples if s.origin == origin and s.destination == destination]
        if routed:
            samples = routed
            scope = f"{origin}-{destination}"

    by_watch: dict[str, list[float]] = {}
    for sample in samples:
        by_watch.setdefault(sample.watch_id, []).append(sample.price)
    baselines = {
        watch_id: median(prices)
        for watch_id, prices in by_watch.items()
        if prices and median(prices) > 0
    }

    grouped: dict[tuple[int, int], list[float]] = {}
    contributors: dict[tuple[int, int], set[str]] = {}

    for sample in samples:
        baseline = baselines.get(sample.watch_id)
        if not baseline:
            continue
        try:
            days = (
                date.fromisoformat(sample.depart_date)
                - parse_iso(sample.observed_on).date()
            ).days
        except ValueError:
            continue
        if days < 0:
            continue
        key = bucket_for_days(days)
        if key is None:
            continue
        grouped.setdefault(key, []).append(sample.price / baseline)
        contributors.setdefault(key, set()).add(sample.watch_id)

    buckets = tuple(
        HorizonBucket(
            low=low,
            high=high,
            index=float(median(values)),
            samples=len(values),
            watches=len(contributors[(low, high)]),
        )
        for (low, high), values in sorted(grouped.items())
        if len(values) >= settings.horizon_min_bucket_samples
        and len(contributors[(low, high)]) >= settings.horizon_min_watches
    )

    return HorizonCurve(
        buckets=buckets,
        scope=scope,
        samples=sum(b.samples for b in buckets),
        watches=len({w for group in contributors.values() for w in group}),
    )


def curve_for_watch(
    samples: Sequence[HorizonSample],
    settings: Settings,
    origin: Optional[str],
    destination: Optional[str],
) -> HorizonCurve:
    """The watch's own route if that route has enough data, otherwise the pool.

    Preferring the route unconditionally would be worse than useless: one watch
    per route is the normal case, and a route curve can never form from one
    watch, so the pooled curve would never be consulted at all.
    """
    routed = build_curve(samples, settings, origin, destination)
    if routed.usable:
        return routed
    return build_curve(samples, settings)


WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


@dataclass(frozen=True)
class WaitingRecord:
    """When this watch was around this cheap before, did waiting pay?

    A frequency drawn from the watch's own history, not a model: of the past
    runs priced at a comparable point in their own distribution, how many were
    followed by something cheaper, and by how much. It answers the question
    actually being asked — buy now, or wait — without pretending to forecast.
    """

    cases: int
    followed_lower: int
    median_drop: float

    @property
    def share(self) -> float:
        return self.followed_lower / self.cases if self.cases else 0.0

    def describe(self) -> str:
        # "About this low" would be wrong half the time — the same reading
        # applies when the price is sitting near its high, and that is exactly
        # when the answer matters most.
        when = f"{self.cases} past run(s) priced around here"
        if not self.followed_lower:
            return f"in {when}, nothing cheaper ever followed"
        return (
            f"in {self.followed_lower} of {when}, something cheaper followed — "
            f"typically {self.median_drop:.0%} lower"
        )


def _rank(price: float, earlier: Sequence[float]) -> Optional[float]:
    """What fraction of the earlier prices were strictly below this one."""
    if not earlier:
        return None
    return sum(1 for value in earlier if value < price) / len(earlier)


def waiting_record(
    history: Sequence[RunPoint], price: float, settings: Settings, band: float = 0.15
) -> Optional[WaitingRecord]:
    """How often waiting paid, at past runs priced like this one."""
    prices = [point.price for point in history]
    here = _rank(price, prices)
    if here is None:
        return None

    lookahead = max(settings.waiting_lookahead_runs, 1)
    cases = 0
    lower = 0
    drops: list[float] = []

    for index, point in enumerate(history):
        future = prices[index + 1 :]
        # A run near the end of the series has barely any future to judge, and
        # counting it would bias the answer towards "waiting never paid".
        if len(future) < lookahead:
            break
        # Ranked against the same population as the price being judged. Using
        # only each run's own past would rank every point in a falling series
        # at zero, and nothing would ever match.
        rank = _rank(point.price, prices)
        if rank is None or abs(rank - here) > band:
            continue
        cases += 1
        cheapest = min(future)
        if cheapest < point.price:
            lower += 1
            drops.append((point.price - cheapest) / point.price)

    if cases < settings.min_waiting_cases:
        return None
    return WaitingRecord(cases, lower, float(median(drops)) if drops else 0.0)


@dataclass(frozen=True)
class WeekdayProfile:
    """Relative price by departure weekday, pooled across watches."""

    index: dict[int, float]
    samples: dict[int, int]
    scope: str
    watches: int

    @property
    def usable(self) -> bool:
        return len(self.index) >= 3

    @property
    def cheapest(self) -> Optional[int]:
        return min(self.index, key=self.index.get) if self.index else None

    def standing(self, weekday: int) -> Optional[str]:
        best = self.cheapest
        if best is None or weekday not in self.index:
            return None
        here = self.index[weekday]
        gap = (here / self.index[best]) - 1
        if best == weekday:
            return f"{WEEKDAY_NAMES[weekday]} is the cheapest day to depart"
        if gap < 0.02:
            return (
                f"{WEEKDAY_NAMES[weekday]} is about as cheap as the best day, "
                f"{WEEKDAY_NAMES[best]}"
            )
        return (
            f"{WEEKDAY_NAMES[weekday]} departures run about {gap:.0%} above "
            f"{WEEKDAY_NAMES[best]}, the cheapest day"
        )


def build_weekday_profile(
    samples: Sequence[HorizonSample],
    settings: Settings,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
) -> WeekdayProfile:
    """Pool observations by departure weekday, normalised per watch.

    Same two-watch gate as the horizon curve, and for the same reason: a single
    watch whose departures all fall on one weekday would otherwise "prove" that
    weekday is cheap.
    """
    scope = "all watches"
    if origin and destination:
        routed = [s for s in samples if s.origin == origin and s.destination == destination]
        if routed:
            samples, scope = routed, f"{origin}-{destination}"

    baselines: dict[str, float] = {}
    grouped: dict[str, list[float]] = {}
    for sample in samples:
        grouped.setdefault(sample.watch_id, []).append(sample.price)
    for watch_id, prices in grouped.items():
        middle = median(prices)
        if middle > 0:
            baselines[watch_id] = middle

    by_day: dict[int, list[float]] = {}
    contributors: dict[int, set[str]] = {}
    for sample in samples:
        baseline = baselines.get(sample.watch_id)
        if not baseline:
            continue
        try:
            weekday = date.fromisoformat(sample.depart_date).weekday()
        except ValueError:
            continue
        by_day.setdefault(weekday, []).append(sample.price / baseline)
        contributors.setdefault(weekday, set()).add(sample.watch_id)

    index = {
        day: float(median(values))
        for day, values in by_day.items()
        if len(values) >= settings.weekday_min_samples
        and len(contributors[day]) >= settings.weekday_min_watches
    }
    return WeekdayProfile(
        index=index,
        samples={day: len(by_day[day]) for day in index},
        scope=scope,
        watches=len({w for group in contributors.values() for w in group}),
    )


@dataclass(frozen=True)
class DateStanding:
    """How this date sits against its own window, versus how it usually does.

    The within-run comparison says a date is cheaper than its neighbours today.
    This says whether it is cheaper *than it usually is* relative to them —
    which separates a date-specific opportunity from the whole window moving.
    """

    depart_date: str
    now: float
    usual: float
    samples: int

    @property
    def delta(self) -> float:
        return self.now - self.usual

    def describe(self) -> str:
        # Below half a percent rounds to "0%", which reads as broken.
        if abs(self.usual) < 0.005:
            usual = "normally level with the rest of its window"
        else:
            usual = (
                f"normally {abs(self.usual):.0%} "
                f"{'below' if self.usual < 0 else 'above'} the rest of its window"
            )
        if abs(self.now) < 0.005:
            here = "today it is level with them"
        else:
            here = (
                f"today it is {abs(self.now):.0%} "
                f"{'below' if self.now < 0 else 'above'}"
            )
        if abs(self.delta) < 0.02:
            return (
                f"{self.depart_date} is {usual}, and {here} — no better placed "
                f"than usual ({self.samples} runs)"
            )
        verdict = (
            "unusually well placed against its own window"
            if self.delta < 0
            else "less well placed against its own window than usual"
        )
        return (
            f"{self.depart_date} is {usual}, but {here} — {verdict} "
            f"({self.samples} runs)"
        )


def date_standing(
    rows: Sequence[tuple[str, str, float]], depart_date: Optional[str], minimum: int = 5
) -> Optional[DateStanding]:
    """Track one date's gap to its window over time, not just today."""
    if not depart_date:
        return None

    by_run: dict[str, dict[str, float]] = {}
    for timestamp, day, price in rows:
        run = by_run.setdefault(timestamp, {})
        if day not in run or price < run[day]:
            run[day] = price

    residuals: list[float] = []
    latest: Optional[float] = None
    for timestamp in sorted(by_run):
        prices = by_run[timestamp]
        # A window of one date has no "rest of the window" to sit against.
        if len(prices) < 2 or depart_date not in prices:
            continue
        others = [p for day, p in prices.items() if day != depart_date]
        typical = float(median(others))
        if typical <= 0:
            continue
        residual = prices[depart_date] / typical - 1
        residuals.append(residual)
        latest = residual

    if latest is None or len(residuals) < minimum:
        return None
    return DateStanding(
        depart_date=depart_date,
        now=latest,
        usual=float(median(residuals[:-1])) if len(residuals) > 1 else latest,
        samples=len(residuals),
    )


def horizon_adjust(
    history: Sequence[RunPoint],
    curve: HorizonCurve,
    departure: date,
    now: datetime,
) -> tuple[list[RunPoint], bool]:
    """Re-base past prices onto today's point in the booking window.

    Prices move systematically with distance from departure, so a history
    collected months ago is not on the same footing as today's price. Each past
    price is scaled by the ratio of the curve at today's horizon to the curve at
    the horizon it was taken at, which puts the whole series on today's terms
    before any percentile is computed.

    Returns the history unchanged, and False, whenever the curve cannot say
    enough — this never silently invents an adjustment.
    """
    if not curve.usable:
        return list(history), False

    here = curve.bucket_for((departure - now.date()).days)
    if here is None or here.index <= 0:
        return list(history), False

    adjusted: list[RunPoint] = []
    changed = False
    for point in history:
        days = (departure - parse_iso(point.timestamp).date()).days
        bucket = curve.bucket_for(days) if days >= 0 else None
        if bucket is None or bucket.index <= 0 or bucket is here:
            adjusted.append(point)
            continue
        adjusted.append(
            RunPoint(point.timestamp, point.price * (here.index / bucket.index))
        )
        changed = True
    return adjusted, changed


@dataclass(frozen=True)
class Forecast:
    direction: str
    confidence: str
    headline: str
    notes: tuple[str, ...] = ()
    days_out: Optional[int] = None
    holiday: Optional[str] = None
    trend: Optional[Trend] = None
    move: Optional[Move] = None
    curve_scope: Optional[str] = None
    waiting: Optional[WaitingRecord] = None
    standing: Optional[DateStanding] = None

    @property
    def known(self) -> bool:
        return self.direction != UNKNOWN


def build_trend(
    history: Sequence[RunPoint], settings: Settings
) -> Optional[Trend]:
    """A robust slope through the watch's own run history."""
    if len(history) < settings.min_trend_observations:
        return None

    recent = list(history)[-settings.trend_window_runs :]
    origin = parse_iso(recent[0].timestamp)
    points = [
        ((parse_iso(point.timestamp) - origin).total_seconds() / 86400.0, point.price)
        for point in recent
    ]
    per_day = theil_sen(points)
    if per_day is None:
        return None

    span = int(round(points[-1][0] - points[0][0]))
    return Trend(
        per_week=per_day * 7,
        observations=len(recent),
        span_days=max(span, 1),
        latest=recent[-1].price,
    )


def compare_neighbours(
    rows: Sequence[tuple[str, float]], best_depart: Optional[str], currency: str
) -> Optional[str]:
    """Set the chosen date against the other departures priced in the same run.

    This is the reading that works from the very first run: it needs no
    history, only the window the watch already searches.
    """
    if best_depart is None or len(rows) < 2:
        return None

    by_date: dict[str, float] = {}
    for depart, price in rows:
        if depart and (depart not in by_date or price < by_date[depart]):
            by_date[depart] = price
    if len(by_date) < 2:
        return None

    best = by_date.get(best_depart)
    if best is None:
        return None

    earlier = [p for d, p in by_date.items() if d < best_depart]
    later = [p for d, p in by_date.items() if d > best_depart]
    others = earlier + later
    if not others:
        return None

    spread = max(by_date.values()) - min(by_date.values())
    if spread <= max(best * 0.02, 1):
        return (
            f"every date in the window is within {currency} {spread:,.0f} of the "
            f"others — this is route-wide pricing, not one cheap date"
        )

    parts = []
    if earlier:
        gap = median(earlier) - best
        parts.append(
            f"{currency} {abs(gap):,.0f} {'below' if gap > 0 else 'above'} the "
            f"{len(earlier)} earlier departure(s)"
        )
    if later:
        gap = median(later) - best
        parts.append(
            f"{currency} {abs(gap):,.0f} {'below' if gap > 0 else 'above'} the "
            f"{len(later)} later one(s)"
        )
    return f"{best_depart} is " + " and ".join(parts)


def holiday_note(best_depart: Optional[str], departures: Sequence[str]) -> Optional[str]:
    """Flag when the cheap date is cheap because it is a different trip."""
    if best_depart is None:
        return None
    try:
        best = date.fromisoformat(best_depart)
    except ValueError:
        return None

    label = describe(best)
    others: list[date] = []
    for text in departures:
        try:
            day = date.fromisoformat(text)
        except (ValueError, TypeError):
            continue
        if day != best:
            others.append(day)

    peak_others = [d for d in others if is_peak(d)]
    if not is_peak(best) and peak_others:
        window = peak_window(peak_others[0])
        name = window.name if window else "the holiday"
        return (
            f"{best_depart} is {label} while {len(peak_others)} other date(s) in "
            f"the window fall inside the {name} peak — cheaper, but not the same "
            f"trip"
        )
    return label


def build_forecast(
    conn: Connection,
    watch: Watch,
    history: Sequence[RunPoint],
    settings: Settings,
    now: datetime,
    best_depart: Optional[str] = None,
    run_rows: Sequence[tuple[str, float]] = (),
    currency: str = "USD",
    price: Optional[float] = None,
) -> Forecast:
    """Combine every available reading into one annotation."""
    notes: list[str] = []

    trend = build_trend(history, settings)
    move = recent_move(history, settings)
    if move is not None:
        notes.append(f"This flight is {move.describe(currency)}.")
    elif trend is not None:
        notes.append(f"This flight has been {trend.describe(currency)}.")

    days_out: Optional[int] = None
    if best_depart:
        try:
            days_out = (date.fromisoformat(best_depart) - now.date()).days
        except ValueError:
            days_out = None

    samples = horizon_samples(conn)
    curve = curve_for_watch(samples, settings, watch.origin, watch.destination)
    curve_direction = UNKNOWN
    trough: Optional[HorizonBucket] = None

    if curve.usable and days_out is not None and days_out >= 0:
        here = curve.bucket_for(days_out)
        ahead = curve.ahead_of(days_out)
        if here is not None and ahead:
            cheapest = min(ahead, key=lambda b: b.index)
            if cheapest.index < here.index * (1 - MEANINGFUL):
                curve_direction, trough = FALLING, cheapest
                notes.append(
                    f"Across {curve.samples} observations ({curve.scope}), prices "
                    f"at {here.label} out typically fall a further "
                    f"{(1 - cheapest.index / here.index) * 100:.0f}% by "
                    f"{cheapest.label} out."
                )
            elif all(b.index > here.index * (1 + MEANINGFUL) for b in ahead):
                curve_direction = RISING
                notes.append(
                    f"Across {curve.samples} observations ({curve.scope}), prices "
                    f"only go up from {here.label} out — waiting has cost money."
                )
            else:
                curve_direction = FLAT
                notes.append(
                    f"Across {curve.samples} observations ({curve.scope}), prices "
                    f"at {here.label} out are usually flat from here."
                )
    elif not curve.usable:
        notes.append(
            "Booking-horizon curve: not enough data yet — it needs several "
            f"watches with overlapping histories (have {curve.watches})."
        )

    profile = build_weekday_profile(
        samples, settings, watch.origin, watch.destination
    )
    if profile.usable and best_depart:
        try:
            standing = profile.standing(date.fromisoformat(best_depart).weekday())
        except ValueError:
            standing = None
        if standing:
            total = sum(profile.samples.values())
            notes.append(f"{standing} ({total} observations, {profile.scope}).")

    record = waiting_record(history, price, settings) if price is not None else None
    if record is not None:
        notes.append(f"Historically, {record.describe()}.")

    placing = date_standing(date_prices(conn, watch.id), best_depart)
    if placing is not None:
        notes.append(placing.describe()[0].upper() + placing.describe()[1:] + ".")

    neighbours = compare_neighbours(run_rows, best_depart, currency)
    if neighbours:
        notes.append(neighbours[0].upper() + neighbours[1:] + ".")

    holiday = holiday_note(best_depart, [d for d, _ in run_rows])
    if holiday:
        notes.append(holiday[0].upper() + holiday[1:] + ".")

    direction, confidence, headline = _combine(trend, move, curve_direction, trough)

    return Forecast(
        direction=direction,
        confidence=confidence,
        headline=headline,
        notes=tuple(notes),
        days_out=days_out,
        holiday=holiday,
        trend=trend,
        move=move,
        curve_scope=curve.scope if curve.usable else None,
        waiting=record,
        standing=placing,
    )


def _combine(
    trend: Optional[Trend],
    move: Optional[Move],
    curve_direction: str,
    trough: Optional[HorizonBucket],
) -> tuple[str, str, str]:
    """Reconcile the readings, and say plainly when they disagree.

    A material step change speaks for the watch's own history in place of the
    drift: a 20% drop last week is what the price is doing, whatever the slope
    across the previous two months says.
    """
    if move is not None:
        trend_direction = move.direction
    elif trend is not None:
        trend_direction = trend.direction
    else:
        trend_direction = UNKNOWN

    if curve_direction == UNKNOWN and trend_direction == UNKNOWN:
        return UNKNOWN, "none", "No view yet — still collecting data."

    if curve_direction == UNKNOWN:
        return (
            trend_direction,
            "low",
            {
                FALLING: "Has been falling; no horizon curve yet to say whether it keeps going.",
                RISING: "Has been rising; no horizon curve yet to say whether it keeps going.",
                FLAT: "Flat so far, with no horizon curve yet.",
            }[trend_direction],
        )

    if trend_direction == UNKNOWN:
        return curve_direction, "low", _curve_sentence(curve_direction, trough)

    if trend_direction == curve_direction:
        return curve_direction, "medium", _curve_sentence(curve_direction, trough)

    return (
        curve_direction,
        "low",
        _curve_sentence(curve_direction, trough)
        + f" Its own history disagrees — that has been {trend_direction}.",
    )


def _curve_sentence(direction: str, trough: Optional[HorizonBucket]) -> str:
    if direction == FALLING:
        where = f" around {trough.label} out" if trough else ""
        return f"Prices like this usually fall further; the trough is{where}."
    if direction == RISING:
        return "Past the usual trough — prices like this tend to rise from here."
    return "Prices like this are usually flat from here."


def rebase(
    samples: Sequence[HorizonSample],
    watch: Watch,
    history: Sequence[RunPoint],
    settings: Settings,
    now: datetime,
) -> tuple[list[RunPoint], bool]:
    """Put a watch's history on today's terms, if the curve is good enough.

    The second value says whether anything actually changed, so a caller can
    report an adjusted baseline honestly rather than claiming one that was
    never applied.
    """
    if not settings.horizon_adjusted_baseline or not watch.depart_dates:
        return list(history), False
    curve = curve_for_watch(samples, settings, watch.origin, watch.destination)
    return horizon_adjust(history, curve, min(watch.depart_dates), now)


# --- Forward projection ------------------------------------------------------
#
# The line on the chart. Its shape comes from `flighttracker.model`, an additive
# decomposition of log price into level, booking horizon, holiday and weekday,
# fitted to every observation with each component shrunk toward a prior by
# weight of evidence.
#
# That means there is no longer a switch between "your data" and "a generic
# shape". There is one estimate that starts as the general advance-purchase
# pattern and is pulled toward what your own watches show as they accumulate,
# and it reports how far along that journey it is.
#
# A flight's own recent slope is deliberately not the line's geometry: ten days
# of drift carried out to a December departure is drawing, not forecasting. It
# is reported in words instead.

PROJECTION_STEP_DAYS = 7


@dataclass(frozen=True)
class Projected:
    day: date
    price: float
    low: float
    high: float


@dataclass(frozen=True)
class Projection:
    points: tuple[Projected, ...] = ()
    method: str = ""
    note: str = ""
    evidence: float = 0.0

    @property
    def usable(self) -> bool:
        return len(self.points) >= 2


def _trend_clause(history, settings: Settings, currency: str = "") -> str:
    trend = build_trend(history, settings)
    if trend is None:
        return ""
    if trend.direction == FLAT:
        return f" Its own price has been flat over the last {trend.span_days} days."
    way = "down" if trend.per_week < 0 else "up"
    unit = f"{currency} " if currency else ""
    return (
        f" Its own price has been drifting {way} about {unit}"
        f"{abs(trend.per_week):,.0f} a week over the last {trend.span_days} days."
    )


def project(
    history: Sequence[RunPoint],
    watch: Watch,
    model: PriceModel,
    settings: Settings,
    now: datetime,
    price: Optional[float] = None,
    currency: str = "",
) -> Projection:
    """Where this watch's price is expected to go, with calibrated uncertainty."""
    prices = [point.price for point in history]
    if not prices or not watch.depart_dates:
        return Projection(note="No prices recorded yet.")

    current = price if price is not None else prices[-1]
    departure = min(watch.depart_dates)
    days_out = (departure - now.date()).days
    if days_out <= 0:
        return Projection(note="Departure has passed.")
    if days_out < PROJECTION_STEP_DAYS:
        return Projection(
            note=f"Departure is {days_out} day(s) away — too close to project over."
        )

    steps = list(range(PROJECTION_STEP_DAYS, days_out + 1, PROJECTION_STEP_DAYS))
    if steps and steps[-1] != days_out:
        steps.append(days_out)

    points: list[Projected] = []
    for ahead in steps:
        step = model_forecast(
            model, days_out, days_out - ahead, steps_ahead=ahead / 7.0
        )
        expected, low, high = step.band(current)
        points.append(
            Projected(now.date() + timedelta(days=ahead), expected, max(low, 1.0), high)
        )

    if len(points) < 2:
        return Projection(note="Not enough of the booking window left to project over.")

    evidence = model.evidence()
    if evidence >= 0.5:
        source = (
            f"Fitted mostly to your own data: {evidence:.0%} of this curve comes "
            f"from {model.observations} observations across {model.watches} "
            f"watch(es), the rest from the general advance-purchase pattern."
        )
    elif evidence > 0.02:
        source = (
            f"Mostly the general advance-purchase pattern — fares sit high far "
            f"out, trough around six to eight weeks before departure, then climb "
            f"through the last three. Only {evidence:.0%} of this curve is yet "
            f"drawn from your own {model.observations} observations; it shifts "
            "toward them as they accumulate."
        )
    else:
        source = (
            "The general advance-purchase pattern: fares sit high far out, trough "
            "around six to eight weeks before departure, then climb through the "
            "last three. Your own observations have not yet moved it — they will, "
            "as watches accumulate at differing distances from departure."
        )

    return Projection(
        points=tuple(points),
        method="model",
        note=source + _trend_clause(history, settings, currency),
        evidence=evidence,
    )
