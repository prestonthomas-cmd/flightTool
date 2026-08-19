import unittest
from datetime import date, timedelta

from flighttracker.holidays import (
    describe,
    easter,
    holiday_key,
    holidays_for,
    is_peak,
    last_weekday,
    nearest_holiday,
    nth_weekday,
    peak_window,
)


def day_of(year: int, name: str) -> date:
    return {h.name: h.day for h in holidays_for(year)}[name]


class WeekdayRules(unittest.TestCase):
    def test_nth_weekday_finds_the_right_occurrence(self):
        # 1 November 2026 is a Sunday, so the first Thursday is the 5th.
        self.assertEqual(nth_weekday(2026, 11, 3, 1), date(2026, 11, 5))
        self.assertEqual(nth_weekday(2026, 11, 3, 4), date(2026, 11, 26))

    def test_last_weekday_handles_a_month_ending_on_that_day(self):
        self.assertEqual(last_weekday(2027, 5, 0), date(2027, 5, 31))
        self.assertEqual(last_weekday(2026, 5, 0), date(2026, 5, 25))

    def test_last_weekday_of_december_does_not_run_into_next_year(self):
        self.assertEqual(last_weekday(2026, 12, 0), date(2026, 12, 28))


class KnownDates(unittest.TestCase):
    """Checked against the published calendars, past and future."""

    def test_thanksgiving(self):
        for year, expected in [
            (2019, date(2019, 11, 28)),
            (2024, date(2024, 11, 28)),
            (2025, date(2025, 11, 27)),
            (2026, date(2026, 11, 26)),
            (2027, date(2027, 11, 25)),
        ]:
            self.assertEqual(day_of(year, "Thanksgiving"), expected, year)

    def test_easter(self):
        for year, expected in [
            (2019, date(2019, 4, 21)),
            (2024, date(2024, 3, 31)),
            (2025, date(2025, 4, 20)),
            (2026, date(2026, 4, 5)),
            (2027, date(2027, 3, 28)),
        ]:
            self.assertEqual(easter(year), expected, year)

    def test_memorial_and_labor_day(self):
        self.assertEqual(day_of(2026, "Memorial Day"), date(2026, 5, 25))
        self.assertEqual(day_of(2027, "Memorial Day"), date(2027, 5, 31))
        self.assertEqual(day_of(2026, "Labor Day"), date(2026, 9, 7))

    def test_mlk_and_presidents_day(self):
        self.assertEqual(day_of(2026, "Martin Luther King Jr. Day"), date(2026, 1, 19))
        self.assertEqual(day_of(2026, "Presidents' Day"), date(2026, 2, 16))

    def test_fixed_date_holidays(self):
        self.assertEqual(day_of(2026, "Christmas"), date(2026, 12, 25))
        self.assertEqual(day_of(2026, "Juneteenth"), date(2026, 6, 19))


class PeakWindows(unittest.TestCase):
    def test_the_days_before_christmas_are_peak(self):
        self.assertTrue(is_peak(date(2026, 12, 20)))
        self.assertEqual(peak_window(date(2026, 12, 20)).name, "Christmas")

    def test_a_quiet_date_is_not_peak(self):
        self.assertFalse(is_peak(date(2026, 10, 3)))
        self.assertIsNone(peak_window(date(2026, 10, 3)))

    def test_the_christmas_window_reaches_across_the_year_boundary(self):
        self.assertTrue(is_peak(date(2027, 1, 2)))
        self.assertTrue(is_peak(date(2026, 12, 31)))

    def test_early_january_is_matched_to_the_new_year_not_the_old_christmas(self):
        self.assertEqual(peak_window(date(2027, 1, 3)).name, "New Year's Day")

    def test_veterans_day_has_no_travel_peak(self):
        self.assertFalse(is_peak(date(2026, 11, 11)))

    def test_the_window_is_asymmetric_around_thanksgiving(self):
        # Out beforehand, home for several days afterwards.
        self.assertTrue(is_peak(date(2026, 11, 23)))
        self.assertTrue(is_peak(date(2026, 12, 1)))
        self.assertFalse(is_peak(date(2026, 11, 21)))


class Nearest(unittest.TestCase):
    def test_distance_is_negative_before_the_holiday(self):
        holiday, offset = nearest_holiday(date(2026, 12, 20), major_only=True)
        self.assertEqual(holiday.name, "Christmas")
        self.assertEqual(offset, -5)

    def test_distance_is_positive_after(self):
        _, offset = nearest_holiday(date(2026, 12, 28), major_only=True)
        self.assertEqual(offset, 3)

    def test_a_search_window_can_come_back_empty(self):
        self.assertIsNone(nearest_holiday(date(2026, 10, 3), major_only=True, within_days=3))

    def test_a_date_in_january_can_match_the_previous_december(self):
        holiday, offset = nearest_holiday(date(2027, 1, 1), major_only=True)
        self.assertEqual(holiday.name, "New Year's Day")
        self.assertEqual(offset, 0)


class CrossYearComparison(unittest.TestCase):
    def test_the_same_holiday_offset_matches_across_years(self):
        """The whole point: two dates three years apart, same holiday position."""
        self.assertEqual(holiday_key(date(2025, 11, 25)), ("Thanksgiving", -2))
        self.assertEqual(holiday_key(date(2026, 11, 24)), ("Thanksgiving", -2))
        self.assertEqual(holiday_key(date(2027, 11, 23)), ("Thanksgiving", -2))

    def test_the_same_calendar_date_does_not_match_across_years(self):
        self.assertNotEqual(
            holiday_key(date(2025, 11, 25)), holiday_key(date(2026, 11, 25))
        )

    def test_a_date_far_from_any_holiday_has_no_key(self):
        self.assertIsNone(holiday_key(date(2026, 10, 3)))


class Descriptions(unittest.TestCase):
    def test_peak_dates_say_so(self):
        self.assertIn("peak travel", describe(date(2026, 12, 20)))
        self.assertIn("Christmas", describe(date(2026, 12, 20)))

    def test_the_holiday_itself_reads_naturally(self):
        self.assertEqual(describe(date(2026, 12, 25)), "Christmas itself")

    def test_a_date_near_a_holiday_is_measured_against_it(self):
        self.assertEqual(describe(date(2026, 11, 20)), "6d before Thanksgiving")

    def test_a_quiet_date_says_there_is_nothing_nearby(self):
        self.assertEqual(describe(date(2026, 10, 3)), "no major holiday nearby")

    def test_most_of_the_year_is_quiet(self):
        """A label that fires on every date would carry no information."""
        quiet = sum(
            holiday_key(date(2026, 1, 1) + timedelta(days=n)) is None
            for n in range(365)
        )
        self.assertGreater(quiet, 90)


if __name__ == "__main__":
    unittest.main()
