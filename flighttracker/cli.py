"""Command line entry point: `python -m flighttracker ...`."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from .config import Config, Settings, load_config
from .dashboard import render_document
from .dates import describe
from .digest import (
    EmailNotConfigured,
    SmtpConfig,
    build_message,
    render_text,
    send,
)
from .backfill import SearchApiHistory, import_history
from .backtest import format_result, format_sweep, run_backtest, sweep
from .env import load_env_file
from .errors import ConfigError, FlightTrackerError
from .evaluate import evaluate_stored, format_evaluation
from .fetch import GoogleFlightsFetcher
from .health import check as check_health
from .health import summarize as summarize_health
from .run import commit_alerts, evaluate_only, execute_run
from .store import (
    connect,
    latest_run,
    observations_for_run,
    parse_iso,
    run_history,
    utc_now,
)

DEFAULT_CONFIG = "watches.yaml"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_RUN_FAILED = 3
EXIT_EMAIL_FAILED = 4
EXIT_STALE = 5


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    load_env_file(args.env_file)

    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"Watchlist problems:\n{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except FlightTrackerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_RUN_FAILED
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flighttracker",
        description="Track cash prices for specific flights and email a buy signal.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        type=Path,
        help=f"watchlist file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="override the database path"
    )
    parser.add_argument(
        "--env-file", type=Path, default=Path(".env"), help="env file to read"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fetch prices, store them, email any signals")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the digest instead of sending it, and write nothing to the database",
    )
    run.add_argument("--no-email", action="store_true", help="store results, skip email")
    run.add_argument(
        "--always-email",
        action="store_true",
        help="send the digest even when nothing is flagged",
    )
    run.add_argument("--quiet", action="store_true", help="only print the summary")
    run.add_argument(
        "--fail-if-stale",
        action="store_true",
        help="exit non-zero when a watch has stopped being tracked, so cron mails you",
    )
    run.add_argument("--proxy", default=None, help="proxy for the scraper")
    run.set_defaults(handler=_run)

    validate = sub.add_parser("validate", help="check the watchlist and show the plan")
    validate.set_defaults(handler=_validate)

    history = sub.add_parser("history", help="print stored price history")
    history.add_argument("watch_id", nargs="?", help="limit to one watch")
    history.add_argument("--limit", type=int, default=30, help="rows per watch")
    history.set_defaults(handler=_history)

    signals = sub.add_parser(
        "signals", help="re-judge the latest stored run without fetching"
    )
    signals.set_defaults(handler=_signals)

    dashboard = sub.add_parser(
        "dashboard", help="write a self-contained HTML dashboard"
    )
    dashboard.add_argument(
        "--out",
        type=Path,
        default=Path("index.html"),
        help="where to write it (default: index.html, which is what GitHub Pages"
        " serves from the repo root)",
    )
    dashboard.set_defaults(handler=_dashboard)

    test_email = sub.add_parser("test-email", help="send a sample digest")
    test_email.set_defaults(handler=_test_email)

    backfill = sub.add_parser(
        "backfill",
        help="import Google's own price history so a watch is not blind on day one",
    )
    backfill.add_argument("watch_id", nargs="?", help="limit to one watch")
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report, but write nothing",
    )
    backfill.add_argument(
        "--api-key",
        default=None,
        help="SearchAPI key (defaults to the SEARCHAPI_KEY environment variable)",
    )
    backfill.set_defaults(handler=_backfill)

    doctor = sub.add_parser(
        "doctor", help="check the tool is still collecting usable prices"
    )
    doctor.add_argument(
        "--live",
        action="store_true",
        help="also run one real lookup, to prove the scraper still works",
    )
    doctor.add_argument("--proxy", default=None, help="proxy for the live check")
    doctor.set_defaults(handler=_doctor)

    backtest = sub.add_parser(
        "backtest", help="replay stored history against an alerting rule"
    )
    backtest.add_argument("watch_id", nargs="?", help="limit to one watch")
    backtest.add_argument(
        "--percentile", type=float, default=None, help="override the threshold"
    )
    backtest.add_argument(
        "--min-observations", type=int, default=None, help="override the warm-up"
    )
    backtest.add_argument(
        "--cooldown", type=float, default=None, help="override alert_cooldown_hours"
    )
    backtest.add_argument(
        "--raw-baseline",
        action="store_true",
        help="judge against raw history instead of re-basing it onto the "
        "booking window, to see what the adjustment is worth",
    )
    backtest.add_argument(
        "--sweep",
        action="store_true",
        help="compare several percentile thresholds side by side",
    )
    backtest.set_defaults(handler=_backtest)

    evaluate = sub.add_parser(
        "evaluate",
        help="score the price model against baselines on stored history",
    )
    evaluate.add_argument(
        "--horizons",
        default="1,3,7,14",
        help="days ahead to score, comma separated (default 1,3,7,14)",
    )
    evaluate.add_argument(
        "--shrinkage",
        type=float,
        default=8.0,
        help="how hard to pull thin estimates toward the prior (default 8)",
    )
    evaluate.add_argument(
        "--min-train",
        type=int,
        default=12,
        help="observations required before a prediction is scored (default 12)",
    )
    evaluate.set_defaults(handler=_evaluate)

    return parser


def _load(args) -> Config:
    config = load_config(args.config)
    if args.db is not None:
        config = Config(
            settings=_with_db(config.settings, args.db),
            watches=config.watches,
            source=config.source,
        )
    return config


def _with_db(settings: Settings, db_path: Path) -> Settings:
    values = {
        name: getattr(settings, name) for name in Settings.__dataclass_fields__
    }
    values["db_path"] = db_path
    return Settings(**values)


def make_fetcher(currency: str, proxy: Optional[str]):
    """The seam the tests replace so no test ever reaches Google Flights."""
    return GoogleFlightsFetcher(currency=currency, proxy=proxy)


def _run(args) -> int:
    config = _load(args)
    settings = config.settings

    def note(message: str) -> None:
        if not args.quiet:
            print(message, flush=True)

    if args.dry_run:
        note("Dry run: nothing will be written and no email will be sent.")
        note(
            "History still comes from the real database, so these are the signals "
            "a real run would produce."
        )

    conn = connect(settings.db_path)
    fetcher = make_fetcher(settings.currency, args.proxy)
    now = utc_now()

    outcome = execute_run(
        config, conn, fetcher, now, on_event=note, persist=not args.dry_run
    )

    concerns = check_health(conn, config, now)

    note("")
    note(render_text(outcome.when, outcome.verdicts, outcome.failures, concerns))

    if not outcome.priced and outcome.failures:
        print(
            f"Every lookup failed ({len(outcome.failures)} of them) — nothing stored.",
            file=sys.stderr,
        )
        return EXIT_RUN_FAILED

    if args.dry_run:
        return EXIT_OK

    blocking = [c for c in concerns if c.blocking]

    if args.no_email:
        commit_alerts(conn, outcome)
        return EXIT_STALE if (blocking and args.fail_if_stale) else EXIT_OK

    # A watch that has stopped being tracked always earns an email. The prices
    # you are not seeing are the ones that can cost you, and a tracker that
    # fails silently is the failure this tool most needs to avoid.
    should_send = bool(outcome.flagged) or bool(blocking) or args.always_email
    if not should_send:
        note("Nothing flagged — no email sent (use --always-email to send anyway).")
        return EXIT_OK

    try:
        smtp = SmtpConfig.from_env()
    except EmailNotConfigured as exc:
        print(f"Email not configured: {exc}", file=sys.stderr)
        return EXIT_EMAIL_FAILED

    message = build_message(
        smtp, outcome.when, outcome.verdicts, outcome.failures, concerns
    )
    try:
        send(smtp, message)
    except OSError as exc:
        # The alerts stay unrecorded, so the next run retries them rather than
        # treating an undelivered signal as already seen.
        print(f"Sending the digest failed: {exc}", file=sys.stderr)
        return EXIT_EMAIL_FAILED

    commit_alerts(conn, outcome)
    note(f"Digest sent to {', '.join(smtp.recipients)}.")
    return EXIT_STALE if (blocking and args.fail_if_stale) else EXIT_OK


def _validate(args) -> int:
    config = _load(args)
    settings = config.settings
    total = 0

    print(f"{config.source}: {len(config.watches)} watch(es)")
    print(f"database: {settings.db_path}")
    print(f"currency: {settings.currency}")
    print()

    for watch in config.watches:
        searches = watch.searches()
        total += len(searches)
        print(f"{watch.name}  [{watch.id}]")
        print(f"  {watch.route}, {watch.cabin}, {watch.passengers.total} passenger(s)")
        print(f"  {len(searches)} search(es): {_preview(searches)}")
        ceiling = (
            f"{settings.currency} {watch.max_price_alert:,.0f}"
            if watch.max_price_alert is not None
            else "none"
        )
        print(
            f"  alerts: ceiling {ceiling}, bottom "
            f"{watch.threshold_percentile(settings):g}% after "
            f"{watch.required_observations(settings)} runs"
        )
        print()

    spacing = settings.request_delay_seconds + settings.request_jitter_seconds / 2
    print(f"{total} search(es) per run, roughly {total * spacing / 60:.1f} min of scraping")
    return EXIT_OK


def _preview(searches, limit: int = 3) -> str:
    shown = ", ".join(describe(s) for s in searches[:limit])
    if len(searches) > limit:
        shown += f", ... (+{len(searches) - limit})"
    return shown


def _history(args) -> int:
    config = _load(args)
    conn = connect(config.settings.db_path)
    watches = [w for w in config.watches if not args.watch_id or w.id == args.watch_id]
    if args.watch_id and not watches:
        print(f"No watch named {args.watch_id!r} in {config.source}.", file=sys.stderr)
        return EXIT_CONFIG

    for watch in watches:
        points = run_history(conn, watch.id)
        print(f"{watch.name}  [{watch.id}]  {watch.route}")
        if not points:
            print("  no observations yet")
            print()
            continue
        for point in points[-args.limit :]:
            when = parse_iso(point.timestamp)
            print(
                f"  {when:%Y-%m-%d %H:%M}  {config.settings.currency} "
                f"{point.price:>9,.0f}"
            )
        prices = [p.price for p in points]
        print(
            f"  {len(points)} runs — low {min(prices):,.0f}, high {max(prices):,.0f}"
        )
        print()
    return EXIT_OK


def _signals(args) -> int:
    config = _load(args)
    conn = connect(config.settings.db_path)
    verdicts = evaluate_only(config, conn, utc_now())
    print(render_text(utc_now(), verdicts, []))
    return EXIT_OK


def _backfill(args) -> int:
    """One request per watch, not per run — see flighttracker/backfill.py."""
    config = _load(args)
    settings = config.settings
    conn = connect(settings.db_path)

    watches = [w for w in config.watches if not args.watch_id or w.id == args.watch_id]
    if args.watch_id and not watches:
        print(f"No watch named {args.watch_id!r} in {config.source}.", file=sys.stderr)
        return EXIT_CONFIG

    if not args.api_key and not os.environ.get("SEARCHAPI_KEY"):
        print(
            "Backfill needs a SearchAPI key: set SEARCHAPI_KEY in .env or pass\n"
            "--api-key. Google's price history is not in the page this tool\n"
            "scrapes, so there is no free route to it — see the README.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    provider = SearchApiHistory(api_key=args.api_key, currency=settings.currency)
    if args.dry_run:
        print("Dry run: fetching history, writing nothing.\n")

    failures = 0
    for watch in watches:
        latest = latest_run(conn, watch.id)
        cheapest = None
        if latest:
            rows = observations_for_run(conn, watch.id, latest)
            if rows:
                cheapest = (rows[0]["depart_date"], rows[0]["return_date"])
        try:
            result = import_history(
                conn, watch, provider, settings, cheapest, persist=not args.dry_run
            )
        except Exception as exc:  # noqa: BLE001 - provider and network both raise
            print(f"{watch.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print("\n".join(result.describe()))
        print()

    if failures:
        return EXIT_RUN_FAILED
    if not args.dry_run:
        print(
            "Imported prices are marked as imported and never overwrite one this\n"
            "tool observed itself, so this is safe to re-run."
        )
    return EXIT_OK


def _doctor(args) -> int:
    """A health check meant for cron: `flighttracker doctor || mail me`."""
    config = _load(args)
    conn = connect(config.settings.db_path)
    concerns = check_health(conn, config, utc_now())

    print(summarize_health(concerns))
    for concern in concerns:
        print(f"  {'!!' if concern.blocking else '- '} {concern.message}")

    broken = False
    if args.live:
        broken = not _live_check(config, args.proxy)

    if broken or any(c.blocking for c in concerns):
        return EXIT_STALE
    return EXIT_OK


def _live_check(config: Config, proxy: Optional[str]) -> bool:
    """One real lookup, so a broken scraper is found before a watch goes stale.

    Staleness takes a day or more to show up. This says so immediately, which
    is what makes it worth running on its own schedule.
    """
    watch = config.watches[0]
    depart, back = watch.searches()[0]
    print()
    print(f"Live check: {watch.route} on {depart}...")

    fetcher = make_fetcher(config.settings.currency, proxy)
    try:
        quote = fetcher.fetch(watch, depart, back)
    except Exception as exc:  # noqa: BLE001 - the scraper raises freely
        print(
            f"  FAILED — {type(exc).__name__}: {exc}\n"
            "  The scraper could not reach or parse Google Flights. If this "
            "keeps happening, `fast-flights` most likely needs updating.",
            file=sys.stderr,
        )
        return False

    if quote is None:
        print(
            "  FAILED — the request worked but no priced itinerary came back, "
            "which usually means the page layout changed.",
            file=sys.stderr,
        )
        return False

    print(f"  OK — {quote.currency} {quote.price:,.0f}")
    return True


def _backtest(args) -> int:
    config = _load(args)
    settings = config.settings

    overrides = {}
    if args.percentile is not None:
        overrides["percentile"] = args.percentile
    if args.min_observations is not None:
        overrides["min_observations"] = args.min_observations
    if args.cooldown is not None:
        overrides["alert_cooldown_hours"] = args.cooldown
    if args.raw_baseline:
        overrides["horizon_adjusted_baseline"] = False
    if overrides:
        settings = replace(settings, **overrides)

    conn = connect(settings.db_path)
    watches = [w for w in config.watches if not args.watch_id or w.id == args.watch_id]
    if args.watch_id and not watches:
        print(f"No watch named {args.watch_id!r} in {config.source}.", file=sys.stderr)
        return EXIT_CONFIG

    print("Replaying stored history through the same rule a real run uses.")
    print("This measures the alerting rule, not the future.")
    print()

    for watch in watches:
        if args.sweep:
            print(f"{watch.name}  [{watch.id}]")
            for line in format_sweep(
                sweep(conn, watch, settings), settings.currency
            ):
                print(f"  {line}")
        else:
            for line in format_result(
                run_backtest(conn, watch, settings), settings.currency
            ):
                print(line)
        print()
    return EXIT_OK


def _dashboard(args) -> int:
    config = _load(args)
    conn = connect(config.settings.db_path)
    html = render_document(conn, config, utc_now())

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(f"Wrote {args.out} ({len(html):,} bytes).")
    return EXIT_OK


def _test_email(args) -> int:
    config = _load(args)
    conn = connect(config.settings.db_path)
    verdicts = list(evaluate_only(config, conn, utc_now()))

    try:
        smtp = SmtpConfig.from_env()
    except EmailNotConfigured as exc:
        print(f"Email not configured: {exc}", file=sys.stderr)
        return EXIT_EMAIL_FAILED

    when = utc_now()
    message = build_message(smtp, when, verdicts, [])
    message["Subject"] = "[test] " + str(message["Subject"])
    try:
        send(smtp, message)
    except OSError as exc:
        print(f"Sending failed: {exc}", file=sys.stderr)
        return EXIT_EMAIL_FAILED

    print(f"Test digest sent to {', '.join(smtp.recipients)}.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())


def _evaluate(args) -> int:
    config = _load(args)
    conn = connect(config.settings.db_path)

    try:
        horizons = tuple(
            int(part) for part in args.horizons.split(",") if part.strip()
        )
    except ValueError:
        print(f"Could not read --horizons {args.horizons!r}.", file=sys.stderr)
        return EXIT_CONFIG
    if not horizons:
        print("--horizons needs at least one number.", file=sys.stderr)
        return EXIT_CONFIG

    result = evaluate_stored(
        conn,
        horizons=horizons,
        shrinkage=args.shrinkage,
        minimum_train=args.min_train,
    )
    for line in format_evaluation(result):
        print(line)
    return EXIT_OK
