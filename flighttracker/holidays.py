"""US holiday dates, computed by rule for any year.

Holiday travel demand tracks the *holiday*, not the calendar date: the
Wednesday before Thanksgiving is expensive whether that falls on 26 November
or 22 November. Everything here exists so a price can be compared against the
same point in a previous year's holiday, rather than the same date.

The rules are what generates the dates, so "historic holiday schedules" comes
free — `holidays_for(2019)` is as correct as `holidays_for(2027)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

MONDAY, THURSDAY = 0, 3

# How close a date has to be to a major holiday before that holiday is what is
# driving its price. Wider than this and almost every date in the year gets
# labelled — at 45 days only one day in 2026 came back clean, which makes the
# label meaningless. Three weeks is about where holiday demand actually shows
# up in airfares.
NEARBY_DAYS = 21


@dataclass(frozen=True)
class Holiday:
    name: str
    day: date
    # How many days either side of the holiday carry holiday pricing. Christmas
    # runs long because the trip spans the New Year; Veterans Day barely moves
    # airfares at all and gets no window.
    peak_before: int = 0
    peak_after: int = 0
    major: bool = False

    @property
    def peak_start(self) -> date:
        return self.day - timedelta(days=self.peak_before)

    @property
    def peak_end(self) -> date:
        return self.day + timedelta(days=self.peak_after)

    def covers(self, day: date) -> bool:
        return self.peak_start <= day <= self.peak_end

    @property
    def has_peak(self) -> bool:
        return self.peak_before > 0 or self.peak_after > 0


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth given weekday of a month, e.g. the 4th Thursday of November."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    """The last given weekday of a month, e.g. the last Monday of May."""
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Gregorian Easter, by the anonymous computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def holidays_for(year: int) -> list[Holiday]:
    """Every holiday this tool knows about, for one year.

    Peak windows are deliberately asymmetric: people fly out before
    Thanksgiving and home after it, and the Christmas window has to reach past
    New Year because that is one trip, not two.
    """
    thanksgiving = nth_weekday(year, 11, THURSDAY, 4)
    easter_sunday = easter(year)

    return [
        Holiday("New Year's Day", date(year, 1, 1), 3, 4, major=True),
        Holiday("Martin Luther King Jr. Day", nth_weekday(year, 1, MONDAY, 3), 3, 1),
        Holiday("Presidents' Day", nth_weekday(year, 2, MONDAY, 3), 3, 1),
        Holiday("Good Friday", easter_sunday - timedelta(days=2)),
        Holiday("Easter", easter_sunday, 7, 2, major=True),
        Holiday("Memorial Day", last_weekday(year, 5, MONDAY), 4, 1, major=True),
        Holiday("Juneteenth", date(year, 6, 19)),
        Holiday("Independence Day", date(year, 7, 4), 4, 4, major=True),
        Holiday("Labor Day", nth_weekday(year, 9, MONDAY, 1), 4, 1, major=True),
        Holiday("Indigenous Peoples' / Columbus Day", nth_weekday(year, 10, MONDAY, 2), 3, 1),
        Holiday("Veterans Day", date(year, 11, 11)),
        Holiday("Thanksgiving", thanksgiving, 3, 5, major=True),
        Holiday("Christmas", date(year, 12, 25), 8, 3, major=True),
        Holiday("New Year's Eve", date(year, 12, 31), 2, 1),
    ]


def _around(day: date) -> list[Holiday]:
    """Holidays from the neighbouring years too, so late December works."""
    found: list[Holiday] = []
    for year in (day.year - 1, day.year, day.year + 1):
        found.extend(holidays_for(year))
    return found


def nearest_holiday(
    day: date, major_only: bool = False, within_days: Optional[int] = None
) -> Optional[tuple[Holiday, int]]:
    """The closest holiday and its signed distance: negative means it is ahead.

    `-3` reads as "three days before the holiday", which is the direction
    airfares care about.
    """
    candidates: Iterable[Holiday] = _around(day)
    if major_only:
        candidates = [h for h in candidates if h.major]

    best: Optional[tuple[Holiday, int]] = None
    for holiday in candidates:
        offset = (day - holiday.day).days
        if within_days is not None and abs(offset) > within_days:
            continue
        if best is None or abs(offset) < abs(best[1]):
            best = (holiday, offset)
    return best


def peak_window(day: date) -> Optional[Holiday]:
    """The holiday whose travel peak covers this date, if any.

    Where windows overlap — Christmas and New Year's Eve always do — the one
    whose own day is closest wins, so a 27 December flight is reported against
    the holiday it is actually between.
    """
    covering = [h for h in _around(day) if h.has_peak and h.covers(day)]
    if not covering:
        return None
    return min(covering, key=lambda h: (abs((day - h.day).days), h.name))


def is_peak(day: date) -> bool:
    return peak_window(day) is not None


def describe(day: date) -> str:
    """A short phrase for the email and the dashboard."""
    window = peak_window(day)
    if window is not None:
        offset = (day - window.day).days
        if offset == 0:
            return f"{window.name} itself"
        side = "before" if offset < 0 else "after"
        return f"{abs(offset)}d {side} {window.name} (peak travel)"

    nearest = nearest_holiday(day, major_only=True, within_days=NEARBY_DAYS)
    if nearest is None:
        return "no major holiday nearby"
    holiday, offset = nearest
    side = "before" if offset < 0 else "after"
    return f"{abs(offset)}d {side} {holiday.name}"


def holiday_key(day: date) -> Optional[tuple[str, int]]:
    """A year-independent label for a date: which holiday, and how far off.

    This is what makes cross-year comparison honest — `("Thanksgiving", -2)`
    means the same thing in every year, where "23 November" does not.
    """
    window = peak_window(day)
    if window is not None:
        return (window.name, (day - window.day).days)

    nearest = nearest_holiday(day, major_only=True, within_days=NEARBY_DAYS)
    if nearest is None:
        return None
    holiday, offset = nearest
    return (holiday.name, offset)
