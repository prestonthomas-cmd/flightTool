import unittest
from datetime import datetime, timedelta, timezone

from flighttracker.backtest import format_result, format_sweep, run_backtest, sweep
from flighttracker.run import execute_run
from flighttracker.store import connect

from .support import StubFetcher, make_config, make_watch, no_sleep

START = datetime(2026, 5, 1, tzinfo=timezone.utc)


class Replaying(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def track(self, prices, **settings):
        config = make_config(make_watch("tokyo", label="NYC to Tokyo"), **settings)
        for index, price in enumerate(prices):
            execute_run(
                config,
                self.conn,
                StubFetcher(default=price),
                START + timedelta(days=index),
                sleep=no_sleep,
            )
        return config

    def test_no_history_is_reported_not_crashed(self):
        config = make_config(make_watch("tokyo"))
        result = run_backtest(self.conn, config.watches[0], config.settings)
        self.assertEqual(result.runs, 0)
        self.assertIn("no history", format_result(result)[0])

    def test_it_replays_every_stored_run(self):
        config = self.track([900] * 10, min_observations=3)
        result = run_backtest(self.conn, config.watches[0], config.settings)
        self.assertEqual(result.runs, 10)
        self.assertEqual(result.span_days, 9)

    def test_a_steady_price_never_alerts(self):
        config = self.track([900] * 12, min_observations=3, percentile=20)
        result = run_backtest(self.conn, config.watches[0], config.settings)
        self.assertEqual(result.alerts, ())
        self.assertIn("no alerts at all", "\n".join(format_result(result)))

    def test_a_genuine_drop_alerts_once_the_warm_up_is_over(self):
        config = self.track(
            [1000] * 10 + [700], min_observations=5, alert_cooldown_hours=0
        )
        result = run_backtest(self.conn, config.watches[0], config.settings)
        self.assertEqual(len(result.alerts), 1)
        self.assertEqual(result.alerts[0].price, 700)

    def test_the_warm_up_is_respected_so_early_runs_cannot_alert(self):
        config = self.track([1000, 400] + [1000] * 10, min_observations=8)
        result = run_backtest(self.conn, config.watches[0], config.settings)
        # The 400 arrives on run 2, long before the history is worth comparing.
        self.assertEqual(result.alerts, ())

    def test_no_run_can_see_a_price_that_had_not_happened_yet(self):
        """A backtest that leaks the future measures nothing."""
        config = self.track([1000] * 8 + [500, 900], min_observations=4,
                            alert_cooldown_hours=0)
        result = run_backtest(self.conn, config.watches[0], config.settings)

        # 500 is an all-time low when it lands, and must be alerted on then —
        # not skipped because a later run knew the series recovered.
        self.assertTrue(any(a.price == 500 for a in result.alerts))

    def test_the_cooldown_applies_exactly_as_it_does_in_a_real_run(self):
        prices = [1000] * 8 + [600, 600, 600, 600]
        quiet = self.track(prices, min_observations=4, alert_cooldown_hours=0)
        many = run_backtest(self.conn, quiet.watches[0], quiet.settings)

        held = make_config(
            make_watch("tokyo"), min_observations=4, alert_cooldown_hours=48,
            alert_improvement=0.03,
        )
        few = run_backtest(self.conn, held.watches[0], held.settings)
        self.assertLess(len(few.alerts), len(many.alerts))

    def test_it_reports_the_gap_between_the_first_alert_and_the_best_price(self):
        config = self.track(
            [1000] * 8 + [800, 700, 600], min_observations=4, alert_cooldown_hours=0
        )
        result = run_backtest(self.conn, config.watches[0], config.settings)

        self.assertEqual(result.best_price, 600)
        self.assertEqual(result.first_alert.price, 800)
        self.assertEqual(result.premium, 200)
        self.assertAlmostEqual(result.premium_fraction, 200 / 600)

    def test_acting_on_the_best_possible_alert_shows_no_premium(self):
        config = self.track([1000] * 8 + [600], min_observations=4,
                            alert_cooldown_hours=0)
        result = run_backtest(self.conn, config.watches[0], config.settings)
        self.assertEqual(result.premium, 0)
        self.assertIn("the best price it ever saw", "\n".join(format_result(result)))

    def test_the_alert_rate_is_reported_per_month(self):
        config = self.track([1000] * 8 + [600, 550, 500] * 4,
                            min_observations=4, alert_cooldown_hours=0)
        result = run_backtest(self.conn, config.watches[0], config.settings)
        self.assertGreater(result.alerts_per_month, 0)
        self.assertIn("a month", "\n".join(format_result(result)))

    def test_a_ceiling_alert_shows_up_in_the_replay(self):
        config = make_config(
            make_watch("tokyo", max_price_alert=900), min_observations=99
        )
        for index, price in enumerate([1000, 1000, 880]):
            execute_run(config, self.conn, StubFetcher(default=price),
                        START + timedelta(days=index), sleep=no_sleep)
        result = run_backtest(self.conn, config.watches[0], config.settings)
        self.assertEqual(result.first_alert.price, 880)
        self.assertIn("under_ceiling", result.first_alert.reasons)


class Sweeping(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_a_looser_threshold_never_alerts_less_often(self):
        config = make_config(make_watch("tokyo"), min_observations=4,
                             alert_cooldown_hours=0)
        prices = [1000, 950, 1100, 900, 1050, 980, 870, 1020, 940, 890, 960, 910]
        for index, price in enumerate(prices):
            execute_run(config, self.conn, StubFetcher(default=price),
                        START + timedelta(days=index), sleep=no_sleep)

        results = sweep(self.conn, config.watches[0], config.settings,
                        percentiles=(10, 20, 30, 40))
        counts = [len(r.alerts) for r in results]
        self.assertEqual(counts, sorted(counts))

    def test_the_table_names_every_threshold_tried(self):
        config = make_config(make_watch("tokyo"), min_observations=2)
        for index, price in enumerate([1000, 900, 800, 700]):
            execute_run(config, self.conn, StubFetcher(default=price),
                        START + timedelta(days=index), sleep=no_sleep)

        table = "\n".join(
            format_sweep(sweep(self.conn, config.watches[0], config.settings,
                               percentiles=(10, 25)), "USD")
        )
        self.assertIn("10%", table)
        self.assertIn("25%", table)
        self.assertIn("percentile", table)

    def test_each_sweep_row_keeps_its_own_threshold(self):
        config = make_config(make_watch("tokyo"), min_observations=2)
        for index, price in enumerate([1000, 900, 800]):
            execute_run(config, self.conn, StubFetcher(default=price),
                        START + timedelta(days=index), sleep=no_sleep)
        results = sweep(self.conn, config.watches[0], config.settings,
                        percentiles=(10, 25))
        self.assertEqual([r.settings.percentile for r in results], [10, 25])


if __name__ == "__main__":
    unittest.main()
