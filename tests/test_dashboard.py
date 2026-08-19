import re
import unittest
from datetime import date, datetime, timedelta, timezone

from flighttracker.charts import Bar, Point, bar_chart, gridlines, line_chart, nice_step
from flighttracker.config import Config, Settings
from flighttracker.dashboard import render_body, render_document
from flighttracker.run import execute_run
from flighttracker.store import connect

from .support import StubFetcher, make_config, make_watch, no_sleep

START = datetime(2026, 8, 1, tzinfo=timezone.utc)


class Axes(unittest.TestCase):
    def test_steps_are_round_numbers(self):
        self.assertIn(nice_step(1000, 4), (200, 250, 500))
        self.assertEqual(nice_step(0, 4), 1.0)

    def test_gridlines_land_inside_the_range_on_round_numbers(self):
        values = gridlines(910, 1420)
        self.assertTrue(all(910 <= v <= 1420 for v in values), values)
        self.assertEqual(values, [1000, 1200, 1400])

    def test_a_gridline_exactly_on_the_bound_is_kept(self):
        self.assertIn(1000.0, gridlines(1000, 1400))

    def test_a_flat_range_still_produces_one_line(self):
        self.assertEqual(gridlines(500, 500), [500])


class Charts(unittest.TestCase):
    def points(self, *values):
        return [Point(float(i), float(v), f"tip {v}") for i, v in enumerate(values)]

    def test_a_line_needs_at_least_two_points(self):
        self.assertIn("Not enough runs", line_chart(self.points(900)))

    def test_a_line_carries_a_path_and_hover_targets(self):
        svg = line_chart(self.points(900, 950, 880))
        self.assertIn("<path", svg)
        self.assertIn("data-tip=", svg)
        self.assertIn("<svg", svg)

    def test_identical_prices_do_not_divide_by_zero(self):
        svg = line_chart(self.points(900, 900, 900))
        self.assertIn("<path", svg)

    def test_axis_labels_at_the_ends_are_anchored_inwards(self):
        svg = line_chart(
            self.points(900, 950, 880), x_labels=[(0.0, "A"), (1.0, "B"), (2.0, "C")]
        )
        self.assertIn('text-anchor="start"', svg)
        self.assertIn('text-anchor="end"', svg)

    def test_bars_render_with_labels_and_tips(self):
        svg = bar_chart([Bar("Thu 10 Dec", 948, "tip", 1), Bar("Sat 19 Dec", 1763, "tip", 2)])
        self.assertIn("Thu 10 Dec", svg)
        self.assertIn("series-2", svg)

    def test_no_bars_says_so(self):
        self.assertIn("No priced dates", bar_chart([]))

    def test_chart_text_is_escaped(self):
        svg = bar_chart([Bar("<script>", 100, "<script>", 1)])
        self.assertNotIn("<script>", svg)


class Rendering(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def track(self, config, prices, days):
        for index in range(days):
            execute_run(
                config,
                self.conn,
                StubFetcher(default=prices[index % len(prices)]),
                START + timedelta(days=index),
                sleep=no_sleep,
            )

    def test_a_full_page_is_a_valid_standalone_document(self):
        config = make_config(make_watch("tokyo", label="NYC to Tokyo"))
        self.track(config, [900, 880, 910, 870], 6)
        html = render_document(self.conn, config, START + timedelta(days=6))

        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("<title>Flight Price Watch</title>", html)
        self.assertIn("viewport", html)
        self.assertIn("</html>", html)

    def test_a_body_fragment_carries_no_document_tags(self):
        config = make_config(make_watch("tokyo"))
        self.track(config, [900, 880], 4)
        body = render_body(self.conn, config, START + timedelta(days=4))

        self.assertNotIn("<!doctype", body.lower())
        self.assertNotIn("<html", body.lower())
        self.assertNotIn("<body", body.lower())
        self.assertIn("<style>", body)

    def test_the_page_is_entirely_self_contained(self):
        config = make_config(make_watch("tokyo"))
        self.track(config, [900, 880, 910], 5)
        html = render_document(self.conn, config, START + timedelta(days=5))

        # Nothing may be fetched: no CDN, no font host, no remote image.
        self.assertFalse(re.search(r'(src|href)="https?://[^"]*"', html.replace(
            'href="https://www.google.com/travel/flights', 'LINK')))
        self.assertNotIn("<link", html)

    def test_the_run_count_agrees_with_its_own_table(self):
        """The card said 110 runs above a table listing 111 before this."""
        config = make_config(make_watch("tokyo"))
        self.track(config, [900, 880, 910, 870], 7)
        html = render_document(self.conn, config, START + timedelta(days=7))

        self.assertIn("7 runs", html)
        self.assertIn("Show the numbers (7 runs)", html)

    def test_the_summary_answers_before_the_detail(self):
        config = make_config(
            make_watch("tokyo", label="NYC to Tokyo", max_price_alert=900),
            min_observations=2,
        )
        self.track(config, [880], 4)
        html = render_document(self.conn, config, START + timedelta(days=4))

        self.assertIn("Buy signals", html)
        self.assertIn("Best price now", html)
        self.assertIn("Next departure", html)

    def test_state_is_carried_by_form_not_only_by_the_label(self):
        config = make_config(
            make_watch("tokyo", max_price_alert=900), min_observations=2
        )
        self.track(config, [880], 4)
        self.assertIn('class="card flagged"',
                      render_document(self.conn, config, START + timedelta(days=4)))

    def test_a_watch_with_no_price_is_marked_as_such(self):
        config = make_config(make_watch("gone"))
        self.assertIn('class="card stale"', render_document(self.conn, config, START))

    def test_a_flagged_watch_is_marked_and_gives_its_reason(self):
        config = make_config(
            make_watch("tokyo", max_price_alert=900), min_observations=2
        )
        self.track(config, [880], 4)
        html = render_document(self.conn, config, START + timedelta(days=4))

        self.assertIn("Buy signal", html)
        self.assertIn("ceiling", html)

    def test_a_watch_with_no_data_does_not_break_the_page(self):
        config = make_config(make_watch("fresh"))
        html = render_document(self.conn, config, START)
        self.assertIn("Flight Price Watch", html)
        self.assertIn("No priced dates stored yet.", html)

    def test_holiday_dates_are_marked_and_given_a_legend(self):
        watch = make_watch(
            "tokyo",
            depart=(date(2026, 12, 10), date(2026, 12, 20)),
            returns=(date(2026, 12, 28),),
        )
        config = Config(settings=Settings(), watches=(watch,))
        self.track(config, [900], 3)
        html = render_document(self.conn, config, START + timedelta(days=3))

        self.assertIn("Holiday peak travel", html)
        self.assertIn("peak", html)

    def test_watch_labels_are_escaped(self):
        config = make_config(make_watch("x", label="<script>alert(1)</script>"))
        self.track(config, [900], 3)
        html = render_document(self.conn, config, START + timedelta(days=3))

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_thin_horizon_curve_explains_itself_rather_than_drawing_noise(self):
        config = make_config(make_watch("tokyo"))
        self.track(config, [900, 880], 4)
        html = render_document(self.conn, config, START + timedelta(days=4))

        self.assertIn("Booking-horizon curve", html)
        self.assertIn("Not enough data yet", html)


if __name__ == "__main__":
    unittest.main()
