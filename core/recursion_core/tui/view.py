"""Thin view layer: a frame is a list of (text, style) rows (CSL-799, candidate).

The app builds frames from sanitized text only; this module turns a frame into plain text (tests,
screen readers), into one full-screen ANSI repaint, or into inline paints that keep scrollback.
Colour is decoration on top of text labels, never the only signal, and is off when ``NO_COLOR`` is set
to a non-empty value (https://no-color.org). Swapping the toolkit later means replacing this module and ``term.py`` only.
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


def _row(text: str, style: str | None, color: bool) -> str:
    code = _SGR.get(style or "") if color else None
    return f"\x1b[{code}m{text}\x1b[0m\x1b[K" if code else f"{text}\x1b[K"


def to_ansi(frame: list[Row], color: bool) -> str:
    """Alternate screen: home the cursor and repaint every row. Rows are already sanitized and clipped."""

    return "\x1b[H" + "\r\n".join(_row(text, style, color) for text, style in frame) + "\x1b[J"


class InlinePainter:
    """``--no-alt-screen``: the normal buffer keeps its scrollback. Unlike ``to_ansi`` this never homes
    the cursor or clears above its own live region. Each finished line is written once, above the live
    region, and scrolls away as plain text; only the few live rows at the bottom are redrawn."""

    def __init__(self, color: bool) -> None:
        self.color = color
        self.live_height = 0

    def _erase_live(self) -> str:
        if not self.live_height:
            return ""
        up = f"\x1b[{self.live_height - 1}A" if self.live_height > 1 else ""
        self.live_height = 0
        return f"\r{up}\x1b[J"  # back to the first live row, clear from there down

    def paint(self, finished: list[str], live: list[Row]) -> str:
        """Rows in ``live`` must already be clipped to less than the terminal width (no autowrap)."""

        out = self._erase_live() + "".join(f"{line}\r\n" for line in finished)
        out += "\r\n".join(_row(text, style, self.color) for text, style in live)
        self.live_height = len(live)
        return out

    def end(self, finished: list[str]) -> str:
        """The last finished lines, then no live region, so the shell prompt follows the output."""

        return self._erase_live() + "".join(f"{line}\r\n" for line in finished)
