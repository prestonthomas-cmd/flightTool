"""The price model: an additive decomposition fitted with shrinkage.

A fare is not one number moving randomly. It is a level that belongs to the
route and cabin, times a booking-curve effect that depends on how far out you
are, times a seasonal effect that depends on when you fly. Modelling it as one
undifferentiated series throws all of that away.

So, in log space where those multiplicative effects become additive:

    log(price) = level[watch]
               + horizon(days before departure)
               + holiday(departure date)
               + weekday(departure date)
               + noise

Fitted by backfitting — cycle through the components, each time fitting one to
what the others have not explained — which converges in a handful of passes and
stays readable, unlike anything that would need a matrix library.

**Every component is shrunk toward a prior**, by a weight that depends on how
much data supports it:

    estimate = (n · observed + k · prior) / (n + k)

With no data you get the prior exactly. With plenty you get the data exactly.
In between you get a blend that moves smoothly from one to the other as
observations accumulate. That is the whole answer to "I have 19 points and do
not want to either ignore them or overfit them" — no mode switching, no cliff
where the model suddenly changes its mind, and no pretending a handful of
observations is a fitted curve.

The prior for the horizon component is the general advance-purchase shape.
It is an assumption, and `Component.prior_weight` reports exactly how much of
any given estimate still rests on it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from statistics import fmean, median
from typing import Optional, Sequence

from .holidays import is_peak, peak_window
from .store import HorizonSample, parse_iso

# Days before departure. Edges sit on the advance-purchase boundaries airlines
# write fare rules against, so a step in price lands between buckets.
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

# The general advance-purchase shape, as log multipliers relative to a
# trip's own average. Fares sit high far out, trough around six to eight weeks,
# and climb steeply through the last three. This is the *prior* — an assumption
# about flights in general, which observations pull away from as they arrive.
PRIOR_HORIZON: dict[tuple[int, int], float] = {
    (0, 2): math.log(1.85),
    (3, 6): math.log(1.62),
    (7, 13): math.log(1.38),
    (14, 20): math.log(1.18),
    (21, 29): math.log(1.06),
    (30, 44): math.log(0.97),
    (45, 59): math.log(0.94),
    (60, 89): math.log(0.97),
    (90, 119): math.log(1.03),
    (120, 179): math.log(1.07),
    (180, 730): math.log(1.12),
}

NO_HOLIDAY = "none"
# A pass that moves every component less than this in log space has converged.
TOLERANCE = 1e-4
# Weekly log-scale movement assumed even of a fare seen to be perfectly stable.
WALK_FLOOR = 0.04
# A ceiling on backfitting passes; convergence normally stops it far sooner.
PASSES = 200


def bucket_for(days: int) -> Optional[tuple[int, int]]:
    for low, high in BUCKETS:
        if low <= days <= high:
            return (low, high)
    return None


def holiday_key(day: date) -> str:
    """Which holiday peak a departure sits in, or none.

    Coarse on purpose: a key per holiday rather than per day-offset keeps
    enough observations behind each level to estimate anything at all.
    """
    if not is_peak(day):
        return NO_HOLIDAY
    window = peak_window(day)
    return window.name if window else NO_HOLIDAY


@dataclass(frozen=True)
class Component:
    """One fitted effect, and how much of it is still the prior talking."""

    value: float
    samples: int = 0
    prior_weight: float = 1.0

    @property
    def multiplier(self) -> float:
        return math.exp(self.value)

    @property
    def from_data(self) -> float:
        return 1.0 - self.prior_weight


@dataclass(frozen=True)
class Observation:
    """One priced search, reduced to the model's variables."""

    watch_id: str
    log_price: float
    days_out: int
    bucket: tuple[int, int]
    holiday: str
    weekday: int


@dataclass
class Fit:
    levels: dict[str, float] = field(default_factory=dict)
    horizon: dict[tuple[int, int], Component] = field(default_factory=dict)
    holiday: dict[str, Component] = field(default_factory=dict)
    weekday: dict[int, Component] = field(default_factory=dict)
    sigma: float = 0.0
    observations: int = 0
    watches: int = 0
    span_days: int = 0

    @property
    def usable(self) -> bool:
        return self.observations > 0

    def horizon_at(self, days: int) -> Component:
        key = bucket_for(days)
        if key is None:
            return Component(0.0, 0, 1.0)
        return self.horizon.get(key, Component(PRIOR_HORIZON.get(key, 0.0), 0, 1.0))

    def holiday_at(self, day: date) -> Component:
        return self.holiday.get(holiday_key(day), Component(0.0, 0, 1.0))

    def weekday_at(self, day: date) -> Component:
        return self.weekday.get(day.weekday(), Component(0.0, 0, 1.0))

    def evidence(self) -> float:
        """How much of the horizon curve rests on data rather than assumption."""
        if not self.horizon:
            return 0.0
        return fmean(component.from_data for component in self.horizon.values())


