import unittest
from datetime import datetime, timedelta, timezone

from flighttracker.config import Settings
from flighttracker.signals import (
    ALL_TIME_LOW,
    required_discount,
    volatility_of,
    BELOW_PERCENTILE,
    UNDER_CEILING,
    evaluate,
    percentile,
    summarize,
)
from flighttracker.store import RunPoint, SentAlert, to_iso

from .support import make_watch

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def history(*prices) -> list[RunPoint]:
    return [
        RunPoint(to_iso(NOW - timedelta(hours=12 * (len(prices) - i))), float(price))
        for i, price in enumerate(prices)
    ]


class Percentile(unittest.TestCase):
    def test_matches_linear_interpolation(self):
        values = [100, 200, 300, 400, 500]
        self.assertEqual(percentile(values, 20), 180)
        self.assertEqual(percentile(values, 50), 300)
        self.assertEqual(percentile(values, 0), 100)
        self.assertEqual(percentile(values, 100), 500)

    def test_single_value_is_its_own_percentile(self):
        self.assertEqual(percentile([742], 20), 742)

    def test_order_does_not_matter(self):
        self.assertEqual(percentile([500, 100, 300, 200, 400], 20), 180)

    def test_empty_and_out_of_range_are_refused(self):
        with self.assertRaises(ValueError):
            percentile([], 20)
        with self.assertRaises(ValueError):
            percentile([1, 2], 101)


class Summarising(unittest.TestCase):
    def test_stats_come_from_the_supplied_history(self):
        stats = summarize(history(500, 700, 900), make_watch(), Settings(percentile=20))
        self.assertEqual(stats.count, 3)
        self.assertEqual(stats.minimum, 500)
        self.assertEqual(stats.maximum, 900)
        self.assertEqual(stats.median, 700)
        self.assertEqual(stats.threshold, 580)

    def test_no_history_is_not_an_error(self):
        stats = summarize([], make_watch(), Settings())
        self.assertEqual(stats.count, 0)
        self.assertFalse(stats.has_history)


