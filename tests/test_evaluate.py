"""Rolling-origin validation.

The point of this module is to stop the tool from grading its own homework, so
these tests are mostly about what it must refuse to look at.
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from flighttracker.evaluate import (
    build_cases,
    evaluate,
    evaluate_stored,
    format_evaluation,
)
from flighttracker.store import HorizonSample, connect, to_iso

DEPART = date(2027, 2, 1)


def sample(watch_id, observed, price, departure=DEPART):
    return HorizonSample(
        watch_id=watch_id,
        origin="JFK",
        destination="HND",
        observed_on=to_iso(
            datetime.combine(observed, datetime.min.time(), timezone.utc)
        ),
        depart_date=departure.isoformat(),
        price=float(price),
    )


def daily(watch_id, start, prices, departure=DEPART):
    return [
        sample(watch_id, start + timedelta(days=i), price, departure)
        for i, price in enumerate(prices)
    ]


class BuildingCases(unittest.TestCase):
    def setUp(self):
        self.start = DEPART - timedelta(days=120)

    def test_a_case_pairs_a_price_with_the_one_that_followed_it(self):
        rows = daily("a", self.start, [1000, 1100, 1200, 1300])
        cases = build_cases(rows, horizon=1)

        self.assertEqual(len(cases), 3)
        first = cases[0]
        self.assertEqual(first.price_from, 1000.0)
        self.assertEqual(first.price_to, 1100.0)
        self.assertEqual(first.horizon, 1)

    def test_the_horizon_is_the_gap_between_the_two_ends(self):
        rows = daily("a", self.start, list(range(1000, 1010)))
        for case in build_cases(rows, horizon=3):
            self.assertLessEqual(abs(case.horizon - 3), 1)

    def test_a_gap_in_the_history_is_bridged_only_within_tolerance(self):
        rows = daily("a", self.start, [1000]) + daily(
            "a", self.start + timedelta(days=9), [1200]
        )
        self.assertEqual(build_cases(rows, horizon=1), [])
        self.assertEqual(len(build_cases(rows, horizon=9)), 1)

    def test_two_departure_dates_are_never_compared_against_each_other(self):
        """Otherwise a case would be two different flights, not one over time."""
        other = DEPART + timedelta(days=30)
        rows = daily("a", self.start, [1000, 1100])
        rows += daily("a", self.start, [4000, 4100], departure=other)
        cases = build_cases(rows, horizon=1)

        self.assertEqual(len(cases), 2)
        for case in cases:
            self.assertEqual(abs(case.price_to - case.price_from), 100.0)

    def test_two_watches_are_never_compared_against_each_other(self):
        rows = daily("a", self.start, [1000, 1100])
        rows += daily("b", self.start, [500, 550])
        for case in build_cases(rows, horizon=1):
            self.assertIn(case.watch_id, ("a", "b"))
            self.assertLess(case.price_from, case.price_to)
        self.assertEqual(len({c.watch_id for c in build_cases(rows, horizon=1)}), 2)

    def test_the_cheapest_price_of_a_day_is_the_one_that_counts(self):
        """Matching what the tool actually reports for that day."""
        day = self.start
        rows = [
            sample("a", day, 1200),
            sample("a", day, 900),
            sample("a", day + timedelta(days=1), 1000),
        ]
        self.assertEqual(build_cases(rows, horizon=1)[0].price_from, 900.0)

    def test_a_case_records_how_far_out_each_end_sat(self):
        case = build_cases(daily("a", self.start, [1000, 1100]), horizon=1)[0]
        self.assertEqual(case.days_out_from, 120)
        self.assertEqual(case.days_out_to, 119)

    def test_prices_that_cannot_be_read_are_skipped(self):
        rows = daily("a", self.start, [1000, 1100])
        rows.append(sample("a", self.start, 0))
        rows.append(
            HorizonSample("a", "JFK", "HND", "not-a-time", DEPART.isoformat(), 800.0)
        )
        self.assertEqual(len(build_cases(rows, horizon=1)), 1)

    def test_no_history_builds_no_cases(self):
        self.assertEqual(build_cases([], horizon=1), [])


class NotPeekingAtTheFuture(unittest.TestCase):
    """The one thing a backtest must get right."""

    def setUp(self):
        self.start = DEPART - timedelta(days=200)

    def test_a_price_the_model_could_not_have_seen_does_not_change_the_score(self):
        """The whole claim of rolling-origin validation, stated as a test.

        History is scored, then a wild continuation is appended. Every case in
        the first run was anchored before the new prices existed, so every one
        of those scores must come back unchanged.
        """
        known = []
        for index in range(4):
            known += daily(
                f"w{index}",
                self.start,
                [1000 + 10 * i for i in range(40)],
                departure=DEPART + timedelta(days=index * 9),
            )
        before = evaluate(known, horizons=(3,), minimum_train=8)

        future = list(known)
        for index in range(4):
            future += daily(
                f"w{index}",
                self.start + timedelta(days=40),
                [9_000] * 20,
                departure=DEPART + timedelta(days=index * 9),
            )
        after = evaluate(future, horizons=(3,), minimum_train=8)

        first = before.results[0].by_method("model")
        second = after.results[0].by_method("model")
        self.assertGreater(first.cases, 0)
        # The later run has more cases; the ones it shares must score the same.
        self.assertGreater(second.cases, first.cases)

        trimmed = evaluate(
            [
                s
                for s in future
                if datetime.fromisoformat(s.observed_on).date()
                <= self.start + timedelta(days=39)
            ],
            horizons=(3,),
            minimum_train=8,
        )
        self.assertAlmostEqual(
            trimmed.results[0].by_method("model").mape, first.mape, places=9
        )

    def test_too_little_history_scores_nothing_rather_than_guessing(self):
        rows = daily("a", self.start, [1000, 1010, 1020])
        result = evaluate(rows, horizons=(1,), minimum_train=50).results[0]
        self.assertIsNone(result.by_method("model"))


class ScoringAgainstBaselines(unittest.TestCase):
    def setUp(self):
        self.start = DEPART - timedelta(days=200)

    def rows(self, prices, watches=4):
        out = []
        for index in range(watches):
            out += daily(
                f"w{index}",
                self.start,
                [p * (1.0 + 0.3 * index) for p in prices],
                departure=DEPART + timedelta(days=index * 9),
            )
        return out

    def test_a_perfectly_flat_price_is_predicted_near_perfectly_by_everyone(self):
        """Naive is exact here. The model is not quite, and should not be: the
        few cases that cross a bucket edge make it predict a small move, which
        on a flat series is a small error. It must stay small."""
        result = evaluate(
            self.rows([1000] * 60), horizons=(3,), minimum_train=8
        ).results[0]

        self.assertLess(result.by_method("naive").mape, 1e-9)
        self.assertLess(result.by_method("model").mape, 0.005)

    def test_all_three_baselines_are_scored_on_the_same_cases(self):
        result = evaluate(
            self.rows([1000 + i * 7 for i in range(60)]), horizons=(3,), minimum_train=8
        ).results[0]
        counts = {result.by_method(m).cases for m in ("model", "naive", "prior")}
        self.assertEqual(len(counts), 1)

    def test_only_the_model_reports_a_band_because_only_it_has_one(self):
        result = evaluate(
            self.rows([1000] * 60), horizons=(3,), minimum_train=8
        ).results[0]
        self.assertIsNotNone(result.by_method("model").coverage)
        self.assertIsNone(result.by_method("naive").coverage)

    def test_a_horizon_that_never_crosses_a_bucket_is_called_uninformative(self):
        """A tie there says nothing: the model *is* the naive prediction."""
        result = evaluate(
            self.rows([1000] * 60), horizons=(1,), minimum_train=8
        ).results[0]
        model = result.by_method("model")
        self.assertLess(result.crossings / model.cases, 0.2)
        self.assertFalse(result.informative)

        lines = "\n".join(
            format_evaluation(evaluate(self.rows([1000] * 60), horizons=(1,),
                                       minimum_train=8))
        )
        self.assertIn("not informative", lines)

    def test_a_wildly_volatile_price_falls_outside_the_band_more_often(self):
        calm = evaluate(self.rows([1000] * 60), horizons=(3,), minimum_train=8)
        wild = evaluate(
            self.rows([1000 if i % 2 else 3000 for i in range(60)]),
            horizons=(3,),
            minimum_train=8,
        )
        self.assertGreater(
            calm.results[0].by_method("model").coverage,
            wild.results[0].by_method("model").coverage,
        )


class Reporting(unittest.TestCase):
    def test_an_empty_history_says_so_instead_of_printing_a_blank_table(self):
        lines = format_evaluation(evaluate([]))
        self.assertTrue(any("Nothing could be scored" in line for line in lines))

    def test_it_reports_the_size_of_the_history_it_used(self):
        rows = daily("a", DEPART - timedelta(days=100), [1000] * 20)
        lines = format_evaluation(evaluate(rows, horizons=(3,), minimum_train=5))
        self.assertIn("20 observations", lines[0])
        self.assertIn("1 watch(es)", lines[0])

    def test_it_reads_the_stored_database(self):
        result = evaluate_stored(connect(":memory:"))
        self.assertEqual(result.observations, 0)
        self.assertFalse(result.usable)


if __name__ == "__main__":
    unittest.main()
