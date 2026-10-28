"""Credential storage: the OS keyring plus a file fallback, on every platform.

agy stores a single "active token" in the OS keyring, under the target
``gemini:antigravity``, and mirrors it to a JSON file.  Which mechanism wins
depends on the platform, so this module abstracts all of them behind one API:

===========  ===========================================================
Windows      Credential Manager via ``advapi32`` (ctypes, no dependency)
macOS        ``security`` (Keychain), value tagged ``go-keyring-base64:``
Linux        ``secret-tool`` (libsecret / Secret Service), same tagging
any          JSON file under ``~/.gemini/`` as the final fallback
===========  ===========================================================

The file fallback is what makes the tool work even when no keyring daemon is
available (containers, WSL without a session bus, minimal Linux installs).

Design notes
------------
* Reading probes *all* sources and keeps the token with the **latest expiry**,
  so a stale Keychain entry can never shadow a fresh file written by agy.
* Writing fans out to the keyring and to every token file that already exists,
  keeping them consistent.  If no file exists the canonical one is created.
* Every operation is best-effort: a failure on one backend never stops another
  and never raises.  ``agy-swap doctor`` reports what actually worked.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from agyswap import paths
from agyswap.timeutil import parse_iso_ts

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


# ---------------------------------------------------------------------------
# token helpers
# ---------------------------------------------------------------------------


def looks_like_token(text: Optional[str]) -> bool:
    """Cheap guard: is this a plausible agy OAuth token payload?"""
    if not text or "access_token" not in text:
        return False
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict)


def token_json(text: Optional[str]) -> dict:
    """Return the token mapping, unwrapping agy's ``{"token": {...}}`` envelope."""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    inner = parsed.get("token")
    return inner if isinstance(inner, dict) else parsed


def access_token(text: Optional[str]) -> Optional[str]:
    value = token_json(text).get("access_token")
    return value if isinstance(value, str) and value else None


def refresh_token(text: Optional[str]) -> Optional[str]:
    value = token_json(text).get("refresh_token")
    return value if isinstance(value, str) and value else None


def token_expiry(text: Optional[str]) -> Optional[float]:
    """Epoch seconds at which the access token expires, if known."""
    data = token_json(text)
    for key in ("expiry", "expires_at", "expiresAt", "expiration"):
        ts = parse_iso_ts(data.get(key))
        if ts is not None:
            return ts
    return None


def token_is_expired(text: Optional[str], *, skew: float = 60.0) -> bool:
    """True when the token is expired or expires within ``skew`` seconds.

    A token with no expiry field is treated as *not* expired: absence of
    evidence is not evidence of expiry, and probing will reveal the truth.
    """
    expiry = token_expiry(text)
    if expiry is None:
        return False
    return expiry - skew <= time.time()


def _jwt_claims(jwt: Optional[str]) -> dict:
    if not isinstance(jwt, str) or jwt.count(".") < 2:
        return {}
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _email_from_userinfo(access: str) -> Optional[str]:
    """Online fallback used only when no ``id_token`` is present."""
    import urllib.request

    try:
        request = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v1/userinfo?alt=json",
            headers={"Authorization": f"Bearer {access}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8")).get("email") or None
    except Exception:
        return None


def email_from_token(text: Optional[str], *, allow_network: bool = True) -> Optional[str]:
    """Derive the account email from a token without trusting any local hint.

    Two shapes carry the identity, depending on where agy wrote it:

    * the file keeps the signed ``id_token`` JWT (verified against agy 1.4.x),
    * the Credential Manager entry keeps only ``id_token_email``, a plain
      string, with no JWT at all.

    Both are offline sources, which is what makes account identity reliable on a
    machine where ``google_accounts.json`` is stale or missing.
    """
    if not text:
        return None
    data = token_json(text)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    candidates = [parsed.get("id_token"), data.get("id_token")]
    for jwt in candidates:
        email = _jwt_claims(jwt).get("email")
        if isinstance(email, str) and email:
            return email

    for source in (parsed, data):
        for key in ("id_token_email", "email", "account", "user_email"):
            value = source.get(key)
            if isinstance(value, str) and "@" in value:
                return value.strip()

    access = access_token(text)
    if access and allow_network:
        return _email_from_userinfo(access)
    return None


# ---------------------------------------------------------------------------
# atomic writes + permission hardening
# ---------------------------------------------------------------------------


