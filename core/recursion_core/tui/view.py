"""Thin view layer: a frame is a list of (text, style) rows (CSL-799, candidate).

The app builds frames from sanitized text only; this module turns a frame into plain text (tests,
screen readers) or into one ANSI repaint for a real terminal. Colour is decoration on top of text
labels, never the only signal, and is off when ``NO_COLOR`` is set to a non-empty value
(https://no-color.org). Swapping the toolkit later means replacing this module and ``term.py`` only.
"""

from __future__ import annotations

from collections.abc import Mapping

Row = tuple[str, str | None]

_SGR = {"header": "1", "REFUSED": "1;31", "UNAVAILABLE": "1;31", "UNCERTAIN": "1;33", "STALE": "1;35",
        "UNRECONCILED": "1;33", "error": "1;31", "selected": "7", "prompt": "1"}


def color_enabled(environ: Mapping[str, str]) -> bool:
    return not environ.get("NO_COLOR") and environ.get("TERM") != "dumb"


def to_text(frame: list[Row]) -> str:
    return "\n".join(text.rstrip() for text, _ in frame)


def to_ansi(frame: list[Row], color: bool) -> str:
    """Home the cursor and repaint every row. Rows are already sanitized and clipped to width."""

    parts = ["\x1b[H"]
    for i, (text, style) in enumerate(frame):
        if i:
            parts.append("\r\n")
        code = _SGR.get(style or "") if color else None
        parts.append(f"\x1b[{code}m{text}\x1b[0m\x1b[K" if code else f"{text}\x1b[K")
    parts.append("\x1b[J")
    return "".join(parts)
