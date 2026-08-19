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
from datetime import date, datetime
from sqlite3 import Connection
from statistics import median
from typing import Optional, Sequence

from .config import Settings, Watch
from .holidays import describe, is_peak, peak_window
from .store import HorizonSample, RunPoint, horizon_samples, parse_iso

FALLING = "falling"
RISING = "rising"
FLAT = "flat"
UNKNOWN = "unknown"

# Days from departure. Narrow near the end, where prices move fastest.
BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 6),
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

    curve = curve_for_watch(
        horizon_samples(conn), settings, watch.origin, watch.destination
    )
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
