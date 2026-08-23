"""Importing Google's own price history, so a watch is not blind on day one.

A new watch needs ~20 runs before its statistics mean anything, which is about
ten days at twice daily. Google already knows what the flight has cost for the
past couple of months — it is the graph on the search page — but that data does
not come back in the page this tool scrapes. It is available through SearchAPI,
a paid Google Flights proxy that `fast-flights` already integrates.

The economics suit a one-off: this is a single request per watch, not per run.

Imported prices are marked as imported, never overwrite a price this tool
observed itself, and can be re-run safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from sqlite3 import Connection
from typing import Optional, Protocol

from .config import Settings, Watch
from .store import Observation, record_imported, to_iso

SOURCE = "searchapi"

# Imported points are daily and carry no time of day. Noon keeps them clear of
# the scheduled runs at 07:00 and 19:00, so an import can never land on the same
# timestamp as a real observation and be mistaken for one.
IMPORT_HOUR = 12


@dataclass(frozen=True)
class PricePoint:
    day: date
    price: float


@dataclass(frozen=True)
class History:
    """What the provider knows about one origin/destination/date search."""

    points: tuple[PricePoint, ...] = ()
    typical_low: Optional[float] = None
    typical_high: Optional[float] = None
    level: Optional[str] = None
    currency: str = "USD"


class HistoryProvider(Protocol):
    """One historical lookup. Implemented by SearchAPI and by test doubles."""

    def history(
        self, watch: Watch, depart: date, back: Optional[date]
    ) -> History: ...


def parse_point_date(value) -> Optional[date]:
    """Accept what a price-history entry might carry for its date.

    `iso_date` is the documented field, but an epoch seconds value is the other
    shape these APIs commonly return, and a full timestamp is not unusual.
    Guessing wrong here would silently place a year of history on one day.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit() and len(text) >= 9:
        return datetime.fromtimestamp(int(text), tz=timezone.utc).date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


class SearchApiHistory:
    """`fast-flights`' SearchAPI integration, narrowed to the history series."""

    def __init__(self, api_key: Optional[str] = None, currency: str = "USD"):
        self.api_key = api_key
        self.currency = currency

    def history(self, watch: Watch, depart: date, back: Optional[date]) -> History:
        # Imported lazily so `backfill --dry-run`, the tests and every other
        # command work without the scraping stack or an API key.
        from fast_flights import FlightQuery, Passengers, create_query, get_flights
        from fast_flights.integrations.searchapi import SearchApi

        legs = [
            FlightQuery(
                date=depart.isoformat(),
                from_airport=watch.origin,
                to_airport=watch.destination,
                max_stops=watch.max_stops,
            )
        ]
        if back is not None:
            legs.append(
                FlightQuery(
                    date=back.isoformat(),
                    from_airport=watch.destination,
                    to_airport=watch.origin,
                    max_stops=watch.max_stops,
                )
            )

        query = create_query(
            flights=legs,
            seat=watch.cabin,
            trip="round-trip" if back is not None else "one-way",
            passengers=Passengers(
                adults=watch.passengers.adults,
                children=watch.passengers.children,
                infants_in_seat=watch.passengers.infants_in_seat,
                infants_on_lap=watch.passengers.infants_on_lap,
            ),
            currency=self.currency,
        )

        result = get_flights(query, integration=SearchApi(api_key=self.api_key))
        insights = getattr(result, "price_insights", None)
        if insights is None:
            return History(currency=self.currency)

        points = []
        for entry in getattr(insights, "price_history", None) or []:
            day = parse_point_date(getattr(entry, "iso_date", None))
            price = getattr(entry, "price", None)
            if day is None or not isinstance(price, (int, float)) or price <= 0:
                continue
            points.append(PricePoint(day, float(price)))

        span = getattr(insights, "typical_price_range", None) or (None, None)
        return History(
            points=tuple(sorted(points, key=lambda p: p.day)),
            typical_low=span[0],
            typical_high=span[1],
            level=getattr(insights, "price_level", None) or None,
            currency=self.currency,
        )


@dataclass(frozen=True)
class Imported:
    watch: Watch
    depart: date
    back: Optional[date]
    history: History
    written: int
    considered: int

    @property
    def skipped(self) -> int:
        return self.considered - self.written

    def describe(self) -> list[str]:
        watch = self.watch
        dates = self.depart.isoformat()
        if self.back:
            dates += f" to {self.back.isoformat()}"

        if not self.history.points:
            return [
                f"{watch.name}  [{watch.id}]",
                f"  {dates}: the provider returned no price history",
            ]

        prices = [p.price for p in self.history.points]
        first, last = self.history.points[0].day, self.history.points[-1].day
        unit = self.history.currency
        lines = [
            f"{watch.name}  [{watch.id}]",
            f"  {dates}: {len(self.history.points)} day(s) of history, "
            f"{first} to {last}",
            f"  range {unit} {min(prices):,.0f} to {unit} {max(prices):,.0f}",
        ]
        if self.history.typical_low and self.history.typical_high:
            lines.append(
                f"  provider calls {unit} {self.history.typical_low:,.0f}-"
                f"{self.history.typical_high:,.0f} typical"
                + (f", currently {self.history.level}" if self.history.level else "")
            )
        lines.append(
            f"  imported {self.written}"
            + (
                f", left {self.skipped} already present"
                if self.skipped
                else ""
            )
        )
        return lines


def choose_search(
    watch: Watch, cheapest: Optional[tuple[str, str]] = None
) -> tuple[date, Optional[date]]:
    """Which of a watch's date combinations to ask about.

    One request covers one departure/return pair, while a watch may search
    thirty. The one the watch is currently cheapest on is the most useful
    single answer, since that is the combination its own price series is
    usually tracking; failing that, the first.
    """
    searches = watch.searches()
    if cheapest:
        depart_text, return_text = cheapest
        for depart, back in searches:
            same_return = (back.isoformat() if back else None) == (return_text or None)
            if depart.isoformat() == depart_text and same_return:
                return (depart, back)
    return searches[0]


def import_history(
    conn: Connection,
    watch: Watch,
    provider: HistoryProvider,
    settings: Settings,
    cheapest: Optional[tuple[str, str]] = None,
    persist: bool = True,
) -> Imported:
    """Fetch and store one watch's price history."""
    depart, back = choose_search(watch, cheapest)
    history = provider.history(watch, depart, back)

    rows = []
    for point in history.points:
        stamp = to_iso(
            datetime.combine(point.day, time(IMPORT_HOUR), tzinfo=timezone.utc)
        )
        rows.append(
            (
                stamp,
                Observation(
                    watch_id=watch.id,
                    price=point.price,
                    depart_date=depart.isoformat(),
                    return_date=back.isoformat() if back else None,
                    currency=history.currency or settings.currency,
                    origin=watch.origin,
                    destination=watch.destination,
                    fare=watch.fare_signature(),
                    source=SOURCE,
                ),
            )
        )

    written = record_imported(conn, rows) if persist else 0
    return Imported(
        watch=watch,
        depart=depart,
        back=back,
        history=history,
        written=written,
        considered=len(rows),
    )
