"""``agyswap upgrade``: detecting the installer and planning its commands."""

import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import time
import shutil
import unittest
from pathlib import Path
from unittest import mock

from agyswap import upgrade

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _which(name):
    return {"uv": "/bin/uv", "pipx": "/bin/pipx", "git": "/bin/git"}.get(name)


class FakeEnv:
    """A tool environment laid out the way uv or pipx leaves it."""

    def __init__(self, root: Path, manager=None, direct_url=None, version="0.2.0"):
        self.prefix = root / "tools" / "agy-swap"
        site = Path(sysconfig.get_path(
            "purelib", vars={"base": str(self.prefix), "platbase": str(self.prefix)}
        ))
        dist = site / f"agy_swap-{version}.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(f"Name: agy-swap\nVersion: {version}\n", encoding="utf-8")
        if direct_url is not None:
            (dist / "direct_url.json").write_text(json.dumps(direct_url), encoding="utf-8")
        if manager == "uv":
            (self.prefix / "uv-receipt.toml").write_text(
                "[tool]\n"
                'requirements = [{ name = "agy-swap", git = "https://example.com/agy-swap" }]\n'
                "entrypoints = [\n"
                '    { name = "agyswap", install-path = "C:/bin/agyswap.exe", from = "agy-swap" },\n'
                '    { name = "agyswap-statusline", install-path = "C:\\\\bin\\\\agyswap-statusline.exe", from = "agy-swap" },\n'
                "]\n",
                encoding="utf-8",
            )
        elif manager == "pipx":
            (self.prefix / "pipx_metadata.json").write_text(
                json.dumps({"main_package": {"apps": ["agyswap", "agyswap-statusline"]}}),
                encoding="utf-8",
            )


class UpgradeTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="agyswap-upgrade-"))
        patcher = mock.patch("agyswap.upgrade.shutil.which", side_effect=_which)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class DetectTest(UpgradeTestCase):
    def test_uv_git_install(self):
        env = FakeEnv(self.root, "uv", {"url": "https://example.com/agy-swap",
                                        "vcs_info": {"vcs": "git", "commit_id": COMMIT}})
        install = upgrade.detect(env.prefix)
        self.assertEqual(install.manager, "uv")
        self.assertIsNone(install.editable)
        self.assertEqual(install.revision, "0.2.0 (0123456)")
        self.assertIsNone(install.pinned)

    def test_pinned_revision_is_reported(self):
        env = FakeEnv(self.root, "uv", {"url": "https://example.com/agy-swap", "vcs_info": {
            "vcs": "git", "commit_id": COMMIT, "requested_revision": "v0.2.0"}})
        self.assertEqual(upgrade.detect(env.prefix).pinned, "v0.2.0")

    def test_pipx_install(self):
        env = FakeEnv(self.root, "pipx", {"url": "https://example.com/agy-swap",
                                          "vcs_info": {"vcs": "git", "commit_id": COMMIT}})
        self.assertEqual(upgrade.detect(env.prefix).manager, "pipx")

    def test_editable_install_points_at_the_checkout(self):
        src = self.root / "src"
        src.mkdir()
        env = FakeEnv(self.root, "uv", {"url": src.as_uri(), "dir_info": {"editable": True}})
        with mock.patch.object(upgrade, "_git_head", return_value="abc1234"):
            install = upgrade.detect(env.prefix)
        self.assertEqual(install.editable, src)
        self.assertEqual(install.commit, "abc1234")

    def test_reads_new_metadata_from_disk(self):
        """After an upgrade the running process must see the new commit."""
        env = FakeEnv(self.root, "uv", {"url": "u", "vcs_info": {"commit_id": COMMIT}})
        before = upgrade.detect(env.prefix).revision
        (upgrade._dist_info(env.prefix) / "direct_url.json").write_text(
            json.dumps({"url": "u", "vcs_info": {"commit_id": "f" * 40}}), encoding="utf-8")
        self.assertNotEqual(upgrade.detect(env.prefix).revision, before)

    def test_unmanaged_environment(self):
        install = upgrade.detect(self.root / "nothing-here")
        self.assertIsNone(install.manager)
        self.assertEqual(install.version, "unknown")


