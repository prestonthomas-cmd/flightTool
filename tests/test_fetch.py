import random
import unittest
from datetime import date
from types import SimpleNamespace

from flighttracker.config import Settings
from flighttracker.fetch import GoogleFlightsFetcher, collect, search_url

from .support import StubFetcher, make_watch


class Recorder:
    """A stand-in for `time.sleep` that remembers what it was asked to wait."""

    def __init__(self):
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class Collecting(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            request_delay_seconds=4, request_jitter_seconds=0, retry_backoff_seconds=5
        )
        self.rng = random.Random(0)

    def test_every_combination_of_every_watch_is_priced(self):
        watch = make_watch(
            depart=(date(2026, 12, 10), date(2026, 12, 11)),
            returns=(date(2026, 12, 24),),
        )
        fetcher = StubFetcher(default=700)
        result = collect([watch], fetcher, self.settings, sleep=Recorder(), rng=self.rng)

        self.assertEqual(len(result.quotes), 2)
        self.assertEqual(len(fetcher.calls), 2)

    def test_the_cheapest_quote_is_the_one_reported_for_the_watch(self):
        watch = make_watch(
            depart=(date(2026, 12, 10), date(2026, 12, 11)),
            returns=(date(2026, 12, 24),),
        )
        fetcher = StubFetcher(
            prices={
                ("test", date(2026, 12, 10), date(2026, 12, 24)): 900,
                ("test", date(2026, 12, 11), date(2026, 12, 24)): 640,
            }
        )
        result = collect([watch], fetcher, self.settings, sleep=Recorder(), rng=self.rng)

        cheapest = result.cheapest("test")
        self.assertEqual(cheapest.price, 640)
        self.assertEqual(cheapest.depart_date, date(2026, 12, 11))

    def test_requests_are_spaced_out_but_the_first_one_is_not_delayed(self):
        watch = make_watch(
            depart=(date(2026, 12, 10), date(2026, 12, 11), date(2026, 12, 12)),
            returns=(date(2026, 12, 24),),
        )
        waits = Recorder()
        collect([watch], StubFetcher(default=700), self.settings, sleep=waits, rng=self.rng)

        self.assertEqual(waits.waits, [4, 4])

    def test_jitter_lands_inside_the_configured_window(self):
        settings = Settings(request_delay_seconds=4, request_jitter_seconds=2)
        watch = make_watch(
            depart=(date(2026, 12, 10), date(2026, 12, 11)), returns=(date(2026, 12, 24),)
        )
        waits = Recorder()
        collect([watch], StubFetcher(default=700), settings, sleep=waits, rng=random.Random(7))

        self.assertEqual(len(waits.waits), 1)
        self.assertGreaterEqual(waits.waits[0], 4)
        self.assertLessEqual(waits.waits[0], 6)

    def test_a_transient_failure_is_retried_with_growing_backoff(self):
        watch = make_watch()
        fetcher = StubFetcher(
            default=700, errors={"test": [RuntimeError("boom"), RuntimeError("boom"), None]}
        )
        waits = Recorder()
        result = collect([watch], fetcher, self.settings, sleep=waits, rng=self.rng)

        self.assertEqual(len(result.quotes), 1)
        self.assertEqual(len(result.failures), 0)
        self.assertEqual(waits.waits, [5, 10])

    def test_a_search_that_never_succeeds_becomes_a_reported_failure(self):
        watch = make_watch()
        fetcher = StubFetcher(default=700, errors={"test": RuntimeError("blocked")})
        result = collect([watch], fetcher, self.settings, sleep=Recorder(), rng=self.rng)

        self.assertEqual(result.quotes, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("blocked", result.failures[0].message)
        self.assertIsNone(result.cheapest("test"))

    def test_one_failing_search_does_not_stop_the_others(self):
        good = make_watch("good")
        bad = make_watch("bad", origin="SFO", destination="LIS")
        fetcher = StubFetcher(
            prices={"good": 700}, errors={"bad": RuntimeError("blocked")}
        )
        result = collect(
            [bad, good], fetcher, self.settings, sleep=Recorder(), rng=self.rng
        )

        self.assertEqual(result.cheapest("good").price, 700)
        self.assertEqual(len(result.failures_for("bad")), 1)

    def test_a_search_with_no_priced_itinerary_is_a_failure_not_a_zero(self):
        fetcher = StubFetcher(default=None)
        result = collect(
            [make_watch()], fetcher, self.settings, sleep=Recorder(), rng=self.rng
        )
        self.assertEqual(result.quotes, ())
        self.assertIn("no priced itinerary", result.failures[0].message)

    def test_retries_are_capped_by_max_retries(self):
        settings = Settings(max_retries=2, request_delay_seconds=0, request_jitter_seconds=0)
        fetcher = StubFetcher(errors={"test": RuntimeError("nope")})
        collect([make_watch()], fetcher, settings, sleep=Recorder(), rng=self.rng)
        self.assertEqual(len(fetcher.calls), 2)


class PermanentFailures(unittest.TestCase):
    """A broken dependency cost 105 attempts over twelve minutes in CI."""

    def setUp(self):
        self.settings = Settings(
            request_delay_seconds=0, request_jitter_seconds=0,
            retry_backoff_seconds=5, max_retries=3,
        )
        self.rng = random.Random(0)

    def watches(self):
        return [
            make_watch(
                "many",
                depart=(date(2026, 12, 10), date(2026, 12, 11), date(2026, 12, 12)),
                returns=(date(2026, 12, 24),),
            ),
            make_watch("other", origin="SFO", destination="LIS"),
        ]

    def test_a_missing_dependency_is_not_retried(self):
        fetcher = StubFetcher(errors={"many": ModuleNotFoundError("no typing_extensions")})
        waits = Recorder()
        collect(self.watches(), fetcher, self.settings, sleep=waits, rng=self.rng)

        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(waits.waits, [])

    def test_the_run_stops_rather_than_repeating_the_same_failure(self):
        fetcher = StubFetcher(errors={"many": ImportError("boom")})
        result = collect(self.watches(), fetcher, self.settings,
                         sleep=Recorder(), rng=self.rng)

        # One failure reported, not one per search across every watch.
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(fetcher.calls), 1)

    def test_the_message_names_the_real_cause(self):
        fetcher = StubFetcher(errors={"many": ModuleNotFoundError("no typing_extensions")})
        result = collect(self.watches(), fetcher, self.settings,
                         sleep=Recorder(), rng=self.rng)
        self.assertIn("typing_extensions", result.failures[0].message)
        self.assertIn("ModuleNotFoundError", result.failures[0].message)

    def test_an_ordinary_failure_still_retries_and_carries_on(self):
        fetcher = StubFetcher(default=700, errors={"many": RuntimeError("blocked")})
        result = collect(self.watches(), fetcher, self.settings,
                         sleep=Recorder(), rng=self.rng)

        self.assertEqual(len(result.failures), 3)
        self.assertEqual(result.cheapest("other").price, 700)


