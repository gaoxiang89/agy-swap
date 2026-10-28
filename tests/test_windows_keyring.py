"""Windows Credential Manager round-trip, on an isolated target name.

The real agy credential (``gemini:antigravity``) is never touched: these tests
temporarily point the module at ``agy-swap-selftest:probe`` and delete it again
afterwards, so the suite stays safe to run on a developer's own machine.
"""

import json
import os
import unittest
from unittest import mock

from tests import bootstrap

bootstrap.reset()

from agyswap import credentials, paths  # noqa: E402

SELFTEST_TARGET = "agy-swap-selftest:probe"
SELFTEST_ACCOUNT = "probe"


def _delete_selftest_credential() -> None:
    import ctypes
    import ctypes.wintypes as wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    advapi.CredDeleteW(SELFTEST_TARGET, 1, 0)


@unittest.skipUnless(os.name == "nt", "Windows Credential Manager only")
class WindowsKeyringTest(unittest.TestCase):
    def setUp(self):
        _delete_selftest_credential()
        self._target = mock.patch.object(paths, "KEYRING_TARGET", SELFTEST_TARGET)
        self._account = mock.patch.object(paths, "KEYRING_ACCOUNT", SELFTEST_ACCOUNT)
        self._target.start()
        self._account.start()

    def tearDown(self):
        self._target.stop()
        self._account.stop()
        _delete_selftest_credential()

    def test_roundtrip_stores_raw_json(self):
        payload = json.dumps({"token": {"access_token": "abc"}})
        self.assertTrue(credentials._windows_keyring_write(payload))
        self.assertEqual(credentials._windows_keyring_read(), payload)

    def test_write_overwrites_existing(self):
        credentials._windows_keyring_write(json.dumps({"token": {"access_token": "one"}}))
        credentials._windows_keyring_write(json.dumps({"token": {"access_token": "two"}}))
        self.assertIn("two", credentials._windows_keyring_read())

    def test_read_missing_returns_none(self):
        self.assertIsNone(credentials._windows_keyring_read())

    def test_read_ignores_non_token_payload(self):
        self.assertTrue(credentials._windows_keyring_read() is None)
        # Store something that is valid UTF-8 but not a token: reading must
        # reject it rather than hand garbage to the switcher.
        credentials._windows_keyring_write("just a string, not json")
        self.assertIsNone(credentials._windows_keyring_read())


if __name__ == "__main__":
    unittest.main()
