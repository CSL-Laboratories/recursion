"""Untrusted engine text to inert display text (CSL-799, candidate; fixtures UI-10, RJ-F21).

Engine output is data. ``OutputSanitizer`` is a streaming parser: ESC, CSI, OSC (including OSC 52
clipboard and OSC 0/2 titles), DCS, SOS, PM, APC, their C1 forms, other C0/C1 controls, bidi controls,
and other invisible characters never reach the terminal. Each is replaced by a visible marker such as
``⟦OSC 52⟧`` so nothing is hidden either. Invisible characters are every Unicode format (``Cf``),
private-use (``Co``) and unassigned (``Cn``) character, plus variation selectors and the blank Hangul
fillers, which are not in those categories but render as nothing. Parser state carries across chunks,
so splitting the same bytes at any point gives the same text. A control string longer than ``MAX_SEQ``
is cut, marked ``overlong`` and the parser returns to text, so a missing terminator cannot swallow
later output.
"""

from __future__ import annotations

import codecs
import unicodedata

MAX_SEQ = 4096

# Not Cf/Co/Cn, but blank or invisible. U+034F and U+FE0F are the code points measured in the
# t1435u review; the rest of each variation-selector block is the same mechanism. U+115F and
# U+3164 are the blank Hangul fillers (t1435u and bot review 4503921323).
_EXTRA = frozenset("\u034f\u115f\u3164")
_STRING_INTRO = {"]": "OSC", "P": "DCS", "X": "SOS", "^": "PM", "_": "APC"}
_C1_STRING = {"\x9d": "OSC", "\x90": "DCS", "\x98": "SOS", "\x9e": "PM", "\x9f": "APC"}
_NAMED = {"\x07": "BEL", "\x08": "BS", "\x7f": "DEL", "\x0b": "VT", "\x0c": "FF", "\x18": "CAN", "\x1a": "SUB"}

_GROUND, _ESC, _ESC_INT, _CSI, _STR, _STR_ESC = range(6)


def _marker(name: str) -> str:
    return f"⟦{name}⟧"


def _marked(ch: str) -> bool:
    """True when ``ch`` must become a marker instead of reaching the terminal.

    Newlines, tabs, CR and ESC are marked too; ``_ground`` still gives them their own handling.
    """

    o = ord(ch)
    if o < 0x20 or o == 0x7F or 0x80 <= o <= 0x9F:
        return True
    if 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
        return True
    if o < 0x80:
        return False
    if ch in _EXTRA:
        return True
    return unicodedata.category(ch) in ("Cf", "Co", "Cn")


def _next_marked(text: str, start: int) -> int:
    n = len(text)
    i = start
    while i < n:
        if _marked(text[i]):
            return i
        i += 1
    return n


def _char_marker(ch: str) -> str:
    o = ord(ch)
    if ch in _NAMED:
        return _marker(_NAMED[ch])
    if o < 0x20:
        return _marker("^" + chr(o + 0x40))
    if 0x80 <= o <= 0x9f:
        return _marker(f"C1 {o:02X}")
    return _marker(f"U+{o:04X}")


