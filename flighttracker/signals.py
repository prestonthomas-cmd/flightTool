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

    def with_forecast(self, forecast) -> "Verdict":
        return replace(self, forecast=forecast)

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


def summarize(history: Sequence[RunPoint], watch: Watch, settings: Settings) -> Stats:
    prices = [point.price for point in history]
    if not prices:
        return Stats(count=0)
    q = watch.threshold_percentile(settings)
    return Stats(
        count=len(prices),
        minimum=min(prices),
        maximum=max(prices),
        median=float(median(prices)),
        mean=float(fmean(prices)),
        threshold=percentile(prices, q),
        percentile_used=q,
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
        elif stats.threshold is not None and price <= stats.threshold:
            reasons.append(
                Reason(
                    BELOW_PERCENTILE,
                    f"{money} is in the cheapest {stats.percentile_used:g}% of "
                    f"{stats.count} runs (threshold {currency} "
                    f"{stats.threshold:,.0f}, median {currency} "
                    f"{stats.median:,.0f})",
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
