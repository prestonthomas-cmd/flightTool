import unittest
from datetime import date, datetime, timedelta, timezone

from flighttracker.config import Settings
from flighttracker.forecast import (
    FALLING,
    curve_for_watch,
    recent_move,
    FLAT,
    RISING,
    UNKNOWN,
    bucket_for_days,
    build_curve,
    build_forecast,
    build_trend,
    compare_neighbours,
    holiday_note,
    theil_sen,
)
from flighttracker.store import HorizonSample, RunPoint, connect, to_iso

from .support import make_watch

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def runs(*prices, step_hours=12):
    """A price history ending now, one point every `step_hours`."""
    count = len(prices)
    return [
        RunPoint(to_iso(NOW - timedelta(hours=step_hours * (count - 1 - i))), float(p))
        for i, p in enumerate(prices)
    ]


class Slopes(unittest.TestCase):
    def test_a_clean_line_gives_its_own_slope(self):
        points = [(float(x), 100.0 + 10 * x) for x in range(6)]
        self.assertAlmostEqual(theil_sen(points), 10.0)

    def test_one_wild_outlier_does_not_move_it(self):
        points = [(float(x), 100.0 + 10 * x) for x in range(8)]
        points[4] = (4.0, 9000.0)
        self.assertAlmostEqual(theil_sen(points), 10.0)

    def test_too_few_points_gives_nothing(self):
        self.assertIsNone(theil_sen([(1.0, 2.0)]))


