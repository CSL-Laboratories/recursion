"""Terminal input to key names (lane TUI, slice 1). Toolkit-neutral and clock-free: bytes in, keys out.

``KeyDecoder`` is a stateful parser of what a terminal sends. State carries across reads, so a
sequence split between two reads decodes as if it had come in one. It yields:

- every printable character, in order, as itself (a one-character string);
- named keys: ``enter`` (CR or LF; cbreak mode turns Enter into LF), ``tab``, ``shift+tab``,
  ``backspace`` (DEL or BS), ``esc``, ``up``, ``down``, ``left``, ``right``, ``home``, ``end``,
  ``pageup``, ``pagedown``, ``insert``, ``delete``, ``f1`` to ``f12``, ``ctrl+a`` to ``ctrl+z`` (and
  ``ctrl+space``, ``ctrl+\\``, ``ctrl+]``, ``ctrl+^``, ``ctrl+_``), with xterm's modifier parameter
  as a ``ctrl+``, ``alt+`` and ``shift+`` prefix, in that order (``ESC [1;5C`` is ``ctrl+right``);
- ``alt+<key>`` for ESC followed by a key (``ESC x`` is ``alt+x``);
- one ``Paste`` for a bracketed paste (``ESC [200~`` ... ``ESC [201~``), whatever it holds.

CSI (``ESC [``) and SS3 (``ESC O``) sequences are parsed by their grammar; one that names no key here
(focus reports, device answers, unknown function keys) is dropped, never read as ``esc`` or as text.

A lone ESC cannot be told apart from the start of a sequence until a short wait passes with nothing
after it. The decoder does not keep time: ``waiting`` says that it holds such a start, and the caller
(``term.Tty``) waits ``ESC_WAIT_S`` for more input, then calls ``timeout()``, which makes a lone ESC
``esc``. A bracketed paste is not timed out: it ends at its end marker, or at ``end()``.

Nothing here imports ``os`` or reads a terminal; ``term.py`` does that.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

from recursion_core.tui.sanitize import visible

ESC_WAIT_S = 0.05  # how long a lone ESC waits for the rest of a sequence (Neovim's default ttimeoutlen)
PASTE_CAP = 65_536  # characters of one paste that are kept; the rest are counted in Paste.dropped
MAX_SEQ = 64  # parameter characters of one CSI or SS3 sequence; a longer one is dropped whole

_PASTE_START, _PASTE_END = "200", "\x1b[201~"
_GROUND, _ESC, _CSI, _SS3, _SKIP, _PASTE = range(6)

_CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
              "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
_SS3_FINAL = {**_CSI_FINAL, "M": "enter"}
_TILDE = {"1": "home", "2": "insert", "3": "delete", "4": "end", "5": "pageup", "6": "pagedown", "7": "home",
          "8": "end", "11": "f1", "12": "f2", "13": "f3", "14": "f4", "15": "f5", "17": "f6", "18": "f7",
          "19": "f8", "20": "f9", "21": "f10", "23": "f11", "24": "f12"}
_MODS = {1: "", 2: "shift+", 3: "alt+", 4: "alt+shift+", 5: "ctrl+", 6: "ctrl+shift+", 7: "ctrl+alt+",
         8: "ctrl+alt+shift+"}
_CTRL_PUNCT = {0x00: "ctrl+space", 0x1C: "ctrl+\\", 0x1D: "ctrl+]", 0x1E: "ctrl+^", 0x1F: "ctrl+_"}


@dataclass(frozen=True, slots=True)
class Paste:
    """One bracketed paste: its text as the terminal sent it (``dropped`` characters past ``PASTE_CAP``
    were not kept). Whoever takes it decides what of it to keep; it never acts as keys."""

    text: str
    dropped: int = 0


Key = str | Paste


def _plain(ch: str) -> str | None:
    """One character outside any sequence: its key name, or None when it is not a key."""

    if ch in "\r\n":
        return "enter"
    if ch == "\t":
        return "tab"
    if ch in "\x7f\x08":
        return "backspace"
    o = ord(ch)
    if o in _CTRL_PUNCT:
        return _CTRL_PUNCT[o]
    if o < 0x20:
        return "ctrl+" + chr(o + 0x60)
    return ch if visible(ch) else None  # C1 controls, bidi and invisible characters are not keys


def _alt(name: str) -> str:
    return "ctrl+alt+" + name[5:] if name.startswith("ctrl+") else "alt+" + name


def _modified(base: str | None, mod: str) -> str | None:
    if base is None or not mod.isdigit():
        return None
    prefix = _MODS.get(int(mod))
    return None if prefix is None else prefix + base


def _csi_name(params: str, final: str) -> str | None:
    """``ESC [ params final`` as a key name, or None for a sequence that names no key here."""

    if final == "Z":
        return "shift+tab" if params == "" else None
    parts = params.split(";")
    if final == "~":
        if len(parts) > 2:
            return None
        return _modified(_TILDE.get(parts[0]), parts[1] if len(parts) == 2 else "1")
    if final not in _CSI_FINAL:
        return None
    if params == "":
        return _CSI_FINAL[final]
    if len(parts) != 2 or parts[0] not in ("", "1"):
        return None
    return _modified(_CSI_FINAL[final], parts[1])


class KeyDecoder:
    """Feed bytes (or text) as they are read; get back the keys they finish, in order."""

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._state, self._seq = _GROUND, ""
        self._paste: list[str] = []
        self._paste_len = self._paste_dropped = 0
        self._paste_tail = ""  # the end of the paste so far that may be the start of its end marker

    @property
    def waiting(self) -> bool:
        """It holds an ESC or an unfinished sequence that a short wait may complete."""

        return self._state in (_ESC, _CSI, _SS3, _SKIP)

    @property
    def in_paste(self) -> bool:
        return self._state == _PASTE

    def feed(self, data: bytes | str) -> list[Key]:
        text = self._utf8.decode(data) if isinstance(data, (bytes, bytearray)) else data
        out: list[Key] = []
        i = 0
        while i < len(text):
            if self._state == _PASTE:
                i = self._paste_feed(text, i, out)
            else:
                self._step(text[i], out)
                i += 1
        return out

    def timeout(self) -> list[Key]:
        """The short wait after an ESC ended with nothing more: a lone ESC is ``esc``; ``ESC [`` and
        ``ESC O`` alone are ``alt+[`` and ``alt+O``; a longer unfinished sequence is dropped."""

        out: list[Key] = []
        if self._state == _ESC:
            out.append("esc")
        elif self._state in (_CSI, _SS3) and self._seq == "":
            out.append("alt+[" if self._state == _CSI else "alt+O")
        if self.waiting:
            self._state, self._seq = _GROUND, ""
        return out

    def end(self) -> list[Key]:
        """End of input: what ``timeout`` gives, and an unfinished paste with what arrived of it."""

        out = self.feed(self._utf8.decode(b"", final=True))
        out += self.timeout()
        if self._state == _PASTE:
            self._take(self._paste_tail)
            out.append(self._finish_paste())
        return out

    # -- the state machine ------------------------------------------------------------------------
    def _step(self, ch: str, out: list[Key]) -> None:
        st = self._state
        if st == _GROUND:
            if ch == "\x1b":
                self._state = _ESC
            else:
                key = _plain(ch)
                if key is not None:
                    out.append(key)
        elif st == _ESC:
            if ch == "[":
                self._state, self._seq = _CSI, ""
            elif ch == "O":
                self._state, self._seq = _SS3, ""
            elif ch == "\x1b":  # ESC ESC: the first is a lone Esc; the second may start a sequence
                out.append("esc")
            else:
                self._state = _GROUND
                key = _plain(ch)
                if key is not None:
                    out.append(_alt(key))
        elif st in (_CSI, _SS3, _SKIP):
            o = ord(ch)
            if 0x20 <= o <= 0x3F:  # parameter and intermediate characters
                if st != _SKIP:
                    self._seq += ch
                    if len(self._seq) > MAX_SEQ:
                        self._state, self._seq = _SKIP, ""
            elif 0x40 <= o <= 0x7E:  # the final character
                params, self._state, self._seq = self._seq, _GROUND, ""
                if st == _CSI and params == _PASTE_START and ch == "~":
                    self._state = _PASTE
                elif st == _CSI:
                    self._emit(_csi_name(params, ch), out)
                elif st == _SS3:
                    self._emit(_SS3_FINAL.get(ch) if params == "" else None, out)
            else:  # a control or non-ASCII character ends the sequence unfinished; it is read on its own
                self._state, self._seq = _GROUND, ""
                self._step(ch, out)

    @staticmethod
    def _emit(name: str | None, out: list[Key]) -> None:
        if name is not None:
            out.append(name)

    # -- bracketed paste --------------------------------------------------------------------------
    def _paste_feed(self, text: str, i: int, out: list[Key]) -> int:
        """Take paste text from ``text[i:]``; return the index after what was used."""

        held = self._paste_tail
        buf = held + text[i:]
        k = buf.find(_PASTE_END)
        if k < 0:
            keep = next((n for n in range(min(len(buf), len(_PASTE_END) - 1), 0, -1)
                         if _PASTE_END.startswith(buf[-n:])), 0)
            self._take(buf[:len(buf) - keep])
            self._paste_tail = buf[len(buf) - keep:]
            return len(text)
        self._take(buf[:k])
        self._paste_tail = ""
        out.append(self._finish_paste())
        self._state = _GROUND
        return i + k + len(_PASTE_END) - len(held)

    def _take(self, part: str) -> None:
        room = max(0, PASTE_CAP - self._paste_len)
        kept = part[:room]
        if kept:
            self._paste.append(kept)
            self._paste_len += len(kept)
        self._paste_dropped += len(part) - len(kept)

    def _finish_paste(self) -> Paste:
        paste = Paste("".join(self._paste), self._paste_dropped)
        self._paste, self._paste_len, self._paste_dropped, self._paste_tail = [], 0, 0, ""
        return paste
