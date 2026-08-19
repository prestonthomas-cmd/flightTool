"""Shared helpers: building watches and standing in for the scraper."""

from __future__ import annotations

from datetime import date
from typing import Optional

from flighttracker.config import Config, Settings, Watch
from flighttracker.fetch import Quote


def make_watch(
    watch_id: str = "test",
    origin: str = "JFK",
    destination: str = "HND",
    depart=(date(2026, 12, 10),),
    returns=(date(2026, 12, 24),),
    **kwargs,
) -> Watch:
    return Watch(
        id=watch_id,
        origin=origin,
        destination=destination,
        depart_dates=tuple(depart),
        return_dates=tuple(returns) if returns is not None else None,
        **kwargs,
    )


def make_config(*watches: Watch, **settings) -> Config:
    return Config(settings=Settings(**settings), watches=tuple(watches))


class StubFetcher:
    """Returns scripted prices, or raises scripted errors, per search."""

    def __init__(self, prices=None, default=None, errors=None, currency="USD"):
        self.prices = prices or {}
        self.default = default
        self.errors = errors or {}
        self.currency = currency
        self.calls: list[tuple[str, date, Optional[date]]] = []

    def fetch(self, watch, depart, back) -> Optional[Quote]:
        key = (watch.id, depart, back)
        self.calls.append(key)

        error = self.errors.get(key) or self.errors.get(watch.id)
        if error is not None:
            if isinstance(error, list):
                if error:
                    raised = error.pop(0)
                    if raised is not None:
                        raise raised
            else:
                raise error

        price = self.prices.get(key, self.prices.get(watch.id, self.default))
        if price is None:
            return None
        return Quote(
            watch_id=watch.id,
            depart_date=depart,
            return_date=back,
            price=float(price),
            currency=self.currency,
            airlines=("Test Air",),
            stops=0,
            duration_minutes=780,
            origin=watch.origin,
            destination=watch.destination,
            fare=watch.fare_signature(),
        )


def no_sleep(_seconds: float) -> None:
    """Drop-in for `time.sleep` so tests never actually wait."""
