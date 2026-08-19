"""Deciding whether a price is worth an email.

This is statistics, not prediction: every judgement is made against the watch's
own price history, so a route that is simply expensive does not alert forever
and a cheap one does not stay silent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from statistics import fmean, median
from typing import Optional, Sequence

from .config import Settings, Watch
from .store import RunPoint, SentAlert, parse_iso

ALL_TIME_LOW = "all_time_low"
BELOW_PERCENTILE = "below_percentile"
UNDER_CEILING = "under_ceiling"


def percentile(values: Sequence[float], q: float) -> float:
    """The q-th percentile by linear interpolation between order statistics.

    The same definition NumPy uses, so the threshold means what a reader
    expects. Written out here to keep the tool dependency-light.
    """
    if not values:
        raise ValueError("percentile of no values")
    if not 0 <= q <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * (q / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


@dataclass(frozen=True)
class Stats:
    """Trailing stats for one watch, computed from runs before the current one."""

    count: int
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    median: Optional[float] = None
    mean: Optional[float] = None
    threshold: Optional[float] = None
    percentile_used: Optional[float] = None
    # Median absolute deviation over the median: how much this watch's price
    # actually moves, as a fraction. Robust to the odd scraped outlier in a way
    # a standard deviation is not.
    volatility: Optional[float] = None
    required_discount: Optional[float] = None

    @property
    def has_history(self) -> bool:
        return self.count > 0


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str


@dataclass(frozen=True)
class Verdict:
    """What the run concluded about one watch."""

    watch: Watch
    price: Optional[float]
    currency: str
    stats: Stats
    reasons: tuple[Reason, ...] = ()
    suppressed: Optional[str] = None
    best_depart: Optional[str] = None
    best_return: Optional[str] = None
    # Filled in after the fact by `run.attach_forecasts`: where the price looks
    # to be heading. Never changes whether the watch is flagged.
    forecast: object = None
    # "raw", or "horizon-adjusted" when the history was re-based onto today's
    # point in the booking window before being judged.
    baseline: str = "raw"

    @property
    def horizon_adjusted(self) -> bool:
        return self.baseline == "horizon-adjusted"

    def with_forecast(self, forecast) -> "Verdict":
        return replace(self, forecast=forecast)

    def with_baseline(self, baseline: str) -> "Verdict":
        return replace(self, baseline=baseline)

    @property
    def flagged(self) -> bool:
        return bool(self.reasons) and self.suppressed is None

    @property
    def reason_codes(self) -> str:
        return ",".join(r.code for r in self.reasons)

    @property
    def drop_from_median(self) -> Optional[float]:
        if self.price is None or not self.stats.median:
            return None
        return (self.stats.median - self.price) / self.stats.median


def volatility_of(prices: Sequence[float]) -> Optional[float]:
    """How much this price moves, as a fraction of its typical level.

    Median absolute deviation rather than a standard deviation: one scraped
    outlier should not make a stable fare look volatile.
    """
    if len(prices) < 3:
        return None
    middle = median(prices)
    if middle <= 0:
        return None
    return float(median([abs(price - middle) for price in prices])) / middle


def required_discount(volatility: Optional[float], settings: Settings) -> float:
    """How far below its median a price must sit before it is worth an email."""
    if not settings.adaptive_discount or volatility is None:
        return settings.min_discount
    scaled = volatility * settings.discount_volatility_multiple
    return min(max(scaled, settings.min_discount), settings.max_discount)


def summarize(history: Sequence[RunPoint], watch: Watch, settings: Settings) -> Stats:
    prices = [point.price for point in history]
    if not prices:
        return Stats(count=0)
    q = watch.threshold_percentile(settings)
    swing = volatility_of(prices)
    return Stats(
        count=len(prices),
        minimum=min(prices),
        maximum=max(prices),
        median=float(median(prices)),
        mean=float(fmean(prices)),
        threshold=percentile(prices, q),
        percentile_used=q,
        volatility=swing,
        required_discount=required_discount(swing, settings),
    )


def evaluate(
    watch: Watch,
    price: Optional[float],
    history: Sequence[RunPoint],
    settings: Settings,
    currency: str,
    last: Optional[SentAlert] = None,
    now=None,
    best_depart: Optional[str] = None,
    best_return: Optional[str] = None,
) -> Verdict:
    """Judge this run's price for one watch against its own history."""
    stats = summarize(history, watch, settings)
    if price is None:
        return Verdict(
            watch=watch,
            price=None,
            currency=currency,
            stats=stats,
            suppressed="no price returned this run",
        )

    money = f"{currency} {price:,.0f}"
    reasons: list[Reason] = []

    # A hard ceiling is the user's own judgement, so it stands on its own and
    # does not wait for history to accumulate.
    if watch.max_price_alert is not None and price <= watch.max_price_alert:
        reasons.append(
            Reason(
                UNDER_CEILING,
                f"{money} is at or below your {currency} "
                f"{watch.max_price_alert:,.0f} ceiling",
            )
        )

    required = watch.required_observations(settings)
    if stats.count >= required:
        if stats.minimum is not None and price < stats.minimum:
            reasons.append(
                Reason(
                    ALL_TIME_LOW,
                    f"{money} is the lowest seen in {stats.count} runs — under the "
                    f"previous best of {currency} {stats.minimum:,.0f}",
                )
            )
        elif (
            stats.threshold is not None
            and price <= stats.threshold
            and _is_materially_cheap(price, stats, settings)
        ):
            reasons.append(
                Reason(
                    BELOW_PERCENTILE,
                    f"{money} is in the cheapest {stats.percentile_used:g}% of "
                    f"{stats.count} runs and {stats.required_discount:.0%} or "
                    f"more below its median of {currency} {stats.median:,.0f}"
                    + (
                        f" (a bar set by this watch's own "
                        f"{stats.volatility:.0%} volatility)"
                        if settings.adaptive_discount and stats.volatility
                        else ""
                    ),
                )
            )

    verdict = Verdict(
        watch=watch,
        price=price,
        currency=currency,
        stats=stats,
        reasons=tuple(reasons),
        best_depart=best_depart,
        best_return=best_return,
    )

    if not reasons:
        if stats.count < required:
            return _with_note(
                verdict,
                f"building history — {stats.count} of {required} runs needed before "
                "comparisons mean anything",
            )
        return verdict

    hold = _cooldown(verdict, settings, last, now)
    return _with_note(verdict, hold) if hold else verdict


