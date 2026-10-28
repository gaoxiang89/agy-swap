"""Small time helpers shared by the quota, credential and UI layers."""

from __future__ import annotations

import datetime
import re
from typing import Any

#: Anything from 1 to 9 fractional-second digits, as Go/.NET like to emit.
_FRACTION_SECONDS = re.compile(r"\.(\d{1,9})")

_EXTRA_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _clamp_fraction(match: re.Match) -> str:
    """Trim/pad fractional seconds to exactly 6 digits (microseconds)."""
    return "." + (match.group(1) + "000000")[:6]


def now_iso() -> str:
    """Current UTC time in the format agy-swap stores in its own files."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_ts(value: Any) -> float | None:
    """Parse an ISO-8601 / .NET timestamp into epoch seconds.

    Accepts the shapes actually seen in the wild: ``2026-11-03T02:09:32Z``,
    ``...+00:00``, naive strings (assumed UTC), ``2026/11/3 2:09:32`` and agy's
    .NET-style ``2026-10-28T18:02:28.3629063+08:00``.

    That last one matters: Python 3.10's ``fromisoformat`` only accepts 3 or 6
    fractional digits, so a raw 7-digit value silently became ``None`` -- which
    made an expired token look fresh.  Fractional seconds are normalised first.

    Returns ``None`` instead of raising so callers can degrade gracefully.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    normalized = _FRACTION_SECONDS.sub(_clamp_fraction, normalized, count=1)
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in _EXTRA_FORMATS:
            try:
                parsed = datetime.datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def iso_from_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def format_duration(seconds: float) -> str:
    """Compact human duration: ``45s``, ``12m``, ``3h 20m``, ``6d 4h``."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(total // 3600, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"
