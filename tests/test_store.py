import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flighttracker.store import (
    Observation,
    connect,
    known_watch_ids,
    last_alert,
    observations_for_run,
    record_alerts,
    record_errors,
    record_observations,
    run_history,
)


def observation(watch_id="w", price=500.0, depart="2026-12-10", back="2026-12-24"):
    return Observation(
        watch_id=watch_id,
        price=price,
        depart_date=depart,
        return_date=back,
        currency="USD",
        airlines="Test Air",
        stops=0,
        duration_minutes=780,
    )


class Storage(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_a_run_keeps_one_row_per_date_combination(self):
        record_observations(
            self.conn,
            "2026-08-19T00:00:00+00:00",
            [
                observation(price=700, depart="2026-12-10"),
                observation(price=650, depart="2026-12-11"),
            ],
        )
        rows = observations_for_run(self.conn, "w", "2026-08-19T00:00:00+00:00")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["price"], 650)

    def test_a_watchs_price_for_a_run_is_the_cheapest_combination(self):
        record_observations(
            self.conn,
            "2026-08-19T00:00:00+00:00",
            [observation(price=700), observation(price=650, depart="2026-12-11")],
        )
        points = run_history(self.conn, "w")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].timestamp, "2026-08-19T00:00:00+00:00")
        self.assertEqual(points[0].price, 650)

    def test_rerunning_the_same_timestamp_overwrites_rather_than_doubling(self):
        stamp = "2026-08-19T00:00:00+00:00"
        record_observations(self.conn, stamp, [observation(price=700)])
        record_observations(self.conn, stamp, [observation(price=690)])
        rows = observations_for_run(self.conn, "w", stamp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 690)

    def test_one_way_rows_do_not_collide_with_each_other(self):
        stamp = "2026-08-19T00:00:00+00:00"
        record_observations(
            self.conn,
            stamp,
            [
                observation(price=300, depart="2026-12-10", back=None),
                observation(price=320, depart="2026-12-11", back=None),
            ],
        )
        self.assertEqual(len(observations_for_run(self.conn, "w", stamp)), 2)

    def test_history_is_oldest_first_and_grouped_by_run(self):
        for stamp, price in [
            ("2026-08-17T00:00:00+00:00", 800),
            ("2026-08-18T00:00:00+00:00", 700),
            ("2026-08-19T00:00:00+00:00", 600),
        ]:
            record_observations(self.conn, stamp, [observation(price=price)])
        self.assertEqual([p.price for p in run_history(self.conn, "w")], [800, 700, 600])

    def test_before_excludes_the_run_being_judged(self):
        for stamp, price in [
            ("2026-08-17T00:00:00+00:00", 800),
            ("2026-08-19T00:00:00+00:00", 600),
        ]:
            record_observations(self.conn, stamp, [observation(price=price)])
        past = run_history(self.conn, "w", before="2026-08-19T00:00:00+00:00")
        self.assertEqual([p.price for p in past], [800])

    def test_watches_are_kept_apart(self):
        stamp = "2026-08-19T00:00:00+00:00"
        record_observations(
            self.conn, stamp, [observation(watch_id="a", price=100),
                               observation(watch_id="b", price=200)]
        )
        self.assertEqual(known_watch_ids(self.conn), ["a", "b"])
        self.assertEqual(run_history(self.conn, "a")[0].price, 100)

    def test_failures_are_recorded_without_becoming_prices(self):
        record_errors(
            self.conn,
            "2026-08-19T00:00:00+00:00",
            [("w", "2026-12-10", "2026-12-24", "timed out")],
        )
        self.assertEqual(run_history(self.conn, "w"), [])
        count = self.conn.execute("SELECT COUNT(*) FROM fetch_errors").fetchone()[0]
        self.assertEqual(count, 1)

    def test_the_latest_alert_is_the_one_returned(self):
        record_alerts(self.conn, "2026-08-17T00:00:00+00:00", [("w", 800, "ceiling")])
        record_alerts(self.conn, "2026-08-19T00:00:00+00:00", [("w", 600, "all_time_low")])
        latest = last_alert(self.conn, "w")
        self.assertEqual(latest.price, 600)
        self.assertEqual(latest.reasons, "all_time_low")

    def test_no_alert_yet_is_none_not_an_error(self):
        self.assertIsNone(last_alert(self.conn, "w"))

    def test_writing_nothing_is_harmless(self):
        self.assertEqual(record_observations(self.conn, "t", []), 0)
        self.assertEqual(record_errors(self.conn, "t", []), 0)
        self.assertEqual(record_alerts(self.conn, "t", []), 0)


class OnDisk(unittest.TestCase):
    def test_the_database_file_and_its_folder_are_created(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "prices.db"
            conn = connect(path)
            conn.close()
            self.assertTrue(path.exists())

    def test_reopening_keeps_what_was_written(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.db"
            conn = connect(path)
            record_observations(conn, "2026-08-19T00:00:00+00:00", [observation()])
            conn.close()

            again = connect(path)
            self.assertEqual(len(run_history(again, "w")), 1)
            again.close()


if __name__ == "__main__":
    unittest.main()
