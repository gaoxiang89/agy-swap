from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import ListItem, Static

from agyswap.timeutil import format_duration, parse_iso_ts
from agyswap.tui.theme import (
    ACCENT,
    FOREGROUND,
    MUTED,
    SEV_WARN,
    TRACK,
    severity_color,
)

if TYPE_CHECKING:
    from agyswap.tui.app import AgySwapApp

_BAR_FILLED = "━"
_BAR_HALF = "╸"
_BAR_EMPTY = "─"
_BAR_TICK = "┃"

#: After this long without a fresh sample the bars are dimmed.
STALE_OK_S = 180

__all__ = [
    "AccountsPanel",
    "AccountCard",
    "AccountItem",
    "MenuItem",
    "account_card_text",
    "bar_cells",
    "format_duration",
    "reset_clock",
    "reset_text",
    "usage_bar",
    "usage_rows",
]


def reset_text(resets_at: str | None, now: float) -> str | None:
    remaining = parse_iso_ts(resets_at)
    if remaining is None:
        return None
    delta = remaining - now
    if delta <= 0:
        return "resets now"
    return f"resets {format_duration(delta)}"


def reset_clock(resets_at: str | None, now: float) -> str | None:
    timestamp = parse_iso_ts(resets_at)
    if timestamp is None or timestamp - now <= 0:
        return None
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%H:%M")


def bar_cells(
    pct: float | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
) -> Text:
    text = Text()
    if pct is None:
        text.append(_BAR_EMPTY * width, style=TRACK)
        return text
    fraction = min(max(pct, 0.0), 100.0) / 100.0
    cells = fraction * width
    full = int(cells)
    half = (cells - full) >= 0.5 and full < width
    tick_at: int | None = None
    if threshold is not None:
        tick_at = min(width - 1, max(0, round(threshold / 100.0 * width)))
    color = severity_color(pct)
    fill_style = f"{color} dim" if stale else color
    for index in range(width):
        if tick_at is not None and index == tick_at:
            text.append(_BAR_TICK, style=SEV_WARN)
        elif index < full:
            text.append(_BAR_FILLED, style=fill_style)
        elif index == full and half:
            text.append(_BAR_HALF, style=fill_style)
        else:
            text.append(_BAR_EMPTY, style=TRACK)
    return text


def usage_bar(
    label: str,
    pct: float | None,
    suffix: str | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
) -> Text:
    text = Text()
    text.append(f"{label} ", style=MUTED)
    text.append(bar_cells(pct, width, stale=stale, threshold=threshold))
    if pct is None:
        text.append("  —", style=MUTED)
    else:
        color = severity_color(pct)
        text.append(f" {pct:3.0f}%", style=f"{color} dim" if stale else color)
    if suffix:
        text.append(f"  {suffix}", style=MUTED)
    return text


def usage_rows(acc: dict, now: float) -> list[tuple[str, float, str, str]]:
    """Rows of ``(label, used_pct, short_suffix, long_suffix)``."""
    rows: list[tuple[str, float, str, str]] = []
    for window in acc.get("quota_windows") or ():
        pct = window.get("used_pct")
        if pct is None:
            continue
        label = str(window.get("label") or window.get("key") or "?")
        reset = reset_text(window.get("resets_at"), now)
        clock = reset_clock(window.get("resets_at"), now) if reset else None
        short = reset or ""
        long = f"{reset} · {clock}" if (reset and clock) else short
        rows.append((label, float(pct), short, long))
    return rows


def _quota_placeholder(acc: dict) -> str:
    status = acc.get("quota_status") or ""
    detail = (acc.get("quota_detail") or "").strip()
    if detail:
        return detail
    if status == "expired":
        return "token expired — run agy to refresh"
    if status == "untokened":
        return "no stored token"
    return "quota unavailable"


def account_card_text(
    acc: dict,
    width: int,
    *,
    threshold: float | None = None,
    now: float | None = None,
) -> Text:
    now = now if now is not None else time.time()
    text = Text()
    text.append(f"{acc['number']:>2}  ", style=f"bold {FOREGROUND}")
    if acc.get("alias"):
        text.append(acc["alias"], style=f"bold {ACCENT}")
        text.append(f" ({acc['email']})", style=FOREGROUND)
    else:
        text.append(acc["email"], style=FOREGROUND)
    if acc.get("active"):
        text.append("   ● active", style=f"bold {ACCENT}")
    if acc.get("disabled"):
        text.append("   (disabled)", style=MUTED)

    fetched_at = acc.get("quota_fetched_at")
    if fetched_at:
        age = now - float(fetched_at)
        if age > 0:
            text.append(f"   {format_duration(age)} ago", style=MUTED)
    if acc.get("quota_status") == "expired":
        text.append("   (expired)", style=MUTED)

    rows = usage_rows(acc, now)
    if not rows:
        text.append("\n    ")
        text.append(_quota_placeholder(acc), style=MUTED)
        return text

    stale = bool(fetched_at) and (now - float(fetched_at)) > STALE_OK_S
    label_width = max(len(label) for label, _pct, _short, _long in rows)
    bar_width = max(12, min(30, width - 42 - label_width))
    row_overhead = 4 + label_width + 1 + bar_width + 5 + 2
    for label, pct, short, long in rows:
        suffix = long if (long != short and row_overhead + len(long) <= width) else short
        text.append("\n    ")
        text.append(
            usage_bar(
                f"{label:<{label_width}}",
                pct,
                suffix or None,
                bar_width,
                stale=stale,
                threshold=threshold,
            )
        )
    return text


class AccountsPanel(Static):
    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", lambda _: self.refresh(layout=True))

    def render(self) -> Text:
        app: AgySwapApp = self.app  # type: ignore[assignment]
        snap = getattr(app, "snapshot", None)
        if snap is None:
            return Text("loading…", style=MUTED)
        if not snap:
            return Text(
                "No managed accounts yet.\nUse the menu below to add one.",
                style=MUTED,
            )
        now = time.time()
        width = (self.size.width or 80) - 2
        blocks: list[Text] = [
            account_card_text(row, width, threshold=app.threshold_pct, now=now)
            for row in snap
        ]
        text = Text()
        previous_multiline = False
        for index, block in enumerate(blocks):
            multiline = "\n" in block.plain
            if index:
                text.append("\n\n" if (multiline or previous_multiline) else "\n")
            text.append(block)
            previous_multiline = multiline
        return text


class AccountCard(Static):
    def __init__(self, acc: dict, *, threshold: float | None = None) -> None:
        super().__init__()
        self._acc = acc
        self._threshold = threshold

    def set_account(self, acc: dict) -> None:
        self._acc = acc
        self.refresh(layout=True)

    def render(self) -> Text:
        return account_card_text(
            self._acc, self.size.width or 80, threshold=self._threshold
        )


class AccountItem(ListItem):
    def __init__(self, acc: dict) -> None:
        super().__init__(AccountCard(acc))
        self.number = acc["number"]
        self.email = acc["email"]

    def set_account(self, acc: dict) -> None:
        self.number = acc["number"]
        self.email = acc["email"]
        self.query_one(AccountCard).set_account(acc)


class MenuItem(ListItem):
    def __init__(self, label: str, action_id: str, *, muted: bool = False) -> None:
        style = MUTED if muted else FOREGROUND
        super().__init__(Static(Text(label, style=style)))
        self.action_id = action_id
