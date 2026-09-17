"""The page is one chart per watch and nothing else. These tests hold it there."""

import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from flighttracker.charts import (
    BandPoint,
    Point,
    gridlines,
    history_and_forecast,
    nice_step,
)
from flighttracker.dashboard import render_body, render_document, repo_slug
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

    def test_a_flat_range_still_produces_one_line(self):
        self.assertEqual(gridlines(500, 500), [500])


class ForecastChart(unittest.TestCase):
    def points(self, *values, start=0.0):
        return [
            Point(start + i, float(v), f"tip {v}") for i, v in enumerate(values)
        ]

    def test_a_line_needs_at_least_two_points(self):
        self.assertIn("Not enough runs", history_and_forecast(self.points(900)))

    def test_the_projection_is_drawn_dashed_and_joined_to_the_last_reading(self):
        svg = history_and_forecast(
            self.points(900, 950, 880),
            self.points(870, 860, start=3.0),
        )
        self.assertIn("series projected", svg)
        # Two paths: the recorded line, and the projection continuing from it.
        self.assertEqual(svg.count('class="series'), 2)

    def test_the_band_is_a_filled_polygon_behind_the_lines(self):
        svg = history_and_forecast(
            self.points(900, 950),
            self.points(940, start=2.0),
            [BandPoint(2.0, 900.0, 980.0)],
        )
        self.assertIn('class="band"', svg)
        self.assertLess(svg.index('class="band"'), svg.index('class="series"'))

    def test_no_projection_means_no_dashed_path(self):
        svg = history_and_forecast(self.points(900, 950, 880))
        self.assertNotIn("projected", svg)

    def test_a_wide_band_still_fits_inside_the_plot(self):
        svg = history_and_forecast(
            self.points(900, 910),
            self.points(905, start=2.0),
            [BandPoint(2.0, 500.0, 1500.0)],
        )
        self.assertIn('class="band"', svg)
        ys = [float(m) for m in re.findall(r",(\d+\.\d)", svg)]
        self.assertTrue(all(-1 <= y <= 261 for y in ys), max(ys))

    def test_every_point_gets_a_hover_target(self):
        svg = history_and_forecast(
            self.points(900, 950), self.points(940, start=2.0)
        )
        self.assertEqual(svg.count("data-tip="), 3)

    def test_chart_text_is_escaped(self):
        svg = history_and_forecast(
            self.points(900, 950), x_labels=[(0.0, "<script>")]
        )
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)


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

    def page(self, config, days_on=6):
        return render_document(self.conn, config, START + timedelta(days=days_on))

    def test_a_full_page_is_a_valid_standalone_document(self):
        config = make_config(make_watch("tokyo", label="NYC to Tokyo"))
        self.track(config, [900, 880, 910, 870], 6)
        html = self.page(config)

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
        self.assertIn("<style>", body)

    def test_the_page_loads_nothing_from_anywhere_else(self):
        """It has to render with no network: opened from disk, or offline.

        A link the reader can tap is not a load — it fetches nothing until
        they choose to follow it — so anchors are allowed and resources are
        not.
        """
        config = make_config(make_watch("tokyo"))
        self.track(config, [900, 880, 910], 5)
        html = self.page(config, 5)

        self.assertFalse(re.search(r'src="https?://', html), "remote resource")
        self.assertFalse(re.search(r"@import|url\(\s*['\"]?https?://", html), "remote CSS")
        for tag in ("<link", "<iframe", "<img", "<object", "<embed"):
            self.assertNotIn(tag, html, f"{tag} loads something external")

        # The only absolute URLs are anchors the reader can choose to follow.
        for match in re.finditer(r'href="(https?://[^"]*)"', html):
            start = html.rfind("<", 0, match.start())
            self.assertTrue(
                html[start:start + 2] == "<a", f"non-anchor href: {match.group(1)}"
            )

    def test_the_page_is_a_chart_and_not_a_dashboard(self):
        """The point of the rewrite: everything else was removed on purpose."""
        config = make_config(make_watch("tokyo"))
        self.track(config, [900, 880, 910, 870], 6)
        html = self.page(config)

        for gone in (
            "Buy signals",          # the summary strip
            "Booking-horizon curve",
            "Dates in the latest run",
            "Outlook",
            "Days out",
            "Best dates",
        ):
            self.assertNotIn(gone, html, gone)

    def test_one_chart_per_watch_and_no_more(self):
        config = make_config(make_watch("a"), make_watch("b", origin="SFO"))
        self.track(config, [900, 880, 910], 5)
        html = self.page(config, 5)
        self.assertEqual(html.count("<svg"), 2)

    def test_a_flagged_watch_is_marked(self):
        config = make_config(
            make_watch("tokyo", max_price_alert=900), min_observations=2
        )
        self.track(config, [880], 4)
        html = self.page(config, 4)
        self.assertIn("Buy signal", html)
        self.assertIn('class="card flagged"', html)

    def test_an_ordinary_watch_gets_no_pill(self):
        config = make_config(make_watch("tokyo"), min_observations=99)
        self.track(config, [900, 880], 4)
        self.assertNotIn("Buy signal", self.page(config, 4))

    def test_a_watch_with_no_data_does_not_break_the_page(self):
        config = make_config(make_watch("fresh"))
        html = render_document(self.conn, config, START)
        self.assertIn("Flight Price Watch", html)
        self.assertIn("Not enough runs yet", html)

    def test_a_watch_with_no_price_says_so(self):
        config = make_config(make_watch("gone"))
        self.assertIn("No price returned", render_document(self.conn, config, START))

    def test_watch_labels_are_escaped(self):
        config = make_config(make_watch("x", label="<script>alert(1)</script>"))
        self.track(config, [900], 3)
        html = self.page(config, 3)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_the_numbers_stay_reachable_behind_a_disclosure(self):
        """A chart nobody can read as numbers is not an accessible chart."""
        config = make_config(make_watch("tokyo"))
        self.track(config, [900, 880, 910, 870], 7)
        html = self.page(config, 7)
        self.assertIn("Show the numbers (7 runs)", html)
        self.assertIn("<table>", html)

    def test_imported_history_is_still_declared(self):
        from datetime import date as date_type
        from flighttracker.backfill import History, PricePoint, import_history

        class Stub:
            def history(self, watch, depart, back):
                return History(points=(
                    PricePoint(date_type(2026, 6, 1), 1200.0),
                    PricePoint(date_type(2026, 6, 2), 1150.0),
                ), currency="USD")

        config = make_config(make_watch("tokyo"))
        import_history(self.conn, config.watches[0], Stub(), config.settings)
        self.track(config, [900], 2)
        self.assertIn("2 imported", self.page(config, 2))

    def test_a_projection_appears_once_there_is_enough_history(self):
        config = make_config(make_watch("tokyo"), min_trend_observations=4)
        self.track(config, [900, 880, 910, 870, 890, 875], 10)
        html = self.page(config, 10)

        self.assertIn("series projected", html)
        self.assertIn('class="band"', html)
        self.assertIn("Projected", html)
        self.assertIn("Range", html)

    def test_the_page_carries_no_explanatory_prose(self):
        """The page is the chart. Everything wordy lives in the README now."""
        config = make_config(make_watch("tokyo"), min_trend_observations=4)
        self.track(config, [900, 880, 910, 870, 890, 875], 10)
        html = self.page(config, 10)

        for prose in (
            "advance-purchase",
            "of this curve is yet drawn from your own",
            "scraped from Google Flights",
            "not a promise",
            "<footer",
        ):
            self.assertNotIn(prose, html)

    def test_a_watch_with_no_prices_draws_no_projection(self):
        config = make_config(make_watch("gone"))
        html = render_document(self.conn, config, START)
        self.assertNotIn("series projected", html)
        self.assertNotIn("Could be anywhere in here", html)



