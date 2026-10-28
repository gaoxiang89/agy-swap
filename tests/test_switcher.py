import json
import unittest
from unittest import mock

from tests import bootstrap

bootstrap.reset()

from agyswap import credentials, paths  # noqa: E402
from agyswap.switcher import AgyError, AgySwitcher, current_email  # noqa: E402
from tests.test_credentials import make_token  # noqa: E402


class SwitcherTestCase(unittest.TestCase):
    """Every test runs with the OS keyring stubbed out.

    Without this, a Windows test run would read -- and worse, *write* -- the
    developer's real Credential Manager entry for agy.
    """

    def setUp(self):
        bootstrap.reset()
        paths.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        paths.BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

        self._patches = [
            mock.patch.object(credentials, "read_keyring", return_value=None),
            mock.patch.object(credentials, "write_keyring", return_value=False),
            mock.patch.object(credentials, "email_from_token",
                              side_effect=lambda text, allow_network=True:
                              self._offline_email(text)),
        ]
        for patch in self._patches:
            patch.start()
        self._keyring_written = []

    def tearDown(self):
        for patch in self._patches:
            patch.stop()

    @staticmethod
    def _offline_email(text):
        return credentials._jwt_claims(
            credentials.token_json(text).get("id_token")
        ).get("email")

    def set_live_token(self, email, **kwargs):
        token = make_token(email, **kwargs)
        paths.TOKEN_PATH.write_text(token, encoding="utf-8")
        return token


class AddTest(SwitcherTestCase):
    def test_add_creates_account_from_token_identity(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        account = switcher.add_current()
        self.assertEqual(account.number, 1)
        self.assertEqual(account.email, "alice@example.com")
        self.assertIsNotNone(
            credentials.read_token_backup(1, "alice@example.com")
        )

    def test_add_without_token_raises(self):
        switcher = AgySwitcher()
        with self.assertRaises(AgyError):
            switcher.add_current()

    def test_add_twice_is_idempotent(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        first = switcher.add_current()
        second = switcher.add_current()
        self.assertEqual(first.number, second.number)

    def test_add_second_account_gets_next_number(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        self.set_live_token("bob@example.com")
        second = switcher.add_current()
        self.assertEqual(second.number, 2)

    def test_email_override_rejected_when_identity_known(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        with self.assertRaises(AgyError):
            switcher.add_current(email_override="work@example.com")
        self.assertEqual(switcher.list_accounts(), [])

    def test_email_override_used_when_identity_unknown(self):
        # No id_token and no network: the override is the only signal left.
        token = json.dumps({"token": {"access_token": "a", "refresh_token": "r"}})
        paths.TOKEN_PATH.write_text(token, encoding="utf-8")
        switcher = AgySwitcher()
        account = switcher.add_current(email_override="work@example.com")
        self.assertEqual(account.email, "work@example.com")


class ListAndStatusTest(SwitcherTestCase):
    def test_list_reports_quota_windows(self):
        from agyswap import quota
        from tests.test_quota import REAL_STATUSLINE

        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        quota.statusline_capture({**REAL_STATUSLINE, "email": "alice@example.com"})

        rows = switcher.list_accounts()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["has_token"])
        self.assertEqual(
            [w["label"] for w in row["quota_windows"]],
            ["gemini 5h", "gemini 7d", "3p 5h", "3p 7d"],
        )
        self.assertEqual(row["quota_status"], "ok")

    def test_active_flag_follows_token(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        self.set_live_token("bob@example.com")
        switcher.add_current()

        rows = {row["email"]: row for row in switcher.list_accounts()}
        self.assertFalse(rows["alice@example.com"]["active"])
        self.assertTrue(rows["bob@example.com"]["active"])

    def test_status_reports_slot(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        info = switcher.status()
        self.assertEqual(info["active"], "alice@example.com")
        self.assertEqual(info["number"], 1)


class SwitchTest(SwitcherTestCase):
    def test_switch_replaces_token_file(self):
        live = self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        bob_token = self.set_live_token("bob@example.com")
        switcher.add_current()

        result = switcher.switch("1")
        self.assertEqual(result["active"], "alice@example.com")
        self.assertEqual(paths.TOKEN_PATH.read_text(encoding="utf-8"), live)
        self.assertNotEqual(paths.TOKEN_PATH.read_text(encoding="utf-8"), bob_token)

    def test_switch_preserves_google_accounts_old(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        self.set_live_token("bob@example.com")
        switcher.add_current()

        paths.GOOGLE_ACCOUNTS_PATH.write_text(
            json.dumps({"active": "bob@example.com", "old": ["prev1", "prev2"]}),
            encoding="utf-8",
        )
        switcher.switch("1")
        data = json.loads(paths.GOOGLE_ACCOUNTS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["active"], "alice@example.com")
        self.assertEqual(data["old"], ["prev1", "prev2"])

    def test_switch_by_alias_and_email(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        self.set_live_token("bob@example.com")
        switcher.add_current()
        switcher.alias("1", "work")

        self.assertEqual(switcher.switch("work")["active"], "alice@example.com")
        self.assertEqual(switcher.switch("bob@example.com")["active"], "bob@example.com")

    def test_rotate_cycles_enabled_accounts(self):
        self.set_live_token("a@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        self.set_live_token("b@example.com")
        switcher.add_current()
        self.set_live_token("c@example.com")
        switcher.add_current()

        self.assertEqual(switcher.switch()["active"], "a@example.com")
        switcher.reload()
        self.assertEqual(switcher.switch()["active"], "b@example.com")

    def test_rotate_skips_disabled(self):
        self.set_live_token("a@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        self.set_live_token("b@example.com")
        switcher.add_current()
        switcher.set_disabled("2", True)
        switcher.switch("1")

        # 1 is active and 2 is disabled: rotation must stay on 1.
        self.assertEqual(switcher.switch()["active"], "a@example.com")

    def test_switch_rejects_mismatched_token(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        self.set_live_token("bob@example.com")
        switcher.add_current()

        # Corrupt alice's backup with bob's token.
        credentials.save_token_backup(
            1, "alice@example.com", make_token("bob@example.com")
        )
        with self.assertRaises(AgyError):
            switcher.switch("1")

    def test_switch_reports_written_targets(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        self.set_live_token("bob@example.com")
        switcher.add_current()
        result = switcher.switch("1")
        self.assertTrue(result["targets"])


class RemoveTest(SwitcherTestCase):
    def test_remove_drops_registry_and_backup(self):
        self.set_live_token("alice@example.com")
        switcher = AgySwitcher()
        switcher.add_current()
        switcher.remove_account("1")
        self.assertEqual(switcher.list_accounts(), [])
        self.assertIsNone(credentials.read_token_backup(1, "alice@example.com"))


class LegacyIdentityTest(SwitcherTestCase):
    def test_falls_back_to_google_accounts(self):
        paths.GOOGLE_ACCOUNTS_PATH.write_text(
            json.dumps({"active": "legacy@example.com", "old": []}), encoding="utf-8"
        )
        self.assertEqual(current_email(), "legacy@example.com")


if __name__ == "__main__":
    unittest.main()
