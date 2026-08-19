import random
import unittest
from datetime import date, datetime, timedelta, timezone

from flighttracker.run import commit_alerts, evaluate_only, execute_run
from flighttracker.store import connect, last_alert, run_history

from .support import StubFetcher, make_config, make_watch, no_sleep

START = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


class TrackingOverTime(unittest.TestCase):
    """Drive several runs end to end against an in-memory database."""

    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        self.rng = random.Random(0)

    def go(self, config, prices, when, commit=True):
        fetcher = StubFetcher(prices=prices) if isinstance(prices, dict) else StubFetcher(default=prices)
        outcome = execute_run(
            config, self.conn, fetcher, when, sleep=no_sleep, rng=self.rng
        )
        if commit:
            commit_alerts(self.conn, outcome)
        return outcome

    def test_a_run_stores_a_price_and_judges_it(self):
        config = make_config(make_watch("tokyo"), min_observations=3)
        outcome = self.go(config, 800, START)

        self.assertEqual(outcome.stored, 1)
        self.assertEqual(len(run_history(self.conn, "tokyo")), 1)
        self.assertFalse(outcome.flagged)
        self.assertIn("building history", outcome.verdicts[0].suppressed)

    def test_history_builds_up_and_then_a_drop_is_flagged(self):
        config = make_config(make_watch("tokyo"), min_observations=3, alert_cooldown_hours=0)
        for index, price in enumerate([900, 850, 880]):
            outcome = self.go(config, price, START + timedelta(hours=12 * index))
            self.assertFalse(outcome.flagged)

        outcome = self.go(config, 600, START + timedelta(hours=36))
        self.assertTrue(outcome.flagged)
        self.assertEqual(outcome.flagged[0].price, 600)
        self.assertIn("lowest seen in 3 runs", outcome.flagged[0].reasons[0].detail)

    def test_this_runs_own_price_is_not_part_of_the_history_it_beats(self):
        config = make_config(make_watch("tokyo"), min_observations=2, alert_cooldown_hours=0)
        self.go(config, 900, START)
        self.go(config, 850, START + timedelta(hours=12))
        outcome = self.go(config, 600, START + timedelta(hours=24))

        self.assertTrue(outcome.flagged)
        self.assertEqual(outcome.verdicts[0].stats.count, 2)
        self.assertEqual(outcome.verdicts[0].stats.minimum, 850)

    def test_a_repeat_alert_is_held_back_by_the_cooldown(self):
        config = make_config(
            make_watch("tokyo"), min_observations=2, alert_cooldown_hours=48
        )
        self.go(config, 900, START)
        self.go(config, 850, START + timedelta(hours=12))
        first = self.go(config, 600, START + timedelta(hours=24))
        self.assertTrue(first.flagged)

        second = self.go(config, 590, START + timedelta(hours=36))
        self.assertFalse(second.flagged)
        self.assertIn("already alerted", second.verdicts[0].suppressed)

    def test_alerts_are_only_recorded_once_the_digest_goes_out(self):
        config = make_config(make_watch("tokyo"), min_observations=1, alert_cooldown_hours=48)
        self.go(config, 900, START)
        outcome = self.go(config, 600, START + timedelta(hours=12), commit=False)

        self.assertTrue(outcome.flagged)
        self.assertIsNone(last_alert(self.conn, "tokyo"))

        commit_alerts(self.conn, outcome)
        self.assertEqual(last_alert(self.conn, "tokyo").price, 600)

    def test_a_ceiling_alerts_on_the_very_first_run(self):
        config = make_config(make_watch("tokyo", max_price_alert=900), min_observations=10)
        outcome = self.go(config, 850, START)
        self.assertTrue(outcome.flagged)
        self.assertEqual(outcome.flagged[0].reason_codes, "under_ceiling")

    def test_the_cheapest_date_combination_is_the_one_reported(self):
        watch = make_watch(
            "tokyo",
            depart=(date(2026, 12, 10), date(2026, 12, 11)),
            returns=(date(2026, 12, 24),),
            max_price_alert=900,
        )
        prices = {
            ("tokyo", date(2026, 12, 10), date(2026, 12, 24)): 950,
            ("tokyo", date(2026, 12, 11), date(2026, 12, 24)): 700,
        }
        outcome = self.go(make_config(watch), prices, START)

        self.assertEqual(outcome.verdicts[0].price, 700)
        self.assertEqual(outcome.verdicts[0].best_depart, "2026-12-11")
        self.assertEqual(outcome.verdicts[0].best_return, "2026-12-24")
        self.assertEqual(outcome.stored, 2)

    def test_a_watch_whose_lookups_all_fail_is_reported_and_the_rest_still_run(self):
        config = make_config(make_watch("good", max_price_alert=900), make_watch("bad"))
        fetcher = StubFetcher(prices={"good": 700}, errors={"bad": RuntimeError("blocked")})
        outcome = execute_run(config, self.conn, fetcher, START, sleep=no_sleep, rng=self.rng)

        self.assertEqual(len(outcome.failures), 1)
        self.assertEqual(len(outcome.priced), 1)
        self.assertTrue(outcome.flagged)
        self.assertIsNone(outcome.verdicts[1].price)
        self.assertEqual(len(run_history(self.conn, "bad")), 0)

    def test_a_dry_run_judges_but_writes_nothing(self):
        config = make_config(make_watch("tokyo"), min_observations=1, alert_cooldown_hours=0)
        self.go(config, 900, START)

        fetcher = StubFetcher(default=600)
        outcome = execute_run(
            config,
            self.conn,
            fetcher,
            START + timedelta(hours=12),
            sleep=no_sleep,
            rng=self.rng,
            persist=False,
        )

        self.assertTrue(outcome.flagged)
        self.assertEqual(outcome.stored, 0)
        self.assertEqual(len(run_history(self.conn, "tokyo")), 1)


class ReJudgingStoredData(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_signals_can_be_recomputed_without_fetching(self):
        config = make_config(make_watch("tokyo"), min_observations=2, alert_cooldown_hours=0)
        for index, price in enumerate([900, 850, 600]):
            execute_run(
                config,
                self.conn,
                StubFetcher(default=price),
                START + timedelta(hours=12 * index),
                sleep=no_sleep,
            )

        verdicts = list(evaluate_only(config, self.conn, START + timedelta(hours=30)))
        self.assertEqual(verdicts[0].price, 600)
        self.assertTrue(verdicts[0].flagged)
        self.assertEqual(verdicts[0].best_depart, "2026-12-10")

    def test_a_watch_with_no_data_yet_is_handled(self):
        config = make_config(make_watch("fresh"))
        verdicts = list(evaluate_only(config, self.conn, START))
        self.assertIsNone(verdicts[0].price)


if __name__ == "__main__":
    unittest.main()
