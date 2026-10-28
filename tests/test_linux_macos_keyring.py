"""Linux and macOS keyring branches, exercised with stubbed subprocesses.

We cannot run a Secret Service daemon or a Keychain in CI, but the parsing and
attribute-probing logic is worth pinning down: it is the part that silently
returns ``None`` and makes the tool fall back to files when it is wrong.
"""

import base64
import json
import unittest
from unittest import mock

from tests import bootstrap

bootstrap.reset()

from agyswap import credentials, paths  # noqa: E402

TOKEN = json.dumps({"token": {"access_token": "abc"}})


def completed(stdout: str, returncode: int = 0):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr="")


class LinuxSecretToolTest(unittest.TestCase):
    def setUp(self):
        credentials._KEYRING_WRAPPED = None
        credentials._KEYRING_SEEN = False
        self._patches = [
            mock.patch.object(credentials, "IS_LINUX", True),
            mock.patch.object(credentials, "IS_WINDOWS", False),
            mock.patch.object(credentials, "IS_MACOS", False),
            mock.patch.object(credentials, "_linux_secret_tool", return_value="/usr/bin/secret-tool"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()

    def test_reads_raw_json_and_strips_newline(self):
        with mock.patch.object(credentials.subprocess, "run", return_value=completed(TOKEN + "\n")) as run:
            self.assertEqual(credentials.read_keyring(), TOKEN)
        self.assertEqual(run.call_args[0][0][:2], ["/usr/bin/secret-tool", "lookup"])

    def test_unwraps_go_keyring_base64(self):
        encoded = paths.KEYRING_PREFIX + base64.b64encode(TOKEN.encode()).decode()
        with mock.patch.object(credentials.subprocess, "run", return_value=completed(encoded)):
            self.assertEqual(credentials.read_keyring(), TOKEN)

    def test_tries_each_attribute_set_until_one_hits(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if len(calls) < 2:
                return completed("", returncode=1)
            return completed(TOKEN)

        with mock.patch.object(credentials.subprocess, "run", side_effect=fake_run):
            self.assertEqual(credentials.read_keyring(), TOKEN)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("username", calls[0])

    def test_returns_none_when_all_lookups_miss(self):
        with mock.patch.object(credentials.subprocess, "run", return_value=completed("", returncode=1)):
            self.assertIsNone(credentials.read_keyring())

    def test_returns_none_when_secret_tool_missing(self):
        with mock.patch.object(credentials, "_linux_secret_tool", return_value=None):
            self.assertIsNone(credentials.read_keyring())

    def test_backend_reports_none_without_secret_tool(self):
        self.assertEqual(credentials._keyring_backend(), "secret-tool")
        with mock.patch.object(credentials, "_linux_secret_tool", return_value=None):
            self.assertEqual(credentials._keyring_backend(), "none")

    def test_write_does_not_create_entries_it_never_read(self):
        """agy does not use Secret Service on Linux, so do not invent entries."""
        with mock.patch.object(credentials.subprocess, "run", return_value=completed("")) as run:
            self.assertFalse(credentials.write_keyring(TOKEN))
        run.assert_not_called()

    def test_write_updates_existing_entry_in_its_own_encoding(self):
        with mock.patch.object(credentials.subprocess, "run", return_value=completed(TOKEN)):
            self.assertEqual(credentials.read_keyring(), TOKEN)

        with mock.patch.object(credentials.subprocess, "run", return_value=completed("")) as run:
            self.assertTrue(credentials.write_keyring(TOKEN))

        args = run.call_args[0][0]
        self.assertEqual(args[1], "store")
        self.assertTrue(any(a.startswith("--label=") for a in args))
        # The entry was raw, so the write stays raw.
        self.assertEqual(run.call_args[1]["input"], TOKEN)


class MacOSKeychainTest(unittest.TestCase):
    def setUp(self):
        credentials._KEYRING_WRAPPED = None
        credentials._KEYRING_SEEN = False
        self._patches = [
            mock.patch.object(credentials, "IS_LINUX", False),
            mock.patch.object(credentials, "IS_WINDOWS", False),
            mock.patch.object(credentials, "IS_MACOS", True),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()

    def test_reads_prefixed_secret(self):
        encoded = paths.KEYRING_PREFIX + base64.b64encode(TOKEN.encode()).decode()
        with mock.patch.object(credentials.subprocess, "run", return_value=completed(encoded + "\n")):
            self.assertEqual(credentials.read_keyring(), TOKEN)

    def test_reads_plain_secret(self):
        with mock.patch.object(credentials.subprocess, "run", return_value=completed(TOKEN + "\n")):
            self.assertEqual(credentials.read_keyring(), TOKEN)

    def test_nonzero_exit_returns_none(self):
        with mock.patch.object(credentials.subprocess, "run", return_value=completed("", returncode=44)):
            self.assertIsNone(credentials.read_keyring())

    def test_writes_with_update_flag(self):
        with mock.patch.object(credentials.subprocess, "run", return_value=completed("")) as run:
            self.assertTrue(credentials.write_keyring(TOKEN))
        args = run.call_args[0][0]
        self.assertIn("add-generic-password", args)
        self.assertIn("-U", args)

    def test_write_preserves_prefixed_encoding(self):
        """After reading a prefixed secret, writes must stay prefixed."""
        encoded = paths.KEYRING_PREFIX + base64.b64encode(TOKEN.encode()).decode()
        with mock.patch.object(credentials.subprocess, "run", return_value=completed(encoded)):
            credentials.read_keyring()
        with mock.patch.object(credentials.subprocess, "run", return_value=completed("")) as run:
            credentials.write_keyring(TOKEN)
        written = run.call_args[0][0][run.call_args[0][0].index("-w") + 1]
        self.assertTrue(written.startswith(paths.KEYRING_PREFIX))


class DisabledTest(unittest.TestCase):
    def test_env_flag_short_circuits_everything(self):
        with mock.patch.dict("os.environ", {"AGYSWAP_DISABLE_KEYRING": "1"}):
            self.assertEqual(credentials._keyring_backend(), "disabled")
            self.assertIsNone(credentials.read_keyring())
            self.assertFalse(credentials.write_keyring(TOKEN))
            self.assertFalse(credentials.keyring_status()["available"])


if __name__ == "__main__":
    unittest.main()
