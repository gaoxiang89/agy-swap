"""OS labelling used by ``agyswap doctor``, and the status-line launcher."""

import json
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
from agyswap.timeutil import iso_from_ts  # noqa: E402
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


class QuotaCellTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def _row(self, windows, fetched_ago=30.0):
        return {
            "quota_windows": windows,
            "quota_status": "ok" if windows else "unknown",
            "quota_fetched_at": self.NOW - fetched_ago if fetched_ago is not None else None,
        }

    def _window(self, label, pct, reset_in):
        resets = "" if reset_in is None else iso_from_ts(self.NOW + reset_in)
        return {"key": label, "label": label, "used_pct": pct, "resets_at": resets}

    def test_every_window_gets_a_line_with_bar_pct_and_reset(self):
        cell = cli._quota_cell(
            self._row([self._window("gemini 5h", 47, 3600 + 17 * 60),
                       self._window("gemini 7d", 28, 4 * 86400)]),
            now=self.NOW,
        ).plain.splitlines()
        self.assertEqual(len(cell), 3)
        self.assertTrue(cell[0].startswith("gemini 5h ━━━━━─────  47%"))
        self.assertIn("↻ 1h 17m", cell[0])
        self.assertIn("28%", cell[1])
        self.assertIn("↻ 4d", cell[1])
        self.assertEqual(cell[2], "updated 30s ago")

    def test_labels_are_aligned(self):
        lines = cli._quota_cell(
            self._row([self._window("3p 5h", 0, None), self._window("gemini 7d", 50, None)]),
            now=self.NOW,
        ).plain.splitlines()
        self.assertEqual(lines[0].index("─"), lines[1].index("━"))

    def test_bar_can_be_dropped_for_narrow_terminals(self):
        line = cli._quota_cell(
            self._row([self._window("gemini 5h", 47, 600)]), now=self.NOW, bar_width=0
        ).plain.splitlines()[0]
        self.assertEqual(line, "gemini 5h  47% ↻ 10m")

    def test_list_fits_the_terminal_width(self):
        from rich.console import Console

        rows = [{
            "number": 1, "email": "someone.long@example.com", "alias": "", "active": True,
            "disabled": False, "has_token": True,
            **self._row([self._window("gemini 5h", 47, 3600), self._window("argon 7d", 3, 86400)]),
        }]
        for width in (80, 120):
            with self.subTest(width=width):
                out = Console(width=width, file=StringIO(), color_system=None)
                with mock.patch.object(cli, "console", out), \
                     mock.patch.object(cli.time, "time", return_value=self.NOW):
                    cli.cmd_list(mock.Mock(list_accounts=mock.Mock(return_value=rows)), None)
                printed = out.file.getvalue().splitlines()
                self.assertTrue(all(len(line) <= width for line in printed))
                self.assertTrue(any("47%" in line and "↻ 1h" in line for line in printed))
                self.assertTrue(any("someone.long@example.com" in line for line in printed))

    def test_past_reset_is_hidden_for_unused_window_but_flagged_when_used(self):
        lines = cli._quota_cell(
            self._row([self._window("3p 5h", 0, -60), self._window("gemini 5h", 40, -60)]),
            now=self.NOW,
        ).plain.splitlines()
        self.assertNotIn("↻", lines[0])
        self.assertIn("↻ reset since sample", lines[1])

    def test_stale_sample_is_dimmed(self):
        cell = cli._quota_cell(
            self._row([self._window("gemini 5h", 95, None)], fetched_ago=3600), now=self.NOW
        )
        self.assertTrue(any("dim" in str(span.style) for span in cell.spans))
        self.assertIn("updated 1h ago", cell.plain)

    def test_no_samples(self):
        self.assertEqual(cli._quota_cell(self._row([], None), now=self.NOW).plain, "no samples yet")

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


