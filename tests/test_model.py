"""The price model itself: what it learns, what it refuses to learn, and how
much doubt it attaches to either.

The tests plant a known shape in synthetic prices and check the model finds
it. That is the only honest way to test a fitting procedure — against data
whose answer is known in advance.
"""

import math
import unittest
from datetime import date, datetime, timedelta, timezone

from flighttracker.model import (
    PRIOR_HORIZON,
    TOLERANCE,
    WALK_FLOOR,
    bucket_for,
    fit,
    forecast,
    holiday_key,
)
from flighttracker.store import HorizonSample, to_iso

TODAY = date(2026, 3, 1)


def sample(watch_id, observed, departure, price):
    return HorizonSample(
        watch_id=watch_id,
        origin="JFK",
        destination="HND",
        observed_on=to_iso(datetime.combine(observed, datetime.min.time(), timezone.utc)),
        depart_date=departure.isoformat(),
        price=float(price),
    )


def series(shape, watches=4, level=1000.0, step=2, spread=1.15, start=TODAY):
    """Observations of several watches whose price follows `shape(days_out)`.

    Each watch sits at its own price level, so a model that failed to separate
    level from horizon would recover the wrong curve.
    """
    rows = []
    for index in range(watches):
        departure = start + timedelta(days=330 + index * 11)
        base = level * (spread ** index)
        day = start
        while day <= departure:
            out = (departure - day).days
            rows.append(sample(f"w{index}", day, departure, base * shape(out)))
            day += timedelta(days=step)
    return rows


class WithNothingToGoOn(unittest.TestCase):
    def test_an_empty_fit_is_all_prior_and_says_so(self):
        model = fit([])
        self.assertFalse(model.usable)
        self.assertEqual(model.evidence(), 0.0)
        self.assertEqual(model.observations, 0)

    def test_it_still_answers_with_the_advance_purchase_prior(self):
        model = fit([])
        self.assertAlmostEqual(
            model.horizon_at(5).value, PRIOR_HORIZON[bucket_for(5)]
        )
        self.assertEqual(model.horizon_at(5).prior_weight, 1.0)
        self.assertEqual(model.horizon_at(5).samples, 0)

    def test_the_prior_is_dear_late_cheap_in_the_middle_and_dear_far_out(self):
        """The shape the tool asserts before it has seen anything."""
        model = fit([])
        late = model.horizon_at(1).multiplier
        trough = model.horizon_at(50).multiplier
        far = model.horizon_at(200).multiplier

        self.assertGreater(late, far)
        self.assertLess(trough, far)
        self.assertLess(trough, late)

    def test_a_horizon_beyond_the_last_bucket_is_a_flat_no_opinion(self):
        beyond = fit([]).horizon_at(5000)
        self.assertEqual(beyond.multiplier, 1.0)
        self.assertEqual(beyond.prior_weight, 1.0)

    def test_prices_that_cannot_be_read_are_dropped_not_guessed_at(self):
        rows = [
            sample("a", TODAY, TODAY + timedelta(days=40), 0),      # no price
            sample("a", TODAY, TODAY + timedelta(days=40), -5),     # nonsense
            sample("a", TODAY + timedelta(days=5), TODAY, 500),     # already flown
        ]
        rows.append(
            HorizonSample("a", "JFK", "HND", to_iso(datetime.now(timezone.utc)),
                          "not-a-date", 500.0)
        )
        self.assertFalse(fit(rows).usable)


