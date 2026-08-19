import unittest
from datetime import datetime, timedelta, timezone

from flighttracker.config import Passengers
from flighttracker.health import CRITICAL, FARE_CHANGED, NEVER_PRICED, STALE, check
from flighttracker.run import execute_run
from flighttracker.store import connect, fares_seen

from .support import StubFetcher, make_config, make_watch, no_sleep

START = datetime(2026, 8, 1, tzinfo=timezone.utc)


class Staleness(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def track(self, config, when, price=700):
        execute_run(config, self.conn, StubFetcher(default=price), when, sleep=no_sleep)

    def test_a_watch_priced_just_now_is_healthy(self):
        config = make_config(make_watch("tokyo"), stale_after_hours=30)
        self.track(config, START)
        self.assertEqual(check(self.conn, config, START + timedelta(hours=1)), [])

    def test_a_watch_that_stopped_being_priced_is_critical(self):
        config = make_config(make_watch("tokyo"), stale_after_hours=30)
        self.track(config, START)

        concerns = check(self.conn, config, START + timedelta(hours=40))
        self.assertEqual(len(concerns), 1)
        self.assertEqual(concerns[0].kind, STALE)
        self.assertEqual(concerns[0].severity, CRITICAL)
        self.assertTrue(concerns[0].blocking)
        self.assertIn("not being tracked", concerns[0].message)

    def test_the_message_says_how_long_it_has_been(self):
        config = make_config(make_watch("tokyo"), stale_after_hours=30)
        self.track(config, START)
        concerns = check(self.conn, config, START + timedelta(hours=48))
        self.assertIn("48h", concerns[0].message)

    def test_a_watch_that_never_priced_is_reported_but_not_critical(self):
        config = make_config(make_watch("fresh"), stale_after_hours=30)
        concerns = check(self.conn, config, START)
        self.assertEqual(concerns[0].kind, NEVER_PRICED)
        self.assertFalse(concerns[0].blocking)

    def test_a_failing_lookup_does_not_refresh_the_clock(self):
        """The bug this whole check exists for: running is not collecting."""
        config = make_config(make_watch("tokyo"), stale_after_hours=30)
        self.track(config, START)

        later = START + timedelta(hours=40)
        execute_run(
            config,
            self.conn,
            StubFetcher(errors={"tokyo": RuntimeError("blocked")}),
            later,
            sleep=no_sleep,
        )
        concerns = check(self.conn, config, later)
        self.assertEqual(concerns[0].kind, STALE)

    def test_zero_disables_the_check(self):
        config = make_config(make_watch("tokyo"), stale_after_hours=0)
        self.track(config, START)
        self.assertEqual(check(self.conn, config, START + timedelta(days=30)), [])

    def test_critical_concerns_sort_first(self):
        config = make_config(
            make_watch("gone"), make_watch("never"), stale_after_hours=30
        )
        self.track(config, START)
        concerns = check(self.conn, config, START + timedelta(hours=40))
        self.assertTrue(concerns[0].blocking)


class FareSignatures(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def track(self, config, when=START):
        execute_run(config, self.conn, StubFetcher(default=700), when, sleep=no_sleep)

    def test_the_signature_is_stored_with_every_price(self):
        config = make_config(make_watch("tokyo"))
        self.track(config)
        stored = fares_seen(self.conn, "tokyo")
        self.assertEqual(len(stored), 1)
        self.assertIn("cabin:economy", stored[0][0])
        self.assertIn("xbe:1", stored[0][0])

    def test_the_signature_covers_what_changes_the_product(self):
        plain = make_watch("a")
        bagged = make_watch("a", checked_bags=1)
        basic = make_watch("a", exclude_basic_economy=False)
        two = make_watch("a", passengers=Passengers(adults=2))

        signatures = {
            w.fare_signature() for w in (plain, bagged, basic, two)
        }
        self.assertEqual(len(signatures), 4)

    def test_an_unchanged_watch_raises_nothing(self):
        config = make_config(make_watch("tokyo"))
        self.track(config)
        self.assertEqual(check(self.conn, config, START + timedelta(hours=1)), [])

    def test_changing_the_search_settings_is_flagged_as_not_comparable(self):
        before = make_config(make_watch("tokyo"))
        self.track(before)

        after = make_config(make_watch("tokyo", checked_bags=2))
        concerns = check(self.conn, after, START + timedelta(hours=1))

        self.assertEqual(len(concerns), 1)
        self.assertEqual(concerns[0].kind, FARE_CHANGED)
        self.assertIn("not like for like", concerns[0].message)
        self.assertIn("new id", concerns[0].message)

    def test_the_message_names_only_what_changed(self):
        before = make_config(make_watch("tokyo"))
        self.track(before)
        after = make_config(make_watch("tokyo", exclude_basic_economy=False))
        concerns = check(self.conn, after, START + timedelta(hours=1))

        self.assertIn("xbe 1 -> 0", concerns[0].message)
        self.assertNotIn("cabin", concerns[0].message)

    def test_it_counts_how_much_of_the_history_is_affected(self):
        before = make_config(make_watch("tokyo"))
        for day in range(3):
            self.track(before, START + timedelta(days=day))

        after = make_config(make_watch("tokyo", cabin="business"))
        self.assertIn("3 of 3", check(self.conn, after, START + timedelta(days=3))[0].message)

    def test_a_history_predating_the_column_is_not_falsely_flagged(self):
        """NULL means unknown, and unknown is not evidence of a change."""
        self.conn.execute(
            "INSERT INTO price_history (watch_id, timestamp, price, depart_date)"
            " VALUES ('tokyo', '2026-08-01T00:00:00+00:00', 700, '2026-12-10')"
        )
        self.conn.commit()
        config = make_config(make_watch("tokyo"), stale_after_hours=0)
        self.assertEqual(check(self.conn, config, START), [])


if __name__ == "__main__":
    unittest.main()