class OutputSanitizer:
    """Feed str or bytes chunks; get back text holding only printable characters and ``\\n``."""

    def __init__(self) -> None:
        self._state, self._kind, self._buf, self._len = _GROUND, "", "", 0
        self._cr = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, chunk: str | bytes | None) -> str:
        # A missing or non-text chunk is empty display text. It must not raise: the caller is the
        # terminal loop, and one bad engine event would otherwise stop keys and Stop.
        if isinstance(chunk, (bytes, bytearray)):
            text = self._decoder.decode(chunk)
        elif isinstance(chunk, str):
            text = chunk
        elif chunk is None:
            text = ""
        else:
            text = str(chunk)
        out: list[str] = []
        i, n = 0, len(text)
        while i < n:
            if self._state == _GROUND and not self._cr:
                end = _next_marked(text, i)
                if end > i:  # fast path: plain text is copied as is
                    out.append(text[i:end])
                    i = end
                    continue
            self._step(text[i], out)
            i += 1
        return "".join(out)

    def flush(self) -> str:
        """End of stream: an unfinished sequence or a trailing CR is shown, not dropped."""

        out: list[str] = []
        if self._cr:
            out.append(_marker("CR"))
            self._cr = False
        if self._state != _GROUND:
            self._end(out, "unterminated")
        return "".join(out)

    # -- state machine --------------------------------------------------------------------------
    def _step(self, ch: str, out: list[str]) -> None:
        if self._cr:
            self._cr = False
            if ch == "\n":
                out.append("\n")
                return
            out.append(_marker("CR"))
        st = self._state
        if st == _GROUND:
            self._ground(ch, out)
        elif st == _ESC:
            self._after_esc(ch, out)
        elif st == _ESC_INT:
            if ch in "\x18\x1a":
                self._end(out, "aborted")
            elif 0x20 <= ord(ch) <= 0x2f:
                self._grow(ch, out)
            else:
                self._end(out)
        elif st == _CSI:
            if ch in "\x18\x1a":
                self._end(out, "aborted")
            elif ch == "\x1b":
                self._end(out, "aborted")
                self._begin(_ESC, "ESC")
            elif 0x40 <= ord(ch) <= 0x7e:
                self._end(out)
            else:
                self._grow(ch, out)
        elif st == _STR:
            if ch == "\x07" and self._kind == "OSC" or ch == "\x9c":
                self._end(out)
            elif ch == "\x1b":
                self._state = _STR_ESC
            elif ch in "\x18\x1a":
                self._end(out, "aborted")
            else:
                self._grow(ch, out)
        else:  # _STR_ESC: ESC \ is ST; any other ESC ends the string and starts a new escape
            if ch == "\\":
                self._end(out)
            else:
                self._end(out, "aborted")
                self._begin(_ESC, "ESC")
                self._after_esc(ch, out)

    def _ground(self, ch: str, out: list[str]) -> None:
        if not _marked(ch):
            out.append(ch)
        elif ch == "\n":
            out.append("\n")
        elif ch == "\t":
            out.append("    ")
        elif ch == "\r":
            self._cr = True
        elif ch == "\x1b":
            self._begin(_ESC, "ESC")
        elif ch == "\x9b":
            self._begin(_CSI, "CSI")
        elif ch in _C1_STRING:
            self._begin(_STR, _C1_STRING[ch])
        else:
            out.append(_char_marker(ch))

    def _after_esc(self, ch: str, out: list[str]) -> None:
        if ch == "[":
            self._begin(_CSI, "CSI")
        elif ch in _STRING_INTRO:
            self._begin(_STR, _STRING_INTRO[ch])
        elif ch == "\x1b":
            self._end(out)
            self._begin(_ESC, "ESC")
        elif ch in "\x18\x1a":
            self._end(out, "aborted")
        elif 0x20 <= ord(ch) <= 0x2f:
            self._state = _ESC_INT
            self._grow(ch, out)
        else:
            self._end(out)

    def _begin(self, state: int, kind: str) -> None:
        self._state, self._kind, self._buf, self._len = state, kind, "", 0

    def _grow(self, ch: str, out: list[str]) -> None:
        self._len += 1
        if len(self._buf) < 8:
            self._buf += ch
        if self._len > MAX_SEQ:
            self._end(out, "overlong")

    def _end(self, out: list[str], note: str = "") -> None:
        name = self._kind
        if name == "OSC":  # name the OSC number (0/2 title, 52 clipboard, 8 hyperlink) so the drop is legible
            num = self._buf.split(";", 1)[0]
            if num.isdigit():
                name = f"OSC {num}"
        out.append(_marker(f"{name} {note}" if note else name))
        self._state, self._kind, self._buf, self._len = _GROUND, "", "", 0


def sanitize(text: str | bytes) -> str:
    """One-shot sanitize of a complete value (an API field, a fixture)."""

    s = OutputSanitizer()
    return s.feed(text) + s.flush()


def field(value: object) -> str:
    """An API-supplied value shown on one row: sanitized, and a newline is shown as a marker."""

    return sanitize(str(value)).replace("\n", _marker("LF"))


def cell_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def wrap(line: str, width: int) -> list[str]:
    """Split one sanitized line into rows of at most ``width`` terminal cells."""

    rows, cur, used = [], [], 0
    for ch in line:
        w = cell_width(ch)
        if used + w > width and cur:
            rows.append("".join(cur))
            cur, used = [], 0
        cur.append(ch)
        used += w
    rows.append("".join(cur))
    return rows


def clip(line: str, width: int) -> str:
    return wrap(line, width)[0] if line else ""