class BuySignals(unittest.TestCase):
    def setUp(self):
        # A flat threshold unless a test is specifically about the adaptive one,
        # so the percentile behaviour is exercised in isolation.
        self.settings = Settings(
            min_observations=5, percentile=20, adaptive_discount=False
        )

    def judge(self, price, past, watch=None, last=None, settings=None):
        return evaluate(
            watch=watch or make_watch(),
            price=price,
            history=past,
            settings=settings or self.settings,
            currency="USD",
            last=last,
            now=NOW,
        )

    def test_a_new_all_time_low_is_flagged(self):
        verdict = self.judge(450, history(500, 600, 700, 800, 900))
        self.assertTrue(verdict.flagged)
        self.assertEqual([r.code for r in verdict.reasons], [ALL_TIME_LOW])
        self.assertIn("lowest seen in 5 runs", verdict.reasons[0].detail)

    def test_a_price_inside_the_bottom_percentile_is_flagged(self):
        verdict = self.judge(560, history(500, 600, 700, 800, 900))
        self.assertTrue(verdict.flagged)
        self.assertEqual([r.code for r in verdict.reasons], [BELOW_PERCENTILE])

    def test_an_ordinary_price_is_not_flagged(self):
        verdict = self.judge(750, history(500, 600, 700, 800, 900))
        self.assertFalse(verdict.flagged)
        self.assertEqual(verdict.reasons, ())

    def test_an_all_time_low_does_not_also_report_the_percentile(self):
        verdict = self.judge(450, history(500, 600, 700, 800, 900))
        self.assertEqual(len(verdict.reasons), 1)

    def test_history_shorter_than_the_minimum_never_alerts_on_statistics(self):
        verdict = self.judge(100, history(500, 600))
        self.assertFalse(verdict.flagged)
        self.assertIn("building history", verdict.suppressed)
        self.assertIn("2 of 5", verdict.suppressed)

    def test_a_ceiling_fires_with_no_history_at_all(self):
        watch = make_watch(max_price_alert=900)
        verdict = self.judge(880, [], watch=watch)
        self.assertTrue(verdict.flagged)
        self.assertEqual([r.code for r in verdict.reasons], [UNDER_CEILING])

    def test_a_ceiling_and_a_low_are_both_reported(self):
        watch = make_watch(max_price_alert=900)
        verdict = self.judge(450, history(500, 600, 700, 800, 900), watch=watch)
        self.assertEqual(
            [r.code for r in verdict.reasons], [UNDER_CEILING, ALL_TIME_LOW]
        )

    def test_a_price_above_the_ceiling_does_not_fire_it(self):
        watch = make_watch(max_price_alert=400)
        verdict = self.judge(450, history(500, 600, 700, 800, 900), watch=watch)
        self.assertEqual([r.code for r in verdict.reasons], [ALL_TIME_LOW])

    def test_a_missing_price_is_reported_not_treated_as_free(self):
        verdict = self.judge(None, history(500, 600, 700, 800, 900))
        self.assertFalse(verdict.flagged)
        self.assertIsNone(verdict.price)
        self.assertIn("no price returned", verdict.suppressed)

    def test_a_price_that_never_moves_does_not_alert_every_run(self):
        """The 20th percentile of a flat series is the series itself.

        Found by the backtest: the rule fired on an entirely ordinary price on
        every run, because `price <= threshold` is trivially true when the
        distribution has no spread.
        """
        verdict = self.judge(900, history(900, 900, 900, 900, 900))
        self.assertFalse(verdict.flagged)

    def test_one_old_outlier_does_not_make_the_modal_price_cheap(self):
        """With ties, the threshold lands on the price everything else sits at."""
        verdict = self.judge(1000, history(400, 1000, 1000, 1000, 1000, 1000, 1000))
        self.assertFalse(verdict.flagged)

    def test_a_barely_below_median_price_is_not_worth_an_email(self):
        settings = Settings(min_observations=3, percentile=50, min_discount=0.02,
                            adaptive_discount=False)
        verdict = self.judge(699, history(500, 700, 900), settings=settings)
        self.assertFalse(verdict.flagged)

    def test_a_real_discount_still_alerts_at_a_high_percentile(self):
        """The guard must not quietly disable percentiles near the median."""
        settings = Settings(min_observations=3, percentile=50, min_discount=0.02,
                            adaptive_discount=False)
        verdict = self.judge(650, history(500, 700, 900), settings=settings)
        self.assertTrue(verdict.flagged)
        self.assertEqual([r.code for r in verdict.reasons], [BELOW_PERCENTILE])

    def test_an_all_time_low_is_unaffected_by_the_discount_guard(self):
        settings = Settings(min_observations=3, min_discount=0.50,
                            adaptive_discount=False)
        verdict = self.judge(899, history(900, 900, 900, 900, 900), settings=settings)
        self.assertTrue(verdict.flagged)
        self.assertEqual([r.code for r in verdict.reasons], [ALL_TIME_LOW])

    def test_per_watch_overrides_beat_the_global_settings(self):
        watch = make_watch(min_observations=2, percentile=50)
        verdict = self.judge(650, history(500, 700, 900), watch=watch)
        self.assertTrue(verdict.flagged)
        self.assertEqual(verdict.stats.percentile_used, 50)