class PickingTheCheapest(unittest.TestCase):
    """The scraper's own selection, exercised without touching the network."""

    def setUp(self):
        self.fetcher = GoogleFlightsFetcher(currency="USD")
        self.watch = make_watch()

    def itinerary(self, price, segments=2):
        return SimpleNamespace(
            price=price,
            airlines=["ANA"],
            flights=[
                SimpleNamespace(duration=390) for _ in range(segments)
            ],
        )

    def choose(self, itineraries):
        return self.fetcher._cheapest(
            self.watch, date(2026, 12, 10), date(2026, 12, 24), itineraries
        )

    def test_the_lowest_price_wins(self):
        quote = self.choose([self.itinerary(900), self.itinerary(640), self.itinerary(720)])
        self.assertEqual(quote.price, 640)
        self.assertEqual(quote.currency, "USD")

    def test_itineraries_with_no_price_are_skipped(self):
        quote = self.choose([self.itinerary(None), self.itinerary(720)])
        self.assertEqual(quote.price, 720)

    def test_a_zero_price_is_not_treated_as_a_bargain(self):
        quote = self.choose([self.itinerary(0), self.itinerary(720)])
        self.assertEqual(quote.price, 720)

    def test_no_usable_itinerary_gives_nothing(self):
        self.assertIsNone(self.choose([]))
        self.assertIsNone(self.choose([self.itinerary(None)]))

    def test_stops_and_duration_come_from_the_segments(self):
        quote = self.choose([self.itinerary(640, segments=2)])
        self.assertEqual(quote.stops, 1)
        self.assertEqual(quote.duration_minutes, 780)

    def test_a_nonstop_reports_zero_stops(self):
        self.assertEqual(self.choose([self.itinerary(640, segments=1)]).stops, 0)


class SearchLinks(unittest.TestCase):
    def test_a_round_trip_link_carries_both_dates(self):
        url = search_url(make_watch(), date(2026, 12, 10), date(2026, 12, 24))
        self.assertIn("JFK", url)
        self.assertIn("2026-12-10", url)
        self.assertIn("2026-12-24", url)
        self.assertNotIn(" ", url)

    def test_a_one_way_link_omits_the_return(self):
        url = search_url(make_watch(), date(2026, 12, 10), None)
        self.assertNotIn("through", url)


if __name__ == "__main__":
    unittest.main()
