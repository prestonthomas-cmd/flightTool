import unittest
from datetime import date, datetime, timezone

from flighttracker.config import Settings
from flighttracker.digest import (
    EmailNotConfigured,
    SmtpConfig,
    build_message,
    render_html,
    render_text,
    subject_for,
)
from flighttracker.fetch import Failure
from flighttracker.signals import evaluate
from flighttracker.store import RunPoint, to_iso

from .support import make_watch

WHEN = datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc)
SETTINGS = Settings(min_observations=3, percentile=20)


def history(*prices):
    return [RunPoint(to_iso(WHEN), float(price)) for price in prices]


def verdict(price, past=(900, 850, 880, 870), watch=None, **kwargs):
    return evaluate(
        watch=watch or make_watch("tokyo", label="NYC to Tokyo, December"),
        price=price,
        history=history(*past),
        settings=SETTINGS,
        currency="USD",
        now=WHEN,
        best_depart="2026-12-11",
        best_return="2026-12-24",
        **kwargs,
    )


class Subjects(unittest.TestCase):
    def test_a_single_signal_names_the_watch_and_price(self):
        flagged = verdict(600)
        self.assertEqual(
            subject_for([flagged], []), "Buy signal: NYC to Tokyo, December at USD 600"
        )

    def test_several_signals_lead_with_the_cheapest(self):
        cheap = verdict(600)
        dearer = verdict(800, watch=make_watch("lisbon", label="Lisbon"), past=(1200, 1100, 1150, 1180))
        subject = subject_for([dearer, cheap], [])
        self.assertIn("2 buy signals", subject)
        self.assertIn("NYC to Tokyo, December", subject)

    def test_a_quiet_run_says_so(self):
        self.assertEqual(subject_for([], []), "Flight tracker: no buy signals")

    def test_failures_are_visible_in_the_subject(self):
        failure = Failure("tokyo", date(2026, 12, 10), date(2026, 12, 24), "blocked")
        self.assertIn("1 lookup failure", subject_for([], [failure]))


class TextDigest(unittest.TestCase):
    def test_a_flagged_watch_shows_price_dates_reason_and_history(self):
        body = render_text(WHEN, [verdict(600)], [])
        self.assertIn("BUY SIGNALS (1)", body)
        self.assertIn("NYC to Tokyo, December", body)
        self.assertIn("USD 600", body)
        self.assertIn("2026-12-11 -> 2026-12-24", body)
        self.assertIn("lowest seen in 4 runs", body)
        self.assertIn("history: 4 runs", body)
        self.assertIn("google.com/travel/flights", body)

    def test_unflagged_watches_appear_as_context_not_noise(self):
        body = render_text(WHEN, [verdict(875)], [])
        self.assertIn("No buy signals this run.", body)
        self.assertIn("ALSO TRACKED", body)
        self.assertIn("below its median", body)

    def test_failures_are_listed_with_their_dates(self):
        failure = Failure("tokyo", date(2026, 12, 10), date(2026, 12, 24), "blocked")
        body = render_text(WHEN, [], [failure])
        self.assertIn("LOOKUPS THAT FAILED (1)", body)
        self.assertIn("2026-12-10 / 2026-12-24", body)
        self.assertIn("blocked", body)

    def test_a_watch_with_no_price_says_why(self):
        body = render_text(WHEN, [verdict(None)], [])
        self.assertIn("no price", body)

    def test_prices_are_never_shown_as_a_quote(self):
        self.assertIn("snapshot, not a quote", render_text(WHEN, [verdict(600)], []))


class HtmlDigest(unittest.TestCase):
    def test_the_card_carries_the_headline_numbers(self):
        html = render_html(WHEN, [verdict(600)], [])
        self.assertIn("Buy signals (1)", html)
        self.assertIn("USD 600", html)
        self.assertIn("href=", html)

    def test_watch_labels_are_escaped(self):
        watch = make_watch("x", label="<script>alert(1)</script>")
        html = render_html(WHEN, [verdict(600, watch=watch)], [])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_failure_messages_are_escaped(self):
        failure = Failure("w", None, None, "<b>boom</b>")
        html = render_html(WHEN, [], [failure])
        self.assertNotIn("<b>boom</b>", html)


class SmtpFromEnvironment(unittest.TestCase):
    BASE = {
        "SMTP_HOST": "smtp.example.com",
        "EMAIL_FROM": "tracker@example.com",
        "EMAIL_TO": "me@example.com",
    }

    def test_defaults_suit_starttls(self):
        config = SmtpConfig.from_env(dict(self.BASE))
        self.assertEqual(config.port, 587)
        self.assertEqual(config.security, "starttls")
        self.assertEqual(config.recipients, ("me@example.com",))

    def test_ssl_defaults_to_its_own_port(self):
        config = SmtpConfig.from_env({**self.BASE, "SMTP_SECURITY": "ssl"})
        self.assertEqual(config.port, 465)

    def test_several_recipients_are_split(self):
        config = SmtpConfig.from_env({**self.BASE, "EMAIL_TO": "a@x.com, b@x.com;c@x.com"})
        self.assertEqual(config.recipients, ("a@x.com", "b@x.com", "c@x.com"))

    def test_missing_settings_are_named(self):
        with self.assertRaises(EmailNotConfigured) as caught:
            SmtpConfig.from_env({"SMTP_HOST": "smtp.example.com"})
        self.assertIn("EMAIL_FROM", str(caught.exception))
        self.assertIn("EMAIL_TO", str(caught.exception))

    def test_an_unknown_security_mode_is_refused(self):
        with self.assertRaises(EmailNotConfigured):
            SmtpConfig.from_env({**self.BASE, "SMTP_SECURITY": "tls-ish"})

    def test_a_nonnumeric_port_is_refused(self):
        with self.assertRaises(EmailNotConfigured):
            SmtpConfig.from_env({**self.BASE, "SMTP_PORT": "five-eight-seven"})


class Messages(unittest.TestCase):
    def test_the_message_carries_both_a_text_and_an_html_part(self):
        config = SmtpConfig.from_env(dict(SmtpFromEnvironment.BASE))
        message = build_message(config, WHEN, [verdict(600)], [])

        self.assertEqual(message["To"], "me@example.com")
        self.assertIn("Buy signal", message["Subject"])
        types = {part.get_content_type() for part in message.walk()}
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)


if __name__ == "__main__":
    unittest.main()