class VolatilityAwareThresholds(unittest.TestCase):
    """"Wait" is only advice worth giving if the price actually moves."""

    def judge(self, price, past, settings):
        return evaluate(
            watch=make_watch(),
            price=price,
            history=past,
            settings=settings,
            currency="USD",
            now=NOW,
        )

    def test_volatility_is_measured_robustly(self):
        steady = [900, 902, 898, 901, 899]
        self.assertLess(volatility_of(steady), 0.01)

        swinging = [700, 1100, 800, 1200, 900]
        self.assertGreater(volatility_of(swinging), 0.10)

    def test_one_outlier_does_not_make_a_stable_fare_look_volatile(self):
        self.assertLess(volatility_of([900, 902, 898, 901, 5000]), 0.01)

    def test_too_little_history_has_no_volatility(self):
        self.assertIsNone(volatility_of([900, 910]))

    def test_a_stable_watch_keeps_the_floor(self):
        settings = Settings(min_discount=0.02, discount_volatility_multiple=0.75)
        self.assertAlmostEqual(required_discount(0.001, settings), 0.02)

    def test_a_volatile_watch_demands_a_bigger_drop(self):
        settings = Settings(min_discount=0.02, discount_volatility_multiple=0.75)
        self.assertAlmostEqual(required_discount(0.20, settings), 0.15)

    def test_the_bar_is_capped(self):
        settings = Settings(max_discount=0.25, discount_volatility_multiple=0.75)
        self.assertAlmostEqual(required_discount(0.90, settings), 0.25)

    def test_turning_it_off_restores_a_flat_bar(self):
        settings = Settings(min_discount=0.02, adaptive_discount=False)
        self.assertAlmostEqual(required_discount(0.40, settings), 0.02)

    def test_an_ordinary_dip_on_a_wild_series_does_not_alert(self):
        settings = Settings(min_observations=3, percentile=50)
        # Swings of ~29%: a 7% dip is just Tuesday.
        self.assertFalse(self.judge(650, history(500, 700, 900), settings).flagged)

    def test_the_same_dip_does_alert_on_a_stable_series(self):
        settings = Settings(min_observations=3, percentile=50)
        steady = history(900, 902, 898, 901, 899, 903)
        self.assertTrue(self.judge(840, steady, settings).flagged)

    def test_on_a_stable_fare_only_a_real_low_can_qualify(self):
        """A worthwhile consequence, not an accident.

        If a price never moves, "in the cheapest 20%" carries no information —
        any price low enough to clear the discount bar is by then also an
        all-time low, so that is what gets reported.
        """
        settings = Settings(min_observations=3, percentile=50)
        steady = history(900, 902, 898, 901, 899, 903)
        verdict = self.judge(840, steady, settings)
        self.assertEqual([r.code for r in verdict.reasons], [ALL_TIME_LOW])

    def test_the_reason_explains_where_the_bar_came_from(self):
        settings = Settings(min_observations=3, percentile=50)
        # Mild movement: 900 clears the bar without being a new low.
        moderate = history(800, 950, 1000, 1000, 1050, 1200)
        verdict = self.judge(900, moderate, settings)

        self.assertEqual([r.code for r in verdict.reasons], [BELOW_PERCENTILE])
        detail = verdict.reasons[0].detail
        self.assertIn("below its median", detail)
        self.assertIn("volatility", detail)


class Cooldown(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            min_observations=3, alert_cooldown_hours=48, alert_improvement=0.03
        )

    def judge(self, price, last, now=NOW):
        return evaluate(
            watch=make_watch(),
            price=price,
            history=history(500, 600, 700, 800, 900),
            settings=self.settings,
            currency="USD",
            last=last,
            now=now,
        )

    def test_a_repeat_of_a_recent_alert_is_held_back(self):
        recent = SentAlert(to_iso(NOW - timedelta(hours=6)), 450.0, "all_time_low")
        verdict = self.judge(450, recent)
        self.assertFalse(verdict.flagged)
        self.assertTrue(verdict.reasons)
        self.assertIn("already alerted", verdict.suppressed)

    def test_a_meaningful_further_drop_gets_through(self):
        recent = SentAlert(to_iso(NOW - timedelta(hours=6)), 450.0, "all_time_low")
        self.assertTrue(self.judge(400, recent).flagged)

    def test_a_trivial_further_drop_stays_held(self):
        recent = SentAlert(to_iso(NOW - timedelta(hours=6)), 450.0, "all_time_low")
        self.assertFalse(self.judge(449, recent).flagged)

    def test_once_the_cooldown_expires_it_alerts_again(self):
        old = SentAlert(to_iso(NOW - timedelta(hours=72)), 450.0, "all_time_low")
        self.assertTrue(self.judge(449, old).flagged)

    def test_a_cooldown_of_zero_disables_the_hold(self):
        self.settings = Settings(min_observations=3, alert_cooldown_hours=0)
        recent = SentAlert(to_iso(NOW - timedelta(hours=1)), 450.0, "all_time_low")
        self.assertTrue(self.judge(450, recent).flagged)


if __name__ == "__main__":
    unittest.main()
