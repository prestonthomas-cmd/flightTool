import io
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from datetime import date

from flighttracker import cli
from flighttracker.config import load_config
from flighttracker.store import connect, observation_counts, run_history
from flighttracker.watchlist import watch_ids

from .support import StubFetcher

WATCHLIST = """
    settings:
      db_path: prices.db
      min_observations: 2
      request_delay_seconds: 0
      request_jitter_seconds: 0
      retry_backoff_seconds: 0
    watches:
      - id: tokyo
        label: NYC to Tokyo, December
        origin: JFK
        destination: HND
        depart_date_range: [2026-12-10, 2026-12-11]
        return_date_range: 2026-12-24
        max_price_alert: 900
"""


class CommandLine(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config = self.tmp / "watches.yaml"
        self.config.write_text(textwrap.dedent(WATCHLIST))
        self.env = self.tmp / ".env"
        self.env.write_text("")

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--config", str(self.config), "--env-file", str(self.env), *argv]
            )
        return code, out.getvalue(), err.getvalue()

    def test_validate_shows_the_plan_without_touching_the_network(self):
        code, out, _ = self.invoke("validate")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("NYC to Tokyo, December", out)
        self.assertIn("2 search(es)", out)
        self.assertIn("JFK <-> HND", out)

    def test_a_broken_watchlist_exits_with_the_problems(self):
        self.config.write_text("watches:\n  - id: oops\n")
        code, _, err = self.invoke("validate")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("Watchlist problems", err)

    def test_a_dry_run_prints_the_digest_and_writes_nothing(self):
        with mock.patch.object(cli, "make_fetcher", return_value=StubFetcher(default=700)):
            code, out, _ = self.invoke("run", "--dry-run")

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Dry run", out)
        self.assertIn("USD 700", out)
        conn = connect(self.tmp / "prices.db")
        self.assertEqual(run_history(conn, "tokyo"), [])

    def test_a_real_run_stores_prices_and_skips_email_when_asked(self):
        with mock.patch.object(cli, "make_fetcher", return_value=StubFetcher(default=700)):
            code, out, _ = self.invoke("run", "--no-email")

        self.assertEqual(code, cli.EXIT_OK)
        conn = connect(self.tmp / "prices.db")
        self.assertEqual(len(run_history(conn, "tokyo")), 1)
        self.assertEqual(run_history(conn, "tokyo")[0].price, 700)

    def test_a_run_where_everything_failed_exits_nonzero(self):
        fetcher = StubFetcher(errors={"tokyo": RuntimeError("blocked")})
        with mock.patch.object(cli, "make_fetcher", return_value=fetcher):
            code, _, err = self.invoke("run", "--no-email")

        self.assertEqual(code, cli.EXIT_RUN_FAILED)
        self.assertIn("Every lookup failed", err)

    def test_a_run_with_a_signal_but_no_email_configured_says_so(self):
        with mock.patch.dict("os.environ", {"SMTP_HOST": "", "EMAIL_FROM": "", "EMAIL_TO": ""}), \
                mock.patch.object(cli, "make_fetcher", return_value=StubFetcher(default=700)):
            code, _, err = self.invoke("run")

        self.assertEqual(code, cli.EXIT_EMAIL_FAILED)
        self.assertIn("Email not configured", err)

    def test_a_quiet_run_with_nothing_flagged_sends_no_email(self):
        self.config.write_text(
            textwrap.dedent(WATCHLIST).replace("max_price_alert: 900", "max_price_alert: 100")
        )
        with mock.patch.object(cli, "make_fetcher", return_value=StubFetcher(default=700)):
            code, out, _ = self.invoke("run")

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("no email sent", out)

    def test_history_reports_what_has_been_collected(self):
        with mock.patch.object(cli, "make_fetcher", return_value=StubFetcher(default=700)):
            self.invoke("run", "--no-email")

        code, out, _ = self.invoke("history")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("USD", out)
        self.assertIn("1 runs", out)

    def test_history_for_an_unknown_watch_is_an_error(self):
        code, _, err = self.invoke("history", "nope")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("No watch named", err)

    def test_signals_rejudges_stored_data_without_fetching(self):
        with mock.patch.object(cli, "make_fetcher", return_value=StubFetcher(default=700)):
            self.invoke("run", "--no-email")

        code, out, _ = self.invoke("signals")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("USD 700", out)

    def test_the_db_flag_overrides_the_watchlist(self):
        elsewhere = self.tmp / "other.db"
        with mock.patch.object(cli, "make_fetcher", return_value=StubFetcher(default=700)):
            self.invoke("--db", str(elsewhere), "run", "--no-email")

        self.assertTrue(elsewhere.exists())
        self.assertFalse((self.tmp / "prices.db").exists())



