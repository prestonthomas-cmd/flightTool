"""One tracking run: fetch, store, judge.

Kept apart from the CLI so a run can be driven end to end in a test with a stub
fetcher and an in-memory database.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection
from typing import Callable, Optional, Sequence

from .config import Config
from .fetch import Failure, Fetcher, RunResult, collect
from .forecast import build_forecast, rebase
from .signals import Verdict, evaluate
from .store import (
    horizon_samples,
    last_alert,
    observations_for_run,
    record_alerts,
    record_errors,
    record_observations,
    run_history,
    to_iso,
)


@dataclass(frozen=True)
class RunOutcome:
    timestamp: str
    when: datetime
    verdicts: tuple[Verdict, ...]
    failures: tuple[Failure, ...]
    stored: int

    @property
    def flagged(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.flagged]

    @property
    def priced(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.price is not None]


def execute_run(
    config: Config,
    conn: Connection,
    fetcher: Fetcher,
    now: datetime,
    sleep: Callable[[float], None] = None,
    rng: Optional[random.Random] = None,
    on_event: Optional[Callable[[str], None]] = None,
    persist: bool = True,
) -> RunOutcome:
    """Price every watch, write the results, and judge each against its history.

    With `persist=False` nothing is written — a dry run still reads the real
    history, so the signals it prints are the ones a real run would produce.
    """
    import time as _time

    timestamp = to_iso(now)
    settings = config.settings

    result: RunResult = collect(
        config.watches,
        fetcher,
        settings,
        sleep=sleep or _time.sleep,
        rng=rng,
        on_event=on_event,
    )

    stored = 0
    if persist:
        stored = record_observations(
            conn, timestamp, [quote.to_observation() for quote in result.quotes]
        )
        record_errors(
            conn,
            timestamp,
            [
                (
                    failure.watch_id,
                    failure.depart_date.isoformat() if failure.depart_date else None,
                    failure.return_date.isoformat() if failure.return_date else None,
                    failure.message,
                )
                for failure in result.failures
            ],
        )

    samples = horizon_samples(conn)
    verdicts = []
    for watch in config.watches:
        best = result.cheapest(watch.id)
        # `before=timestamp` keeps this run's own price out of the history it is
        # being compared against — otherwise a new low could never be a new low.
        history = run_history(conn, watch.id, before=timestamp)
        history, adjusted = rebase(samples, watch, history, settings, now)
        verdicts.append(
            evaluate(
                watch=watch,
                price=best.price if best else None,
                history=history,
                settings=settings,
                currency=best.currency if best else settings.currency,
                last=last_alert(conn, watch.id),
                now=now,
                best_depart=best.depart_date.isoformat() if best else None,
                best_return=(
                    best.return_date.isoformat()
                    if best and best.return_date
                    else None
                ),
            ).with_baseline("horizon-adjusted" if adjusted else "raw")
        )

    priced_rows = {
        watch.id: [
            (q.depart_date.isoformat(), q.price) for q in result.for_watch(watch.id)
        ]
        for watch in config.watches
    }
    verdicts = attach_forecasts(conn, config, verdicts, now, priced_rows)

    return RunOutcome(
        timestamp=timestamp,
        when=now,
        verdicts=tuple(verdicts),
        failures=result.failures,
        stored=stored,
    )


def attach_forecasts(
    conn: Connection,
    config: Config,
    verdicts: Sequence[Verdict],
    now: datetime,
    run_rows: Optional[dict] = None,
) -> list[Verdict]:
    """Annotate each verdict with where its price looks to be heading.

    Deliberately a separate pass over finished verdicts: a forecast can never
    reach back and change whether something was flagged.
    """
    run_rows = run_rows or {}
    annotated = []
    for verdict in verdicts:
        history = run_history(conn, verdict.watch.id, before=to_iso(now))
        forecast = build_forecast(
            conn,
            verdict.watch,
            history,
            config.settings,
            now,
            best_depart=verdict.best_depart,
            run_rows=run_rows.get(verdict.watch.id, ()),
            currency=verdict.currency,
            price=verdict.price,
        )
        annotated.append(verdict.with_forecast(forecast))
    return annotated


def commit_alerts(conn: Connection, outcome: RunOutcome) -> int:
    """Record what was alerted on, so the cooldown has something to work from.

    Called only after the digest actually goes out. A send that fails leaves no
    record, so the next run alerts again rather than silently swallowing it.
    """
    return record_alerts(
        conn,
        outcome.timestamp,
        [
            (verdict.watch.id, verdict.price, verdict.reason_codes)
            for verdict in outcome.flagged
            if verdict.price is not None
        ],
    )


def evaluate_only(
    config: Config, conn: Connection, now: datetime
) -> Sequence[Verdict]:
    """Re-judge the most recent stored run without touching the network."""
    verdicts = []
    run_rows: dict[str, list] = {}
    samples = horizon_samples(conn)
    for watch in config.watches:
        history = run_history(conn, watch.id)
        if not history:
            verdicts.append(
                evaluate(
                    watch=watch,
                    price=None,
                    history=[],
                    settings=config.settings,
                    currency=config.settings.currency,
                )
            )
            continue
        latest = history[-1]
        earlier, adjusted = rebase(samples, watch, history[:-1], config.settings, now)
        rows = observations_for_run(conn, watch.id, latest.timestamp)
        cheapest = rows[0] if rows else None
        run_rows[watch.id] = [
            (row["depart_date"], row["price"]) for row in rows if row["depart_date"]
        ]
        verdicts.append(
            evaluate(
                watch=watch,
                price=latest.price,
                history=earlier,
                settings=config.settings,
                currency=(cheapest["currency"] if cheapest else None)
                or config.settings.currency,
                last=last_alert(conn, watch.id),
                now=now,
                best_depart=cheapest["depart_date"] if cheapest else None,
                best_return=cheapest["return_date"] if cheapest else None,
            ).with_baseline("horizon-adjusted" if adjusted else "raw")
        )
    return attach_forecasts(conn, config, verdicts, now, run_rows)
