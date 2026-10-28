"""Rendering tests for the account card (pure functions, no event loop)."""

import time
import unittest

from agyswap.tui.widgets import (
    account_card_text,
    bar_cells,
    format_duration,
    reset_clock,
    reset_text,
    usage_rows,
)


def account(**overrides) -> dict:
    row = {
        "number": 1,
        "email": "user@example.com",
        "alias": None,
        "disabled": False,
        "active": False,
        "has_token": True,
        "quota_windows": [],
        "quota_status": "unknown",
        "quota_detail": "",
        "quota_fetched_at": None,
    }
    row.update(overrides)
    return row


class DurationTest(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(format_duration(30), "30s")
        self.assertEqual(format_duration(120), "2m")
        self.assertEqual(format_duration(3600), "1h")
        self.assertEqual(format_duration(5400), "1h 30m")
        self.assertEqual(format_duration(86400), "1d")
        self.assertEqual(format_duration(86400 + 7200), "1d 2h")


class ResetTest(unittest.TestCase):
    def test_future(self):
        now = time.time()
        self.assertTrue(reset_text("2026-11-02T07:35:46Z", now - 0).startswith("resets"))

    def test_past_says_now(self):
        self.assertEqual(reset_text("2020-01-01T00:00:00Z", time.time()), "resets now")

    def test_garbage_returns_none(self):
        self.assertIsNone(reset_text("not-a-date", time.time()))
        self.assertIsNone(reset_text(None, time.time()))

    def test_clock_is_local_time(self):
        clock = reset_clock("2099-01-01T00:00:00Z", time.time())
        self.assertRegex(clock, r"^\d{2}:\d{2}$")


class BarTest(unittest.TestCase):
    def test_empty_bar_has_full_width(self):
        text = bar_cells(None, 10)
        self.assertEqual(len(text.plain), 10)

    def test_full_bar_is_all_filled(self):
        self.assertEqual(bar_cells(100, 8).plain, "━" * 8)

    def test_zero_bar_is_all_track(self):
        self.assertEqual(bar_cells(0, 8).plain, "─" * 8)

    def test_half_cell_marker(self):
        # 5% of 10 cells = 0.5 -> one half cell.
        self.assertEqual(bar_cells(5, 10).plain, "╸" + "─" * 9)

    def test_clamps_out_of_range(self):
        self.assertEqual(bar_cells(150, 6).plain, "━" * 6)
        self.assertEqual(bar_cells(-20, 6).plain, "─" * 6)


class UsageRowsTest(unittest.TestCase):
    def test_builds_one_row_per_window(self):
        now = time.time()
        row = account(
            quota_windows=[
                {"key": "gemini-weekly", "label": "gemini", "used_pct": 3.0,
                 "resets_at": "2099-01-01T00:00:00Z"},
                {"key": "3p-weekly", "label": "3p", "used_pct": 0.0, "resets_at": ""},
            ]
        )
        rows = usage_rows(row, now)
        self.assertEqual([r[0] for r in rows], ["gemini", "3p"])
        self.assertEqual(rows[1][2], "")

    def test_skips_null_percentages(self):
        row = account(quota_windows=[{"label": "x", "used_pct": None, "resets_at": ""}])
        self.assertEqual(usage_rows(row, time.time()), [])


class CardTest(unittest.TestCase):
    def test_shows_alias_and_active_marker(self):
        text = account_card_text(
            account(alias="work", active=True,
                    quota_windows=[{"label": "gemini", "used_pct": 3.0,
                                    "resets_at": "2099-01-01T00:00:00Z"}]),
            100,
        ).plain
        self.assertIn("work", text)
        self.assertIn("user@example.com", text)
        self.assertIn("● active", text)
        self.assertIn("gemini", text)
        self.assertIn("3%", text)

    def test_placeholder_prefers_detail(self):
        text = account_card_text(
            account(quota_status="expired",
                    quota_detail="token expired — run agy to refresh"), 100
        ).plain
        self.assertIn("token expired", text)

    def test_placeholder_falls_back_to_status(self):
        text = account_card_text(account(quota_status="untokened"), 100).plain
        self.assertIn("no stored token", text)

    def test_stale_age_is_shown(self):
        text = account_card_text(
            account(quota_fetched_at=time.time() - 7200,
                    quota_windows=[{"label": "gemini", "used_pct": 10.0,
                                    "resets_at": ""}]),
            100,
        ).plain
        self.assertIn("2h ago", text)

    def test_expired_marker_only_with_data(self):
        with_windows = account_card_text(
            account(quota_status="expired",
                    quota_windows=[{"label": "gemini", "used_pct": 10.0,
                                    "resets_at": ""}]),
            100,
        ).plain
        self.assertIn("(expired)", with_windows)

    def test_narrow_width_does_not_crash(self):
        text = account_card_text(
            account(quota_windows=[{"label": "gemini", "used_pct": 50.0,
                                    "resets_at": "2099-01-01T00:00:00Z"}]),
            20,
        )
        self.assertTrue(text.plain)


if __name__ == "__main__":
    unittest.main()
