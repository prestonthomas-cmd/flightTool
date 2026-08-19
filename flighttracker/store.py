"""SQLite storage for observed prices, failures and sent alerts.

One file, no server. `price_history` holds one row per date combination per
run, so the granular record survives; a watch's price *for a run* is the
minimum across that run's rows, which is what the buy signal compares.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
  id           INTEGER PRIMARY KEY,
  watch_id     TEXT    NOT NULL,
  timestamp    TEXT    NOT NULL,
  price        REAL    NOT NULL,
  currency     TEXT,
  depart_date  TEXT,
  return_date  TEXT,
  airlines     TEXT,
  stops        INTEGER,
  duration_minutes INTEGER,
  origin       TEXT,
  destination  TEXT,
  fare         TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS price_history_run_leg
  ON price_history (watch_id, timestamp, depart_date, IFNULL(return_date, ''));

CREATE INDEX IF NOT EXISTS price_history_watch_time
  ON price_history (watch_id, timestamp);

CREATE TABLE IF NOT EXISTS fetch_errors (
  id          INTEGER PRIMARY KEY,
  watch_id    TEXT NOT NULL,
  timestamp   TEXT NOT NULL,
  depart_date TEXT,
  return_date TEXT,
  message     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS fetch_errors_watch_time
  ON fetch_errors (watch_id, timestamp);

CREATE TABLE IF NOT EXISTS alerts (
  id          INTEGER PRIMARY KEY,
  watch_id    TEXT NOT NULL,
  timestamp   TEXT NOT NULL,
  price       REAL NOT NULL,
  reasons     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS alerts_watch_time ON alerts (watch_id, timestamp);

CREATE INDEX IF NOT EXISTS price_history_route
  ON price_history (origin, destination);
"""

# Columns added after the first release. A database written by an older version
# is upgraded in place on open, so an existing price history is never lost to a
# schema change.
LATER_COLUMNS = {
    "price_history": {"origin": "TEXT", "destination": "TEXT", "fare": "TEXT"},
}


@dataclass(frozen=True)
class Observation:
    """One priced date combination, as stored."""

    watch_id: str
    price: float
    depart_date: Optional[str]
    return_date: Optional[str]
    currency: Optional[str] = None
    airlines: Optional[str] = None
    stops: Optional[int] = None
    duration_minutes: Optional[int] = None
    # Denormalised from the watch so the forecast can pool observations by
    # route without needing the watchlist that produced them.
    origin: Optional[str] = None
    destination: Optional[str] = None
    # What this price actually buys — see `Watch.fare_signature`.
    fare: Optional[str] = None


@dataclass(frozen=True)
class HorizonSample:
    """One observation, reduced to what the booking-horizon curve needs."""

    watch_id: str
    origin: Optional[str]
    destination: Optional[str]
    observed_on: str
    depart_date: str
    price: float


@dataclass(frozen=True)
class RunPoint:
    """A watch's cheapest price in one run."""

    timestamp: str
    price: float


@dataclass(frozen=True)
class SentAlert:
    timestamp: str
    price: float
    reasons: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Second-resolution UTC, so run timestamps group cleanly and sort as text."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database, creating the file and schema if they are not there."""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any columns a database written by an older version is missing."""
    added = []
    for table, columns in LATER_COLUMNS.items():
        present = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, kind in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                added.append(f"{table}.{name}")
    if added:
        conn.commit()
    return added


def record_observations(
    conn: sqlite3.Connection, timestamp: str, observations: Iterable[Observation]
) -> int:
    """Write a run's prices. Re-running the same timestamp overwrites, not doubles."""
    rows = [
        (
            o.watch_id,
            timestamp,
            float(o.price),
            o.currency,
            o.depart_date,
            o.return_date,
            o.airlines,
            o.stops,
            o.duration_minutes,
            o.origin,
            o.destination,
            o.fare,
        )
        for o in observations
    ]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            """
            INSERT INTO price_history
              (watch_id, timestamp, price, currency, depart_date, return_date,
               airlines, stops, duration_minutes, origin, destination, fare)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (watch_id, timestamp, depart_date, IFNULL(return_date, ''))
            DO UPDATE SET
              price = excluded.price,
              currency = excluded.currency,
              airlines = excluded.airlines,
              stops = excluded.stops,
              duration_minutes = excluded.duration_minutes,
              origin = excluded.origin,
              destination = excluded.destination,
              fare = excluded.fare
            """,
            rows,
        )
    return len(rows)


