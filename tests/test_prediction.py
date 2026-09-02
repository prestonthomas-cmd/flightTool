"""The readings added on top of the buy signal: waiting, weekday, date
standing, and re-basing history onto the booking window."""

import unittest
from datetime import date, datetime, timedelta, timezone

from flighttracker.config import Settings
from flighttracker.forecast import (
    MIN_BAND,
    HorizonBucket,
    HorizonCurve,
    build_weekday_profile,
    date_standing,
    horizon_adjust,
    project,
    rebase,
    waiting_record,
)
from flighttracker.store import HorizonSample, RunPoint, to_iso

from .support import make_watch

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def runs(*prices):
    count = len(prices)
    return [
        RunPoint(to_iso(NOW - timedelta(days=count - 1 - i)), float(p))
        for i, p in enumerate(prices)
    ]


class DidWaitingPay(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(min_waiting_cases=3, waiting_lookahead_runs=3)

    def test_a_price_that_kept_falling_says_waiting_paid(self):
        history = runs(*range(1200, 700, -25))
        record = waiting_record(history, 1100, self.settings)
        self.assertIsNotNone(record)
        self.assertEqual(record.followed_lower, record.cases)
        self.assertGreater(record.median_drop, 0)
        self.assertIn("something cheaper followed", record.describe())
        self.assertIn("priced around here", record.describe())

    def test_a_price_on_a_rising_series_says_waiting_did_not(self):
        # Prices only went up from here, so nothing cheaper ever came along.
        history = runs(*range(700, 1200, 25))
        record = waiting_record(history, 900, self.settings)
        self.assertIsNotNone(record)
        self.assertEqual(record.followed_lower, 0)
        self.assertIn("nothing cheaper ever followed", record.describe())

    def test_runs_near_the_end_are_excluded_for_having_no_future(self):
        """Counting them would bias the answer towards 'waiting never paid'."""
        history = runs(*range(1200, 700, -25))
        settings = Settings(min_waiting_cases=1, waiting_lookahead_runs=5)
        record = waiting_record(history, 1100, settings)
        self.assertIsNotNone(record)
        self.assertLessEqual(record.cases, len(history) - 5)

    def test_too_few_comparable_runs_gives_nothing(self):
        settings = Settings(min_waiting_cases=50, waiting_lookahead_runs=2)
        self.assertIsNone(waiting_record(runs(1000, 900, 800, 700), 800, settings))

    def test_no_history_gives_nothing(self):
        self.assertIsNone(waiting_record([], 900, self.settings))

    def test_only_runs_priced_similarly_are_counted(self):
        history = runs(*([1000] * 10 + [500] * 10))
        cheap = waiting_record(history, 500, self.settings)
        dear = waiting_record(history, 1000, self.settings)
        self.assertIsNotNone(cheap)
        self.assertIsNotNone(dear)
        # The dear runs all had cheaper prices ahead of them; the cheap ones did not.
        self.assertGreater(dear.share, cheap.share)


def samples_for(watch_id, departures, price=1000.0, origin="JFK", destination="HND"):
    return [
        HorizonSample(
            watch_id=watch_id,
            origin=origin,
            destination=destination,
            observed_on=to_iso(NOW),
            depart_date=day.isoformat(),
            price=price,
        )
        for day in departures
    ]


class WeekdayProfiles(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(weekday_min_samples=2, weekday_min_watches=2)

    def build(self, rows):
        return build_weekday_profile(rows, self.settings)

    def days(self, weekday, count):
        """`count` dates all falling on the given weekday."""
        start = date(2027, 3, 1)  # a Monday
        first = start + timedelta(days=(weekday - start.weekday()) % 7)
        return [first + timedelta(weeks=n) for n in range(count)]

    def test_a_single_watch_never_forms_a_profile(self):
        rows = samples_for("a", self.days(1, 4), 900)
        rows += samples_for("a", self.days(4, 4), 1200)
        self.assertFalse(self.build(rows).usable)

    def test_two_watches_reveal_the_cheap_day(self):
        rows = []
        for watch in ("a", "b"):
            rows += samples_for(watch, self.days(1, 3), 900)    # Tuesday
            rows += samples_for(watch, self.days(4, 3), 1200)   # Friday
            rows += samples_for(watch, self.days(6, 3), 1100)   # Sunday
        profile = self.build(rows)

        self.assertTrue(profile.usable)
        self.assertEqual(profile.cheapest, 1)
        self.assertIn("Tuesday is the cheapest", profile.standing(1))

    def test_a_dear_day_is_quantified_against_the_best_one(self):
        rows = []
        for watch in ("a", "b"):
            rows += samples_for(watch, self.days(1, 3), 1000)
            rows += samples_for(watch, self.days(4, 3), 1200)
            rows += samples_for(watch, self.days(6, 3), 1100)
        standing = self.build(rows).standing(4)
        self.assertIn("Friday", standing)
        self.assertIn("20%", standing)
        self.assertIn("Tuesday", standing)

    def test_prices_are_normalised_so_watches_of_different_size_can_pool(self):
        rows = samples_for("cheap", self.days(1, 3), 200)
        rows += samples_for("cheap", self.days(4, 3), 240)
        rows += samples_for("dear", self.days(1, 3), 2000)
        rows += samples_for("dear", self.days(4, 3), 2400)
        profile = self.build(rows)
        ratio = profile.index[4] / profile.index[1]
        self.assertAlmostEqual(ratio, 1.2, places=6)

    def test_an_unknown_weekday_has_no_standing(self):
        rows = []
        for watch in ("a", "b"):
            rows += samples_for(watch, self.days(1, 3), 900)
            rows += samples_for(watch, self.days(4, 3), 1200)
            rows += samples_for(watch, self.days(6, 3), 1100)
        self.assertIsNone(self.build(rows).standing(2))


class DateAgainstItsWindow(unittest.TestCase):
    def rows(self, series):
        """`series` is a list of {depart_date: price} per run."""
        out = []
        for index, run in enumerate(series):
            stamp = to_iso(NOW - timedelta(days=len(series) - index))
            for day, price in run.items():
                out.append((stamp, day, float(price)))
        return out

    def test_a_level_date_reads_as_level_not_as_zero_percent(self):
        series = [{"2026-12-10": 1000, "2026-12-20": 1000}] * 6
        series.append({"2026-12-10": 700, "2026-12-20": 1000})
        standing = date_standing(self.rows(series), "2026-12-10")
        self.assertIn("normally level with", standing.describe())
        self.assertNotIn("normally 0%", standing.describe())

    def test_a_date_that_got_unusually_cheap_is_called_out(self):
        # 10 Dec normally sits ~10% under 20 Dec; today it is ~30% under.
        series = [{"2026-12-10": 900, "2026-12-20": 1000}] * 6
        series.append({"2026-12-10": 700, "2026-12-20": 1000})
        standing = date_standing(self.rows(series), "2026-12-10")

        self.assertIsNotNone(standing)
        self.assertAlmostEqual(standing.usual, -0.10, places=6)
        self.assertAlmostEqual(standing.now, -0.30, places=6)
        self.assertIn("unusually well placed", standing.describe())

    def test_a_window_that_moved_together_is_not_a_date_specific_deal(self):
        """The whole point: a market-wide move should not read as an opportunity."""
        series = [{"2026-12-10": 900, "2026-12-20": 1000}] * 6
        series.append({"2026-12-10": 630, "2026-12-20": 700})
        standing = date_standing(self.rows(series), "2026-12-10")

        self.assertAlmostEqual(standing.now, -0.10, places=6)
        self.assertIn("no better placed than usual", standing.describe())

    def test_a_date_that_lost_ground_is_reported_as_such(self):
        series = [{"2026-12-10": 900, "2026-12-20": 1000}] * 6
        series.append({"2026-12-10": 1050, "2026-12-20": 1000})
        standing = date_standing(self.rows(series), "2026-12-10")
        self.assertIn("less well placed", standing.describe())

    def test_a_window_of_one_date_has_nothing_to_sit_against(self):
        series = [{"2026-12-10": 900}] * 8
        self.assertIsNone(date_standing(self.rows(series), "2026-12-10"))

    def test_too_little_history_gives_nothing(self):
        series = [{"2026-12-10": 900, "2026-12-20": 1000}] * 2
        self.assertIsNone(date_standing(self.rows(series), "2026-12-10"))


def curve(*buckets) -> HorizonCurve:
    return HorizonCurve(
        buckets=tuple(
            HorizonBucket(low, high, index, 20, 2) for low, high, index in buckets
        ),
        scope="all watches",
        samples=100,
        watches=2,
    )


class RebasingOntoTheBookingWindow(unittest.TestCase):
    def setUp(self):
        self.departure = NOW.date() + timedelta(days=50)
        # Dearer far out, cheaper near in.
        self.curve = curve((45, 59, 0.90), (60, 89, 1.00), (90, 119, 1.20))

    def test_old_dear_prices_are_marked_down_to_todays_terms(self):
        # A price taken 100 days out, judged from 50 days out.
        old = RunPoint(to_iso(NOW - timedelta(days=50)), 1200.0)
        adjusted, changed = horizon_adjust([old], self.curve, self.departure, NOW)

        self.assertTrue(changed)
        # 1200 * (0.90 / 1.20)
        self.assertAlmostEqual(adjusted[0].price, 900.0)

    def test_prices_already_at_todays_horizon_are_untouched(self):
        here = RunPoint(to_iso(NOW - timedelta(days=2)), 1000.0)
        adjusted, changed = horizon_adjust([here], self.curve, self.departure, NOW)
        self.assertFalse(changed)
        self.assertEqual(adjusted[0].price, 1000.0)

    def test_an_unusable_curve_changes_nothing(self):
        thin = HorizonCurve(buckets=(), scope="all watches", samples=0, watches=0)
        history = [RunPoint(to_iso(NOW - timedelta(days=50)), 1200.0)]
        adjusted, changed = horizon_adjust(history, thin, self.departure, NOW)

        self.assertFalse(changed)
        self.assertEqual([p.price for p in adjusted], [1200.0])

    def test_a_horizon_the_curve_cannot_speak_to_is_left_alone(self):
        far = curve((45, 59, 0.90), (60, 89, 1.00))
        history = [RunPoint(to_iso(NOW - timedelta(days=60)), 1200.0)]
        adjusted, _ = horizon_adjust(history, far, self.departure, NOW)
        self.assertEqual(adjusted[0].price, 1200.0)

    def test_the_setting_switches_it_off_entirely(self):
        watch = make_watch("t", depart=(self.departure,), returns=None)
        history = [RunPoint(to_iso(NOW - timedelta(days=50)), 1200.0)]
        rows = [
            HorizonSample("a", "JFK", "HND", to_iso(NOW), self.departure.isoformat(), 1000.0)
        ]
        _, changed = rebase(
            rows, watch, history, Settings(horizon_adjusted_baseline=False), NOW
        )
        self.assertFalse(changed)

    def test_rebasing_makes_a_stale_high_baseline_less_flattering(self):
        """Why this exists: a months-old, dearer history sets too low a bar."""
        history = [
            RunPoint(to_iso(NOW - timedelta(days=50)), 1200.0),
            RunPoint(to_iso(NOW - timedelta(days=49)), 1180.0),
        ]
        adjusted, _ = horizon_adjust(history, self.curve, self.departure, NOW)
        self.assertLess(
            sum(p.price for p in adjusted), sum(p.price for p in history)
        )


if __name__ == "__main__":
    unittest.main()


class ProjectingForward(unittest.TestCase):
    """The chart's dashed line. It must never claim more than the data supports."""

    def setUp(self):
        self.settings = Settings(min_trend_observations=4)
        self.departure = NOW.date() + timedelta(days=120)
        self.watch = make_watch("t", depart=(self.departure,), returns=None)
        self.thin = HorizonCurve(buckets=(), scope="all watches", samples=0, watches=0)

    def project(self, history, curve=None, **kwargs):
        return project(
            history, self.watch, curve or self.thin, self.settings, NOW, **kwargs
        )

    def test_no_history_means_no_projection(self):
        outlook = self.project([])
        self.assertFalse(outlook.usable)
        self.assertIn("No prices recorded", outlook.note)

    def test_the_typical_pattern_carries_it_all_the_way_to_departure(self):
        outlook = self.project(runs(*([900] * 12)))
        self.assertTrue(outlook.usable)
        self.assertEqual(outlook.method, "typical")
        self.assertEqual(outlook.points[-1].day, self.departure)

    def test_it_says_plainly_that_the_shape_is_not_this_flights_own_history(self):
        note = self.project(runs(*([900] * 12))).note
        self.assertIn("typical advance-purchase pattern", note)
        self.assertIn("not this flight's own history", note)

    def test_the_projection_troughs_and_then_climbs_into_departure(self):
        """The whole point: a flat line told the reader nothing."""
        outlook = self.project(runs(*([900] * 12)))
        prices = [p.price for p in outlook.points]

        self.assertLess(min(prices), prices[0])      # dips below today
        self.assertGreater(prices[-1], prices[0])    # and ends above it
        self.assertGreater(prices[-1], min(prices) * 1.5)

    def test_the_recent_trend_is_reported_in_words_not_drawn_as_the_line(self):
        falling = self.project(runs(*range(1200, 900, -25)))
        self.assertIn("drifting down", falling.note)
        # The geometry still follows the booking pattern, not the slope.
        self.assertEqual(falling.method, "typical")

    def test_a_flat_flight_says_so(self):
        self.assertIn("has been flat", self.project(runs(*([900] * 12))).note)

    def test_the_band_widens_the_further_out_it_goes(self):
        outlook = self.project(runs(*([900] * 12)))
        widths = [(p.high - p.low) / p.price for p in outlook.points]
        self.assertGreater(widths[-1], widths[0])

    def test_a_generic_shape_admits_more_doubt_than_your_own_data(self):
        outlook = self.project(runs(*([900] * 12)))
        first = outlook.points[0]
        self.assertGreaterEqual(
            (first.high - first.price) / first.price, MIN_BAND * 2
        )

    def test_your_own_curve_is_preferred_when_it_can_form(self):
        curve = HorizonCurve(
            buckets=tuple(
                HorizonBucket(low, high, index, 20, 2)
                for low, high, index in (
                    (30, 44, 0.85), (45, 59, 0.90), (60, 89, 0.95),
                    (90, 119, 1.05), (120, 179, 1.15),
                )
            ),
            scope="all watches", samples=200, watches=3,
        )
        outlook = self.project(runs(*([1000] * 12)), curve=curve)

        self.assertEqual(outlook.method, "horizon")
        self.assertIn("your own", outlook.note)
        self.assertLess(outlook.points[-1].price, outlook.points[0].price)

    def test_your_own_curve_carries_a_tighter_band(self):
        curve = HorizonCurve(
            buckets=tuple(
                HorizonBucket(low, high, index, 20, 2)
                for low, high, index in ((90, 119, 1.05), (120, 179, 1.15))
            ),
            scope="all watches", samples=200, watches=3,
        )
        mine = self.project(runs(*([1000] * 12)), curve=curve)
        generic = self.project(runs(*([1000] * 12)))

        def width(p):
            return (p.points[0].high - p.points[0].low) / p.points[0].price

        self.assertLess(width(mine), width(generic))

    def test_the_current_price_anchors_the_projection(self):
        outlook = self.project(runs(*([900] * 12)), price=600.0)
        self.assertLess(outlook.points[0].price, 700)

    def test_it_never_projects_past_departure(self):
        soon = NOW.date() + timedelta(days=40)
        watch = make_watch("t", depart=(soon,), returns=None)
        outlook = project(runs(*([900] * 12)), watch, self.thin, self.settings, NOW)
        for point in outlook.points:
            self.assertLessEqual(point.day, soon)
        self.assertEqual(outlook.points[-1].day, soon)

    def test_a_departure_already_past_projects_nothing(self):
        watch = make_watch("t", depart=(NOW.date() - timedelta(days=1),), returns=None)
        outlook = project(runs(*([900] * 12)), watch, self.thin, self.settings, NOW)
        self.assertFalse(outlook.usable)

    def test_a_departure_within_the_week_is_too_close_to_project(self):
        watch = make_watch("t", depart=(NOW.date() + timedelta(days=3),), returns=None)
        outlook = project(runs(*([900] * 12)), watch, self.thin, self.settings, NOW)
        self.assertFalse(outlook.usable)
        self.assertIn("too close", outlook.note)
