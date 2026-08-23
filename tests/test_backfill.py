import unittest
from datetime import date, datetime, timezone

from flighttracker.backfill import (
    IMPORT_HOUR,
    SOURCE,
    History,
    PricePoint,
    choose_search,
    import_history,
    parse_point_date,
)
from flighttracker.config import Settings
from flighttracker.run import execute_run
from flighttracker.store import connect, run_history, source_counts

from .support import StubFetcher, make_watch, no_sleep

NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


class StubProvider:
    """Stands in for SearchAPI so no test needs a key or the network."""

    def __init__(self, history=None, error=None):
        self.history_to_return = history or History()
        self.error = error
        self.calls = []

    def history(self, watch, depart, back):
        self.calls.append((watch.id, depart, back))
        if self.error:
            raise self.error
        return self.history_to_return


def series(*pairs, currency="USD", **kwargs):
    return History(
        points=tuple(PricePoint(date.fromisoformat(d), float(p)) for d, p in pairs),
        currency=currency,
        **kwargs,
    )


class DateParsing(unittest.TestCase):
    """Guessing wrong would pile a year of history onto one day."""

    def test_an_iso_date(self):
        self.assertEqual(parse_point_date("2026-06-01"), date(2026, 6, 1))

    def test_a_full_timestamp_keeps_its_date(self):
        self.assertEqual(parse_point_date("2026-06-01T09:30:00Z"), date(2026, 6, 1))

    def test_epoch_seconds_as_a_number(self):
        self.assertEqual(parse_point_date(1780272000), date(2026, 6, 1))

    def test_epoch_seconds_as_a_string(self):
        self.assertEqual(parse_point_date("1780272000"), date(2026, 6, 1))

    def test_nonsense_is_refused_rather_than_guessed(self):
        for value in ("", "  ", "not a date", None, True, "2026-13-45"):
            self.assertIsNone(parse_point_date(value), value)


class ChoosingASearch(unittest.TestCase):
    def test_the_only_search_is_the_obvious_choice(self):
        watch = make_watch("w")
        self.assertEqual(choose_search(watch), (date(2026, 12, 10), date(2026, 12, 24)))

    def test_the_combination_the_watch_is_cheapest_on_wins(self):
        watch = make_watch(
            "w",
            depart=(date(2026, 12, 10), date(2026, 12, 11)),
            returns=(date(2026, 12, 24),),
        )
        chosen = choose_search(watch, cheapest=("2026-12-11", "2026-12-24"))
        self.assertEqual(chosen, (date(2026, 12, 11), date(2026, 12, 24)))

    def test_an_unrecognised_hint_falls_back_to_the_first(self):
        watch = make_watch("w")
        chosen = choose_search(watch, cheapest=("2099-01-01", "2099-01-08"))
        self.assertEqual(chosen, (date(2026, 12, 10), date(2026, 12, 24)))

    def test_a_one_way_hint_matches_a_one_way_search(self):
        watch = make_watch("w", returns=None)
        self.assertEqual(choose_search(watch, cheapest=("2026-12-10", None))[1], None)


