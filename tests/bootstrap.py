"""Test bootstrap.

Paths are resolved from environment variables at import time, so the whole
suite runs against a throwaway directory instead of the developer's real
``~/.gemini`` and ``~/.agy-swap``.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="agyswap-tests-"))
os.environ["AGYSWAP_DATA_DIR"] = str(ROOT / "data")
os.environ["AGY_GEMINI_DIR"] = str(ROOT / "gemini")

#: Do not litter /tmp (or %TEMP%) with one directory per test run.
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)


def reset() -> None:
    """Give each test a pristine home."""
    for name in ("data", "gemini"):
        target = ROOT / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)


def assert_isolated() -> None:
    """Abort the run if agyswap.paths resolved outside the sandbox.

    ``agyswap.paths`` freezes its constants at import time.  Whoever imports it
    first wins, and if that happened before :mod:`tests.bootstrap` ran, the
    constants point at the real home -- a suite that reads and writes the
    developer's actual credentials.  Failing here costs one test run; not
    failing cost a real token file once already.
    """
    from agyswap import paths  # imported late, on purpose

    root = ROOT.resolve()
    for name in ("GEMINI_DIR", "BACKUP_ROOT"):
        resolved = Path(getattr(paths, name)).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise RuntimeError(
                f"test isolation broken: paths.{name} = {resolved} is outside the "
                f"sandbox {root}.  Import tests.bootstrap before agyswap, or the "
                f"suite will touch real credentials."
            ) from None
