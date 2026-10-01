"""Running against the hosted watchlist instead of a local YAML file.

The web app owns the watches and the prices; this module is how the scraper
talks to it. Two calls: ask what to search, and hand back what was found.

Everything else — the fetcher, the model, the holiday rules — is unchanged and
unaware. That is the point of keeping this in its own module: the scraper that
was proven against Google Flights is the same code either way, and only its
source of work and destination for results differ.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Sequence

from .config import Watch
from .errors import FlightTrackerError
from .model import Fit
from .model import fit as fit_model
from .model import forecast as model_forecast
from .store import HorizonSample

AIRPORT = re.compile(r"^[A-Z]{3}$")
TIMEOUT = 60
PROJECTION_STEP_DAYS = 7
# Matches the local dashboard's 80% interval, so both halves of the product
# draw a band that means the same thing.
Z80 = 1.2816


class CloudError(FlightTrackerError):
    """The hosted watchlist could not be reached, or refused us."""


@dataclass(frozen=True)
class Cloud:
    """The web app's sync endpoint."""

    base_url: str
    secret: str

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/") + "/api/sync"

    def _call(self, method: str, payload: Optional[dict] = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.secret}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise CloudError(
                    "The sync secret was rejected. Check SYNC_SECRET here "
                    "matches the one set in the web app's environment."
                ) from None
            # The body usually carries a reason; the status alone rarely says
            # enough to act on.
            detail = error.read().decode()[:200]
            raise CloudError(f"{self.url} returned {error.code}: {detail}") from None
        except urllib.error.URLError as error:
            raise CloudError(f"Could not reach {self.url}: {error.reason}") from None

    def watchlist(self) -> tuple[list[Watch], list[HorizonSample]]:
        """What to search, and the history to fit the model against."""
        payload = self._call("GET")
        watches = [as_watch(row) for row in payload.get("watches", [])]
        history = [as_sample(row) for row in payload.get("history", [])]
        return (
            [watch for watch in watches if watch is not None],
            [sample for sample in history if sample is not None],
        )

    def publish(
        self,
        prices: Sequence[dict],
        projections: Sequence[dict],
        run: Optional[dict] = None,
    ) -> dict:
        return self._call(
            "POST",
            {
                "prices": list(prices),
                "projections": list(projections),
                "run": run or {},
            },
        )


def _days(first: str, last: str) -> tuple[date, ...]:
    """Every date in a closed range, as the watch config wants them."""
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return tuple(
        start + timedelta(days=offset) for offset in range((end - start).days + 1)
    )


def as_watch(row: dict) -> Optional[Watch]:
    """One stored watch as the scraper's own type.

    Returns None rather than raising on a row that cannot be read: one bad
    watch should cost its owner a run, not everybody.
    """
    try:
        depart = _days(row["depart_from"], row["depart_to"])
        returns = (
            _days(row["return_from"], row["return_to"])
            if row.get("return_from")
            else None
        )
        origin = str(row["origin"] or "").upper()
        destination = str(row["destination"] or "").upper()
        # Checked here rather than trusted from the database. `str(None)` is
        # the string "None", which would sail through as an airport code and
        # then fail every search this watch ever made.
        if not AIRPORT.match(origin) or not AIRPORT.match(destination):
            return None
        if origin == destination or not depart:
            return None

        return Watch(
            id=str(row["id"]),
            origin=origin,
            destination=destination,
            depart_dates=depart,
            return_dates=returns,
            cabin=str(row.get("cabin") or "economy"),
            max_stops=row.get("max_stops"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def as_sample(row: dict) -> Optional[HorizonSample]:
    """A stored price as the model's input."""
    try:
        price = float(row["price"])
        if price <= 0:
            return None
        return HorizonSample(
            watch_id=str(row["watch_id"]),
            origin="",
            destination="",
            observed_on=str(row["observed_at"]),
            depart_date=str(row["depart_date"]),
            price=price,
        )
    except (KeyError, ValueError, TypeError):
        return None


def fit_against(history: Iterable[HorizonSample]) -> Fit:
    """One model across everybody's prices.

    Pooling is what makes the booking-horizon curve work at all. It needs
    watches sitting at different distances from departure, and one person with
    three trips rarely has them — a few hundred people do. Nothing identifying
    is pooled: the model sees a price, a date and an opaque id.
    """
    return fit_model(list(history))


def project_for(
    watch: Watch, model: Fit, current: float, now: datetime
) -> list[dict]:
    """Where this watch's price is expected to go, as rows to store.

    Only the horizon component moves the line — level, holiday and weekday are
    the same at both ends of a fixed departure and cancel — so this is the same
    projection the local dashboard draws, written down instead of rendered.
    """
    if current <= 0 or not watch.depart_dates:
        return []

    departure = min(watch.depart_dates)
    days_out = (departure - now.date()).days
    if days_out < PROJECTION_STEP_DAYS:
        return []

    steps = list(range(PROJECTION_STEP_DAYS, days_out + 1, PROJECTION_STEP_DAYS))
    if steps and steps[-1] != days_out:
        steps.append(days_out)

    evidence = model.evidence()
    rows = []
    for ahead in steps:
        step = model_forecast(
            model, days_out, days_out - ahead, steps_ahead=ahead / 7.0
        )
        expected, low, high = step.band(current, z=Z80)
        rows.append(
            {
                "watch_id": watch.id,
                "day": (now.date() + timedelta(days=ahead)).isoformat(),
                "price": round(expected, 2),
                "low": round(max(low, 1.0), 2),
                "high": round(high, 2),
                "evidence": round(evidence, 4),
            }
        )
    return rows
