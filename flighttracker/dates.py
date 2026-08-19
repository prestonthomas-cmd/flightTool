"""Turning the date ranges in the watchlist into concrete dates to query."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Optional, Sequence, Tuple

DateCombination = Tuple[date, Optional[date]]


def coerce_date(value) -> date:
    """Accept what YAML gives us for a date: a `date`, a `datetime`, or a string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"expected a date, got {value!r}")


def expand_range(bounds: Sequence[date]) -> list[date]:
    """Every date from the first bound to the last, inclusive.

    A one-element range is a single fixed date, which is the common case for a
    trip whose dates are already settled.
    """
    if len(bounds) == 1:
        return [bounds[0]]
    start, end = bounds[0], bounds[-1]
    if end < start:
        raise ValueError(f"range ends ({end}) before it starts ({start})")
    span = (end - start).days
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def combinations(
    depart_dates: Iterable[date],
    return_dates: Optional[Iterable[date]],
    trip_length_nights: Optional[Tuple[int, int]] = None,
) -> list[DateCombination]:
    """Pair up departure and return dates into the searches to actually run.

    With no return dates this is a one-way watch and each departure stands
    alone. Otherwise every departure is paired with every return that falls
    after it, optionally narrowed to a trip length in nights — which is the
    lever that keeps a wide pair of ranges from exploding into hundreds of
    searches.
    """
    departures = sorted(depart_dates)
    if return_dates is None:
        return [(day, None) for day in departures]

    returns = sorted(return_dates)
    paired: list[DateCombination] = []
    for out in departures:
        for back in returns:
            nights = (back - out).days
            if nights < 1:
                continue
            if trip_length_nights is not None:
                low, high = trip_length_nights
                if nights < low or nights > high:
                    continue
            paired.append((out, back))
    return paired


def iso(value: Optional[date]) -> Optional[str]:
    """Render a date the way it is stored: ISO 8601, or NULL for one-way."""
    return value.isoformat() if value is not None else None


def describe(combination: DateCombination) -> str:
    """A short human label for one search, for emails and log lines."""
    out, back = combination
    if back is None:
        return out.isoformat()
    return f"{out.isoformat()} to {back.isoformat()} ({(back - out).days}n)"


def iter_dates(start: date, end: date) -> Iterator[date]:
    """Every date in a closed interval."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
