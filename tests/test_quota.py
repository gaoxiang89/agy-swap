import json
import time
import unittest

from tests import bootstrap

bootstrap.reset()

from agyswap import paths, quota  # noqa: E402

#: Exactly the payload shape agy 1.4.3 emits, verified against a real capture.
REAL_STATUSLINE = {
    "model": {"id": "gemini-3-pro", "display_name": "Gemini 3 Pro", "effort": "high"},
    "context_window": {"total_input_tokens": 42000, "context_window_size": 1000000},
    "quota": {
        "3p-5h": {
            "remaining_fraction": 1,
            "reset_time": "2026-10-28T14:02:44Z",
            "reset_in_seconds": 18000,
        },
        "3p-weekly": {
            "remaining_fraction": 1,
            "reset_time": "2026-11-04T09:02:44Z",
            "reset_in_seconds": 604800,
        },
        "gemini-5h": {
            "remaining_fraction": 1,
            "reset_time": "2026-10-28T14:02:44Z",
            "reset_in_seconds": 18000,
        },
        "gemini-weekly": {
            "remaining_fraction": 0.969828,
            "reset_time": "2026-11-02T07:35:46Z",
            "reset_in_seconds": 537966,
        },
    },
    "plan_tier": "Google AI Pro",
    "email": "user@example.com",
}



class StatuslineMappingTest(unittest.TestCase):
    def test_real_payload(self):
        mapped = quota.map_statusline(REAL_STATUSLINE)
        windows = mapped["windows"]
        self.assertEqual(
            set(windows), {"3p-5h", "3p-weekly", "gemini-5h", "gemini-weekly"}
        )
        self.assertEqual(windows["gemini-5h"]["used_pct"], 0.0)
        self.assertEqual(windows["3p-weekly"]["used_pct"], 0.0)
        self.assertAlmostEqual(windows["gemini-weekly"]["used_pct"], 3.0, places=1)
        self.assertEqual(windows["gemini-weekly"]["resets_at"], "2026-11-02T07:35:46Z")

    def test_legacy_rate_limits_shape(self):
        payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": 42, "resets_at": "2026-10-28T20:00:00Z"},
                "seven_day": {"used_percentage": 11, "resets_at": "2026-11-01T00:00:00Z"},
            }
        }
        windows = quota.map_statusline(payload)["windows"]
        self.assertEqual(windows["five_hour"]["used_pct"], 42.0)
        self.assertEqual(windows["seven_day"]["used_pct"], 11.0)

    def test_empty_payload(self):
        self.assertEqual(quota.map_statusline({})["windows"], {})

    def test_ignores_malformed_entries(self):
        payload = {"quota": {"broken": "nope", "gemini-weekly": {"remaining_fraction": None}}}
        self.assertEqual(quota.map_statusline(payload)["windows"], {})


class DisplayTest(unittest.TestCase):
    def test_orders_known_windows_first(self):
        entry = {
            "windows": {
                "custom": {"used_pct": 5, "resets_at": ""},
                "seven_day": {"used_pct": 7, "resets_at": ""},
                "gemini-weekly": {"used_pct": 3, "resets_at": ""},
            }
        }
        labels = [row["label"] for row in quota.display_windows(entry)]
        self.assertEqual(labels, ["gemini 7d", "7d", "custom"])

    def test_empty_entry(self):
        self.assertEqual(quota.display_windows({}), [])


class CacheTest(unittest.TestCase):
    def setUp(self):
        bootstrap.reset()

    def test_migrates_v1_flat_keys(self):
        legacy = {
            "accounts": {
                "old@example.com": {
                    "five_hour": 90.0,
                    "five_resets_at": "2026-10-28T20:00:00Z",
                    "seven_day": 90.0,
                    "seven_resets_at": "2026-10-28T20:00:00Z",
                    "captured_at": 1000,
                }
            },
            "captured_at": 1000,
        }
        paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        paths.QUOTA_CACHE.write_text(json.dumps(legacy), encoding="utf-8")

        cache = quota.load_cache()
        self.assertEqual(cache["schema"], quota.SCHEMA)
        entry = cache["accounts"]["old@example.com"]
        self.assertIn("five_hour", entry["windows"])
        self.assertEqual(entry["sources"]["statusline"], 1000)

    def test_save_skips_unchanged(self):
        cache = {"schema": quota.SCHEMA, "accounts": {}}
        self.assertTrue(quota.save_cache(cache))
        self.assertFalse(quota.save_cache(cache))

    def test_statusline_capture_files_under_email(self):
        email = quota.statusline_capture(REAL_STATUSLINE)
        self.assertEqual(email, "user@example.com")
        entry = quota.load_cache()["accounts"]["user@example.com"]
        self.assertIn("gemini-weekly", entry["windows"])
        self.assertIn("captured_at", entry)

    def test_statusline_capture_throttles_identical_samples(self):
        quota.statusline_capture(REAL_STATUSLINE)
        before = paths.QUOTA_CACHE.stat().st_mtime_ns
        quota.statusline_capture(REAL_STATUSLINE)
        self.assertEqual(paths.QUOTA_CACHE.stat().st_mtime_ns, before)

    def test_statusline_capture_rejects_payload_without_quota(self):
        self.assertIsNone(quota.statusline_capture({"email": "x@example.com"}))

    def test_statusline_capture_merges_new_windows(self):
        quota.statusline_capture(REAL_STATUSLINE)
        changed = {
            **REAL_STATUSLINE,
            "quota": {
                **REAL_STATUSLINE["quota"],
                "five_hour": {"remaining_fraction": 0.5, "reset_time": ""},
            },
        }
        quota.statusline_capture(changed)
        entry = quota.load_cache()["accounts"]["user@example.com"]
        self.assertIn("gemini-weekly", entry["windows"])
        self.assertIn("five_hour", entry["windows"])
        self.assertEqual(entry["windows"]["five_hour"]["used_pct"], 50.0)


class DescribeTest(unittest.TestCase):
    def setUp(self):
        bootstrap.reset()

    def test_unknown_account_explains_itself(self):
        info = quota.describe("ghost@example.com")
        self.assertEqual(info["windows"], [])
        self.assertEqual(info["status"], "unknown")
        self.assertIn("no samples", info["detail"])

    def test_known_account_reports_windows_and_age(self):
        quota.statusline_capture(REAL_STATUSLINE)
        info = quota.describe("user@example.com")
        self.assertEqual(
            [w["label"] for w in info["windows"]],
            ["gemini 5h", "gemini 7d", "3p 5h", "3p 7d"],
        )
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["detail"], "")
        self.assertIsNotNone(info["captured_at"])

    def test_describe_accepts_a_preloaded_cache(self):
        quota.statusline_capture(REAL_STATUSLINE)
        cache = quota.load_cache()
        info = quota.describe("user@example.com", cache)
        self.assertEqual(len(info["windows"]), 4)


if __name__ == "__main__":
    unittest.main()
