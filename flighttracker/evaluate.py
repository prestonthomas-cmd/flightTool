"""Scoring the model against the alternatives, on data it was not fitted to.

A model that has never been measured is a claim, not a tool. This runs
rolling-origin validation: walk forward through the stored history, and at each
point fit only on what was known *then*, predict a price some days ahead, and
compare against what actually happened.

Three methods are scored side by side, because a model is only worth its
complexity if it beats the simple thing:

- **naive** — the price will be what it is now. Brutally hard to beat over
  short horizons, and the honest benchmark.
- **prior** — the general advance-purchase shape, with nothing fitted.
- **model** — the fitted additive model.

Two questions get answered. Is the middle of the prediction close (MAPE)? And
is the stated uncertainty honest — does an 80% band actually contain the truth
about 80% of the time (coverage)? A model can be accurate and badly calibrated,
or calibrated and useless; both matter, so both are reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from sqlite3 import Connection
from statistics import fmean
from typing import Optional, Sequence

from .model import PRIOR_HORIZON, Fit, bucket_for, fit, forecast
from .store import HorizonSample, horizon_samples, parse_iso

Z80 = 1.2816


@dataclass(frozen=True)
class Case:
    """One prediction that can be checked: from a known price to a known outcome."""

    watch_id: str
    anchor_on: date
    target_on: date
    days_out_from: int
    days_out_to: int
    departure: date
    price_from: float
    price_to: float

    @property
    def horizon(self) -> int:
        return (self.target_on - self.anchor_on).days


@dataclass(frozen=True)
class Score:
    method: str
    cases: int
    mape: float
    coverage: Optional[float] = None

    def describe(self) -> str:
        covered = f"  {self.coverage:.0%} in band" if self.coverage is not None else ""
        return f"{self.method:<7} {self.mape:>7.1%} MAPE{covered}"


@dataclass(frozen=True)
class HorizonResult:
    horizon: int
    scores: tuple[Score, ...]
    crossings: int = 0

    def by_method(self, name: str) -> Optional[Score]:
        return next((s for s in self.scores if s.method == name), None)

    @property
    def model_beats_naive(self) -> Optional[bool]:
        model, naive = self.by_method("model"), self.by_method("naive")
        if not model or not naive:
            return None
        return model.mape < naive.mape

    @property
    def informative(self) -> bool:
        """Whether this horizon can tell the model apart from naive at all.

        The model only moves a price when the two ends of a case fall in
        different horizon buckets. Where they do not, it predicts no change —
        which *is* the naive prediction — so a tie says nothing about the
        model's quality, only that the test had no room to discriminate.
        """
        model = self.by_method("model")
        if not model or not model.cases:
            return False
        return self.crossings / model.cases >= 0.2


@dataclass(frozen=True)
class Evaluation:
    results: tuple[HorizonResult, ...]
    observations: int
    watches: int
    span_days: int

    @property
    def usable(self) -> bool:
        return any(result.scores for result in self.results)


def build_cases(
    samples: Sequence[HorizonSample], horizon: int, tolerance: int = 1
) -> list[Case]:
    """Every (now, then) pair in the history separated by roughly `horizon` days.

    Pairs are built per watch *and per departure date*, so a case always
    compares the same flight with itself rather than two different itineraries
    that happen to sit in the same watch.
    """
    series: dict[tuple[str, str], dict[date, float]] = {}
    for sample in samples:
        if sample.price <= 0:
            continue
        try:
            observed = parse_iso(sample.observed_on).date()
            departure = date.fromisoformat(sample.depart_date)
        except (ValueError, TypeError):
            continue
        key = (sample.watch_id, sample.depart_date)
        day = series.setdefault(key, {})
        # One price per day: the cheapest, matching what the tool reports.
        if observed not in day or sample.price < day[observed]:
            day[observed] = sample.price

    cases: list[Case] = []
    for (watch_id, depart_text), day in series.items():
        departure = date.fromisoformat(depart_text)
        days = sorted(day)
        for anchor in days:
            wanted = anchor + timedelta(days=horizon)
            target = min(
                (d for d in days if abs((d - wanted).days) <= tolerance),
                key=lambda d: abs((d - wanted).days),
                default=None,
            )
            if target is None or target <= anchor:
                continue
            cases.append(
                Case(
                    watch_id=watch_id,
                    anchor_on=anchor,
                    target_on=target,
                    days_out_from=(departure - anchor).days,
                    days_out_to=(departure - target).days,
                    departure=departure,
                    price_from=day[anchor],
                    price_to=day[target],
                )
            )
    return cases


def _prior_ratio(days_from: int, days_to: int) -> float:
    here = bucket_for(days_from)
    there = bucket_for(days_to)
    if here is None or there is None:
        return 1.0
    return math.exp(PRIOR_HORIZON.get(there, 0.0) - PRIOR_HORIZON.get(here, 0.0))


def evaluate(
    samples: Sequence[HorizonSample],
    horizons: Sequence[int] = (1, 3, 7, 14),
    shrinkage: float = 8.0,
    minimum_train: int = 12,
) -> Evaluation:
    """Walk forward through the history, scoring each method as it goes."""
    results: list[HorizonResult] = []
    fits: dict[date, Fit] = {}

    for horizon in horizons:
        cases = build_cases(samples, horizon)
        errors: dict[str, list[float]] = {"model": [], "naive": [], "prior": []}
        covered: list[bool] = []

        for case in cases:
            # Only what was knowable when the prediction would have been made.
            train = [
                s
                for s in samples
                if parse_iso(s.observed_on).date() <= case.anchor_on
            ]
            if len(train) < minimum_train:
                continue
            if case.anchor_on not in fits:
                fits[case.anchor_on] = fit(train, shrinkage=shrinkage)
            model = fits[case.anchor_on]
            if not model.usable:
                continue

            predicted = forecast(
                model,
                case.days_out_from,
                case.days_out_to,
                steps_ahead=case.horizon / 7.0,
            )
            expected, low, high = predicted.band(case.price_from, Z80)

            errors["model"].append(abs(expected - case.price_to) / case.price_to)
            errors["naive"].append(
                abs(case.price_from - case.price_to) / case.price_to
            )
            errors["prior"].append(
                abs(
                    case.price_from
                    * _prior_ratio(case.days_out_from, case.days_out_to)
                    - case.price_to
                )
                / case.price_to
            )
            covered.append(low <= case.price_to <= high)

        scores = []
        for method in ("model", "naive", "prior"):
            values = errors[method]
            if not values:
                continue
            scores.append(
                Score(
                    method=method,
                    cases=len(values),
                    mape=float(fmean(values)),
                    coverage=(
                        float(fmean(covered)) if method == "model" and covered else None
                    ),
                )
            )
        crossings = sum(
            1
            for case in cases
            if bucket_for(case.days_out_from) != bucket_for(case.days_out_to)
        )
        results.append(
            HorizonResult(horizon=horizon, scores=tuple(scores), crossings=crossings)
        )

    days = [parse_iso(s.observed_on).date() for s in samples]
    return Evaluation(
        results=tuple(results),
        observations=len(samples),
        watches=len({s.watch_id for s in samples}),
        span_days=(max(days) - min(days)).days if days else 0,
    )


def evaluate_stored(conn: Connection, **kwargs) -> Evaluation:
    return evaluate(horizon_samples(conn), **kwargs)


def format_evaluation(evaluation: Evaluation) -> list[str]:
    lines = [
        f"Rolling-origin validation — {evaluation.observations} observations, "
        f"{evaluation.watches} watch(es), {evaluation.span_days} days of history.",
        "Each prediction is made from what was known at the time, and checked "
        "against what happened.",
        "",
        f"{'horizon':>8} {'cases':>6} {'model':>9} {'naive':>9} {'prior':>9} "
        f"{'in 80% band':>12}",
        "  ".join(["-" * 8, "-" * 5, "-" * 8, "-" * 8, "-" * 8, "-" * 11]),
    ]

    scored = False
    for result in evaluation.results:
        model = result.by_method("model")
        naive = result.by_method("naive")
        prior = result.by_method("prior")
        if not model:
            lines.append(f"{result.horizon:>7}d {'—':>6}   not enough history yet")
            continue
        scored = True
        lines.append(
            f"{result.horizon:>7}d {model.cases:>6} {model.mape:>8.1%} "
            f"{(naive.mape if naive else float('nan')):>8.1%} "
            f"{(prior.mape if prior else float('nan')):>8.1%} "
            f"{(model.coverage if model.coverage is not None else 0):>11.0%}"
        )

    if not scored:
        lines += [
            "",
            "Nothing could be scored yet: validation needs history on both sides "
            "of a prediction.",
        ]
        return lines

    lines.append("")
    for result in evaluation.results:
        verdict = result.model_beats_naive
        if verdict is None:
            continue
        model, naive = result.by_method("model"), result.by_method("naive")
        if not result.informative:
            share = result.crossings / model.cases
            lines.append(
                f"At {result.horizon}d the comparison is not informative: only "
                f"{share:.0%} of cases ({result.crossings} of {model.cases}) "
                "span a change in the booking window, so the model is "
                "predicting no change — the naive prediction — almost "
                f"everywhere. Both score {model.mape:.1%}; neither is being "
                "tested. It becomes a real test once the history reaches "
                "further into the booking window."
            )
        elif verdict:
            lines.append(
                f"At {result.horizon}d the model beats naive "
                f"({model.mape:.1%} vs {naive.mape:.1%}), over "
                f"{result.crossings} cases that span a change in the window."
            )
        else:
            lines.append(
                f"At {result.horizon}d the model does NOT beat naive "
                f"({model.mape:.1%} vs {naive.mape:.1%}) across "
                f"{result.crossings} cases that span a change in the window. "
                "That is a real miss, not an artefact of the test."
            )

    coverage = [
        r.by_method("model").coverage
        for r in evaluation.results
        if r.by_method("model") and r.by_method("model").coverage is not None
    ]
    if coverage:
        average = fmean(coverage)
        lines.append("")
        if average < 0.7:
            lines.append(
                f"Calibration: {average:.0%} of outcomes landed in the 80% band. "
                "The band is too narrow — the model is overconfident."
            )
        elif average > 0.92:
            lines.append(
                f"Calibration: {average:.0%} of outcomes landed in the 80% band. "
                "The band is wider than it needs to be."
            )
        else:
            lines.append(
                f"Calibration: {average:.0%} of outcomes landed in the 80% band, "
                "against 80% intended."
            )
    return lines