class TheAddRemoveInterface(unittest.TestCase):
    """The page cannot edit the watchlist itself. It asks GitHub to."""

    def setUp(self):
        self.conn = connect(":memory:")
        self.config = make_config(make_watch("tokyo"), make_watch("lisbon"))

    def page(self, repo):
        return render_document(self.conn, self.config, START, repo=repo)

    def test_it_offers_a_button_to_add_and_one_per_watch_to_remove(self):
        html = self.page("someone/flightTool")
        self.assertIn("data-add", html)
        self.assertIn('data-drop="tokyo"', html)
        self.assertIn('data-drop="lisbon"', html)

    def test_it_carries_the_forms_they_open(self):
        html = self.page("someone/flightTool")
        for dialog in ("add-dialog", "drop-dialog", "key-dialog"):
            self.assertIn(f'id="{dialog}"', html)

    def test_it_tells_the_script_which_repo_and_workflow_to_ask(self):
        html = self.page("someone/flightTool")
        found = re.search(r"window\.__watchlist = (\{.*?\});", html)
        self.assertIsNotNone(found)
        config = json.loads(found.group(1))
        self.assertEqual(config["repo"], "someone/flightTool")
        self.assertEqual(config["workflow"], "watchlist.yml")
        self.assertEqual(config["ref"], "main")

    def test_without_a_repo_the_whole_interface_is_left_off(self):
        """Better no controls than controls that go nowhere."""
        html = self.page("")
        for absent in ("data-add", "data-drop", "<dialog", "__watchlist"):
            self.assertNotIn(absent, html)
        self.assertIn("Flight Price Watch", html)
        self.assertIn("tokyo", html)

    def test_a_repo_name_that_is_not_one_is_refused(self):
        """It is written into a script tag, so it is checked before it goes in."""
        for hostile in (
            'a"><script>alert(1)</script>/b',
            "owner/repo</script><script>alert(1)</script>",
            "owner/repo\u2028alert(1)",
            "not-a-slug",
            "too/many/parts",
        ):
            with self.subTest(repo=hostile):
                html = self.page(hostile)
                self.assertNotIn("__watchlist", html)
                self.assertNotIn("alert(1)", html)

    def test_the_config_cannot_break_out_of_its_script_tag(self):
        """`json.dumps` escapes quotes but not `<`, which is not enough here."""
        from flighttracker.dashboard import _script_json

        encoded = _script_json({"repo": "x</script><script>alert(1)</script>"})
        self.assertNotIn("</script>", encoded)
        self.assertNotIn("<", encoded)
        self.assertEqual(
            json.loads(encoded)["repo"], "x</script><script>alert(1)</script>"
        )

    def test_the_token_lives_in_the_browser_and_not_in_the_page(self):
        """It must never be baked into a file that gets committed."""
        html = self.page("someone/flightTool")
        self.assertIn("localStorage", html)

        # The only mention of a token's shape is the input's placeholder.
        for spelling in ("github_pat_", "ghp_"):
            for found in re.finditer(re.escape(spelling), html):
                line = html[html.rfind("\n", 0, found.start()) : found.end() + 40]
                self.assertIn("placeholder", line, f"{spelling} outside a placeholder")

        # ...and nothing that looks like a real token is assigned anywhere.
        self.assertFalse(re.search(r"(token|key)\s*[:=]\s*['\"][A-Za-z0-9_]{12,}", html))

    def test_it_only_ever_talks_to_the_github_api(self):
        html = self.page("someone/flightTool")
        for url in re.findall(r"'(https?://[^']+)'", html):
            self.assertTrue(
                url.startswith("https://api.github.com/"), f"unexpected host: {url}"
            )