class Trends(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(min_trend_observations=4)

    def test_a_falling_price_reads_as_falling(self):
        trend = build_trend(runs(1000, 960, 920, 880, 840), self.settings)
        self.assertEqual(trend.direction, FALLING)
        self.assertLess(trend.per_week, 0)
        self.assertIn("drifting down", trend.describe("USD"))

    def test_a_rising_price_reads_as_rising(self):
        trend = build_trend(runs(840, 880, 920, 960, 1000), self.settings)
        self.assertEqual(trend.direction, RISING)
        self.assertIn("drifting up", trend.describe("USD"))

    def test_noise_around_a_level_reads_as_flat(self):
        trend = build_trend(runs(900, 905, 898, 903, 899, 901), self.settings)
        self.assertEqual(trend.direction, FLAT)
        self.assertIn("holding steady", trend.describe("USD"))

    def test_too_little_history_gives_no_trend(self):
        self.assertIsNone(build_trend(runs(900, 880), self.settings))

    def test_only_the_recent_window_counts(self):
        settings = Settings(min_trend_observations=4, trend_window_runs=4)
        # A long fall, then a flat stretch: only the flat stretch is in window.
        trend = build_trend(runs(2000, 1500, 1000, 900, 900, 901, 899), settings)
        self.assertEqual(trend.observations, 4)
        self.assertEqual(trend.direction, FLAT)


class RecentMoves(unittest.TestCase):
    """A step change that a robust slope is designed not to notice."""

    def setUp(self):
        self.settings = Settings(
            move_recent_runs=3, move_baseline_runs=6, move_threshold=0.05
        )

    def test_a_cliff_after_a_long_flat_stretch_is_caught(self):
        history = runs(1000, 1005, 995, 1000, 1002, 998, 800, 790, 795)
        move = recent_move(history, self.settings)
        self.assertIsNotNone(move)
        self.assertEqual(move.direction, FALLING)
        self.assertLess(move.fraction, -0.15)
        self.assertIn("down 20%", move.describe("USD"))

    def test_the_drift_line_misses_that_same_cliff(self):
        """Why the step is measured separately at all.

        Over a realistic history the cliff is a handful of runs among dozens,
        so the median pairwise slope barely registers it — which is exactly
        what a robust estimator is supposed to do, and exactly why it cannot be
        the only thing reporting on the recent past.
        """
        flat = [1000, 1005, 995, 1002, 998] * 12
        history = runs(*flat, 800, 790, 795, 792, 798)
        settings = Settings(min_trend_observations=4, move_recent_runs=5,
                            move_baseline_runs=20)

        self.assertEqual(build_trend(history, settings).direction, FLAT)
        self.assertEqual(recent_move(history, settings).direction, FALLING)

    def test_a_jump_upwards_is_caught_too(self):
        history = runs(800, 795, 805, 800, 802, 798, 1000, 1010, 1005)
        self.assertEqual(recent_move(history, self.settings).direction, RISING)

    def test_ordinary_wobble_is_not_a_move(self):
        history = runs(1000, 1005, 995, 1000, 1002, 998, 1001, 999, 1003)
        self.assertIsNone(recent_move(history, self.settings))

    def test_too_little_history_gives_no_move(self):
        self.assertIsNone(recent_move(runs(1000, 900, 800), self.settings))

    def test_a_move_speaks_for_the_watch_over_the_drift(self):
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        flat = [1000, 1005, 995, 1002, 998] * 12
        outlook = build_forecast(
            conn,
            make_watch("t"),
            runs(*flat, 800, 790, 795, 792, 798),
            Settings(min_trend_observations=4, move_recent_runs=5,
                     move_baseline_runs=20),
            NOW,
        )
        self.assertEqual(outlook.direction, FALLING)
        self.assertTrue(any("down 20%" in note for note in outlook.notes))
        # The drift line is superseded, not printed alongside as a contradiction.
        self.assertFalse(any("holding steady" in note for note in outlook.notes))


class CurveSelection(unittest.TestCase):
    """A route curve is only worth preferring when it can actually form."""

    def setUp(self):
        self.settings = Settings(horizon_min_bucket_samples=2, horizon_min_watches=2)

    def test_a_lone_watch_on_a_route_falls_back_to_the_pool(self):
        rows = samples("solo", "AUS", "DEN", [(100, 300), (95, 290), (50, 250), (45, 240)])
        rows += samples("a", "JFK", "HND", [(100, 900), (95, 890), (50, 800), (45, 790)])
        rows += samples("b", "SFO", "LIS", [(100, 900), (95, 890), (50, 800), (45, 790)])

        routed = build_curve(rows, self.settings, "AUS", "DEN")
        self.assertFalse(routed.usable)

        chosen = curve_for_watch(rows, self.settings, "AUS", "DEN")
        self.assertTrue(chosen.usable)
        self.assertEqual(chosen.scope, "all watches")

    def test_a_route_with_enough_watches_is_preferred(self):
        rows = samples("a", "JFK", "HND", [(100, 900), (95, 890), (50, 800), (45, 790)])
        rows += samples("b", "JFK", "HND", [(100, 900), (95, 890), (50, 800), (45, 790)])
        chosen = curve_for_watch(rows, self.settings, "JFK", "HND")
        self.assertEqual(chosen.scope, "JFK-HND")


class Buckets(unittest.TestCase):
    def test_days_land_in_the_expected_bucket(self):
        self.assertEqual(bucket_for_days(0), (0, 2))
        self.assertEqual(bucket_for_days(25), (21, 29))
        self.assertEqual(bucket_for_days(200), (180, 730))

    def test_edges_sit_on_the_advance_purchase_boundaries(self):
        """Airlines write fare rules at 21, 14, 7 and 3 days out."""
        for boundary in (3, 7, 14, 21):
            self.assertEqual(bucket_for_days(boundary)[0], boundary)
            self.assertEqual(bucket_for_days(boundary - 1)[1], boundary - 1)

    def test_a_departure_years_away_is_off_the_scale(self):
        self.assertIsNone(bucket_for_days(2000))


def samples(watch_id, origin, destination, pairs):
    """`pairs` is (days_before_departure, price)."""
    depart = date(2027, 6, 1)
    return [
        HorizonSample(
            watch_id=watch_id,
            origin=origin,
            destination=destination,
            observed_on=to_iso(
                datetime(2027, 6, 1, tzinfo=timezone.utc) - timedelta(days=days)
            ),
            depart_date=depart.isoformat(),
            price=float(price),
        )
        for days, price in pairs
    ]


class HorizonCurves(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(horizon_min_bucket_samples=2, horizon_min_watches=2)

    def curve_of(self, *watch_specs, **kwargs):
        rows = []
        for watch_id, pairs in watch_specs:
            rows.extend(samples(watch_id, "JFK", "HND", pairs))
        return build_curve(rows, self.settings, **kwargs)

    def test_a_single_watch_never_forms_a_curve(self):
        """One watch cannot separate a horizon effect from the calendar."""
        curve = self.curve_of(("a", [(100, 900), (95, 890), (50, 800), (45, 790)]))
        self.assertFalse(curve.usable)

    def test_two_watches_with_overlapping_histories_do(self):
        pairs = [(100, 900), (95, 890), (50, 800), (45, 790)]
        curve = self.curve_of(("a", pairs), ("b", pairs))
        self.assertTrue(curve.usable)
        self.assertEqual(len(curve.buckets), 2)

    def test_prices_are_normalised_so_routes_can_be_pooled(self):
        cheap = [(100, 200), (95, 200), (50, 100), (45, 100)]
        dear = [(100, 2000), (95, 2000), (50, 1000), (45, 1000)]
        curve = self.curve_of(("cheap", cheap), ("dear", dear))
        far = curve.bucket_for(97)
        near = curve.bucket_for(47)
        # Both watches halve, so the index halves regardless of ticket size.
        self.assertAlmostEqual(near.index / far.index, 0.5, places=6)

    def test_buckets_ahead_are_the_ones_closer_to_departure(self):
        pairs = [(100, 900), (95, 890), (50, 800), (45, 790)]
        curve = self.curve_of(("a", pairs), ("b", pairs))
        ahead = curve.ahead_of(100)
        self.assertTrue(all(b.high < 100 for b in ahead))

    def test_a_thin_bucket_is_left_out(self):
        settings = Settings(horizon_min_bucket_samples=10, horizon_min_watches=2)
        pairs = [(100, 900), (95, 890), (50, 800), (45, 790)]
        curve = build_curve(
            samples("a", "JFK", "HND", pairs) + samples("b", "JFK", "HND", pairs),
            settings,
        )
        self.assertFalse(curve.usable)

    def test_the_route_is_preferred_when_it_has_data(self):
        rows = samples("a", "JFK", "HND", [(100, 900), (95, 890)])
        rows += samples("b", "JFK", "HND", [(100, 900), (95, 890)])
        rows += samples("c", "SFO", "LIS", [(100, 400), (95, 390)])
        curve = build_curve(rows, self.settings, origin="JFK", destination="HND")
        self.assertEqual(curve.scope, "JFK-HND")

    def test_an_unseen_route_falls_back_to_the_pool(self):
        rows = samples("a", "JFK", "HND", [(100, 900), (95, 890)])
        rows += samples("b", "JFK", "HND", [(100, 900), (95, 890)])
        curve = build_curve(rows, self.settings, origin="AUS", destination="DEN")
        self.assertEqual(curve.scope, "all watches")

    def test_observations_after_departure_are_ignored(self):
        rows = samples("a", "JFK", "HND", [(-5, 900), (100, 900), (95, 890)])
        rows += samples("b", "JFK", "HND", [(100, 900), (95, 890)])
        curve = build_curve(rows, self.settings)
        self.assertEqual(curve.samples, 4)


class Neighbours(unittest.TestCase):
    def test_a_genuinely_cheap_date_is_called_out(self):
        rows = [
            ("2026-12-10", 700.0),
            ("2026-12-11", 950.0),
            ("2026-12-12", 980.0),
        ]
        note = compare_neighbours(rows, "2026-12-10", "USD")
        self.assertIn("below the 2 later one(s)", note)

    def test_a_flat_window_is_reported_as_route_wide(self):
        rows = [("2026-12-10", 900.0), ("2026-12-11", 905.0), ("2026-12-12", 902.0)]
        note = compare_neighbours(rows, "2026-12-10", "USD")
        self.assertIn("route-wide pricing", note)

    def test_earlier_and_later_dates_are_reported_separately(self):
        rows = [
            ("2026-12-09", 800.0),
            ("2026-12-10", 700.0),
            ("2026-12-11", 1000.0),
        ]
        note = compare_neighbours(rows, "2026-12-10", "USD")
        self.assertIn("earlier departure(s)", note)
        self.assertIn("later one(s)", note)

    def test_a_single_date_has_no_neighbours(self):
        self.assertIsNone(compare_neighbours([("2026-12-10", 700.0)], "2026-12-10", "USD"))

    def test_several_returns_on_one_date_collapse_to_the_cheapest(self):
        rows = [
            ("2026-12-10", 900.0),
            ("2026-12-10", 700.0),
            ("2026-12-20", 1200.0),
        ]
        note = compare_neighbours(rows, "2026-12-10", "USD")
        self.assertIn("USD 500 below the 1 later one(s)", note)


class HolidayContext(unittest.TestCase):
    def test_a_cheap_non_holiday_date_beside_holiday_dates_is_flagged(self):
        note = holiday_note("2026-12-10", ["2026-12-10", "2026-12-20", "2026-12-22"])
        self.assertIn("not the same trip", note)
        self.assertIn("Christmas", note)

    def test_a_peak_date_just_reports_its_position(self):
        note = holiday_note("2026-12-20", ["2026-12-20", "2026-12-21"])
        self.assertIn("Christmas", note)
        self.assertNotIn("not the same trip", note)

    def test_a_window_with_no_holiday_in_it_says_so(self):
        note = holiday_note("2026-10-03", ["2026-10-03", "2026-10-04"])
        self.assertEqual(note, "no major holiday nearby")

    def test_no_date_gives_no_note(self):
        self.assertIsNone(holiday_note(None, []))


class CombinedForecast(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        self.settings = Settings(min_trend_observations=4)
        self.watch = make_watch("tokyo")

    def forecast(self, history, **kwargs):
        return build_forecast(
            self.conn, self.watch, history, self.settings, NOW, **kwargs
        )

    def test_no_data_at_all_gives_no_view(self):
        outlook = self.forecast([])
        self.assertEqual(outlook.direction, UNKNOWN)
        self.assertFalse(outlook.known)
        self.assertIn("collecting data", outlook.headline)

    def test_a_trend_alone_is_low_confidence(self):
        outlook = self.forecast(runs(1000, 960, 920, 880, 840))
        self.assertEqual(outlook.direction, FALLING)
        self.assertEqual(outlook.confidence, "low")
        self.assertIn("no horizon curve yet", outlook.headline)

    def test_the_missing_curve_is_stated_rather_than_hidden(self):
        outlook = self.forecast(runs(1000, 960, 920, 880, 840))
        self.assertTrue(any("not enough data yet" in n for n in outlook.notes))

    def test_days_out_is_measured_from_the_chosen_departure(self):
        outlook = self.forecast([], best_depart="2026-09-18")
        self.assertEqual(outlook.days_out, 30)

    def test_neighbour_and_holiday_notes_need_no_history(self):
        outlook = self.forecast(
            [],
            best_depart="2026-12-10",
            run_rows=[("2026-12-10", 700.0), ("2026-12-20", 1100.0)],
        )
        self.assertTrue(any("below the 1 later" in n for n in outlook.notes))
        self.assertTrue(any("not the same trip" in n for n in outlook.notes))

    def seed_curve(self, shape):
        """Store two watches' worth of history so a real curve can form.

        `shape` maps days-before-departure to a price multiplier.
        """
        from flighttracker.store import Observation, record_observations

        for watch_id in ("alpha", "beta"):
            for days, factor in shape:
                observed = NOW - timedelta(days=days)
                record_observations(
                    self.conn,
                    to_iso(observed),
                    [
                        Observation(
                            watch_id=watch_id,
                            price=1000.0 * factor,
                            depart_date=(observed.date() + timedelta(days=days)).isoformat(),
                            return_date=None,
                            origin="JFK",
                            destination="HND",
                        )
                    ],
                )

    def test_a_curve_that_falls_ahead_says_prices_usually_fall_further(self):
        # Expensive far out, cheap near in: waiting has paid.
        self.seed_curve(
            [(d, 1.30) for d in range(120, 128)] + [(d, 0.85) for d in range(60, 68)]
        )
        outlook = self.forecast([], best_depart=(NOW.date() + timedelta(days=125)).isoformat())
        self.assertEqual(outlook.direction, FALLING)
        self.assertIn("fall further", outlook.headline)
        self.assertTrue(any("typically fall a further" in n for n in outlook.notes))

    def test_a_curve_that_rises_ahead_warns_against_waiting(self):
        # Cheap far out, expensive near in: waiting has cost money.
        self.seed_curve(
            [(d, 0.85) for d in range(120, 128)] + [(d, 1.30) for d in range(60, 68)]
        )
        outlook = self.forecast([], best_depart=(NOW.date() + timedelta(days=125)).isoformat())
        self.assertEqual(outlook.direction, RISING)
        self.assertIn("rise from here", outlook.headline)

    def test_trend_and_curve_agreeing_lifts_confidence(self):
        self.seed_curve(
            [(d, 1.30) for d in range(120, 128)] + [(d, 0.85) for d in range(60, 68)]
        )
        outlook = self.forecast(
            runs(1000, 960, 920, 880, 840),
            best_depart=(NOW.date() + timedelta(days=125)).isoformat(),
        )
        self.assertEqual(outlook.direction, FALLING)
        self.assertEqual(outlook.confidence, "medium")

    def test_a_disagreement_is_stated_not_averaged_away(self):
        self.seed_curve(
            [(d, 0.85) for d in range(120, 128)] + [(d, 1.30) for d in range(60, 68)]
        )
        outlook = self.forecast(
            runs(1000, 960, 920, 880, 840),
            best_depart=(NOW.date() + timedelta(days=125)).isoformat(),
        )
        self.assertEqual(outlook.direction, RISING)
        self.assertEqual(outlook.confidence, "low")
        self.assertIn("disagrees", outlook.headline)

    def test_a_forecast_never_reports_high_confidence(self):
        for history in ([], runs(1000, 960, 920, 880, 840)):
            self.assertIn(self.forecast(history).confidence, {"none", "low", "medium"})


if __name__ == "__main__":
    unittest.main()