class Evaluating(unittest.TestCase):
    """`flighttracker evaluate` — the command that scores the price model."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config = self.tmp / "watches.yaml"
        self.config.write_text(textwrap.dedent(WATCHLIST))
        self.env = self.tmp / ".env"
        self.env.write_text("")

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--config", str(self.config), "--env-file", str(self.env), *argv]
            )
        return code, out.getvalue(), err.getvalue()

    def test_an_empty_database_says_there_is_nothing_to_score(self):
        code, out, _ = self.invoke("evaluate")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Nothing could be scored", out)

    def test_it_scores_what_history_there_is(self):
        with mock.patch.object(
            cli, "make_fetcher", return_value=StubFetcher(default=700)
        ):
            for _ in range(6):
                self.invoke("run")

        code, out, _ = self.invoke("evaluate", "--horizons", "1")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Rolling-origin validation", out)
        self.assertIn("naive", out)

    def test_a_horizon_list_that_cannot_be_read_is_refused(self):
        code, _, err = self.invoke("evaluate", "--horizons", "soon")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("Could not read", err)

    def test_an_empty_horizon_list_is_refused(self):
        code, _, err = self.invoke("evaluate", "--horizons", ",")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("at least one", err)



class ManagingTheWatchlist(unittest.TestCase):
    """`add`, `remove` and `list` — the commands that replace editing YAML."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config = self.tmp / "watches.yaml"
        self.config.write_text(textwrap.dedent(WATCHLIST))
        self.env = self.tmp / ".env"
        self.env.write_text("")

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--config", str(self.config), "--env-file", str(self.env), *argv]
            )
        return code, out.getvalue(), err.getvalue()

    def ids(self):
        return watch_ids(self.config)

    def test_a_one_way_flight_can_be_added(self):
        code, out, _ = self.invoke("add", "SFO-LIS:2027-03-01")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("SFO -> LIS", out)
        self.assertIn("sfo-lis-mar2027", self.ids())

    def test_a_round_trip_can_be_added(self):
        self.invoke("add", "SFO-LIS:2027-03-01", "--return", "2027-03-15")
        watch = next(
            w for w in load_config(self.config).watches if w.id == "sfo-lis-mar2027"
        )
        self.assertFalse(watch.one_way)
        self.assertEqual(watch.return_dates, (date(2027, 3, 15),))

    def test_date_ranges_are_understood(self):
        self.invoke("add", "SFO-LIS:2027-03-01..2027-03-05", "--id", "trip")
        watch = next(w for w in load_config(self.config).watches if w.id == "trip")
        self.assertEqual(watch.depart_dates[0], date(2027, 3, 1))
        self.assertEqual(watch.depart_dates[-1], date(2027, 3, 5))

    def test_the_options_are_carried_through(self):
        self.invoke(
            "add", "SFO-LIS:2027-03-01", "--id", "trip", "--cabin", "business",
            "--adults", "2", "--nonstop", "--max-price", "1500",
            "--label", "Big trip",
        )
        watch = next(w for w in load_config(self.config).watches if w.id == "trip")
        self.assertEqual(watch.cabin, "business")
        self.assertEqual(watch.passengers.adults, 2)
        self.assertEqual(watch.max_stops, 0)
        self.assertEqual(watch.max_price_alert, 1500.0)
        self.assertEqual(watch.name, "Big trip")

    def test_a_flight_can_be_removed(self):
        code, out, _ = self.invoke("remove", "tokyo")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Stopped tracking tokyo", out)
        self.assertEqual(self.ids(), [])

    def test_removing_keeps_the_prices_unless_asked_otherwise(self):
        with mock.patch.object(
            cli, "make_fetcher", return_value=StubFetcher(default=700)
        ):
            self.invoke("run")

        _, out, _ = self.invoke("remove", "tokyo")
        self.assertIn("recorded prices are kept", out)
        conn = connect(self.tmp / "prices.db")
        self.assertGreater(observation_counts(conn).get("tokyo", 0), 0)

    def test_purging_deletes_the_prices_too(self):
        with mock.patch.object(
            cli, "make_fetcher", return_value=StubFetcher(default=700)
        ):
            self.invoke("run")

        _, out, _ = self.invoke("remove", "tokyo", "--purge")
        self.assertIn("Deleted", out)
        conn = connect(self.tmp / "prices.db")
        self.assertEqual(observation_counts(conn).get("tokyo", 0), 0)

    def test_adding_a_flight_back_resumes_its_history(self):
        """Why prices are kept by default: a removal is not always permanent."""
        with mock.patch.object(
            cli, "make_fetcher", return_value=StubFetcher(default=700)
        ):
            self.invoke("run")
        before = observation_counts(connect(self.tmp / "prices.db"))["tokyo"]

        self.invoke("remove", "tokyo")
        self.invoke(
            "add", "JFK-HND:2026-12-10", "--return", "2026-12-24", "--id", "tokyo"
        )
        after = observation_counts(connect(self.tmp / "prices.db"))["tokyo"]
        self.assertEqual(after, before)

    def test_removing_the_last_flight_leaves_a_file_that_still_works(self):
        self.invoke("remove", "tokyo")
        code, out, _ = self.invoke("list")

        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Nothing is being tracked", out)
        # ...and a flight can be added back to it.
        self.invoke("add", "SFO-LIS:2027-03-01", "--id", "next")
        self.assertEqual(self.ids(), ["next"])

    def test_list_shows_what_is_tracked_and_how_much_is_recorded(self):
        code, out, _ = self.invoke("list")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("tokyo", out)
        self.assertIn("JFK <-> HND", out)
        self.assertIn("0 price(s) recorded", out)

    def test_a_route_that_cannot_be_read_is_refused(self):
        before = self.config.read_text()
        for bad in ("JFK-HND", "JFK:2027-03-01", "JFKK-HND:2027-03-01",
                    "JFK-HND:not-a-date"):
            with self.subTest(route=bad):
                code, _, err = self.invoke("add", bad)
                self.assertEqual(code, cli.EXIT_CONFIG)
                self.assertTrue(err.strip())
        self.assertEqual(self.config.read_text(), before)

    def test_a_duplicate_id_is_refused_and_changes_nothing(self):
        before = self.config.read_text()
        code, _, err = self.invoke("add", "JFK-HND:2026-12-10", "--id", "tokyo")

        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("already in the watchlist", err)
        self.assertEqual(self.config.read_text(), before)

    def test_a_return_date_before_the_departure_is_refused(self):
        code, _, err = self.invoke(
            "add", "JFK-HND:2026-12-10", "--return", "2026-12-01"
        )
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("before the departure", err)

    def test_removing_a_flight_that_is_not_there_says_what_is(self):
        code, _, err = self.invoke("remove", "paris")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("tokyo", err)


if __name__ == "__main__":
    unittest.main()