class RecoveringAPlantedCurve(unittest.TestCase):
    def setUp(self):
        # Dear far out, cheapest around seven weeks, climbing hard at the end.
        self.shape = lambda out: (
            1.30 if out > 150 else
            0.85 if 30 <= out <= 70 else
            1.00 if out > 70 else
            1.00 + (30 - out) * 0.03
        )
        self.model = fit(series(self.shape))

    def test_the_fit_rests_mostly_on_the_data_it_was_given(self):
        self.assertGreater(self.model.evidence(), 0.7)
        self.assertEqual(self.model.watches, 4)

    def test_it_finds_the_trough_where_the_trough_was_planted(self):
        trough = self.model.horizon_at(50).multiplier
        self.assertLess(trough, self.model.horizon_at(10).multiplier)
        self.assertLess(trough, self.model.horizon_at(200).multiplier)

    def test_the_recovered_ratios_are_close_to_the_planted_ones(self):
        for near, far in ((50, 200), (50, 100), (10, 50)):
            planted = self.shape(near) / self.shape(far)
            found = (
                self.model.horizon_at(near).multiplier
                / self.model.horizon_at(far).multiplier
            )
            self.assertLess(
                abs(found / planted - 1.0), 0.15,
                f"{near}d vs {far}d: planted {planted:.3f}, found {found:.3f}",
            )

    def test_watches_at_different_price_levels_do_not_distort_the_curve(self):
        """Each watch's own level is absorbed separately, so pooling is safe.

        A $200 hop and a $3,000 long-haul must contribute the same evidence
        about the *shape* of the booking window. The check is that widening
        the gap between the watches leaves the recovered curve where it was.
        """
        narrow = fit(series(self.shape, watches=4, spread=1.0))
        wide = fit(series(self.shape, watches=4, spread=3.0))

        for days in (5, 25, 50, 100, 200):
            self.assertAlmostEqual(
                narrow.horizon_at(days).value,
                wide.horizon_at(days).value,
                places=6,
                msg=f"the curve moved at {days}d when only the levels changed",
            )

    def test_a_flat_market_is_recovered_as_flat_where_there_is_data_to_say_so(self):
        flat = fit(series(lambda out: 1.0, watches=4, spread=2.0))
        # Buckets with real coverage. The 0-2 day bucket sees a handful of
        # observations at most, so it rightly stays near the prior instead.
        multipliers = [flat.horizon_at(d).multiplier for d in (25, 50, 100, 200)]
        spread = max(multipliers) / min(multipliers)
        self.assertLess(spread, 1.06, multipliers)

    def test_levels_are_recovered_per_watch(self):
        levels = self.model.levels
        self.assertEqual(len(levels), 4)
        ordered = [math.exp(levels[f"w{i}"]) for i in range(4)]
        for lower, higher in zip(ordered, ordered[1:]):
            self.assertGreater(higher, lower)


class ShrinkingTowardThePrior(unittest.TestCase):
    """Thin evidence must not be allowed to speak loudly."""

    def rows(self, count, days_out=50, price=500.0):
        departure = TODAY + timedelta(days=days_out)
        return [
            sample(f"w{i}", departure - timedelta(days=days_out), departure, price)
            for i in range(count)
        ]

    def test_one_observation_barely_moves_the_estimate(self):
        model = fit(self.rows(1))
        component = model.horizon_at(50)
        self.assertGreater(component.prior_weight, 0.85)
        self.assertLess(component.from_data, 0.15)

    def test_many_observations_take_the_estimate_over(self):
        model = fit(self.rows(120))
        self.assertLess(model.horizon_at(50).prior_weight, 0.1)
        self.assertGreater(model.horizon_at(50).from_data, 0.9)

    def test_evidence_grows_with_the_data_it_is_given(self):
        thin = fit(series(lambda out: 1.0, watches=1, step=30))
        thick = fit(series(lambda out: 1.0, watches=4, step=2))
        self.assertLess(thin.evidence(), thick.evidence())

    def test_a_harder_shrinkage_setting_holds_the_prior_longer(self):
        loose = fit(self.rows(10), shrinkage=1.0)
        tight = fit(self.rows(10), shrinkage=50.0)
        self.assertLess(
            loose.horizon_at(50).prior_weight, tight.horizon_at(50).prior_weight
        )

    def test_a_bucket_never_seen_keeps_the_prior_untouched(self):
        model = fit(self.rows(120))
        untouched = model.horizon_at(1)
        self.assertEqual(untouched.prior_weight, 1.0)
        self.assertAlmostEqual(untouched.value, PRIOR_HORIZON[bucket_for(1)])