class WorkingOutTheRepo(unittest.TestCase):
    def test_actions_supplies_it_directly(self):
        with mock.patch.dict(
            os.environ, {"GITHUB_REPOSITORY": "someone/flightTool"}, clear=False
        ):
            self.assertEqual(repo_slug(), "someone/flightTool")

    def test_otherwise_it_comes_from_the_git_remote(self):
        forms = {
            "https://github.com/someone/flightTool.git": "someone/flightTool",
            "https://github.com/someone/flightTool": "someone/flightTool",
            "git@github.com:someone/flightTool.git": "someone/flightTool",
            "ssh://git@github.com/someone/flightTool.git": "someone/flightTool",
        }
        for url, expected in forms.items():
            with self.subTest(url=url):
                with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}), \
                     mock.patch("subprocess.run") as run:
                    run.return_value = mock.Mock(returncode=0, stdout=url + "\n")
                    self.assertEqual(repo_slug(), expected)

    def test_no_git_and_no_environment_means_no_repo(self):
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}), \
             mock.patch("subprocess.run", side_effect=OSError("no git")):
            self.assertIsNone(repo_slug())

    def test_a_failing_git_command_is_not_an_error(self):
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=128, stdout="")
            self.assertIsNone(repo_slug())


if __name__ == "__main__":
    unittest.main()