class PlanTest(UpgradeTestCase):
    def test_uv_upgrades_the_running_tool_dir(self):
        env = FakeEnv(self.root, "uv", {"url": "u", "vcs_info": {"commit_id": COMMIT}})
        [step] = upgrade.plan(upgrade.detect(env.prefix))
        self.assertEqual(step.argv, ["/bin/uv", "tool", "upgrade", "agy-swap"])
        self.assertEqual(step.env, {"UV_TOOL_DIR": str(env.prefix.parent),
                                    "UV_TOOL_BIN_DIR": str(Path("C:/bin"))})
        self.assertEqual(step.replaces, [Path("C:/bin/agyswap.exe"),
                                         Path("C:\\bin\\agyswap-statusline.exe")])
        self.assertIn("--reinstall", step.retry_hint)

    def test_pipx_upgrades_its_own_home(self):
        env = FakeEnv(self.root, "pipx", {"url": "u", "vcs_info": {"commit_id": COMMIT}})
        with mock.patch.dict(os.environ, {"PIPX_BIN_DIR": str(self.root / "bin")}):
            [step] = upgrade.plan(upgrade.detect(env.prefix))
        self.assertEqual(step.argv, ["/bin/pipx", "upgrade", "agy-swap"])
        self.assertEqual(step.env, {"PIPX_HOME": str(env.prefix.parent.parent)})
        self.assertEqual(step.replaces[0], self.root / "bin" / "agyswap")

    def test_editable_git_checkout_pulls_first(self):
        src = self.root / "src"
        (src / ".git").mkdir(parents=True)
        env = FakeEnv(self.root, "uv", {"url": src.as_uri(), "dir_info": {"editable": True}})
        with mock.patch.object(upgrade, "_git_head", return_value=None):
            pull, sync = upgrade.plan(upgrade.detect(env.prefix))
        self.assertEqual(pull.argv, ["/bin/git", "-C", str(src), "pull", "--ff-only"])
        self.assertEqual(sync.argv[1:], ["tool", "upgrade", "agy-swap"])

    def test_unknown_installer_has_no_plan(self):
        self.assertEqual(upgrade.plan(upgrade.detect(self.root / "nothing")), [])

    def test_missing_uv_is_an_error(self):
        env = FakeEnv(self.root, "uv", {"url": "u", "vcs_info": {"commit_id": COMMIT}})
        with mock.patch("agyswap.upgrade.shutil.which", return_value=None):
            with self.assertRaises(upgrade.UpgradeError):
                upgrade.plan(upgrade.detect(env.prefix))