def _shrink(
    values: Sequence[float], prior: float, strength: float
) -> Component:
    """Blend what was observed with what was assumed, by weight of evidence."""
    n = len(values)
    if n == 0:
        return Component(prior, 0, 1.0)
    observed = float(median(values))
    prior_weight = strength / (n + strength)
    return Component(
        value=observed * (1 - prior_weight) + prior * prior_weight,
        samples=n,
        prior_weight=prior_weight,
    )


def to_observations(samples: Sequence[HorizonSample]) -> list[Observation]:
    """Reduce stored prices to the model's variables, dropping what cannot be used."""
    out: list[Observation] = []
    for sample in samples:
        if sample.price <= 0:
            continue
        try:
            departure = date.fromisoformat(sample.depart_date)
            observed_on = parse_iso(sample.observed_on).date()
        except (ValueError, TypeError):
            continue
        days_out = (departure - observed_on).days
        if days_out < 0:
            continue
        key = bucket_for(days_out)
        if key is None:
            continue
        out.append(
            Observation(
                watch_id=sample.watch_id,
                log_price=math.log(sample.price),
                days_out=days_out,
                bucket=key,
                holiday=holiday_key(departure),
                weekday=departure.weekday(),
            )
        )
    return out


def _recentre(components, grouped, prior=None):
    """Pin a component to its prior's gauge, leaving every ratio untouched.

    Only differences within a component are identifiable: adding a constant to
    every value in it and taking the same constant off every watch's level
    describes the data exactly as well. Left free, that constant wanders — and
    since each component is shrunk toward its prior on every pass while the
    levels are not, the wandering quietly bleeds the data's contribution away
    into the levels, one pass at a time. A holiday premium fitted this way
    decays toward "no effect" the longer the fit runs.

    Anchoring each component so its sample-weighted mean matches its prior's
    fixes the gauge, and the backfitting then settles instead of drifting.
    """
    weights = {key: len(values) for key, values in grouped.items() if values}
    total = sum(weights.values())
    if not total:
        return components

    def prior_for(key: object) -> float:
        return 0.0 if prior is None else prior.get(key, 0.0)

    here = sum(components[key].value * n for key, n in weights.items()) / total
    there = sum(prior_for(key) * n for key, n in weights.items()) / total
    offset = here - there
    if abs(offset) < 1e-12:
        return components
    return {
        key: replace(component, value=component.value - offset)
        for key, component in components.items()
    }



