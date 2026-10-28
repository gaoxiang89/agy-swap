"""Timestamp parsing, including the shapes agy actually emits."""

import datetime
import unittest

from agyswap.timeutil import format_duration, iso_from_ts, parse_iso_ts


def utc(*args) -> float:
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc).timestamp()


class ParseTest(unittest.TestCase):
    def test_zulu(self):
        self.assertEqual(parse_iso_ts("2026-11-02T07:35:46Z"), utc(2026, 11, 2, 7, 35, 46))

    def test_offset(self):
        self.assertEqual(
            parse_iso_ts("2026-10-28T18:02:28+08:00"),
            parse_iso_ts("2026-10-28T10:02:28Z"),
        )

    def test_dotnet_seven_digit_fraction(self):
        """agy's Credential Manager payload uses 7 fractional digits.

        Python 3.10's ``fromisoformat`` rejects anything that is not 3 or 6
        digits, so this used to return None -- which made an expired token look
        valid and sent a doomed request to the quota API.
        """
        parsed = parse_iso_ts("2026-10-28T18:02:28.3629063+08:00")
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed, utc(2026, 10, 28, 10, 2, 28, 362906), places=3)

    def test_fraction_digit_counts(self):
        expected = parse_iso_ts("2026-10-28T18:02:28+08:00")
        for text in (
            "2026-10-28T18:02:28.3+08:00",
            "2026-10-28T18:02:28.36+08:00",
            "2026-10-28T18:02:28.362906+08:00",
            "2026-10-28T18:02:28.362906300+08:00",
        ):
            parsed = parse_iso_ts(text)
            self.assertIsNotNone(parsed, text)
            self.assertAlmostEqual(parsed, expected, delta=1.0)

    def test_naive_is_treated_as_utc(self):
        self.assertEqual(
            parse_iso_ts("2026-10-28T10:02:28"), parse_iso_ts("2026-10-28T10:02:28Z")
        )

    def test_dotnet_slash_format(self):
        self.assertEqual(
            parse_iso_ts("2026/11/03 02:09:32"), parse_iso_ts("2026-11-03T02:09:32Z")
        )

    def test_numbers_pass_through(self):
        self.assertEqual(parse_iso_ts(1761640000), 1761640000.0)
        self.assertEqual(parse_iso_ts(1761640000.5), 1761640000.5)

    def test_invalid_returns_none(self):
        for value in (None, "", "   ", "not a date", "2026-13-45T99:99:99Z"):
            self.assertIsNone(parse_iso_ts(value), value)


class DurationTest(unittest.TestCase):
    def test_roundtrip(self):
        ts = 1793181748.0
        self.assertEqual(parse_iso_ts(iso_from_ts(ts)), ts)

    def test_format(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(59), "59s")
        self.assertEqual(format_duration(60), "1m")
        self.assertEqual(format_duration(86400), "1d")
        self.assertEqual(format_duration(86400 * 2 + 3600), "2d 1h")


if __name__ == "__main__":
    unittest.main()
