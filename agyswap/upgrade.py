"""Self-upgrade: find out how agy-swap was installed and let that tool do it.

agy-swap is installed from git rather than PyPI, so only the installer knows
where "newer" lives: uv's tool receipt, pipx's metadata, or the git checkout
behind an editable install.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

DIST = "agy-swap"
REPO_URL = "https://github.com/gaoxiang89/agy-swap"


class UpgradeError(Exception):
    pass


@dataclass
class Install:
    prefix: Path
    #: "uv", "pipx", or None when neither manages this environment.
    manager: str | None = None
    #: Source tree of an editable install.
    editable: Path | None = None
    version: str = "unknown"
    commit: str | None = None
    #: Tag/branch/commit the install was pinned to (``git+URL@rev``).
    pinned: str | None = None

    @property
    def label(self) -> str:
        parts = []
        if self.manager:
            parts.append({"uv": "uv tool", "pipx": "pipx"}[self.manager])
        if self.editable:
            parts.append(f"editable checkout {self.editable}")
        return " · ".join(parts) or "unmanaged environment"

    @property
    def revision(self) -> str:
        return f"{self.version} ({self.commit})" if self.commit else self.version


@dataclass
class Step:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    #: Launcher executables this step rewrites.
    replaces: list[Path] = field(default_factory=list)
    #: What to tell the user when the step fails.
    retry_hint: str = ""
    uv_prefix: Path | None = None


def _dist_info(prefix: Path) -> Path | None:
    site = Path(sysconfig.get_path("purelib", vars={"base": str(prefix), "platbase": str(prefix)}))
    # Read straight from disk rather than importlib.metadata: after an upgrade
    # this process must see the *new* metadata, not what it imported at start.
    found = sorted(site.glob("agy_swap-*.dist-info"))
    return found[-1] if found else None


def _metadata_version(dist_info: Path) -> str:
    try:
        text = (dist_info / "METADATA").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"^Version:\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _direct_url(dist_info: Path) -> dict:
    try:
        data = json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_url_path(url: str) -> Path | None:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    return Path(url2pathname(parsed.path))


def _git_head(tree: Path) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        result = subprocess.run(
            [git, "-C", str(tree), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def detect(prefix: Path | None = None) -> Install:
    """Describe the environment this process runs from."""
    prefix = Path(prefix if prefix is not None else sys.prefix)
    install = Install(prefix=prefix)
    if (prefix / "uv-receipt.toml").is_file():
        install.manager = "uv"
    elif (prefix / "pipx_metadata.json").is_file():
        install.manager = "pipx"

    dist_info = _dist_info(prefix)
    if dist_info is None:
        return install
    install.version = _metadata_version(dist_info)

    direct = _direct_url(dist_info)
    if (direct.get("dir_info") or {}).get("editable"):
        install.editable = _file_url_path(direct.get("url", ""))
        if install.editable is not None:
            install.commit = _git_head(install.editable)
    vcs = direct.get("vcs_info") or {}
    if vcs.get("commit_id"):
        install.commit = vcs["commit_id"][:7]
    install.pinned = vcs.get("requested_revision") or None
    return install


def entrypoints(install: Install) -> list[Path]:
    """The launcher executables the package manager installed for us."""
    if install.manager == "uv":
        try:
            receipt = (install.prefix / "uv-receipt.toml").read_text(encoding="utf-8")
        except OSError:
            return []
        found = re.findall(r'install-path\s*=\s*"([^"]+)"', receipt)
        return [Path(path.replace("\\\\", "\\")) for path in found]
    if install.manager == "pipx":
        try:
            meta = json.loads((install.prefix / "pipx_metadata.json").read_text(encoding="utf-8"))
            apps = meta["main_package"]["apps"]
        except (OSError, ValueError, KeyError, TypeError):
            return []
        bin_dir = Path(os.environ.get("PIPX_BIN_DIR") or Path.home() / ".local" / "bin")
        return [bin_dir / app for app in apps]
    return []


_RETRY = (
    "If another agyswap is still running (a dashboard, or agy's status line), "
    "close it, then run: {}"
)


def plan(install: Install) -> list[Step]:
    """Commands that bring *install* up to date, in order; empty if unknown."""
    steps: list[Step] = []
    if install.editable is not None and (install.editable / ".git").exists():
        git = shutil.which("git")
        if not git:
            raise UpgradeError(f"git is not on PATH; cannot update {install.editable}")
        # --ff-only: never merge into, or rewrite, a developer's checkout.
        steps.append(Step(
            [git, "-C", str(install.editable), "pull", "--ff-only"],
            retry_hint=f"Bring {install.editable} up to date yourself, then run "
                       "'agyswap upgrade' again.",
        ))

    if install.manager == "uv":
        uv = shutil.which("uv")
        if not uv:
            raise UpgradeError("agy-swap was installed by uv, but uv is not on PATH")
        # Pin uv to the environment we are running from. uv writes launchers
        # to the *current* bin dir, not the receipt's install-path, so a
        # differing UV_TOOL_BIN_DIR would land them somewhere else entirely.
        launchers = entrypoints(install)
        env = {"UV_TOOL_DIR": str(install.prefix.parent)}
        bin_dirs = {os.path.normcase(str(path.parent)) for path in launchers}
        if len(bin_dirs) == 1:
            env["UV_TOOL_BIN_DIR"] = str(launchers[0].parent)
        steps.append(Step([uv, "tool", "upgrade", DIST], env, launchers,
                          _RETRY.format(f"uv tool upgrade --reinstall {DIST}"),
                          uv_prefix=install.prefix))
    elif install.manager == "pipx":
        pipx = shutil.which("pipx")
        if not pipx:
            raise UpgradeError("agy-swap was installed by pipx, but pipx is not on PATH")
        steps.append(Step([pipx, "upgrade", DIST],
                          {"PIPX_HOME": str(install.prefix.parent.parent)},
                          entrypoints(install), _RETRY.format(f"pipx reinstall {DIST}")))
    return steps


def display(step: Step) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(step.argv)
    import shlex

    return shlex.join(step.argv)


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def must_hand_off(step: Step, argv0: str | None = None) -> bool:
    """True when *step* would overwrite the launcher running this process.

    Windows will not let anyone replace, delete or even rename the running
    ``agyswap.exe`` (uv's launcher holds its own file open), so uv fails with
    os error 32 after it has already upgraded the environment
    (astral-sh/uv#11930).
    """
    if sys.platform != "win32" or not step.replaces:
        return False
    me = Path(argv0 if argv0 is not None else sys.argv[0])
    candidates = [me] if me.suffix else [me, me.with_name(me.name + ".exe")]
    return any(_same_file(c, path) for c in candidates for path in step.replaces)


_HELPER = r"""
import ctypes, filecmp, json, os, subprocess, sys
from importlib.metadata import Distribution
from pathlib import Path


def uv_launchers_match(prefix, launchers):
    prefix = Path(prefix)
    site_packages = prefix / "Lib" / "site-packages"
    installed = next(Distribution.discover(name="agy-swap", path=[site_packages]), None)
    if installed is None:
        return False
    expected = {entry.name.casefold() + ".exe" for entry in installed.entry_points
                if entry.group == "console_scripts"}
    paths = [Path(path) for path in launchers]
    if not expected or not expected.issubset(path.name.casefold() for path in paths):
        return False
    try:
        return all(filecmp.cmp(prefix / "Scripts" / path.name, path, shallow=False)
                   for path in paths)
    except OSError:
        return False


job = json.loads(sys.argv[1])
kernel32 = ctypes.windll.kernel32
handle = kernel32.OpenProcess(0x00100000, False, job["wait_pid"])  # SYNCHRONIZE
if handle:
    kernel32.WaitForSingleObject(handle, 60000)
    kernel32.CloseHandle(handle)
env = dict(os.environ)
env.update(job["env"])
print(flush=True)
try:
    if job["uv_prefix"]:
        result = subprocess.run(job["argv"], env=env, stderr=subprocess.PIPE)
        code = result.returncode
        output = result.stderr
        progress, error, detail = output.partition(b"error: Failed to upgrade agy-swap")
        if (code and error and b"Updated agy-swap " in progress
                and b"Failed to install entrypoint" in detail
                and b"(os error 32)" in detail
                and uv_launchers_match(job["uv_prefix"], job["launchers"])):
            output = progress + b"agy-swap: verified the existing Windows launchers.\n"
            code = 0
        sys.stderr.buffer.write(output)
        sys.stderr.flush()
    else:
        code = subprocess.call(job["argv"], env=env)
except OSError as exc:
    print(exc)
    code = 1
print("agy-swap: upgrade finished." if code == 0 else job["failed"], flush=True)
sys.exit(code)
"""


def hand_off(step: Step, wait_pid: int | None = None) -> None:
    """Run *step* from a helper once the launcher (our parent) has exited."""
    job = {
        "wait_pid": os.getppid() if wait_pid is None else wait_pid,
        "argv": step.argv,
        "env": step.env,
        "failed": f"agy-swap: upgrade failed. {step.retry_hint}",
        "uv_prefix": str(step.uv_prefix) if step.uv_prefix else None,
        "launchers": [str(path) for path in step.replaces],
    }
    # The base interpreter, not the tool environment's: the helper must import
    # nothing from the files it is about to replace.
    python = getattr(sys, "_base_executable", None) or sys.executable
    try:
        subprocess.Popen([python, "-c", _HELPER, json.dumps(job)], close_fds=True)
    except OSError as exc:
        raise UpgradeError(f"could not start the upgrade helper: {exc}") from exc


def run(step: Step) -> int:
    env = dict(os.environ)
    env.update(step.env)
    try:
        return subprocess.call(step.argv, env=env)
    except OSError as exc:
        raise UpgradeError(f"could not run {step.argv[0]}: {exc}") from exc
