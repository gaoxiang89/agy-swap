"""OS labelling used by ``agyswap doctor``, and the status-line launcher."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agyswap import statusline
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


class LauncherTest(unittest.TestCase):
    """The launcher is what makes quoting irrelevant in agy's settings.

    agy may spawn the status-line command through a shell or exec it directly;
    a single bare word with no arguments behaves the same either way.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agyswap-launcher-"))
        self.exe = self.tmp / "agyswap"
        self.exe.write_text("", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, chain=""):
        with mock.patch.object(statusline, "_agyswap_executable", return_value=str(self.exe)):
            return statusline.write_wrapper(chain)

    def test_launcher_lives_next_to_agyswap(self):
        target = self._write()
        self.assertEqual(target.parent, self.tmp)
        self.assertTrue(target.name.startswith("agyswap") or target.name.startswith("agy-statusline"))

    def test_launcher_invokes_statusline_with_absolute_executable(self):
        target = self._write()
        text = target.read_text(encoding="utf-8")
        self.assertIn(str(self.exe), text)
        self.assertIn("statusline", text)

    def test_launcher_bakes_in_the_chain(self):
        target = self._write("agy-tps-statusline")
        self.assertIn("--chain", target.read_text(encoding="utf-8"))
        self.assertIn("agy-tps-statusline", target.read_text(encoding="utf-8"))

    def test_launcher_without_chain_has_no_flag(self):
        target = self._write("")
        self.assertNotIn("--chain", target.read_text(encoding="utf-8"))

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits only")
    def test_launcher_is_executable_on_posix(self):
        target = self._write()
        self.assertTrue(os.access(target, os.X_OK))

    def test_missing_executable_falls_back_to_path_lookup(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(statusline._agyswap_executable(), "agyswap")

    def test_executable_is_dev_suffixed_windows_entry_point(self):
        with mock.patch("shutil.which", return_value="C:/tools/agyswap.EXE"):
            self.assertTrue(statusline._agyswap_executable().endswith("agyswap.EXE"))


if __name__ == "__main__":
    unittest.main()