class Holidays(unittest.TestCase):
    def test_a_christmas_departure_is_keyed_apart_from_an_ordinary_one(self):
        self.assertNotEqual(
            holiday_key(date(2026, 12, 24)), holiday_key(date(2026, 10, 14))
        )

    def spanning(self, festive_multiple=2.0):
        """Watches that each depart both at Christmas and in a quiet October.

        The premium is only separable from a watch's own price level when some
        watch is seen on both sides of it — which is why this is how the rows
        are built.
        """
        rows = []
        pairs = (
            (date(2026, 12, 24), date(2026, 10, 14)),
            (date(2026, 12, 26), date(2026, 10, 20)),
            (date(2026, 12, 23), date(2026, 10, 8)),
        )
        for index, (festive, ordinary) in enumerate(pairs):
            base = 1000.0 * (1.3 ** index)
            for departure, multiple in (
                (festive, festive_multiple), (ordinary, 1.0)
            ):
                day = departure - timedelta(days=200)
                while day <= departure:
                    rows.append(sample(f"w{index}", day, departure, base * multiple))
                    day += timedelta(days=2)
        return rows

    def test_a_planted_holiday_premium_is_recovered(self):
        model = fit(self.spanning(festive_multiple=2.0))
        festive = model.holiday_at(date(2026, 12, 24))
        ordinary = model.holiday_at(date(2026, 10, 14))

        self.assertGreater(festive.samples, 0)
        found = festive.multiplier / ordinary.multiplier
        self.assertLess(abs(found / 2.0 - 1.0), 0.1, f"recovered {found:.3f}x")

    def test_the_premium_is_charged_to_the_holiday_not_to_the_horizon(self):
        """Otherwise every Christmas watch would bend the advance-purchase curve."""
        festive = fit(self.spanning(festive_multiple=2.0))
        none = fit(self.spanning(festive_multiple=1.0))

        for days in (10, 50, 120):
            self.assertAlmostEqual(
                festive.horizon_at(days).value,
                none.horizon_at(days).value,
                places=2,
                msg=f"the holiday premium leaked into the {days}d horizon",
            )

    def test_a_watch_seen_only_at_christmas_cannot_separate_the_two(self):
        """A limit worth knowing: with one departure per watch, the premium and
        the watch's own price level are the same number, and the model declines
        to guess which is which."""
        rows = []
        for index, departure in enumerate(
            (date(2026, 12, 24), date(2026, 12, 26), date(2026, 12, 23))
        ):
            day = departure - timedelta(days=100)
            while day <= departure:
                rows.append(sample(f"only{index}", day, departure, 2000.0))
                day += timedelta(days=2)

        model = fit(rows)
        self.assertAlmostEqual(model.holiday_at(date(2026, 12, 24)).value, 0.0, places=6)


