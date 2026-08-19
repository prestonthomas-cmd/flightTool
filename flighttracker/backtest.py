"""Replay a watch's stored history against a candidate configuration.

Tuning `percentile` and `min_observations` by intuition means waiting weeks to
find out the answer was wrong. This runs the *same* decision code over the
history already collected and reports what it would have done: how often it
would have emailed, what it would have told you to buy at, and how that
compares to the best price the watch ever saw.

It measures the alerting rule, not the future. A rule that looks good on one
watch's history is not thereby a prediction — it is a description of what the
rule would have done, which is the honest and useful thing to know.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from sqlite3 import Connection
from statistics import median
from typing import Optional, Sequence

from .config import Settings, Watch
from .forecast import rebase
from .signals import evaluate
from .store import SentAlert, horizon_samples, parse_iso, run_history


@dataclass(frozen=True)
class Alert:
    timestamp: str
    price: float
    reasons: str


@dataclass(frozen=True)
class Result:
    watch: Watch
    settings: Settings
    runs: int
    alerts: tuple[Alert, ...]
    best_price: Optional[float] = None
    best_at: Optional[str] = None
    median_price: Optional[float] = None
    last_price: Optional[float] = None
    span_days: int = 0
    horizon_adjusted: bool = False

    @property
    def first_alert(self) -> Optional[Alert]:
        return self.alerts[0] if self.alerts else None

    @property
    def cheapest_alert(self) -> Optional[Alert]:
        return min(self.alerts, key=lambda a: a.price) if self.alerts else None

    @property
    def premium(self) -> Optional[float]:
        """What acting on the first alert would have cost above the best price.

        The number that matters: an alerting rule is only worth having if the
        price it puts in front of you is close to the best the watch ever saw.
        """
        if self.first_alert is None or self.best_price is None:
            return None
        return self.first_alert.price - self.best_price

    @property
    def premium_fraction(self) -> Optional[float]:
        if self.premium is None or not self.best_price:
            return None
        return self.premium / self.best_price

    @property
    def saving_vs_median(self) -> Optional[float]:
        if self.first_alert is None or self.median_price is None:
            return None
        return self.median_price - self.first_alert.price

    @property
    def alerts_per_month(self) -> Optional[float]:
        if len(self.alerts) < 1 or self.runs < 2:
            return None
        span = self.span_days
        if not span:
            return None
        return len(self.alerts) * 30.0 / span


def run_backtest(
    conn: Connection, watch: Watch, settings: Settings
) -> Result:
    """Replay one watch through the real signal code, run by run.

    One caveat worth stating plainly: with `horizon_adjusted_baseline` on, the
    curve used to re-base each step is built from the whole stored dataset,
    including runs that had not happened yet at the point being replayed. The
    prices being judged are strictly historical, but the adjustment applied to
    them is not, so an adjusted replay flatters itself slightly. Compare an
    adjusted run against a raw one to see the size of the effect, not to
    conclude the adjustment is free.
    """
    history = run_history(conn, watch.id)
    if not history:
        return Result(watch=watch, settings=settings, runs=0, alerts=())

    samples = horizon_samples(conn) if settings.horizon_adjusted_baseline else []
    alerts: list[Alert] = []
    last: Optional[SentAlert] = None
    adjusted_any = False

    for index, point in enumerate(history):
        # Only what was knowable at the time. Feeding the whole series in would
        # let every run "discover" lows that had not happened yet.
        at = parse_iso(point.timestamp)
        earlier, adjusted = rebase(samples, watch, history[:index], settings, at)
        adjusted_any = adjusted_any or adjusted

        verdict = evaluate(
            watch=watch,
            price=point.price,
            history=earlier,
            settings=settings,
            currency="",
            last=last,
            now=at,
        )
        if verdict.flagged:
            alert = Alert(point.timestamp, point.price, verdict.reason_codes)
            alerts.append(alert)
            last = SentAlert(alert.timestamp, alert.price, alert.reasons)

    prices = [p.price for p in history]
    best = min(history, key=lambda p: p.price)
    span = (parse_iso(history[-1].timestamp) - parse_iso(history[0].timestamp)).days

    return Result(
        watch=watch,
        settings=settings,
        runs=len(history),
        alerts=tuple(alerts),
        best_price=best.price,
        best_at=best.timestamp,
        median_price=float(median(prices)),
        last_price=history[-1].price,
        span_days=span,
        horizon_adjusted=adjusted_any,
    )


def sweep(
    conn: Connection,
    watch: Watch,
    settings: Settings,
    percentiles: Sequence[float] = (10, 15, 20, 25, 30),
) -> list[Result]:
    """The same replay at several thresholds, for choosing between them."""
    return [
        run_backtest(conn, watch, replace(settings, percentile=percentile))
        for percentile in percentiles
    ]


def format_result(result: Result, currency: str = "") -> list[str]:
    """A human-readable report for one replay."""
    watch = result.watch
    if result.runs == 0:
        return [f"{watch.name}: no history stored yet."]

    unit = f"{currency} " if currency else ""
    lines = [
        f"{watch.name}  [{watch.id}]",
        f"  {result.runs} runs over {result.span_days} days · "
        f"low {unit}{result.best_price:,.0f} · "
        f"median {unit}{result.median_price:,.0f} · "
        f"latest {unit}{result.last_price:,.0f}",
    ]
    if result.horizon_adjusted:
        lines.append(
            "  baseline re-based onto each run's point in the booking window "
            "(the curve itself uses the whole dataset, so this flatters itself "
            "a little)"
        )

    if not result.alerts:
        lines.append("  would have sent no alerts at all")
        lines.append(
            "  (a threshold this tight never fires — try a higher percentile, "
            "or a lower min_observations if the history is short)"
        )
        return lines

    first = result.first_alert
    rate = result.alerts_per_month
    pace = f" · about {rate:.1f} a month" if rate else ""
    lines.append(f"  would have sent {len(result.alerts)} alert(s){pace}")
    lines.append(
        f"  first at {parse_iso(first.timestamp):%Y-%m-%d} for "
        f"{unit}{first.price:,.0f} ({first.reasons})"
    )

    if result.premium is not None:
        gap = result.premium_fraction or 0
        if result.premium <= 0:
            verdict = "the best price it ever saw"
        else:
            verdict = (
                f"{unit}{result.premium:,.0f} ({gap:.0%}) above the best it ever "
                f"saw, {unit}{result.best_price:,.0f} on "
                f"{parse_iso(result.best_at):%Y-%m-%d}"
            )
        lines.append(f"  buying on that first alert would have been {verdict}")

    if result.saving_vs_median is not None:
        lines.append(
            f"  and {unit}{result.saving_vs_median:,.0f} below the median price"
        )
    return lines


def format_sweep(results: Sequence[Result], currency: str = "") -> list[str]:
    """A comparison table across thresholds."""
    unit = f"{currency} " if currency else ""
    lines = [
        f"{'percentile':>10}  {'alerts':>7}  {'per month':>9}  "
        f"{'first alert':>12}  {'vs best':>10}",
        "  ".join(["-" * 10, "-" * 7, "-" * 9, "-" * 12, "-" * 10]),
    ]
    for result in results:
        first = result.first_alert
        rate = result.alerts_per_month
        lines.append(
            f"{result.settings.percentile:>9.0f}%  {len(result.alerts):>7}  "
            f"{(f'{rate:.1f}' if rate else '-'):>9}  "
            f"{(f'{unit}{first.price:,.0f}' if first else '-'):>12}  "
            f"{(f'+{result.premium_fraction:.0%}' if result.premium_fraction is not None else '-'):>10}"
        )
    return lines
