"""Is this tool still doing its job?

A price tracker that quietly stops tracking is worse than no tracker at all,
because you carry on believing you are covered. Nothing here looks at whether a
price is good — only at whether the numbers underneath the judgement can still
be trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlite3 import Connection
from typing import Sequence

from .config import Config, Watch
from .store import fares_seen, last_success, parse_iso

STALE = "stale"
NEVER_PRICED = "never_priced"
FARE_CHANGED = "fare_changed"

CRITICAL = "critical"
WARNING = "warning"


@dataclass(frozen=True)
class Concern:
    watch_id: str
    kind: str
    severity: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.severity == CRITICAL


def check(conn: Connection, config: Config, now: datetime) -> list[Concern]:
    """Every reason to distrust what is in the database, worst first."""
    concerns: list[Concern] = []
    for watch in config.watches:
        concerns.extend(_check_watch(conn, watch, config, now))
    concerns.sort(key=lambda c: (c.severity != CRITICAL, c.watch_id))
    return concerns


def _check_watch(
    conn: Connection, watch: Watch, config: Config, now: datetime
) -> list[Concern]:
    found: list[Concern] = []
    limit = config.settings.stale_after_hours

    latest = last_success(conn, watch.id)
    if latest is None:
        found.append(
            Concern(
                watch.id,
                NEVER_PRICED,
                WARNING,
                f"{watch.name}: no price has ever been recorded",
            )
        )
    elif limit > 0:
        age = now - parse_iso(latest)
        if age > timedelta(hours=limit):
            hours = age.total_seconds() / 3600
            found.append(
                Concern(
                    watch.id,
                    STALE,
                    CRITICAL,
                    f"{watch.name}: no price for {hours:.0f}h "
                    f"(last {parse_iso(latest):%d %b %H:%M UTC}) — this watch is "
                    "not being tracked",
                )
            )

    found.extend(_check_fares(conn, watch))
    return found


def _check_fares(conn: Connection, watch: Watch) -> list[Concern]:
    """Flag a history that mixes fare products.

    A basic-economy fare and a regular one are not the same purchase, so a
    percentile computed across both is comparing things that were never
    comparable. The signature is stored per observation precisely so this is
    detectable after the fact rather than only avoidable in advance.
    """
    seen = [(fare, rows) for fare, rows, _ in fares_seen(conn, watch.id) if fare]
    if not seen:
        return []

    current = watch.fare_signature()
    others = [(fare, rows) for fare, rows in seen if fare != current]
    if not others:
        return []

    total = sum(rows for _, rows in seen)
    stale_rows = sum(rows for _, rows in others)
    return [
        Concern(
            watch.id,
            FARE_CHANGED,
            WARNING,
            f"{watch.name}: {stale_rows} of {total} stored prices were collected "
            f"under different search settings ({_diff(others[0][0], current)}) — "
            "those are a different product, so comparisons against them are not "
            "like for like. Give the watch a new id to start a clean history.",
        )
    ]


def _diff(before: str, after: str) -> str:
    """Name only the fields that actually changed."""
    old = dict(part.split(":", 1) for part in before.split("|") if ":" in part)
    new = dict(part.split(":", 1) for part in after.split("|") if ":" in part)
    changes = [
        f"{key} {old.get(key, '?')} -> {new[key]}"
        for key in new
        if old.get(key) != new[key]
    ]
    return ", ".join(changes) if changes else "settings differ"


def summarize(concerns: Sequence[Concern]) -> str:
    if not concerns:
        return "All watches are being tracked."
    critical = sum(1 for c in concerns if c.blocking)
    if critical:
        return f"{critical} watch(es) are not being tracked."
    return f"{len(concerns)} thing(s) worth looking at."
