import io
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from flighttracker import cli
from flighttracker.store import connect, run_history

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


if __name__ == "__main__":
    unittest.main()
