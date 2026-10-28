"""Test package.

Importing this module sets up the throwaway home **before** anything imports
``agyswap.paths``, because that module resolves every location at import time.

This ordering is not cosmetic: unittest discovers modules alphabetically, so a
test file that imports ``agyswap`` without bootstrapping first would freeze the
paths to the developer's real ``~/.gemini`` and ``~/.agy-swap`` -- and the suite
would then happily overwrite real credentials.  ``assert_isolated()`` makes that
failure loud instead of destructive.
"""

from tests import bootstrap  # noqa: F401  - sets AGY_GEMINI_DIR / AGYSWAP_DATA_DIR

bootstrap.assert_isolated()
