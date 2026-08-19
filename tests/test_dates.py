import unittest
from datetime import date

from flighttracker.dates import coerce_date, combinations, describe, expand_range


class ExpandRange(unittest.TestCase):
    def test_single_date_stays_single(self):
        self.assertEqual(expand_range([date(2026, 12, 10)]), [date(2026, 12, 10)])

    def test_range_is_inclusive_of_both_ends(self):
        days = expand_range([date(2026, 12, 10), date(2026, 12, 13)])
        self.assertEqual(days[0], date(2026, 12, 10))
        self.assertEqual(days[-1], date(2026, 12, 13))
        self.assertEqual(len(days), 4)

    def test_backwards_range_is_rejected(self):
        with self.assertRaises(ValueError):
            expand_range([date(2026, 12, 13), date(2026, 12, 10)])


class Combinations(unittest.TestCase):
    def test_one_way_leaves_the_return_empty(self):
        pairs = combinations([date(2026, 12, 10), date(2026, 12, 11)], None)
        self.assertEqual(pairs, [(date(2026, 12, 10), None), (date(2026, 12, 11), None)])

    def test_returns_before_departure_are_dropped(self):
        pairs = combinations(
            [date(2026, 12, 10)], [date(2026, 12, 9), date(2026, 12, 10), date(2026, 12, 11)]
        )
        self.assertEqual(pairs, [(date(2026, 12, 10), date(2026, 12, 11))])

    def test_full_grid_is_every_valid_pairing(self):
        pairs = combinations(
            expand_range([date(2026, 12, 10), date(2026, 12, 12)]),
            expand_range([date(2026, 12, 20), date(2026, 12, 22)]),
        )
        self.assertEqual(len(pairs), 9)

    def test_trip_length_narrows_the_grid(self):
        pairs = combinations(
            expand_range([date(2026, 12, 10), date(2026, 12, 12)]),
            expand_range([date(2026, 12, 20), date(2026, 12, 22)]),
            trip_length_nights=(10, 10),
        )
        self.assertEqual(
            pairs,
            [
                (date(2026, 12, 10), date(2026, 12, 20)),
                (date(2026, 12, 11), date(2026, 12, 21)),
                (date(2026, 12, 12), date(2026, 12, 22)),
            ],
        )

    def test_unreachable_trip_length_yields_nothing(self):
        pairs = combinations(
            [date(2026, 12, 10)], [date(2026, 12, 12)], trip_length_nights=(20, 30)
        )
        self.assertEqual(pairs, [])


class Coercion(unittest.TestCase):
    def test_iso_strings_are_accepted(self):
        self.assertEqual(coerce_date("2026-12-10"), date(2026, 12, 10))

    def test_nonsense_is_rejected(self):
        with self.assertRaises(ValueError):
            coerce_date(12345)

    def test_describe_reports_nights(self):
        self.assertIn("14n", describe((date(2026, 12, 10), date(2026, 12, 24))))
        self.assertEqual(describe((date(2026, 12, 10), None)), "2026-12-10")


if __name__ == "__main__":
    unittest.main()