def record_errors(
    conn: sqlite3.Connection, timestamp: str, failures: Sequence[tuple]
) -> int:
    """`failures` is a sequence of (watch_id, depart_date, return_date, message)."""
    if not failures:
        return 0
    with conn:
        conn.executemany(
            """
            INSERT INTO fetch_errors
              (watch_id, timestamp, depart_date, return_date, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(w, timestamp, d, r, m) for (w, d, r, m) in failures],
        )
    return len(failures)


def run_history(
    conn: sqlite3.Connection, watch_id: str, before: Optional[str] = None
) -> list[RunPoint]:
    """Each past run's cheapest price for this watch, oldest first.

    `before` excludes the run being evaluated, so a price never gets compared
    against a history that already contains it.
    """
    sql = (
        "SELECT timestamp, MIN(price) AS price FROM price_history WHERE watch_id = ?"
    )
    params: list = [watch_id]
    if before is not None:
        sql += " AND timestamp < ?"
        params.append(before)
    sql += " GROUP BY timestamp ORDER BY timestamp"
    return [RunPoint(row["timestamp"], row["price"]) for row in conn.execute(sql, params)]


def last_alert(conn: sqlite3.Connection, watch_id: str) -> Optional[SentAlert]:
    row = conn.execute(
        "SELECT timestamp, price, reasons FROM alerts WHERE watch_id = ?"
        " ORDER BY timestamp DESC LIMIT 1",
        (watch_id,),
    ).fetchone()
    if row is None:
        return None
    return SentAlert(row["timestamp"], row["price"], row["reasons"])


def record_alerts(
    conn: sqlite3.Connection, timestamp: str, alerts: Sequence[tuple]
) -> int:
    """`alerts` is a sequence of (watch_id, price, reasons)."""
    if not alerts:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO alerts (watch_id, timestamp, price, reasons)"
            " VALUES (?, ?, ?, ?)",
            [(w, timestamp, float(p), r) for (w, p, r) in alerts],
        )
    return len(alerts)


def known_watch_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        row["watch_id"]
        for row in conn.execute(
            "SELECT DISTINCT watch_id FROM price_history ORDER BY watch_id"
        )
    ]


def observations_for_run(
    conn: sqlite3.Connection, watch_id: str, timestamp: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM price_history WHERE watch_id = ? AND timestamp = ?"
            " ORDER BY price",
            (watch_id, timestamp),
        )
    )


def horizon_samples(conn: sqlite3.Connection) -> list[HorizonSample]:
    """Every stored price, reduced to what the booking-horizon curve needs."""
    return [
        HorizonSample(
            watch_id=row["watch_id"],
            origin=row["origin"],
            destination=row["destination"],
            observed_on=row["timestamp"],
            depart_date=row["depart_date"],
            price=row["price"],
        )
        for row in conn.execute(
            "SELECT watch_id, origin, destination, timestamp, depart_date, price"
            " FROM price_history WHERE depart_date IS NOT NULL"
        )
    ]


def latest_run(conn: sqlite3.Connection, watch_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(timestamp) AS t FROM price_history WHERE watch_id = ?",
        (watch_id,),
    ).fetchone()
    return row["t"] if row and row["t"] else None


def last_success(conn: sqlite3.Connection, watch_id: str) -> Optional[str]:
    """When this watch last produced a price, as opposed to merely being run."""
    row = conn.execute(
        "SELECT MAX(timestamp) AS t FROM price_history WHERE watch_id = ?",
        (watch_id,),
    ).fetchone()
    return row["t"] if row and row["t"] else None


def fares_seen(conn: sqlite3.Connection, watch_id: str) -> list[tuple[str, int, str]]:
    """Every fare signature stored for a watch: (signature, rows, last seen).

    More than one row here means the history mixes products, and the statistics
    built on it are comparing prices that were never comparable.
    """
    return [
        (row["fare"], row["rows"], row["last_seen"])
        for row in conn.execute(
            "SELECT IFNULL(fare, '') AS fare, COUNT(*) AS rows,"
            " MAX(timestamp) AS last_seen FROM price_history"
            " WHERE watch_id = ? GROUP BY IFNULL(fare, '')"
            " ORDER BY last_seen DESC",
            (watch_id,),
        )
    ]


def date_prices(conn: sqlite3.Connection, watch_id: str) -> list[tuple[str, str, float]]:
    """Every stored (run, departure date, cheapest price) for one watch.

    The raw material for separating "the whole window moved" from "this one
    date got cheap", which the per-run minimum alone cannot distinguish.
    """
    return [
        (row["timestamp"], row["depart_date"], row["price"])
        for row in conn.execute(
            "SELECT timestamp, depart_date, MIN(price) AS price FROM price_history"
            " WHERE watch_id = ? AND depart_date IS NOT NULL"
            " GROUP BY timestamp, depart_date ORDER BY timestamp",
            (watch_id,),
        )
    ]
