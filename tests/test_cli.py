"""OS labelling used by ``agyswap doctor``."""

import sys
import unittest
from unittest import mock

from agyswap.cli import platform_label


class PlatformLabelTest(unittest.TestCase):
    def test_windows_11_uses_build_number(self):
        """Windows 11 reports NT 10.0; only the build (>=22000) says 11."""
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(
            sys, "getwindowsversion", return_value=mock.Mock(build=26200)
        ):
            self.assertEqual(platform_label(), "Windows 11")

    def test_windows_10(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(
            sys, "getwindowsversion", return_value=mock.Mock(build=19045)
        ):
            self.assertEqual(platform_label(), "Windows 10")

    def test_windows_survives_missing_api(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(
            sys, "getwindowsversion", side_effect=AttributeError
        ):
            self.assertTrue(platform_label().startswith("Windows"))

    def test_macos(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch(
            "agyswap.cli.platform.mac_ver", return_value=("14.5", ("", "", ""), "")
        ):
            self.assertEqual(platform_label(), "macOS 14.5")

    def test_linux(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch(
            "agyswap.cli.platform.release", return_value="6.6.87.2-microsoft-standard-WSL2"
        ):
            self.assertEqual(
                platform_label(), "Linux 6.6.87.2-microsoft-standard-WSL2"
            )


if __name__ == "__main__":
    unittest.main()
