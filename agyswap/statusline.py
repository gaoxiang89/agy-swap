#!/usr/bin/env python3
"""Status-line hook for agy, and quota capture in one pass.

Configure it once in ``~/.gemini/settings.json``::

    "statusLine": {
      "type": "",
      "command": "python -m agyswap.statusline",
      "enabled": true
    }

agy pipes a JSON blob to stdin on every status-line refresh (~3x/second).  This
entry point files the quota it contains into agy-swap's cache -- the only
*offline*, no-token-needed source of windowed quota -- and prints a compact
line back.

If you already use your own status-line script, chain it::

    python -m agyswap.statusline --chain "C:/path/to/your-statusline.bat"

The chained script receives the same stdin and its stdout is forwarded
verbatim, so nothing is lost.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Optional

from agyswap import quota
from agyswap.timeutil import format_duration, parse_iso_ts
import time


def _configure_stdio() -> None:
    """Force UTF-8 so Windows' GBK/CP936 console cannot raise on us."""
    for stream, kwargs in (
        (sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
        (sys.stdout, {"encoding": "utf-8", "errors": "replace"}),
    ):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(**kwargs)
        except Exception:
            pass


def _run_chain(command: str, raw: str) -> str:
    """Forward the same stdin to a user script and return its stdout."""
    if not command.strip():
        return ""
    try:
        completed = subprocess.run(
            command,
            input=raw,
            capture_output=True,
            text=True,
            timeout=10,
            shell=True,
        )
    except Exception:
        return ""
    return (completed.stdout or "").rstrip("\n")


def _summary_line(data: dict) -> str:
    """Compact one-liner rendered into agy's status bar.

    Space is scarce: agy truncates long lines.  Every window gets its number,
    but only the most constrained one gets a reset countdown, since the others
    would usually repeat the same figure.
    """
    mapped = quota.map_statusline(data)
    parts: list[str] = []

    now = time.time()
    windows = mapped["windows"]
    ordered = [key for key in quota.WINDOW_ORDER if key in windows]
    ordered += sorted(key for key in windows if key not in ordered)
    if not ordered:
        return ""

    tightest = max(ordered, key=lambda k: windows[k]["used_pct"])
    for key in ordered:
        spec = windows[key]
        text = f"{quota.window_label(key)} {spec['used_pct']:.0f}%"
        resets = parse_iso_ts(spec.get("resets_at"))
        if key == tightest and resets and resets > now:
            text += f" ({format_duration(resets - now)})"
        parts.append(text)

    context = data.get("context_window") or {}
    if isinstance(context, dict):
        used_tokens = context.get("total_input_tokens")
        size = context.get("context_window_size")
        if used_tokens and size:
            parts.append(f"ctx {int(used_tokens) // 1000}k/{int(size) // 1000}k")

    model = data.get("model")
    if isinstance(model, dict):
        name = model.get("display_name") or model.get("id")
        if name:
            parts.insert(0, str(name))

    if not parts:
        return ""
    return "  ".join(parts)


def _agyswap_executable() -> str:
    """Absolute path to the ``agyswap`` launcher, if one is on PATH.

    ``python -m agyswap.statusline`` is not reliable guidance: with ``uv tool``
    or ``pipx`` installs the module lives in an isolated environment, so the
    ``python`` that agy happens to spawn cannot import it.
    """
    import shutil

    found = shutil.which("agyswap")
    if found:
        return found
    return "agyswap"


def wrapper_path() -> "Path":
    """Where the single-word launcher lives: next to the agyswap executable."""
    from pathlib import Path

    executable = Path(_agyswap_executable())
    if executable.is_absolute():
        return executable.parent / ("agy-statusline.cmd" if sys.platform == "win32"
                                    else "agy-statusline")
    return Path.home() / ".local" / "bin" / "agy-statusline"


def write_wrapper(chain: str) -> "Path":
    """Write a launcher that needs no shell quoting in agy's settings.

    agy may spawn the status-line command through a shell or directly; a single
    bare word with no arguments sidesteps the question entirely, which matters
    because a quoted Windows path would otherwise be passed through verbatim.
    """
    from pathlib import Path

    executable = _agyswap_executable()
    target = wrapper_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    chain_part = f' --chain "{chain}"' if chain else ""
    if sys.platform == "win32":
        text = f'@echo off\r\n"{executable}" statusline{chain_part}\r\n'
    else:
        text = f'#!/bin/sh\nexec "{executable}" statusline{chain_part}\n'

    target.write_text(text, encoding="utf-8", newline="")
    if sys.platform != "win32":
        target.chmod(0o755)
    return target


def main(argv: Optional[list[str]] = None) -> int:
    _configure_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)

    chain = ""
    if "--chain" in argv:
        index = argv.index("--chain")
        if index + 1 < len(argv):
            chain = argv[index + 1]

    if "--install" in argv:
        target = write_wrapper(chain)
        print(f"wrote {target}")
        return 0

    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    if not raw.strip():
        return 0

    # Run the chained script first and unconditionally: if our own parsing ever
    # fails, the user's status line must still appear. Losing someone's tok/s
    # readout because agy changed its payload shape would be a bad trade.
    forwarded = _run_chain(chain, raw) if chain else ""

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        if forwarded:
            print(forwarded, flush=True)
        return 0
    if not isinstance(data, dict):
        if forwarded:
            print(forwarded, flush=True)
        return 0

    try:
        quota.statusline_capture(data)
    except Exception:
        pass

    if forwarded:
        print(forwarded, flush=True)
        return 0

    line = _summary_line(data)
    if line:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
