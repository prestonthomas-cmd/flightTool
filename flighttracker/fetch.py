"""Getting current prices out of Google Flights.

Everything that knows about `fast_flights` lives here, behind a small `Quote`
result. Scraping is the fragile part of this tool — the layout can change and
requests can be throttled — so the rest of the package never imports it
directly, and the tests drive a stub fetcher instead of the network.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional, Protocol, Sequence
from urllib.parse import quote_plus

from .config import Settings, Watch
from .dates import DateCombination, describe, iso
from .errors import FetchError
from .store import Observation

# Errors that cannot possibly come good on a retry, and that will hit every
# other search in the run identically. A missing dependency is the case that
# proved this: one broken import turned into 105 attempts over twelve minutes,
# with the real message buried under the noise.
PERMANENT = (ImportError,)


@dataclass(frozen=True)
class Quote:
    """The cheapest itinerary found for one date combination."""

    watch_id: str
    depart_date: date
    return_date: Optional[date]
    price: float
    currency: str
    airlines: tuple[str, ...] = ()
    stops: Optional[int] = None
    duration_minutes: Optional[int] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    fare: Optional[str] = None

    def to_observation(self) -> Observation:
        return Observation(
            watch_id=self.watch_id,
            price=self.price,
            depart_date=iso(self.depart_date),
            return_date=iso(self.return_date),
            currency=self.currency,
            airlines=", ".join(self.airlines) or None,
            stops=self.stops,
            duration_minutes=self.duration_minutes,
            origin=self.origin,
            destination=self.destination,
            fare=self.fare,
        )


@dataclass(frozen=True)
class Failure:
    watch_id: str
    depart_date: Optional[date]
    return_date: Optional[date]
    message: str


@dataclass(frozen=True)
class RunResult:
    quotes: tuple[Quote, ...]
    failures: tuple[Failure, ...]

    def for_watch(self, watch_id: str) -> list[Quote]:
        return [q for q in self.quotes if q.watch_id == watch_id]

    def cheapest(self, watch_id: str) -> Optional[Quote]:
        found = self.for_watch(watch_id)
        return min(found, key=lambda q: q.price) if found else None

    def failures_for(self, watch_id: str) -> list[Failure]:
        return [f for f in self.failures if f.watch_id == watch_id]


class Fetcher(Protocol):
    """One price lookup. Implemented by the scraper and by test doubles."""

    def fetch(
        self, watch: Watch, depart: date, back: Optional[date]
    ) -> Optional[Quote]: ...


class GoogleFlightsFetcher:
    """`fast_flights` wrapped so a search returns the cheapest fare, or nothing."""

    def __init__(self, currency: str = "USD", proxy: Optional[str] = None):
        self.currency = currency
        self.proxy = proxy

    def fetch(
        self, watch: Watch, depart: date, back: Optional[date]
    ) -> Optional[Quote]:
        # Imported lazily so that validating a watchlist, reading history or
        # running the tests never needs the scraping stack installed.
        from fast_flights import FlightQuery, Passengers, create_query, get_flights

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
            max_stops=watch.max_stops,
            carry_on_bags=watch.carry_on_bags,
            checked_bags=watch.checked_bags,
            exclude_basic_economy=watch.exclude_basic_economy,
            hide_separate_and_self_transfer=watch.hide_separate_and_self_transfer,
        )

        results = get_flights(query, proxy=self.proxy)
        return self._cheapest(watch, depart, back, results)

    def _cheapest(self, watch, depart, back, results) -> Optional[Quote]:
        best = None
        for itinerary in results or []:
            price = getattr(itinerary, "price", None)
            # Google leaves the price out of some itineraries entirely; those
            # are not comparable and are skipped rather than counted as zero.
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            if best is None or price < best.price:
                best = _quote_from(watch, depart, back, itinerary, self.currency)
        return best


def _quote_from(watch, depart, back, itinerary, currency) -> Quote:
    segments = list(getattr(itinerary, "flights", []) or [])
    durations = [
        s.duration for s in segments if isinstance(getattr(s, "duration", None), int)
    ]
    airlines = tuple(str(a) for a in (getattr(itinerary, "airlines", None) or []))
    return Quote(
        watch_id=watch.id,
        depart_date=depart,
        return_date=back,
        price=float(itinerary.price),
        currency=currency,
        airlines=airlines,
        origin=watch.origin,
        destination=watch.destination,
        # Segments cover the itinerary Google prices on the results page; for a
        # round trip that is the outbound leg, the return being chosen later.
        stops=max(len(segments) - 1, 0) if segments else None,
        duration_minutes=sum(durations) or None,
    )


def collect(
    watches: Sequence[Watch],
    fetcher: Fetcher,
    settings: Settings,
    sleep: Callable[[float], None] = time.sleep,
    rng: Optional[random.Random] = None,
    on_event: Optional[Callable[[str], None]] = None,
) -> RunResult:
    """Price every date combination of every watch, spacing out the requests."""
    rng = rng or random.Random()
    note = on_event or (lambda _message: None)

    quotes: list[Quote] = []
    failures: list[Failure] = []
    first = True

    for watch in watches:
        searches = watch.searches()
        note(f"{watch.name}: {len(searches)} search(es)")
        for combination in searches:
            if not first:
                sleep(_pause(settings, rng))
            first = False
            depart, back = combination
            try:
                quote = _with_retries(
                    fetcher, watch, combination, settings, sleep, note
                )
            except FetchError as exc:
                note(f"  {describe(combination)}: {exc}")
                failures.append(Failure(watch.id, depart, back, str(exc)))
                if exc.permanent:
                    note(
                        "  this affects every search in the run — stopping "
                        "rather than repeating it"
                    )
                    return RunResult(tuple(quotes), tuple(failures))
                continue
            if quote is None:
                message = "no priced itinerary returned"
                note(f"  {describe(combination)}: {message}")
                failures.append(Failure(watch.id, depart, back, message))
                continue
            note(f"  {describe(combination)}: {quote.currency} {quote.price:,.0f}")
            quotes.append(quote)

    return RunResult(tuple(quotes), tuple(failures))


def _pause(settings: Settings, rng: random.Random) -> float:
    jitter = max(settings.request_jitter_seconds, 0.0)
    return max(settings.request_delay_seconds + rng.uniform(0, jitter), 0.0)


def _with_retries(
    fetcher: Fetcher,
    watch: Watch,
    combination: DateCombination,
    settings: Settings,
    sleep: Callable[[float], None],
    note: Callable[[str], None],
) -> Optional[Quote]:
    depart, back = combination
    attempts = max(settings.max_retries, 1)
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            return fetcher.fetch(watch, depart, back)
        except PERMANENT as exc:
            raise FetchError(
                f"{type(exc).__name__}: {exc}", permanent=True
            ) from exc
        except Exception as exc:  # noqa: BLE001 - the scraper raises freely
            last_error = exc
            if attempt == attempts:
                break
            delay = settings.retry_backoff_seconds * (2 ** (attempt - 1))
            note(
                f"  {describe(combination)}: attempt {attempt} failed "
                f"({type(exc).__name__}: {exc}); retrying in {delay:g}s"
            )
            sleep(delay)

    raise FetchError(
        f"{attempts} attempt(s) failed — {type(last_error).__name__}: {last_error}"
    )


def search_url(watch: Watch, depart: date, back: Optional[date]) -> str:
    """A Google Flights link for the search, so the email is one click from booking."""
    phrase = f"Flights from {watch.origin} to {watch.destination} on {depart.isoformat()}"
    if back is not None:
        phrase += f" through {back.isoformat()}"
    return f"https://www.google.com/travel/flights?q={quote_plus(phrase)}"