def fit(
    samples: Sequence[HorizonSample],
    shrinkage: float = 8.0,
    passes: int = PASSES,
    tolerance: float = TOLERANCE,
) -> Fit:
    """Fit the additive model by backfitting, shrinking each component.

    Each pass re-estimates one component against what the others leave
    unexplained. `passes` is a ceiling, not a target: the loop stops as soon
    as a pass moves nothing by more than `tolerance` in log space. Stopping
    early on a fixed count leaves confounded components — a holiday premium
    and the horizon curve, say — still trading effect between them, which
    shows up as the holiday bending a curve it has nothing to do with.
    """
    rows = to_observations(samples)
    if not rows:
        return Fit()

    levels: dict[str, float] = {}
    horizon: dict[tuple[int, int], Component] = {}
    holiday: dict[str, Component] = {}
    weekday: dict[int, Component] = {}

    def horizon_value(key) -> float:
        component = horizon.get(key)
        return component.value if component else PRIOR_HORIZON.get(key, 0.0)

    def holiday_value(key) -> float:
        component = holiday.get(key)
        return component.value if component else 0.0

    def weekday_value(key) -> float:
        component = weekday.get(key)
        return component.value if component else 0.0

    def snapshot() -> dict:
        return {
            **{("level", k): v for k, v in levels.items()},
            **{("horizon", k): c.value for k, c in horizon.items()},
            **{("holiday", k): c.value for k, c in holiday.items()},
            **{("weekday", k): c.value for k, c in weekday.items()},
        }

    previous: dict = {}
    for _ in range(passes):
        # Level: whatever the shared effects do not explain, per watch.
        by_watch: dict[str, list[float]] = {}
        for row in rows:
            residual = (
                row.log_price
                - horizon_value(row.bucket)
                - holiday_value(row.holiday)
                - weekday_value(row.weekday)
            )
            by_watch.setdefault(row.watch_id, []).append(residual)
        levels = {
            watch_id: float(median(values)) for watch_id, values in by_watch.items()
        }

        # Horizon: shrunk toward the general advance-purchase shape.
        grouped: dict[tuple[int, int], list[float]] = {}
        for row in rows:
            grouped.setdefault(row.bucket, []).append(
                row.log_price
                - levels[row.watch_id]
                - holiday_value(row.holiday)
                - weekday_value(row.weekday)
            )
        horizon = {
            key: _shrink(values, PRIOR_HORIZON.get(key, 0.0), shrinkage)
            for key, values in grouped.items()
        }
        horizon = _recentre(horizon, grouped, PRIOR_HORIZON)
        for key in BUCKETS:
            horizon.setdefault(key, Component(PRIOR_HORIZON.get(key, 0.0), 0, 1.0))

        # Holiday and weekday: shrunk toward no effect, which is the honest
        # default — absent evidence, a date is not special.
        by_holiday: dict[str, list[float]] = {}
        for row in rows:
            by_holiday.setdefault(row.holiday, []).append(
                row.log_price
                - levels[row.watch_id]
                - horizon_value(row.bucket)
                - weekday_value(row.weekday)
            )
        holiday = _recentre(
            {
                key: _shrink(values, 0.0, shrinkage)
                for key, values in by_holiday.items()
            },
            by_holiday,
        )

        by_weekday: dict[int, list[float]] = {}
        for row in rows:
            by_weekday.setdefault(row.weekday, []).append(
                row.log_price
                - levels[row.watch_id]
                - horizon_value(row.bucket)
                - holiday_value(row.holiday)
            )
        weekday = _recentre(
            {
                key: _shrink(values, 0.0, shrinkage)
                for key, values in by_weekday.items()
            },
            by_weekday,
        )

        current = snapshot()
        moved = max(
            (abs(value - previous.get(key, 0.0)) for key, value in current.items()),
            default=0.0,
        )
        previous = current
        if moved < tolerance:
            break

    residuals = [
        row.log_price
        - levels[row.watch_id]
        - horizon_value(row.bucket)
        - holiday_value(row.holiday)
        - weekday_value(row.weekday)
        for row in rows
    ]
    sigma = _spread(residuals)

    days = [row.days_out for row in rows]
    return Fit(
        levels=levels,
        horizon=horizon,
        holiday=holiday,
        weekday=weekday,
        sigma=sigma,
        observations=len(rows),
        watches=len({row.watch_id for row in rows}),
        span_days=max(days) - min(days) if days else 0,
    )


def _spread(values: Sequence[float]) -> float:
    """Robust scale: a median absolute deviation, put on a standard-deviation footing."""
    if len(values) < 3:
        return 0.0
    middle = median(values)
    mad = median([abs(v - middle) for v in values])
    return float(mad) * 1.4826


@dataclass(frozen=True)
class Forecast:
    """A predicted price and how sure the model is, at one future date."""

    ratio: float
    log_sigma: float
    prior_weight: float

    def band(self, current: float, z: float = 1.2816) -> tuple[float, float, float]:
        """Median, low and high. The default z is an 80% interval."""
        expected = current * self.ratio
        return (
            expected,
            expected * math.exp(-z * self.log_sigma),
            expected * math.exp(z * self.log_sigma),
        )


def forecast(
    model: Fit,
    days_from: int,
    days_to: int,
    departure: Optional[date] = None,
    steps_ahead: float = 1.0,
) -> Forecast:
    """How the price is expected to move between two points in the booking window.

    Only the horizon component moves: for a fixed departure date the level,
    holiday and weekday effects are the same at both ends and cancel. They earn
    their place by keeping those influences out of the horizon estimate, not by
    appearing in this ratio.
    """
    here = model.horizon_at(days_from)
    there = model.horizon_at(days_to)

    # Uncertainty has two parts: the noise a price shows anyway, accumulating
    # as a walk does, and doubt about the curve itself — which is larger where
    # the estimate still leans on the prior.
    #
    # The walk carries a floor because a fare moves week to week whether or not
    # this tool has watched it do so. Without one, a model fitted to a handful
    # of unmoved observations reports a band four months out no wider than the
    # band next week, which is a claim the data cannot support.
    walk = max(model.sigma, WALK_FLOOR) * math.sqrt(max(steps_ahead, 1.0))
    curve_doubt = 0.12 * math.sqrt(here.prior_weight * there.prior_weight)
    return Forecast(
        ratio=math.exp(there.value - here.value),
        log_sigma=math.sqrt(walk * walk + curve_doubt * curve_doubt),
        prior_weight=(here.prior_weight + there.prior_weight) / 2,
    )