class Importing(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        self.settings = Settings()
        self.watch = make_watch("tokyo", label="NYC to Tokyo")

    def run_import(self, history, **kwargs):
        return import_history(
            self.conn, self.watch, StubProvider(history), self.settings, **kwargs
        )

    def test_history_becomes_runs_the_signal_can_use(self):
        result = self.run_import(
            series(("2026-06-01", 1200), ("2026-06-02", 1150), ("2026-06-03", 1100))
        )
        self.assertEqual(result.written, 3)
        points = run_history(self.conn, "tokyo")
        self.assertEqual([p.price for p in points], [1200, 1150, 1100])

    def test_imported_rows_are_marked_as_imported(self):
        self.run_import(series(("2026-06-01", 1200)))
        self.assertEqual(source_counts(self.conn, "tokyo"), {SOURCE: 1})

    def test_imported_points_land_clear_of_the_scheduled_runs(self):
        self.run_import(series(("2026-06-01", 1200)))
        stamp = run_history(self.conn, "tokyo")[0].timestamp
        self.assertIn(f"T{IMPORT_HOUR:02d}:00:00", stamp)

    def test_re_running_imports_nothing_twice(self):
        history = series(("2026-06-01", 1200), ("2026-06-02", 1150))
        first = self.run_import(history)
        second = self.run_import(history)

        self.assertEqual(first.written, 2)
        self.assertEqual(second.written, 0)
        self.assertEqual(second.skipped, 2)
        self.assertEqual(len(run_history(self.conn, "tokyo")), 2)

    def test_an_imported_price_never_overwrites_an_observed_one(self):
        """A price this tool watched happen outranks one imported after it."""
        observed_at = datetime(2026, 6, 1, IMPORT_HOUR, tzinfo=timezone.utc)
        config_watch = self.watch
        from flighttracker.config import Config

        execute_run(
            Config(settings=self.settings, watches=(config_watch,)),
            self.conn,
            StubFetcher(default=999),
            observed_at,
            sleep=no_sleep,
        )
        result = self.run_import(series(("2026-06-01", 111)))

        self.assertEqual(result.written, 0)
        points = run_history(self.conn, "tokyo")
        self.assertEqual([p.price for p in points], [999])
        self.assertEqual(source_counts(self.conn, "tokyo"), {"observed": 1})

    def test_a_dry_run_writes_nothing(self):
        result = self.run_import(series(("2026-06-01", 1200)), persist=False)
        self.assertEqual(result.written, 0)
        self.assertEqual(run_history(self.conn, "tokyo"), [])
        self.assertEqual(result.considered, 1)

    def test_no_history_is_reported_not_crashed(self):
        result = self.run_import(History())
        self.assertEqual(result.written, 0)
        self.assertIn("no price history", "\n".join(result.describe()))

    def test_the_report_names_the_span_and_the_range(self):
        result = self.run_import(
            series(("2026-06-01", 1200), ("2026-07-01", 900),
                   typical_low=950.0, typical_high=1400.0, level="low")
        )
        text = "\n".join(result.describe())
        self.assertIn("2 day(s) of history, 2026-06-01 to 2026-07-01", text)
        self.assertIn("USD 900", text)
        self.assertIn("typical", text)
        self.assertIn("currently low", text)

    def test_the_import_records_the_dates_it_actually_asked_about(self):
        self.run_import(series(("2026-06-01", 1200)))
        row = self.conn.execute(
            "SELECT depart_date, return_date, origin, destination, fare"
            " FROM price_history WHERE watch_id = 'tokyo'"
        ).fetchone()
        self.assertEqual(row["depart_date"], "2026-12-10")
        self.assertEqual(row["return_date"], "2026-12-24")
        self.assertEqual(row["origin"], "JFK")
        self.assertIn("cabin:economy", row["fare"])

    def test_the_importer_stores_what_the_provider_hands_it(self):
        """Filtering bad prices is the provider's job, not the importer's.

        `SearchApiHistory.history` drops entries with no usable date or a
        non-positive price; keeping that boundary in one place means a stub
        provider in a test behaves like the real one.
        """
        result = import_history(
            self.conn,
            self.watch,
            StubProvider(History(points=(PricePoint(date(2026, 6, 1), 1200.0),))),
            self.settings,
        )
        self.assertEqual(result.written, 1)

    def test_a_provider_failure_propagates_rather_than_being_swallowed(self):
        with self.assertRaises(RuntimeError):
            import_history(
                self.conn,
                self.watch,
                StubProvider(error=RuntimeError("402 payment required")),
                self.settings,
            )


class MixedHistory(unittest.TestCase):
    def test_imported_and_observed_prices_are_counted_separately(self):
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        from flighttracker.config import Config

        watch = make_watch("tokyo")
        settings = Settings()
        import_history(
            conn, watch,
            StubProvider(series(("2026-06-01", 1200), ("2026-06-02", 1150))),
            settings,
        )
        execute_run(
            Config(settings=settings, watches=(watch,)),
            conn, StubFetcher(default=900),
            datetime(2026, 8, 23, 7, tzinfo=timezone.utc), sleep=no_sleep,
        )

        self.assertEqual(source_counts(conn, "tokyo"), {SOURCE: 2, "observed": 1})
        self.assertEqual(len(run_history(conn, "tokyo")), 3)


if __name__ == "__main__":
    unittest.main()