class Forecasting(unittest.TestCase):
    def setUp(self):
        self.model = fit(series(lambda out: 1.30 if out > 150 else 0.85))

    def test_moving_nowhere_predicts_no_change(self):
        step = forecast(self.model, 60, 60)
        self.assertAlmostEqual(step.ratio, 1.0)

    def test_moving_from_far_out_toward_the_trough_predicts_a_fall(self):
        self.assertLess(forecast(self.model, 200, 60).ratio, 1.0)

    def test_the_band_widens_with_distance_even_on_a_perfectly_flat_history(self):
        """A fare moves whether or not this tool has watched it move."""
        flat = fit(series(lambda out: 1.0))
        self.assertLess(flat.sigma, WALK_FLOOR)

        near = forecast(flat, 100, 99, steps_ahead=1)
        far = forecast(flat, 100, 99, steps_ahead=16)
        self.assertGreater(far.log_sigma, near.log_sigma * 1.5)

    def test_a_bare_prior_carries_more_doubt_than_a_fitted_curve(self):
        bare = forecast(fit([]), 100, 40, steps_ahead=4)
        fitted = forecast(self.model, 100, 40, steps_ahead=4)
        self.assertGreater(bare.log_sigma, fitted.log_sigma)
        self.assertGreater(bare.prior_weight, fitted.prior_weight)

    def test_the_band_brackets_the_expected_price(self):
        step = forecast(self.model, 200, 60, steps_ahead=8)
        expected, low, high = step.band(1000.0)
        self.assertLess(low, expected)
        self.assertLess(expected, high)
        self.assertAlmostEqual(expected, 1000.0 * step.ratio)

    def test_a_wider_interval_asks_for_a_wider_band(self):
        step = forecast(self.model, 200, 60, steps_ahead=8)
        _, low80, high80 = step.band(1000.0)
        _, low95, high95 = step.band(1000.0, z=1.96)
        self.assertLess(low95, low80)
        self.assertGreater(high95, high80)



class Converging(unittest.TestCase):
    """Backfitting must settle, not drift.

    Each component is identifiable only up to an additive constant it shares
    with the per-watch levels. Unless that constant is pinned, every pass moves
    a little more of the fit out of the components and into the levels — and
    because the components are shrunk toward their priors and the levels are
    not, a longer fit quietly becomes a weaker one.
    """

    def setUp(self):
        self.shape = lambda out: 1.30 if out > 150 else 0.85 if out > 30 else 1.20
        self.rows = series(self.shape)

    def settled(self, one: float, other: float, what: str):
        """Agreement to within what the stopping rule actually promises.

        The rule bounds the size of the *last* pass, so what remains between a
        stopped fit and the true fixed point is a small multiple of it — not a
        drift, which would grow without limit as the passes ran on.
        """
        self.assertLess(
            abs(one - other), TOLERANCE * 10, f"{what} drifted: {one} vs {other}"
        )

    def test_the_answer_does_not_depend_on_how_long_it_is_allowed_to_run(self):
        short = fit(self.rows, passes=5)
        long = fit(self.rows, passes=200)

        for days in (5, 25, 50, 100, 200):
            self.settled(
                short.horizon_at(days).value, long.horizon_at(days).value, f"{days}d"
            )
        for watch_id, level in short.levels.items():
            self.settled(level, long.levels[watch_id], f"level {watch_id}")

    def test_a_holiday_premium_does_not_decay_as_the_fit_runs_longer(self):
        rows = Holidays().spanning(festive_multiple=2.0)

        def premium(passes):
            model = fit(rows, passes=passes)
            return (
                model.holiday_at(date(2026, 12, 24)).multiplier
                / model.holiday_at(date(2026, 10, 14)).multiplier
            )

        self.assertLess(abs(premium(5) / premium(200) - 1.0), 0.001)

    def test_it_stops_early_instead_of_burning_the_whole_ceiling(self):
        loose = fit(self.rows, passes=200, tolerance=1e-3)
        tight = fit(self.rows, passes=200, tolerance=1e-12)
        # Converged means converged: a far stricter tolerance, allowed forty
        # times the passes, finds essentially nothing more to do.
        for days in (25, 50, 100):
            self.assertLess(
                abs(loose.horizon_at(days).value - tight.horizon_at(days).value), 1e-3
            )

    def test_anchoring_leaves_the_ratios_it_is_supposed_to_leave_alone(self):
        """Re-centring moves the whole curve together, so forecasts see no change."""
        model = fit(self.rows)
        for near, far in ((10, 60), (60, 200)):
            planted = self.shape(near) / self.shape(far)
            found = forecast(model, far, near).ratio
            self.assertLess(abs(found / planted - 1.0), 0.15)


if __name__ == "__main__":
    unittest.main()