class LoginCliTest(unittest.TestCase):
    def setUp(self):
        bootstrap.reset()
        keyring = mock.patch.object(credentials, "read_keyring", return_value=None)
        with keyring:
            paths.TOKEN_PATH.write_text(make_token("current@example.com"), encoding="utf-8")
            switcher = AgySwitcher()
            self.current = switcher.add_current()
            profile = Path(switcher.prepare_profile("1")["profile"])

        profile_token, _ = credentials.profile_token_paths(profile)
        paths.SSH_TOKEN_PATH.parent.mkdir()
        paths.SSH_TOKEN_PATH.write_text(make_token("ssh@example.com"), encoding="utf-8")
        paths.SSH_TOKEN_MIRROR.write_bytes(paths.SSH_TOKEN_PATH.read_bytes())
        paths.GOOGLE_ACCOUNTS_PATH.write_text('{"active": "current@example.com"}', encoding="utf-8")
        self.existing_files = {
            path: path.read_bytes()
            for path in (*paths.TOKEN_CANDIDATES, paths.SSH_TOKEN_MIRROR,
                         paths.GOOGLE_ACCOUNTS_PATH, profile_token)
        }

    def _login(self, token=None, *, code=0):
        def launch(argv, env):
            self.assertEqual(argv[:3], ["C:/bin/agy.exe", "acp", "--login"])
            flag, target = argv[3].split("=", 1)
            self.assertEqual(flag, "--gemini_dir")
            profile = Path(target)
            self.assertEqual(profile.parent, paths.BACKUP_ROOT)
            self.assertEqual(list(profile.iterdir()), [])
            self.assertEqual(env["AGY_CLI_FORCE_FILE_TOKEN_STORAGE"], "1")
            if token is not None:
                primary, _ = credentials.profile_token_paths(profile)
                primary.parent.mkdir()
                primary.write_bytes(token.encode("utf-8") if isinstance(token, str) else token)
            return code

        with mock.patch.object(cli.sys, "argv", ["agyswap", "login"]), \
             mock.patch.object(cli, "_agy_executable", return_value="C:/bin/agy.exe"), \
             mock.patch.object(cli, "_launch_agy", side_effect=launch), \
             mock.patch.object(credentials, "read_keyring") as read_keyring, \
             mock.patch.object(credentials, "write_keyring") as write_keyring, \
             mock.patch.object(cli, "console") as console, \
             mock.patch.object(cli, "error_console"):
            try:
                cli.main()
            except SystemExit as exc:
                exit_code = exc.code
            else:
                exit_code = 0
        read_keyring.assert_not_called()
        write_keyring.assert_not_called()
        self.assertEqual(list(paths.BACKUP_ROOT.glob("login-*")), [])
        for path, before in self.existing_files.items():
            self.assertEqual(path.read_bytes(), before, str(path))
        return exit_code, console

    def test_login_registers_a_new_runnable_account_without_switching(self):
        email = "new@example.com"
        token = make_token(email)
        exit_code, console = self._login(token)

        self.assertEqual(exit_code, 0)
        self.assertIn("agyswap run 2", console.print.call_args.args[0])
        registry = json.loads(paths.SEQUENCE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(registry["activeEmail"], self.current.email)
        self.assertEqual(registry["accounts"]["2"]["email"], email)
        profile = Path(AgySwitcher().prepare_profile("2")["profile"])
        self.assertEqual(credentials.read_profile_token(profile), token)

    def test_login_again_updates_the_backup_without_adding_an_account(self):
        email = "new@example.com"
        token = make_token(email, expiry=time.time() + 3600)
        newer = make_token(email.upper(), expiry=time.time() + 7200)

        self.assertEqual(self._login(token)[0], 0)
        self.assertEqual(self._login(newer)[0], 0)
        self.assertEqual(self._login(token)[0], 0)

        registry = json.loads(paths.SEQUENCE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(registry["sequence"], [1, 2])
        self.assertEqual(registry["activeEmail"], self.current.email)
        self.assertEqual(credentials.read_token_backup(2, email), newer)

    def test_failed_login_never_registers_even_if_agy_wrote_a_token(self):
        registry = paths.SEQUENCE_FILE.read_bytes()
        self.assertEqual(self._login(make_token("new@example.com"), code=7)[0], 7)
        self.assertEqual(paths.SEQUENCE_FILE.read_bytes(), registry)

    def test_login_requires_a_token_with_an_identity(self):
        registry = paths.SEQUENCE_FILE.read_bytes()
        with mock.patch.object(credentials, "_email_from_userinfo", return_value=None):
            for token in (None, "invalid token", b"\xff", make_token("")):
                with self.subTest(token=token):
                    self.assertEqual(self._login(token)[0], 1)
                    self.assertEqual(paths.SEQUENCE_FILE.read_bytes(), registry)


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
