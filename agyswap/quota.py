"""Quota cache, normalisation and display.

There is exactly **one** working source of quota, and it is agy itself.

agy pushes a JSON blob to its configured status-line command ~3x/second.  That
blob carries the real, windowed quota::

    "quota": {
      "gemini-5h":     {"remaining_fraction": 1.0,    "reset_time": "..."},
      "gemini-weekly": {"remaining_fraction": 0.9698, "reset_time": "..."},
      "3p-5h":         {"remaining_fraction": 1.0,    "reset_time": "..."},
      "3p-weekly":     {"remaining_fraction": 1.0,    "reset_time": "..."}
    }

Window names vary across agy releases (``five_hour``/``seven_day`` showed up on
some builds), so the mapper keeps whatever keys the payload actually contains
instead of assuming a fixed set.  Labels and ordering are cosmetic only.

Why not the quota API?  ``cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota``
answers **403 PERMISSION_DENIED — "You do not have a valid license of this
product"** for Antigravity accounts, while ``loadCodeAssist`` on the same host
accepts the very same token and reports::

    ineligibleTiers: [{reasonCode: "UNSUPPORTED_CLIENT",
      "This client is no longer supported for Gemini Code Assist for
       individuals... migrate to the Antigravity suite of products"}]

So it is a product boundary, not an expiry or header problem.  Probing it only
burned requests and mislabelled healthy accounts as "expired", so it is gone.

Consequence: quota is known for the account that is currently logged in and
being used, and is retained (with an age stamp) once you switch away.  An
account that has never been used since installing agy-swap simply has no samples
yet, and says so.

Schema 2 on disk::

    {
      "schema": 2,
      "accounts": {
        "<email>": {
          "windows": {"gemini-weekly": {"used_pct": 3.0, "resets_at": "..."}},
          "captured_at": 1761640000,
          "sources": {"statusline": 1761640000}
        }
      }
    }
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from agyswap import credentials, paths

SCHEMA = 2

#: Short, bar-friendly labels.  Unknown keys fall back to :func:`_compact_key`.
WINDOW_LABELS = {
    "gemini-5h": "gemini 5h",
    "gemini-weekly": "gemini 7d",
    # Argon (the compute-heavy Gemini 4 family) carries its own weekly budget.
    "gemini-argon-wtu-weekly": "argon 7d",
    "3p-5h": "3p 5h",
    "3p-weekly": "3p 7d",
    # older agy releases
    "five_hour": "5h",
    "seven_day": "7d",
}
WINDOW_ORDER = (
    "gemini-5h",
    "gemini-weekly",
    "gemini-argon-wtu-weekly",
    "3p-5h",
    "3p-weekly",
    "five_hour",
    "seven_day",
)

#: Longest label the card will spend on a bar prefix.
MAX_LABEL_LEN = 14

#: Samples older than this are dimmed in the UI.
STALE_OK_S = 180
#: Skip identical status-line samples written more often than this.
CAPTURE_THROTTLE_S = 10.0


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _num(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


#: Trailing window markers -> short suffix.
_WINDOW_SUFFIXES = (
    ("-weekly", "7d"),
    ("-5h", "5h"),
    ("-daily", "1d"),
    ("-hourly", "1h"),
)


def _compact_key(key: str) -> str:
    """Turn an unknown window key into something that fits a bar prefix.

    agy adds windows as plans change -- ``gemini-argon-wtu-weekly`` appeared with
    Gemini 4 Argon -- and a raw 23-character key would eat the whole row.  Known
    keys have hand-written labels; everything else gets a mechanical shortening
    so a future window degrades instead of wrecking the layout.
    """
    text = key.replace("_", "-").lower()
    suffix = ""
    for token, short in _WINDOW_SUFFIXES:
        if text.endswith(token):
            text, suffix = text[: -len(token)], short
            break
    text = text.removeprefix("gemini-").replace("-wtu", "")
    label = f"{text} {suffix}".strip() if suffix else text
    return label[:MAX_LABEL_LEN]


def window_label(key: str) -> str:
    return WINDOW_LABELS.get(key) or _compact_key(key)


def _read_json(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# source mapper
# ---------------------------------------------------------------------------


def map_statusline(data: dict) -> dict:
    """Normalise agy's status-line payload into ``{"windows": {...}}``.

    Handles the current ``quota`` object (agy 1.4+) and the older
    ``rate_limits`` shape, so one code path survives agy upgrades.
    """
    windows: dict[str, dict] = {}

    quota = data.get("quota")
    if isinstance(quota, dict):
        for key, spec in quota.items():
            if not isinstance(spec, dict):
                continue
            remaining = _num(spec.get("remaining_fraction"))
            if remaining is None:
                continue
            windows[key] = {
                "used_pct": round((1.0 - remaining) * 100, 1),
                "resets_at": spec.get("reset_time") or "",
            }

    rate = data.get("rate_limits")
    if isinstance(rate, dict):
        for key in ("five_hour", "seven_day"):
            spec = rate.get(key)
            if not isinstance(spec, dict):
                continue
            used = _num(spec.get("used_percentage"))
            if used is None:
                remaining = _num(spec.get("remaining_fraction"))
                if remaining is None:
                    continue
                used = (1.0 - remaining) * 100.0
            windows.setdefault(
                key,
                {"used_pct": round(used, 1), "resets_at": spec.get("resets_at") or ""},
            )

    return {"windows": windows}


# ---------------------------------------------------------------------------
# cache persistence
# ---------------------------------------------------------------------------


def load_cache() -> dict:
    raw = _read_json(paths.QUOTA_CACHE)
    if raw.get("schema") == SCHEMA:
        if not isinstance(raw.get("accounts"), dict):
            raw["accounts"] = {}
        return raw
    return _migrate_legacy(raw)


def _migrate_legacy(raw: dict) -> dict:
    """Upgrade a v1 cache (flat ``five_hour``/``seven_day`` keys)."""
    cache: dict = {"schema": SCHEMA, "accounts": {}}
    accounts = raw.get("accounts")
    if not isinstance(accounts, dict):
        return cache
    for email, entry in accounts.items():
        if not isinstance(entry, dict):
            continue
        migrated: dict = {"sources": {}}
        windows: dict = {}
        for key, resets_key in (
            ("five_hour", "five_resets_at"),
            ("seven_day", "seven_resets_at"),
        ):
            used = _num(entry.get(key))
            if used is None:
                continue
            windows[key] = {"used_pct": used, "resets_at": entry.get(resets_key) or ""}
        if windows:
            migrated["windows"] = windows
        captured = _num(entry.get("captured_at"))
        if captured is not None:
            migrated["captured_at"] = captured
            migrated["sources"] = {"statusline": captured}
        cache["accounts"][email] = migrated
    return cache


def save_cache(cache: dict) -> bool:
    """Persist the cache, skipping the write when nothing changed."""
    cache["schema"] = SCHEMA
    serialized = json.dumps(cache, indent=2, sort_keys=True)
    try:
        if paths.QUOTA_CACHE.exists():
            if paths.QUOTA_CACHE.read_text(encoding="utf-8").strip() == serialized.strip():
                return False
    except OSError:
        pass
    credentials.atomic_write(paths.QUOTA_CACHE, serialized, secure=False)
    return True


# ---------------------------------------------------------------------------
# display model consumed by the UI
# ---------------------------------------------------------------------------


def display_windows(entry: dict) -> list[dict]:
    """Ordered windows for rendering."""
    windows = entry.get("windows")
    if not isinstance(windows, dict) or not windows:
        return []
    ordered = [key for key in WINDOW_ORDER if key in windows]
    ordered += sorted(key for key in windows if key not in WINDOW_ORDER)
    rows: list[dict] = []
    for key in ordered:
        spec = windows.get(key)
        if not isinstance(spec, dict):
            continue
        used = _num(spec.get("used_pct"))
        if used is None:
            continue
        rows.append(
            {
                "key": key,
                "label": window_label(key),
                "used_pct": used,
                "resets_at": spec.get("resets_at") or "",
            }
        )
    return rows


def describe(email: str, cache: Optional[dict] = None) -> dict:
    """Everything the UI needs about one account's quota."""
    if cache is None:
        cache = load_cache()
    accounts = cache.get("accounts") or {}
    entry = accounts.get(email)
    if not isinstance(entry, dict):
        entry = {}
    rows = display_windows(entry)
    return {
        "windows": rows,
        "captured_at": _num(entry.get("captured_at")),
        "status": "ok" if rows else "unknown",
        "detail": "" if rows else "no samples yet — run agy with this account",
    }


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def statusline_capture(data: dict) -> Optional[str]:
    """Record a status-line payload.  Returns the email it was filed under."""
    if not isinstance(data, dict):
        return None
    mapped = map_statusline(data)
    if not mapped["windows"]:
        return None

    email = data.get("email")
    if not isinstance(email, str) or "@" not in email:
        token, _ = credentials.read_token()
        email = credentials.email_from_token(token) if token else None
    if not isinstance(email, str) or "@" not in email:
        return None

    cache = load_cache()
    accounts = cache.setdefault("accounts", {})
    entry = accounts.get(email)
    if not isinstance(entry, dict):
        entry = {}
    accounts[email] = entry

    now = time.time()
    existing = entry.get("windows") if isinstance(entry.get("windows"), dict) else {}
    last_seen = _num((entry.get("sources") or {}).get("statusline")) or 0.0
    if existing == mapped["windows"] and now - last_seen < CAPTURE_THROTTLE_S:
        return email

    entry["windows"] = {**existing, **mapped["windows"]}
    entry["captured_at"] = now
    entry.setdefault("sources", {})["statusline"] = now
    save_cache(cache)
    return email


__all__ = [
    "SCHEMA",
    "STALE_OK_S",
    "statusline_capture",
    "describe",
    "display_windows",
    "map_statusline",
    "load_cache",
    "save_cache",
]