def _replace_with_retry(source: str, target: Path, attempts: int = 6) -> None:
    """``os.replace`` that survives Windows sharing violations.

    On Windows a rename fails with ``PermissionError`` while another process
    (agy, an antivirus, a backup agent) holds a handle to the destination.
    A short bounded retry turns a hard failure into a hiccup.
    """
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            os.replace(source, str(target))
            return
        except PermissionError as exc:  # pragma: no cover - Windows only
            last_error = exc
            time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error


def secure_file(path: Path) -> None:
    """Restrict a file to the current user, best effort, per platform.

    On POSIX this is ``chmod 0600``.  On Windows ``chmod`` only toggles the
    read-only bit, so the ACL is rewritten with ``icacls`` instead.  Failure is
    never fatal: the caller may be on a filesystem without ACL support.
    """
    try:
        if not IS_WINDOWS:
            os.chmod(path, 0o600)
            return
        user = os.environ.get("USERNAME") or getpass.getuser()
        if not user:
            return
        subprocess.run(
            [
                "icacls", str(path),
                "/inheritance:r",
                "/grant:r", f"{user}:F",
                "/grant:r", "*S-1-5-18:F",        # SYSTEM
                "/grant:r", "*S-1-5-32-544:F",    # Administrators
            ],
            capture_output=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def secure_dir(path: Path) -> None:
    """Restrict a directory to the current user, best effort, per platform."""
    try:
        if not IS_WINDOWS:
            os.chmod(path, 0o700)
            return
        secure_file(path)
    except Exception:
        pass


def atomic_write(
    path: Path,
    content: str,
    *,
    mode: int = 0o600,
    secure: bool = False,
) -> None:
    """Write ``content`` to ``path`` atomically, never leaving a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        fd = -1
        _replace_with_retry(tmp, path)
        if secure:
            secure_file(path)
        elif not IS_WINDOWS:
            os.chmod(path, mode)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# credential backups managed by agy-swap itself
# ---------------------------------------------------------------------------


def backup_path(number: int, email: str) -> Path:
    safe_email = email.replace("@", "_at_").replace(".", "_dot_")
    return paths.CREDENTIALS_DIR / f".creds-{number}-{safe_email}.enc"


def save_token_backup(number: int, email: str, content: str) -> None:
    """Persist one account's token.

    Still base64 rather than encryption: the file name is historical.  What we
    *can* guarantee cross-platform is an atomic 0600/ACL-restricted write.
    """
    paths.CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    atomic_write(backup_path(number, email), encoded, secure=True)


def read_token_backup(number: int, email: str) -> Optional[str]:
    path = backup_path(number, email)
    if not path.exists():
        return None
    try:
        decoded = base64.b64decode(path.read_text(encoding="utf-8").strip(), validate=True)
        text = decoded.decode("utf-8")
    except Exception:
        return None
    return text if looks_like_token(text) else None


def delete_token_backup(number: int, email: str) -> None:
    try:
        backup_path(number, email).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Windows Credential Manager (advapi32 via ctypes -- no third-party package)
# ---------------------------------------------------------------------------

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


if IS_WINDOWS:  # pragma: no cover - exercised only on Windows
    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


def _advapi32():
    return ctypes.WinDLL("advapi32", use_last_error=True)


def _windows_keyring_read() -> Optional[str]:  # pragma: no cover - Windows only
    if not IS_WINDOWS:
        return None
    try:
        advapi = _advapi32()
        advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        advapi.CredFree.restype = None

        handle = ctypes.POINTER(_CREDENTIALW)()
        if not advapi.CredReadW(
            paths.KEYRING_TARGET, _CRED_TYPE_GENERIC, 0, ctypes.byref(handle)
        ):
            return None
        try:
            cred = handle.contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        finally:
            advapi.CredFree(handle)
        for encoding in ("utf-8", "utf-16-le"):
            try:
                text = blob.decode(encoding)
            except UnicodeDecodeError:
                continue
            if looks_like_token(text.strip()):
                return text.strip()
        return None
    except Exception:
        return None


def _windows_keyring_write(content: str) -> bool:  # pragma: no cover - Windows only
    if not IS_WINDOWS:
        return False
    buffer = None
    try:
        advapi = _advapi32()
        advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        advapi.CredWriteW.restype = wintypes.BOOL

        encoded = content.encode("utf-8")
        buffer = ctypes.create_string_buffer(encoded, len(encoded))
        cred = _CREDENTIALW()
        cred.Flags = 0
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = paths.KEYRING_TARGET
        cred.Comment = None
        cred.CredentialBlobSize = len(encoded)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = paths.KEYRING_ACCOUNT
        return bool(advapi.CredWriteW(ctypes.byref(cred), 0))
    except Exception:
        return False
    finally:
        del buffer


# ---------------------------------------------------------------------------
# macOS Keychain
# ---------------------------------------------------------------------------


def _macos_keyring_read() -> Optional[str]:  # pragma: no cover - macOS only
    if not IS_MACOS:
        return None
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", paths.KEYRING_SERVICE,
                "-a", paths.KEYRING_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()
    except Exception:
        return None


def _macos_keyring_write(content: str) -> bool:  # pragma: no cover - macOS only
    if not IS_MACOS:
        return False
    try:
        result = subprocess.run(
            [
                "security", "add-generic-password",
                "-s", paths.KEYRING_SERVICE,
                "-a", paths.KEYRING_ACCOUNT,
                "-w", content,
                "-U",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Linux Secret Service via secret-tool
# ---------------------------------------------------------------------------

_LINUX_ATTRIBUTE_SETS = (
    ("service", paths.KEYRING_SERVICE, "username", paths.KEYRING_ACCOUNT),
    ("service", paths.KEYRING_SERVICE, "user", paths.KEYRING_ACCOUNT),
    ("service", paths.KEYRING_SERVICE, "application", paths.KEYRING_ACCOUNT),
)


def _linux_secret_tool() -> Optional[str]:
    if not IS_LINUX:
        return None
    return shutil.which("secret-tool")


def _linux_keyring_read() -> Optional[str]:  # pragma: no cover - Linux only
    executable = _linux_secret_tool()
    if not executable:
        return None
    for attributes in _LINUX_ATTRIBUTE_SETS:
        try:
            result = subprocess.run(
                [executable, "lookup", *attributes],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.rstrip("\n")
    return None


def _linux_keyring_write(content: str) -> bool:  # pragma: no cover - Linux only
    executable = _linux_secret_tool()
    if not executable:
        return False
    for attributes in _LINUX_ATTRIBUTE_SETS:
        try:
            result = subprocess.run(
                [
                    executable, "store",
                    f"--label={paths.KEYRING_TARGET}",
                    *attributes,
                ],
                input=content,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue
        if result.returncode == 0:
            return True
    return False


# ---------------------------------------------------------------------------
# cross-platform keyring facade, with transparent go-keyring-base64 handling
# ---------------------------------------------------------------------------

#: What the last successful read looked like, so writes round-trip unchanged.
_KEYRING_WRAPPED: Optional[bool] = None
#: True once a credential has actually been read back out of the OS store.
_KEYRING_SEEN = False


def _keyring_disabled() -> bool:
    """``AGYSWAP_DISABLE_KEYRING=1`` forces the file-only path.

    Useful in containers, on machines with a broken Secret Service session, and
    for tests that must not touch the developer's real credential store.
    """
    return os.environ.get("AGYSWAP_DISABLE_KEYRING", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _keyring_backend() -> str:
    if _keyring_disabled():
        return "disabled"
    if IS_WINDOWS:
        return "wincred"
    if IS_MACOS:
        return "keychain"
    if IS_LINUX:
        return "secret-tool" if _linux_secret_tool() else "none"
    return "none"


def _unwrap_secret(raw: str) -> str:
    """Strip 99designs/keyring's ``go-keyring-base64:`` encoding if present."""
    if raw.startswith(paths.KEYRING_PREFIX):
        encoded = raw[len(paths.KEYRING_PREFIX):]
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except Exception:
            return raw
    return raw


def _wrap_secret(content: str, wrapped: bool) -> str:
    if not wrapped:
        return content
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    return paths.KEYRING_PREFIX + encoded


def read_keyring() -> Optional[str]:
    """Read the active token from the OS keyring, unwrapped to raw JSON."""
    global _KEYRING_WRAPPED, _KEYRING_SEEN
    if _keyring_disabled():
        return None
    try:
        if IS_WINDOWS:
            raw = _windows_keyring_read()
        elif IS_MACOS:
            raw = _macos_keyring_read()
        elif IS_LINUX:
            raw = _linux_keyring_read()
        else:
            raw = None
    except Exception:
        raw = None
    if raw is None:
        return None
    _KEYRING_WRAPPED = raw.startswith(paths.KEYRING_PREFIX)
    unwrapped = _unwrap_secret(raw)
    if looks_like_token(unwrapped):
        _KEYRING_SEEN = True
        return unwrapped
    return None


def write_keyring(content: str) -> bool:
    """Store ``content`` in the OS keyring, preserving the backend's encoding.

    On Linux the entry is only *updated*, never created: agy itself talks to
    WinCred on Windows and to its token file elsewhere, and its binary carries
    no Secret Service strings at all.  Creating entries it never reads would
    just leave a staler copy of the token lying around.  If a Secret Service
    entry does exist (because agy was wired to one), it is kept in sync.
    """
    if _keyring_disabled():
        return False
    if IS_LINUX and not _KEYRING_SEEN:
        return False
    wrapped = _KEYRING_WRAPPED
    if wrapped is None:
        # macOS has historically carried the go-keyring base64 tag; the value
        # observed in WinCred is raw JSON.
        wrapped = IS_MACOS
    payload = _wrap_secret(content, wrapped)
    try:
        if IS_WINDOWS:
            return _windows_keyring_write(payload)
        if IS_MACOS:
            return _macos_keyring_write(payload)
        if IS_LINUX:
            return _linux_keyring_write(payload)
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# public token API
# ---------------------------------------------------------------------------


def _file_candidates() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in paths.TOKEN_CANDIDATES:
        try:
            if path.is_file():
                found.append((path.read_text(encoding="utf-8"), path))
        except OSError:
            continue
    return found


def read_token() -> tuple[Optional[str], str]:
    """Return ``(token_json, source)`` choosing the freshest available copy.

    Freshness is judged by the token's own ``expiry`` claim first and file
    modification time second -- no wall-clock guessing about which store agy
    wrote last.
    """
    scored: list[tuple[tuple[float, float], str, str]] = []

    ring = read_keyring()
    if ring:
        scored.append(
            ((token_expiry(ring) or 0.0, 0.0), ring, f"keyring:{_keyring_backend()}")
        )

    for text, path in _file_candidates():
        if not looks_like_token(text):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        scored.append(((token_expiry(text) or 0.0, mtime), text, str(path)))

    if not scored:
        return None, "none"
    scored.sort(key=lambda item: item[0])
    _, text, source = scored[-1]
    return text, source


def write_token(content: str) -> list[str]:
    """Write ``content`` everywhere agy might look.  Returns what succeeded."""
    written: list[str] = []
    if write_keyring(content):
        written.append(f"keyring:{_keyring_backend()}")

    existing = [path for path in paths.TOKEN_CANDIDATES if path.exists()]
    targets = existing or [paths.TOKEN_PATH]
    for path in targets:
        try:
            atomic_write(path, content)
            written.append(str(path))
        except Exception:
            continue
    return written


def keyring_status() -> dict:
    """Diagnostics for ``agy-swap doctor``."""
    backend = _keyring_backend()
    available = False
    if backend == "secret-tool":
        available = _linux_secret_tool() is not None
    elif backend in ("wincred", "keychain"):
        available = True
    return {"backend": backend, "available": available}


def _token_email_offline(text: Optional[str]) -> Optional[str]:
    return _jwt_claims(token_json(text).get("id_token")).get("email") or None


def token_sources_disagree() -> Optional[str]:
    """Describe a keyring/file mismatch, or ``None`` when they agree.

    agy writes both stores, but not always in the same instant: a machine that
    switched accounts can end up with the keyring holding one account and the
    file another.  Switching through agy-swap rewrites both, which resyncs them.
    """
    ring = read_keyring()
    if not ring:
        return None
    ring_email = _token_email_offline(ring)
    if not ring_email:
        return None

    for path in paths.TOKEN_CANDIDATES:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not looks_like_token(text):
            continue
        file_email = _token_email_offline(text)
        if not file_email or file_email == ring_email:
            continue
        keyring_is_newer = (token_expiry(ring) or 0.0) >= (token_expiry(text) or 0.0)
        winner = "keyring" if keyring_is_newer else path.name
        return (
            f"keyring holds {ring_email}, {path.name} holds {file_email}; "
            f"using the newer one ({winner}) — 'agyswap switch' resyncs both"
        )
    return None