class HandOffTest(unittest.TestCase):
    step = upgrade.Step(["uv"], replaces=[Path("C:/bin/agyswap.exe")])

    def test_running_launcher_needs_hand_off_on_windows(self):
        with mock.patch.object(sys, "platform", "win32"):
            # uv's launcher reports itself without the .exe suffix.
            self.assertTrue(upgrade.must_hand_off(self.step, "C:/bin/agyswap"))
            self.assertTrue(upgrade.must_hand_off(self.step, "C:/BIN/agyswap.exe"))

    def test_other_entry_points_run_inline(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertFalse(upgrade.must_hand_off(self.step, "C:/src/agyswap/__main__.py"))
            self.assertFalse(upgrade.must_hand_off(upgrade.Step(["git"]), "C:/bin/agyswap"))

    def test_posix_never_hands_off(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(upgrade.must_hand_off(self.step, "C:/bin/agyswap"))

    @unittest.skipUnless(sys.platform == "win32", "the helper waits with Win32 APIs")
    def test_helper_waits_for_the_launcher_then_runs_the_step(self):
        tmp = Path(tempfile.mkdtemp(prefix="agyswap-handoff-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        marker = tmp / "ran.txt"
        launcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
        started = time.monotonic()
        step = upgrade.Step(
            [sys.executable, "-c",
             f"import os, time; open({str(marker)!r}, 'w').write(os.environ['AGYSWAP_T'] + ' ' + str(time.monotonic()))"],
            {"AGYSWAP_T": "ok"},
        )
        upgrade.hand_off(step, wait_pid=launcher.pid)
        launcher.wait()
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(marker.exists(), "helper never ran the step")
        self.assertGreaterEqual(time.monotonic() - started, 2)
        self.assertTrue(marker.read_text(encoding="utf-8").startswith("ok "))

    @unittest.skipUnless(sys.platform == "win32", "the helper waits with Win32 APIs")
    def test_uv_sharing_error_is_recovered_only_for_matching_launchers(self):
        with tempfile.TemporaryDirectory(prefix="agyswap-handoff-") as directory:
            root = Path(directory)
            install = FakeEnv(root, "uv", version="0.4.0")
            dist = upgrade._dist_info(install.prefix)
            (dist / "entry_points.txt").write_text(
                "[console_scripts]\nagyswap = agyswap.cli:main\n"
                "agyswap-statusline = agyswap.statusline:main\n",
                encoding="utf-8",
            )
            scripts = install.prefix / "Scripts"
            scripts.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            launchers = [bin_dir / name for name in ("agyswap.exe", "agyswap-statusline.exe")]
            for launcher in launchers:
                content = launcher.name.encode("ascii")
                (scripts / launcher.name).write_bytes(content)
                launcher.write_bytes(content)

            progress = "Updated agy-swap v0.3.0 -> v0.4.0"
            error = "error: Failed to upgrade agy-swap"
            sharing = "  Caused by: Failed to install entrypoint (os error 32)"

            def run_helper(launchers, cause):
                output = "\n".join((progress, error, cause))
                step = upgrade.Step(
                    [sys.executable, "-c", f"import sys; print({output!r}, file=sys.stderr); sys.exit(1)"],
                    replaces=launchers,
                    uv_prefix=install.prefix,
                )
                with mock.patch.object(upgrade.subprocess, "Popen") as spawn:
                    upgrade.hand_off(step, wait_pid=0)
                return subprocess.run(spawn.call_args.args[0], capture_output=True, text=True)

            result = run_helper(launchers, sharing)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr.splitlines(), [
                progress, "agy-swap: verified the existing Windows launchers.",
            ])
            self.assertIn("upgrade finished", result.stdout)

            for paths, cause in ((launchers[:1], sharing), (launchers, "Network error")):
                result = run_helper(paths, cause)
                self.assertEqual(result.returncode, 1)
                self.assertIn(cause, result.stderr)

            launchers[1].write_bytes(b"stale")
            result = run_helper(launchers, sharing)
            self.assertEqual(result.returncode, 1)
            self.assertIn(sharing, result.stderr)


class CommandTest(unittest.TestCase):
    def _run(self, install, steps, *argv):
        from agyswap import cli

        with mock.patch.object(upgrade, "detect", return_value=install), \
                mock.patch.object(upgrade, "plan", return_value=steps), \
                mock.patch.object(upgrade, "run", return_value=0) as run, \
                mock.patch.object(upgrade, "must_hand_off", return_value=False), \
                mock.patch.object(cli.console, "print") as printed, \
                mock.patch.object(sys, "argv", ["agyswap", "upgrade", *argv]), \
                mock.patch.object(cli, "AgySwitcher"):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        text = "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        return code, run, text

    def test_dry_run_prints_without_running(self):
        install = upgrade.Install(prefix=Path("p"), manager="uv")
        code, run, text = self._run(install, [upgrade.Step(["uv", "tool", "upgrade", "agy-swap"])],
                                    "--dry-run")
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("uv tool upgrade agy-swap", text)

    def test_unknown_installer_exits_nonzero_with_instructions(self):
        code, run, text = self._run(upgrade.Install(prefix=Path("p")), [])
        self.assertEqual(code, 1)
        self.assertIn("uv tool install", text)

    def test_pinned_install_is_explained_before_upgrading(self):
        install = upgrade.Install(prefix=Path("p"), manager="uv", pinned="v0.2.0")
        _code, _run, text = self._run(install, [upgrade.Step(["uv"])], "--dry-run")
        self.assertIn("v0.2.0", text)


if __name__ == "__main__":
    unittest.main()
