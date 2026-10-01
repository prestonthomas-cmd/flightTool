"""Talking to the hosted watchlist.

The scraper runs unattended against data many people depend on, so most of
these are about what it does when the input is wrong rather than when it is
right.
"""

import json
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from flighttracker.cloud import (
    Cloud,
    CloudError,
    as_sample,
    as_watch,
    fit_against,
    project_for,
)
from flighttracker.model import fit as fit_model
from flighttracker.store import HorizonSample, to_iso

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def row(**over):
    base = {
        "id": "abc",
        "origin": "JFK",
        "destination": "HND",
        "depart_from": "2027-03-01",
        "depart_to": "2027-03-01",
        "return_from": None,
        "return_to": None,
        "cabin": "economy",
        "max_stops": None,
    }
    base.update(over)
    return base


class ReadingAWatch(unittest.TestCase):
    def test_a_single_date_becomes_a_one_day_window(self):
        watch = as_watch(row())
        self.assertEqual(watch.depart_dates, (date(2027, 3, 1),))
        self.assertTrue(watch.one_way)

    def test_a_range_expands_to_every_day_in_it(self):
        watch = as_watch(row(depart_from="2027-03-01", depart_to="2027-03-04"))
        self.assertEqual(len(watch.depart_dates), 4)
        self.assertEqual(watch.depart_dates[-1], date(2027, 3, 4))

    def test_a_return_window_makes_it_a_round_trip(self):
        watch = as_watch(row(return_from="2027-03-10", return_to="2027-03-12"))
        self.assertFalse(watch.one_way)
        self.assertEqual(len(watch.return_dates), 3)

    def test_the_id_is_carried_through_because_prices_are_keyed_on_it(self):
        self.assertEqual(as_watch(row(id="uuid-here")).id, "uuid-here")

    def test_a_row_that_cannot_be_read_is_skipped_not_raised(self):
        """One person's bad row must not cost everybody else their run."""
        for broken in (
            row(depart_from="not-a-date"),
            row(depart_to=None),
            {"id": "x"},
            row(origin=None),
            # "None" is what str(None) gives, and it is three characters.
            row(origin="None"),
            row(origin="JF"),
            row(origin="JFKK"),
            row(origin="HND", destination="HND"),
        ):
            with self.subTest(broken=broken):
                self.assertIsNone(as_watch(broken))


class ReadingAPrice(unittest.TestCase):
    def test_a_stored_price_becomes_a_model_sample(self):
        sample = as_sample(
            {
                "watch_id": "abc",
                "observed_at": "2026-10-01T12:00:00+00:00",
                "price": 940.5,
                "depart_date": "2027-03-01",
            }
        )
        self.assertEqual(sample.watch_id, "abc")
        self.assertEqual(sample.price, 940.5)

    def test_a_price_that_makes_no_sense_is_dropped(self):
        for broken in (
            {"watch_id": "a", "observed_at": "x", "price": 0, "depart_date": "2027-03-01"},
            {"watch_id": "a", "observed_at": "x", "price": -5, "depart_date": "2027-03-01"},
            {"watch_id": "a", "price": 100},
            {},
        ):
            with self.subTest(broken=broken):
                self.assertIsNone(as_sample(broken))


