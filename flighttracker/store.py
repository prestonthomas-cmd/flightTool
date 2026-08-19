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
  duration_minutes INTEGER
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
"""


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
    conn.commit()
    return conn


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
               airlines, stops, duration_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (watch_id, timestamp, depart_date, IFNULL(return_date, ''))
            DO UPDATE SET
              price = excluded.price,
              currency = excluded.currency,
              airlines = excluded.airlines,
              stops = excluded.stops,
              duration_minutes = excluded.duration_minutes
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
