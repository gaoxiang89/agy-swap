"""Filesystem locations used by agy-swap.

Every root is overridable through an environment variable.  That keeps the test
suite hermetic and lets users with a non-standard ``HOME`` point the tool at the
right place without patching source.

Layout of the *agy* side::

    OS keyring (service "gemini", account "antigravity")  <- primary store
    ~/.gemini/antigravity-cli/antigravity-oauth-token      <- file store agy
                                                              uses when the
                                                              keyring is out of
                                                              reach (SSH,
                                                              containers)
    ~/.gemini/jetski-standalone-oauth-token                <- older builds and
                                                              third-party tools
    ~/.gemini/google_accounts.json                         <- legacy, no longer
                                                              maintained

Layout of agy-swap's own data::

    ~/.agy-swap/sequence.json
    ~/.agy-swap/credentials/.creds-<n>-<email>.enc
    ~/.agy-swap/ssh-token.json                 <- mirror restored over SSH
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

#: File-based token store agy falls back to when the OS keyring is unreachable.
#: agy 1.5.x switches to it whenever it detects an SSH session, and it is the
#: path that has to be populated for an SSH login to work.  Verified against
#: agy 1.5.12 by writing a token to one candidate at a time.
SSH_TOKEN_PATH = AGY_CLI_DIR / "antigravity-oauth-token"
#: Older name for :data:`SSH_TOKEN_PATH`, kept so existing callers keep working.
LEGACY_TOKEN_PATH = SSH_TOKEN_PATH

#: Root-level token file written by some builds and third-party tools.  agy
#: 1.5.x does not read it over SSH, but it is still kept in sync.
TOKEN_PATH = GEMINI_DIR / "jetski-standalone-oauth-token"

#: Probe order for reading; every entry is rewritten on switch/sync.
TOKEN_CANDIDATES = (SSH_TOKEN_PATH, TOKEN_PATH)

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
#: Copy of the active token agy-swap can restore into the file store without
#: touching the keyring, which a Windows SSH logon cannot reach.
SSH_TOKEN_MIRROR = BACKUP_ROOT / "ssh-token.json"

#: go-keyring naming used by agy for its single active-token slot.
KEYRING_SERVICE = "gemini"
KEYRING_ACCOUNT = "antigravity"
KEYRING_TARGET = f"{KEYRING_SERVICE}:{KEYRING_ACCOUNT}"
#: Prefix 99designs/keyring (used by agy) applies on macOS and Linux.
KEYRING_PREFIX = "go-keyring-base64:"
