import textwrap
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from flighttracker.config import load_config
from flighttracker.errors import ConfigError


def write(tmp: Path, body: str) -> Path:
    path = tmp / "watches.yaml"
    path.write_text(textwrap.dedent(body))
    return path


class LoadingAWatchlist(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_the_example_from_the_spec_parses(self):
        path = write(
            self.tmp,
            """
            watches:
              - id: nyc-to-tokyo-dec
                origin: JFK
                destination: HND
                depart_date_range: [2026-12-10, 2026-12-12]
                return_date_range: [2026-12-24, 2026-12-26]
                trip_length_nights: 14
                cabin: economy
                max_price_alert: 900
            """,
        )
        config = load_config(path)
        watch = config.watches[0]

        self.assertEqual(watch.id, "nyc-to-tokyo-dec")
        self.assertEqual(watch.origin, "JFK")
        self.assertEqual(watch.destination, "HND")
        self.assertEqual(watch.depart_dates[0], date(2026, 12, 10))
        self.assertEqual(len(watch.depart_dates), 3)
        self.assertEqual(watch.max_price_alert, 900)
        self.assertEqual(watch.trip_length_nights, (14, 14))
        self.assertEqual(len(watch.searches()), 3)

    def test_a_watch_with_no_return_range_is_one_way(self):
        path = write(
            self.tmp,
            """
            watches:
              - id: one-way
                origin: sfo
                destination: lis
                depart_date_range: 2027-03-01
            """,
        )
        watch = load_config(path).watches[0]
        self.assertTrue(watch.one_way)
        self.assertEqual(watch.searches(), [(date(2027, 3, 1), None)])
        self.assertEqual(watch.origin, "SFO")

    def test_relative_db_path_resolves_next_to_the_watchlist(self):
        path = write(
            self.tmp,
            """
            settings:
              db_path: data/prices.db
            watches:
              - id: w
                origin: JFK
                destination: LHR
                depart_date_range: 2027-01-01
            """,
        )
        config = load_config(path)
        self.assertEqual(config.settings.db_path, self.tmp / "data/prices.db")

    def test_cabin_spellings_are_normalised(self):
        path = write(
            self.tmp,
            """
            watches:
              - id: w
                origin: JFK
                destination: LHR
                depart_date_range: 2027-01-01
                cabin: Premium Economy
            """,
        )
        self.assertEqual(load_config(path).watches[0].cabin, "premium-economy")

    def test_passengers_accept_a_bare_count_or_a_mapping(self):
        path = write(
            self.tmp,
            """
            watches:
              - id: bare
                origin: JFK
                destination: LHR
                depart_date_range: 2027-01-01
                passengers: 2
              - id: detailed
                origin: JFK
                destination: LHR
                depart_date_range: 2027-01-01
                passengers:
                  adults: 2
                  children: 1
            """,
        )
        bare, detailed = load_config(path).watches
        self.assertEqual(bare.passengers.adults, 2)
        self.assertEqual(detailed.passengers.total, 3)


class ReportingProblems(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def assertProblem(self, body: str, fragment: str):
        with self.assertRaises(ConfigError) as caught:
            load_config(write(self.tmp, body))
        joined = "\n".join(caught.exception.problems)
        self.assertIn(fragment, joined)
        return joined

    def test_every_problem_is_reported_at_once(self):
        joined = self.assertProblem(
            """
            watches:
              - id: bad-airport
                origin: NEWYORK
                destination: HND
                depart_date_range: 2027-01-01
              - id: no-dates
                origin: SFO
                destination: LIS
            """,
            "not a 3-letter IATA airport code",
        )
        self.assertIn("`depart_date_range` is required", joined)

    def test_duplicate_ids_are_caught(self):
        self.assertProblem(
            """
            watches:
              - id: same
                origin: JFK
                destination: LHR
                depart_date_range: 2027-01-01
              - id: same
                origin: SFO
                destination: LIS
                depart_date_range: 2027-01-01
            """,
            "used more than once",
        )

    def test_an_oversized_grid_is_refused_with_advice(self):
        joined = self.assertProblem(
            """
            settings:
              max_combinations: 20
            watches:
              - id: wide
                origin: JFK
                destination: HND
                depart_date_range: [2026-12-01, 2026-12-20]
                return_date_range: [2027-01-01, 2027-01-20]
            """,
            "exceeds max_combinations",
        )
        self.assertIn("trip_length_nights", joined)

    def test_ranges_that_produce_no_search_are_refused(self):
        self.assertProblem(
            """
            watches:
              - id: backwards
                origin: JFK
                destination: HND
                depart_date_range: 2027-01-10
                return_date_range: 2027-01-05
            """,
            "no searches",
        )

    def test_same_origin_and_destination_is_refused(self):
        self.assertProblem(
            """
            watches:
              - id: circular
                origin: JFK
                destination: JFK
                depart_date_range: 2027-01-01
            """,
            "origin and destination are both JFK",
        )

    def test_unknown_keys_are_surfaced_not_ignored(self):
        self.assertProblem(
            """
            watches:
              - id: typo
                origin: JFK
                destination: LHR
                depart_date_range: 2027-01-01
                max_price: 500
            """,
            "unknown key 'max_price'",
        )

    def test_an_empty_watchlist_is_refused(self):
        self.assertProblem("watches: []", "nothing to track")

    def test_a_missing_file_is_reported_clearly(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self.tmp / "nope.yaml")
        self.assertIn("not found", str(caught.exception))

    def test_bad_percentile_is_refused(self):
        self.assertProblem(
            """
            settings:
              percentile: 150
            watches:
              - id: w
                origin: JFK
                destination: LHR
                depart_date_range: 2027-01-01
            """,
            "between 0 and 100",
        )


if __name__ == "__main__":
    unittest.main()
