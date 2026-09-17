"""Adding and removing watches.

The watchlist is a commented file a person maintains by hand, so the standard
these tests hold the editor to is that it changes what was asked and nothing
else — comments, settings and formatting all survive untouched.
"""

import textwrap
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from flighttracker.config import load_config
from flighttracker.errors import ConfigError
from flighttracker.watchlist import NewWatch, add, remove, watch_ids

FILE = """\
# The watchlist. Edit me.
settings:
  db_path: prices.db
  # How many runs before a watch's own history counts.
  min_observations: 20

watches:
  # A round trip over the holidays.
  - id: tokyo
    label: NYC to Tokyo
    origin: JFK
    destination: HND
    depart_date_range: [2026-12-10, 2026-12-12]
    return_date_range: 2026-12-24

  # One-way, no return date.
  - id: denver
    origin: AUS
    destination: DEN
    depart_date_range: 2027-01-15
"""


class EditingTheFile(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "watches.yaml"
        self.path.write_text(FILE)

    def text(self) -> str:
        return self.path.read_text()

    def watch(self, **kwargs) -> NewWatch:
        return NewWatch(
            **{
                "id": "lisbon",
                "origin": "SFO",
                "destination": "LIS",
                "depart": (date(2027, 3, 1),),
                **kwargs,
            }
        )

    def test_a_watch_can_be_added_and_is_then_tracked(self):
        add(self.path, self.watch())
        self.assertEqual(watch_ids(self.path), ["tokyo", "denver", "lisbon"])

        watch = next(w for w in load_config(self.path).watches if w.id == "lisbon")
        self.assertEqual(watch.route, "SFO -> LIS")
        self.assertEqual(watch.depart_dates, (date(2027, 3, 1),))

    def test_adding_leaves_every_comment_and_setting_alone(self):
        add(self.path, self.watch())
        text = self.text()

        self.assertIn("# The watchlist. Edit me.", text)
        self.assertIn("# How many runs before a watch's own history counts.", text)
        self.assertIn("# A round trip over the holidays.", text)
        self.assertIn("  min_observations: 20", text)

    def test_removing_what_was_just_added_restores_the_file_exactly(self):
        add(self.path, self.watch())
        remove(self.path, "lisbon")
        self.assertEqual(self.text(), FILE)

    def test_removing_takes_the_watchs_own_comments_with_it(self):
        remove(self.path, "denver")
        text = self.text()

        self.assertNotIn("# One-way, no return date.", text)
        self.assertNotIn("denver", text)
        # ...and leaves its neighbour's comment behind.
        self.assertIn("# A round trip over the holidays.", text)
        self.assertEqual(watch_ids(self.path), ["tokyo"])

    def test_removing_the_first_watch_leaves_the_rest_loadable(self):
        remove(self.path, "tokyo")
        self.assertEqual([w.id for w in load_config(self.path).watches], ["denver"])

    def test_removing_the_last_watch_leaves_a_file_that_still_parses(self):
        remove(self.path, "tokyo")
        remove(self.path, "denver")
        self.assertEqual(load_config(self.path).watches, ())
        self.assertIn("min_observations: 20", self.text())

    def test_adding_several_does_not_pile_up_blank_lines(self):
        add(self.path, self.watch(id="a"))
        add(self.path, self.watch(id="b"))
        add(self.path, self.watch(id="c"))
        self.assertNotIn("\n\n\n", self.text())
        self.assertEqual(watch_ids(self.path), ["tokyo", "denver", "a", "b", "c"])

    def test_adding_and_removing_repeatedly_does_not_drift(self):
        for _ in range(5):
            add(self.path, self.watch())
            remove(self.path, "lisbon")
        self.assertEqual(self.text(), FILE)


class WhatGetsWritten(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "watches.yaml"
        self.path.write_text(FILE)

    def added(self, **kwargs):
        watch = NewWatch(
            **{
                "id": "new",
                "origin": "SFO",
                "destination": "LIS",
                "depart": (date(2027, 3, 1),),
                **kwargs,
            }
        )
        add(self.path, watch)
        return next(w for w in load_config(self.path).watches if w.id == "new")

    def test_a_single_date_is_written_as_a_date_not_a_range(self):
        self.added()
        self.assertIn("depart_date_range: 2027-03-01", self.path.read_text())

    def test_a_range_is_written_as_a_range(self):
        watch = self.added(depart=(date(2027, 3, 1), date(2027, 3, 8)))
        self.assertIn(
            "depart_date_range: [2027-03-01, 2027-03-08]", self.path.read_text()
        )
        self.assertEqual(watch.depart_dates[0], date(2027, 3, 1))
        self.assertEqual(watch.depart_dates[-1], date(2027, 3, 8))

    def test_no_return_date_means_a_one_way(self):
        self.assertTrue(self.added().one_way)

    def test_a_return_date_makes_it_a_round_trip(self):
        watch = self.added(returns=(date(2027, 3, 15),))
        self.assertFalse(watch.one_way)
        self.assertEqual(watch.return_dates, (date(2027, 3, 15),))

    def test_defaults_are_left_out_rather_than_written_as_noise(self):
        self.added()
        block = self.path.read_text().split("- id: new")[1]
        for absent in ("cabin:", "passengers:", "max_stops:", "max_price_alert:"):
            self.assertNotIn(absent, block)

    def test_settings_that_were_asked_for_are_written(self):
        watch = self.added(
            cabin="business", adults=3, max_stops=0, max_price=1500.0,
            label="Big trip", trip_length_nights=(12, 16),
        )
        self.assertEqual(watch.cabin, "business")
        self.assertEqual(watch.passengers.adults, 3)
        self.assertEqual(watch.max_stops, 0)
        self.assertEqual(watch.max_price_alert, 1500.0)
        self.assertEqual(watch.label, "Big trip")
        self.assertEqual(watch.trip_length_nights, (12, 16))

    def test_a_whole_number_price_is_not_written_with_a_decimal_point(self):
        self.added(max_price=900.0)
        self.assertIn("max_price_alert: 900", self.path.read_text())

    def test_a_label_containing_yaml_punctuation_is_quoted(self):
        watch = self.added(label="Tokyo: the big one")
        self.assertEqual(watch.label, "Tokyo: the big one")


class Refusing(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "watches.yaml"
        self.path.write_text(FILE)

    def test_a_duplicate_id_is_refused_and_changes_nothing(self):
        duplicate = NewWatch("tokyo", "SFO", "LIS", (date(2027, 3, 1),))
        with self.assertRaises(ConfigError) as caught:
            add(self.path, duplicate)

        self.assertIn("already in the watchlist", str(caught.exception))
        self.assertEqual(self.path.read_text(), FILE)

    def test_removing_something_that_is_not_there_lists_what_is(self):
        with self.assertRaises(ConfigError) as caught:
            remove(self.path, "paris")

        message = str(caught.exception)
        self.assertIn("paris", message)
        self.assertIn("denver", message)
        self.assertIn("tokyo", message)
        self.assertEqual(self.path.read_text(), FILE)

    def test_a_file_with_no_watches_key_is_refused_not_corrupted(self):
        self.path.write_text("settings:\n  db_path: prices.db\n")
        before = self.path.read_text()
        with self.assertRaises(ConfigError):
            add(self.path, NewWatch("a", "SFO", "LIS", (date(2027, 3, 1),)))
        self.assertEqual(self.path.read_text(), before)

    def test_an_edit_that_would_not_load_is_rolled_back(self):
        """The file on disk is never left in a state the tool cannot read."""
        broken = NewWatch("bad", "SFO", "LIS", ())  # no departure dates
        with self.assertRaises(Exception):
            add(self.path, broken)
        self.assertEqual(self.path.read_text(), FILE)
        self.assertEqual(watch_ids(self.path), ["tokyo", "denver"])


class AnEmptyList(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "watches.yaml"

    def test_a_watch_can_be_added_to_a_file_that_has_none(self):
        self.path.write_text("settings:\n  db_path: prices.db\n\nwatches:\n")
        add(self.path, NewWatch("first", "SFO", "LIS", (date(2027, 3, 1),)))
        self.assertEqual(watch_ids(self.path), ["first"])
        self.assertEqual(len(load_config(self.path).watches), 1)

    def test_a_trailing_comment_after_the_list_is_not_swallowed(self):
        self.path.write_text(
            textwrap.dedent(
                """\
                settings:
                  db_path: prices.db

                watches:
                  - id: tokyo
                    origin: JFK
                    destination: HND
                    depart_date_range: 2026-12-10

                # A note at the end of the file.
                """
            )
        )
        add(self.path, NewWatch("new", "SFO", "LIS", (date(2027, 3, 1),)))
        self.assertIn("# A note at the end of the file.", self.path.read_text())
        self.assertEqual(watch_ids(self.path), ["tokyo", "new"])


if __name__ == "__main__":
    unittest.main()
