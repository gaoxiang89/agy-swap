"""OS labelling used by ``agyswap doctor``, and the status-line launcher."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from tests import bootstrap  # noqa: F401 - isolate paths before importing agyswap

from agyswap import cli, credentials, paths, statusline  # noqa: E402
from agyswap.cli import _agy_executable, cmd_run, cmd_sync, platform_label  # noqa: E402
from agyswap.switcher import AgySwitcher  # noqa: E402
from tests.test_credentials import make_token  # noqa: E402


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


class ChainTest(unittest.TestCase):
    """A chained script must never be dropped because of our own parsing."""

    def _run(self, payload, chain_output):
        import io

        with mock.patch("sys.stdin", io.StringIO(payload)), mock.patch.object(
            statusline, "_run_chain", return_value=chain_output
        ), mock.patch("builtins.print") as printed:
            code = statusline.main(["--chain", "whatever"])
        return code, printed

    def test_chain_output_survives_malformed_input(self):
        code, printed = self._run("not json at all", "TOK/s")
        self.assertEqual(code, 0)
        printed.assert_called_with("TOK/s", flush=True)

    def test_chain_output_survives_non_dict_json(self):
        code, printed = self._run('["a", "b"]', "TOK/s")
        self.assertEqual(code, 0)
        printed.assert_called_with("TOK/s", flush=True)

    def test_empty_input_is_a_no_op(self):
        code, printed = self._run("   ", "TOK/s")
        self.assertEqual(code, 0)
        printed.assert_not_called()


class SyncCliTest(unittest.TestCase):
    def test_write_failure_exits_nonzero_even_when_quiet(self):
        sw = mock.Mock()
        sw.sync.return_value = {
            "status": "synced",
            "source": "keyring:wincred",
            "email": "a@example.com",
            "path": str(paths.SSH_TOKEN_PATH),
            "targets": [],
        }
        args = mock.Mock(if_missing=False, quiet=True)
        with mock.patch("agyswap.cli.console"):
            with self.assertRaises(SystemExit) as ctx:
                cmd_sync(sw, args)
        self.assertEqual(ctx.exception.code, 1)

    def test_quiet_success_prints_nothing(self):
        sw = mock.Mock()
        sw.sync.return_value = {
            "status": "synced",
            "source": "keyring:wincred",
            "email": "a@example.com",
            "path": str(paths.SSH_TOKEN_PATH),
            "targets": [str(paths.SSH_TOKEN_PATH)],
        }
        args = mock.Mock(if_missing=False, quiet=True)
        with mock.patch("agyswap.cli.console") as fake_console:
            cmd_sync(sw, args)
        fake_console.print.assert_not_called()


class RunCliTest(unittest.TestCase):
    """``agyswap run`` launches agy with a private, file-backed token."""

    def setUp(self):
        bootstrap.reset()
        paths.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        paths.BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        self._patches = [
            mock.patch.object(credentials, "read_keyring", return_value=None),
            mock.patch.object(credentials, "write_keyring", return_value=False),
        ]
        for patch in self._patches:
            patch.start()
        self.addCleanup(self._stop_patches)

        paths.TOKEN_PATH.write_text(
            make_token("run@example.com", expiry=time.time() + 3600),
            encoding="utf-8",
        )
        self.switcher = AgySwitcher()
        self.account = self.switcher.add_current()

    def _stop_patches(self):
        for patch in self._patches:
            patch.stop()

    @staticmethod
    def _args(agy_args=("--print", "hi"), identifier="1"):
        return mock.Mock(identifier=identifier, agy_args=list(agy_args))

    def _run(self, *, code=0, side_effect=None, args=None):
        launch = mock.Mock(return_value=code, side_effect=side_effect)
        with mock.patch.object(cli, "_agy_executable", return_value="C:/bin/agy.exe"), \
             mock.patch.object(cli, "_launch_agy", launch), \
             mock.patch.object(cli, "error_console"):
            with self.assertRaises(SystemExit) as ctx:
                cmd_run(self.switcher, args or self._args())
        return ctx.exception.code, launch

    def test_agy_gets_isolated_dir_and_file_storage(self):
        code, run = self._run()

        argv, env = run.call_args.args
        self.assertEqual(code, 0)
        self.assertEqual(argv[0], "C:/bin/agy.exe")
        self.assertTrue(argv[1].startswith("--gemini_dir="))
        self.assertTrue(
            argv[1].split("=", 1)[1].startswith(str(paths.PROFILES_DIR))
        )
        self.assertEqual(argv[2:], ["--print", "hi"])
        self.assertEqual(env["AGY_CLI_FORCE_FILE_TOKEN_STORAGE"], "1")
        self.assertEqual(env["AGYSWAP_PROFILE_EMAIL"], self.account.email)
        self.assertEqual(env["AGYSWAP_DATA_DIR"], str(paths.BACKUP_ROOT))

    def test_relative_data_root_resolves_from_launch_directory(self):
        env = {**os.environ, "AGYSWAP_DATA_DIR": "relative-data"}
        env["PYTHONPATH"] = str(Path(paths.__file__).resolve().parent.parent)
        probe = subprocess.run(
            [sys.executable, "-c", "from agyswap import paths; "
             "print(paths.profile_gemini_dir('run@example.com'))"],
            cwd=bootstrap.ROOT, env=env, capture_output=True, text=True, check=True,
        )
        profile = Path(probe.stdout.strip())
        self.assertEqual(profile.parents[2], bootstrap.ROOT.resolve() / "relative-data")

    def test_run_rejects_profile_overrides_before_preparing(self):
        for agy_args in (
            ["--gemini_dir=shared"],
            ["-gemini_dir", "shared"],
            ["acp", "--gemini-dir", "shared"],
            ["--app_data_dir=shared"],
            ["acp", "--app-data-dir=shared"],
            ["acp", "--cli_dir_name=shared"],
        ):
            with self.subTest(agy_args=agy_args):
                code, launch = self._run(args=self._args(agy_args))
                self.assertEqual(code, 1)
                launch.assert_not_called()
                self.assertFalse(paths.PROFILES_DIR.exists())

    def test_launcher_messages_go_to_stderr(self):
        stdout, stderr = StringIO(), StringIO()

        def launch(argv, env):
            print('{"result":"from agy"}')
            return 0

        with mock.patch.object(cli, "_agy_executable", return_value="C:/bin/agy.exe"), \
             mock.patch.object(cli, "_launch_agy", side_effect=launch), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                cmd_run(self.switcher, self._args())

        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(stdout.getvalue(), '{"result":"from agy"}\n')
        self.assertIn(self.account.email, stderr.getvalue())

    def test_exit_code_is_propagated(self):
        code, _run = self._run(code=7)
        self.assertEqual(code, 7)

    def test_refreshed_token_is_captured_even_on_failure(self):
        def launch(argv, env):
            profile = Path(argv[1].split("=", 1)[1])
            token = profile / paths.APP_DATA_DIR_NAME / "antigravity-oauth-token"
            token.write_text(
                make_token("run@example.com", expiry=time.time() + 7200),
                encoding="utf-8",
            )
            return 7

        self._run(side_effect=launch)

        backup = credentials.read_token_backup(
            self.account.credential_number, self.account.email
        )
        self.assertGreater(credentials.token_expiry(backup), time.time() + 7000)

    def test_exit_captures_launched_account_after_renumbering(self):
        paths.TOKEN_PATH.write_text(
            make_token("second@example.com", expiry=time.time() + 3600),
            encoding="utf-8",
        )
        second = self.switcher.add_current()
        paths.TOKEN_PATH.write_text(
            make_token("third@example.com", expiry=time.time() + 3600),
            encoding="utf-8",
        )
        third = self.switcher.add_current()
        third_backup = credentials.read_token_backup(third.credential_number, third.email)

        def launch(argv, env):
            self.switcher.remove_account("1")
            profile = Path(argv[1].split("=", 1)[1])
            primary, _ = credentials.profile_token_paths(profile)
            primary.write_text(
                make_token(second.email, expiry=time.time() + 7200),
                encoding="utf-8",
            )
            return 0

        code, _ = self._run(side_effect=launch, args=self._args(identifier="2"))

        self.assertEqual(code, 0)
        backup = credentials.read_token_backup(second.credential_number, second.email)
        self.assertGreater(credentials.token_expiry(backup), time.time() + 7000)
        self.assertEqual(
            credentials.read_token_backup(third.credential_number, third.email),
            third_backup,
        )

    def test_missing_agy_exits_nonzero(self):
        with mock.patch.object(cli, "_agy_executable", return_value=None), \
             mock.patch.object(cli, "error_console"):
            with self.assertRaises(SystemExit) as ctx:
                cmd_run(self.switcher, self._args())
        self.assertEqual(ctx.exception.code, 1)

    def test_parser_passes_everything_after_the_id_to_agy(self):
        with mock.patch.object(cli, "cmd_run") as handler, mock.patch.object(
            cli.sys, "argv", ["agyswap", "run", "2", "--print", "hi"]
        ):
            cli.main()
        args = handler.call_args.args[1]
        self.assertEqual(args.identifier, "2")
        self.assertEqual(args.agy_args, ["--print", "hi"])


class AgyExecutableTest(unittest.TestCase):
    def test_agy_exe_override_wins(self):
        tmp = Path(tempfile.mkdtemp(prefix="agyswap-agy-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = tmp / "agy"
        fake.write_text("", encoding="utf-8")

        with mock.patch.dict(os.environ, {"AGY_EXE": str(fake)}):
            self.assertEqual(_agy_executable(), str(fake))


if __name__ == "__main__":
    unittest.main()