class Projecting(unittest.TestCase):
    def setUp(self):
        self.watch = as_watch(
            row(depart_from="2027-02-01", depart_to="2027-02-01")
        )
        self.model = fit_model([])

    def test_it_runs_to_departure_and_no_further(self):
        rows = project_for(self.watch, self.model, 900.0, NOW)
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["day"], "2027-02-01")
        for point in rows:
            self.assertLessEqual(point["day"], "2027-02-01")

    def test_every_point_carries_a_band_around_it(self):
        for point in project_for(self.watch, self.model, 900.0, NOW):
            self.assertLessEqual(point["low"], point["price"])
            self.assertLessEqual(point["price"], point["high"])
            self.assertGreater(point["low"], 0)

    def test_the_band_widens_the_further_out_it_goes(self):
        rows = project_for(self.watch, self.model, 900.0, NOW)
        width = [(p["high"] - p["low"]) / p["price"] for p in rows]
        self.assertGreater(width[-1], width[0])

    def test_a_departure_too_close_projects_nothing(self):
        soon = as_watch(row(depart_from="2026-10-03", depart_to="2026-10-03"))
        self.assertEqual(project_for(soon, self.model, 900.0, NOW), [])

    def test_a_departure_already_past_projects_nothing(self):
        gone = as_watch(row(depart_from="2026-09-01", depart_to="2026-09-01"))
        self.assertEqual(project_for(gone, self.model, 900.0, NOW), [])

    def test_no_price_means_nothing_to_anchor_on(self):
        self.assertEqual(project_for(self.watch, self.model, 0.0, NOW), [])

    def test_every_row_says_how_much_of_it_is_measured(self):
        """A projection that is mostly assumption must not look like one that
        is not, and the page can only say so if the number travels with it."""
        for point in project_for(self.watch, self.model, 900.0, NOW):
            self.assertEqual(point["evidence"], 0.0)

        fitted = fit_against(
            [
                HorizonSample(
                    watch_id=f"w{index}",
                    origin="",
                    destination="",
                    observed_on=to_iso(NOW - timedelta(days=day)),
                    depart_date=(NOW.date() + timedelta(days=200 - day)).isoformat(),
                    price=900.0 + day,
                )
                for index in range(4)
                for day in range(0, 120, 2)
            ]
        )
        points = project_for(self.watch, fitted, 900.0, NOW)
        self.assertGreater(points[0]["evidence"], 0.0)


class TalkingToTheApp(unittest.TestCase):
    def setUp(self):
        self.cloud = Cloud(base_url="https://example.test/", secret="s3cret")

    def test_the_url_is_built_without_a_doubled_slash(self):
        self.assertEqual(self.cloud.url, "https://example.test/api/sync")

    def respond(self, payload):
        body = json.dumps(payload).encode()
        handle = mock.MagicMock()
        handle.read.return_value = body
        handle.__enter__.return_value = handle
        return handle

    def test_it_sends_the_secret_as_a_bearer_token(self):
        with mock.patch("urllib.request.urlopen") as opened:
            opened.return_value = self.respond({"watches": [], "history": []})
            self.cloud.watchlist()

        request = opened.call_args[0][0]
        self.assertEqual(request.get_header("Authorization"), "Bearer s3cret")

    def test_a_rejected_secret_says_what_to_fix(self):
        error = urllib.error.HTTPError(
            self.cloud.url, 401, "Unauthorized", {}, None
        )
        error.read = lambda: b'{"error":"unauthorised"}'
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(CloudError) as caught:
                self.cloud.watchlist()

        self.assertIn("SYNC_SECRET", str(caught.exception))

    def test_an_unreachable_app_is_reported_plainly(self):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(CloudError) as caught:
                self.cloud.watchlist()
        self.assertIn("Could not reach", str(caught.exception))

    def test_bad_rows_are_filtered_out_of_the_watchlist(self):
        with mock.patch("urllib.request.urlopen") as opened:
            opened.return_value = self.respond(
                {
                    "watches": [row(), row(depart_from="nope"), row(id="second")],
                    "history": [
                        {
                            "watch_id": "abc",
                            "observed_at": to_iso(NOW),
                            "price": 900,
                            "depart_date": "2027-03-01",
                        },
                        {"watch_id": "abc", "price": -1},
                    ],
                }
            )
            watches, history = self.cloud.watchlist()

        self.assertEqual([w.id for w in watches], ["abc", "second"])
        self.assertEqual(len(history), 1)

    def test_publishing_posts_what_it_was_given(self):
        with mock.patch("urllib.request.urlopen") as opened:
            opened.return_value = self.respond({"stored": 2})
            self.cloud.publish([{"a": 1}, {"b": 2}], [{"c": 3}], run={"searches": 2})

        request = opened.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        sent = json.loads(request.data.decode())
        self.assertEqual(len(sent["prices"]), 2)
        self.assertEqual(len(sent["projections"]), 1)
        self.assertEqual(sent["run"]["searches"], 2)


if __name__ == "__main__":
    unittest.main()
