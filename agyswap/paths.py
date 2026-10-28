"""Filesystem locations used by agy-swap.

Every root is overridable through an environment variable.  That keeps the test
suite hermetic and lets users with a non-standard ``HOME`` point the tool at the
right place without patching source.

Layout of the *agy* side (v1.4 and newer)::

    ~/.gemini/jetski-standalone-oauth-token   <- current OAuth token
    ~/.gemini/google_accounts.json            <- legacy, no longer maintained

Layout of agy-swap's own data::

    ~/.agy-swap/sequence.json
    ~/.agy-swap/credentials/.creds-<n>-<email>.enc
    ~/.agy-swap/cache/quota.json
"""

from __future__ import annotations

import os
from pathlib import Path


def _root_from_env(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else default


#: agy's configuration root.
GEMINI_DIR = _root_from_env("AGY_GEMINI_DIR", Path.home() / ".gemini")
#: agy-swap's own data root.
BACKUP_ROOT = _root_from_env("AGYSWAP_DATA_DIR", Path.home() / ".agy-swap")

#: Per-version state directory inside agy's root.
AGY_CLI_DIR = GEMINI_DIR / "antigravity-cli"

#: Where agy >= 1.4 keeps its OAuth token (verified against agy.exe 1.4.3).
TOKEN_PATH = GEMINI_DIR / "jetski-standalone-oauth-token"
#: Where older agy builds kept it.  Read as a fallback, written only if present.
LEGACY_TOKEN_PATH = AGY_CLI_DIR / "antigravity-oauth-token"
#: Probe order for reading; all existing entries are rewritten on switch.
TOKEN_CANDIDATES = (TOKEN_PATH, LEGACY_TOKEN_PATH)

#: Legacy account bookkeeping.  agy 1.4.3 no longer maintains it, but older
#: builds did, so it is still read as a last-resort hint and written (with
#: ``old`` preserved) for backwards compatibility.
GOOGLE_ACCOUNTS_PATH = GEMINI_DIR / "google_accounts.json"
#: Another legacy email source used by older builds.
OAUTH_CREDS_PATH = GEMINI_DIR / "oauth_creds.json"

#: agy-swap data files.
SEQUENCE_FILE = BACKUP_ROOT / "sequence.json"
CREDENTIALS_DIR = BACKUP_ROOT / "credentials"
CACHE_DIR = BACKUP_ROOT / "cache"
QUOTA_CACHE = CACHE_DIR / "quota.json"

#: go-keyring naming used by agy for its single active-token slot.
KEYRING_SERVICE = "gemini"
KEYRING_ACCOUNT = "antigravity"
KEYRING_TARGET = f"{KEYRING_SERVICE}:{KEYRING_ACCOUNT}"
#: Prefix 99designs/keyring (used by agy) applies on macOS and Linux.
KEYRING_PREFIX = "go-keyring-base64:"