def _is_materially_cheap(price: float, stats: Stats, settings: Settings) -> bool:
    """Is this price actually below what the watch normally costs?

    Being under the percentile threshold is not enough on its own. A flat
    series puts its 20th percentile exactly on its median, and a series
    carrying one old outlier puts the threshold on its modal price — in both
    cases a completely ordinary price clears the threshold and would alert on
    every run. Requiring a real discount against the median fixes both, and
    unlike a test on the threshold it does not quietly disable higher
    percentile settings, where threshold and median legitimately converge.

    The all-time-low rule needs no such guard: it already demands a strict
    improvement on everything seen before.
    """
    if not stats.median:
        return False
    needed = (
        stats.required_discount
        if stats.required_discount is not None
        else settings.min_discount
    )
    return price <= stats.median * (1 - needed)


def _with_note(verdict: Verdict, note: Optional[str]) -> Verdict:
    if note is None:
        return verdict
    return replace(verdict, suppressed=note)


def _cooldown(
    verdict: Verdict, settings: Settings, last: Optional[SentAlert], now
) -> Optional[str]:
    """Hold back a repeat of an alert already sent, unless the price improved.

    A price sitting at its all-time low satisfies the rule on every run. Without
    this the digest would arrive every few hours saying the same thing, which is
    exactly the noise that gets a tool like this muted.
    """
    if last is None or now is None or verdict.price is None:
        return None
    if settings.alert_cooldown_hours <= 0:
        return None

    since = now - parse_iso(last.timestamp)
    if since >= timedelta(hours=settings.alert_cooldown_hours):
        return None

    improvement = (last.price - verdict.price) / last.price if last.price else 0.0
    if improvement >= settings.alert_improvement:
        return None

    hours = since.total_seconds() / 3600
    return (
        f"already alerted {hours:.0f}h ago at {verdict.currency} {last.price:,.0f}; "
        f"waiting for a {settings.alert_improvement:.0%} drop or "
        f"{settings.alert_cooldown_hours:g}h"
    )
