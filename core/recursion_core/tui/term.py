"""Terminal driver (CSL-799, candidate). The only TUI module that touches the real terminal.

``main`` takes an injected ``api_factory`` like the CLI's ``run``; the TUI owns no transport. Modes:

- default on a terminal: full-screen frames on the alternate screen.
- ``--no-alt-screen``: inline mode in the normal buffer, preserving terminal scrollback history.
  Finished output is appended once as plain lines, so it lands in scrollback and can be copied; only a
  small live region at the bottom (status, conditions, error, input) is redrawn. Keys work as usual.
- ``--plain`` (or ``--screen-reader``), ``TERM=dumb``, or stdin/stdout not a terminal: the linear
  renderer. It reads one CLI-grammar command per line and only appends text lines (no redraw, no cursor
  movement), so no error is redrawn away.

``--no-color`` turns colour off like ``NO_COLOR`` does. Usage errors and a missing API client go to
stderr; stdout carries only the screen or the line-mode transcript.

All three read the app's one event log. Ctrl-C or ``q`` closes the TUI; that sends nothing and stops
no run. So does the end of input (a terminal that hung up): the TUI closes and exits 0, never spins; the
EIO that a hung-up terminal gives on restoring its mode or on the last writes is the expected end, and
any other error is raised.

The terminal is put in raw input mode, with output processing left as it was: no echo, no line
editing, no input translation (CR, LF, parity, stripping, break), and no control character acts by
itself (no signal from Ctrl-C, Ctrl-Z or Ctrl-\\, no Ctrl-S flow control, no Ctrl-V literal), so every
byte of a paste reaches the decoder as sent. Ctrl-C arrives as the key ``ctrl+c``, which ends the TUI as
before. A hangup is the EIO a hung-up terminal gives, after the end of input was read or when the
terminal reports POLLHUP (a ``q`` already read, then the hangup).

Keys come from ``keys.KeyDecoder``: every character of a read, in order, CSI and SS3 keys by name, a
lone Esc after ``keys.ESC_WAIT_S``, and a bracketed paste (turned on while the TUI runs) as one
``keys.Paste``. A key is marked ``burst`` when it came in one read with other input, or with part of
another key (an unfinished UTF-8 character or sequence). That is a heuristic for an unbracketed paste:
input that arrives one key per read cannot be told from typing. The app does not let a burst key, nor a
Stop armed or a command line typed with one, confirm the Stop or send the line on the next key.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import os
import select
import shutil
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from typing import TextIO

from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.events import speech_lines
from recursion_core.tui.keys import ESC_WAIT_S, Key, KeyDecoder
from recursion_core.tui.view import InlinePainter, to_ansi

PASTE_ON, PASTE_OFF = "\x1b[?2004h", "\x1b[?2004l"  # bracketed paste mode (xterm), off again on every exit
READ_BYTES = 4096
FLAGS = {"--plain": "plain", "--screen-reader": "plain", "--no-alt-screen": "no_alt_screen", "--no-color": "no_color"}
USAGE = "usage: recursion-tui [--plain | --screen-reader] [--no-alt-screen] [--no-color]"


class LogReader:
    """Reads the app's event log from where it last stopped: the linear renderer's lines."""

    def __init__(self, app: TuiApp) -> None:
        self.app, self.seq = app, 0

    def new_lines(self) -> list[str]:
        events, skipped = self.app.log.since(self.seq)
        self.seq = self.app.log.last_seq
        head = [f"({skipped} events not shown: the log moved past them)"] if skipped else []
        return head + speech_lines(events)


def plain(api, stdin: Iterable[str], stdout: TextIO, environ: Mapping[str, str],
          clock: Callable[[], float] = time.monotonic, app: TuiApp | None = None) -> int:
    """Linear renderer: each input line is a command; each logged event is said once, in order.

    A condition is said when it appears, changes or clears; ``conditions`` says the current set again."""

    app = app or TuiApp(api, clock=clock, environ=environ)
    reader = LogReader(app)

    def flush() -> None:
        for line in reader.new_lines():
            print(line, file=stdout, flush=True)

    app.open()
    flush()
    for raw in stdin:
        line = raw.strip()
        if line in ("q", "quit", "exit"):
            break
        if line:
            app.sync_conditions()  # a condition that appeared with time alone is said before the answer
            app.command(line)
            flush()
    app.close()
    flush()
    return 0


class TerminalUnsupported(RuntimeError):
    """The real terminal driver needs ``termios`` (POSIX); this platform has none (Windows)."""


class Tty:
    """The real terminal: cbreak mode, key reads and size. Tests pass a fake with the same methods.

    ``read_key`` returns one key per call from what has been read, reading more only when none is
    left. ``eof`` turns true when the terminal ends input (a read of nothing, or EIO after a hangup);
    ``burst`` says whether the key just returned came in one read with other input. The read state is
    created on first use, so a ``Tty`` made without ``__init__`` (as a test may) reads the same way."""

    eof = False
    burst = False
    esc_wait_s = ESC_WAIT_S
    _decoder: KeyDecoder | None = None
    _ready: collections.deque | None = None

    def __init__(self, stdin: TextIO) -> None:
        try:
            import termios  # noqa: F401  absent on Windows; refuse here, before any screen output
            import tty  # noqa: F401
        except ImportError as exc:
            raise TerminalUnsupported("platform_unsupported: the full-screen terminal needs termios") from exc
        self.fd = stdin.fileno()

    def size(self) -> os.terminal_size:
        return shutil.get_terminal_size()

    @contextlib.contextmanager
    def cbreak(self):
        """Raw input for the life of the block, then the saved mode back. (The name is kept for the
        callers and test fakes that use it.) Output processing is not touched: the painters' CR LF and
        colour still reach the screen as before."""

        import termios

        saved = termios.tcgetattr(self.fd)
        mode = list(saved)
        # iflag: every input translation off, as tty.cfmakeraw does: an inherited IGNCR or INLCR would drop
        # or rewrite a pasted CR or LF (review of a1f16a20, C2).
        mode[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.IGNPAR | termios.PARMRK | termios.INPCK
                     | termios.ISTRIP | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON
                     | termios.IXANY | termios.IXOFF)
        mode[2] = (mode[2] & ~(termios.CSIZE | termios.PARENB)) | termios.CS8  # cflag
        mode[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)  # lflag
        mode[6] = list(mode[6])
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        try:
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, mode)
            yield
        finally:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, saved)
            except termios.error as err:  # a hung-up terminal has no mode to restore: that is its EIO
                eio = bool(err.args and err.args[0] == errno.EIO)
                # The hangup may come after the last read (q was already read): ask the terminal itself.
                if not (eio and (self.eof or self._hung_up())):
                    raise

    def _hung_up(self) -> bool:
        """The terminal reports a hangup now (POLLHUP), whether or not a read has seen it yet."""

        poll = getattr(select, "poll", None)
        if poll is None:
            return False
        poller = poll()
        poller.register(self.fd, select.POLLIN)
        return any(flags & select.POLLHUP for _, flags in poller.poll(0))

    def read_key(self, timeout: float) -> Key | None:
        """The next key, waiting up to ``timeout`` for input; None when there is none (or at the end)."""

        if self._decoder is None:
            self._decoder, self._ready = KeyDecoder(), collections.deque()
        if not self._ready and not self.eof:
            self._fill(timeout)
        if not self._ready:
            return None
        key, self.burst = self._ready.popleft()
        return key

    def _read(self, timeout: float) -> bytes | None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        try:
            return os.read(self.fd, READ_BYTES)
        except OSError as err:
            if err.errno != errno.EIO:  # only a hangup is the end of input; any other failure is raised
                raise
            return b""

    def _fill(self, timeout: float) -> None:
        """One read, plus the short waits that tell a lone Esc from the start of a sequence."""

        decoder, keys = self._decoder, []
        held_before = decoder.holding  # part of a key held from an earlier read
        discarded_before = decoder.discarded
        data = self._read(timeout)
        while data is not None:
            if not data:
                self.eof = True
                keys += decoder.end()
                break
            keys += decoder.feed(data)
            if not decoder.waiting:
                break
            data = self._read(self.esc_wait_s)
            if data is None:
                keys += decoder.timeout()  # a lone ESC becomes esc; a longer unfinished sequence is dropped
        # Other input in the same read: another key; part of one held before or after it; a sequence that
        # ended without a key however it ended (dropped by the wait or the end of input, completed as an
        # unknown sequence, broken by another character); or a paste begun beside it, an unfinished
        # sequence that became a paste opener included (reviews of a1f16a20, 46b8d7d8 and d69d18aa, C3).
        # Every sequence held beside a key ends as a key, as one of those sequences or as a paste, and the
        # wait's loop stops at a paste opener, so a paste begun beside a key is still open here.
        # A lone Esc, one key split over the wait, and one paste however split, are each one event.
        units = len(keys) + decoder.discarded - discarded_before
        burst = units > 1 or bool(keys) and (held_before or decoder.holding or decoder.in_paste)
        self._ready.extend((key, burst) for key in keys)


def interactive(api, stdout: TextIO, environ: Mapping[str, str], terminal, *, alt_screen: bool = True,
                on_app: Callable[[TuiApp], None] | None = None, stderr: TextIO | None = None) -> int:
    def size() -> tuple[int, int]:  # polled every tick: a resize gets a frame that fits (no signal handler)
        now = terminal.size()
        return now.columns, now.lines

    app = TuiApp(api, clock=time.monotonic, environ=environ, width=size()[0], height=size()[1])
    if on_app is not None:
        on_app(app)

    gone = [False]  # the terminal's output answered EIO: it hung up, and nothing more is written to it

    def write(text: str) -> None:
        if not text or gone[0]:
            return
        try:
            stdout.write(text)
            stdout.flush()
        except OSError as err:  # on a terminal, EIO is the hangup itself; every other failure is raised
            if err.errno != errno.EIO:
                raise
            gone[0] = True

    if alt_screen:
        painted = [(app.width, app.height)]

        def paint(frame) -> None:
            now = (app.width, app.height)
            clear = "\x1b[2J" if now != painted[0] else ""  # a new size: no row of the old layout stays
            painted[0] = now
            write(clear + to_ansi(frame, app.color))

        loop = Loop(app, api, sink=paint, size=size)
        write("\x1b[?1049h\x1b[?25l")
    else:  # the full-screen painter homes the cursor and clears below; inline mode never uses it
        painter, reader = InlinePainter(app.color), LogReader(app)
        loop = Loop(app, api, sink=lambda frame: write(painter.paint(reader.new_lines(), app.live_rows())),
                    render=lambda: [], size=size)
    try:
        with terminal.cbreak():
            write(PASTE_ON)
            loop.start()
            while not app.closed:
                key = terminal.read_key(0.1)
                if key:
                    loop.press(key, burst=bool(getattr(terminal, "burst", False)))
                elif getattr(terminal, "eof", False) or gone[0]:
                    break  # the terminal ended (R2 finding 10): close below; never read or paint it again
    except KeyboardInterrupt:
        pass  # a SIGINT from outside closes, below, like q: nothing is sent
    finally:
        # The loop paints nothing once shutdown() returns, even if its thread is still busy; the app
        # takes no event after close, so close is the last event whatever that thread does next.
        loop.shutdown()
        if not app.closed:
            app.close()
        write(PASTE_OFF)
        if alt_screen:
            write("\x1b[?25h\x1b[?1049l")
        else:
            write(painter.end(reader.new_lines()))
    if alt_screen and not gone[0]:
        err = stderr or sys.stderr
        try:
            for line in app.errors:  # the error log survives leaving the alternate screen
                print(f"error: {line}", file=err)
        except OSError as exc:  # the same hung-up terminal; anything else is raised
            if exc.errno != errno.EIO:
                raise
    return 0


def main(argv: list[str], api_factory: Callable[[], object] | None, *, stdin: TextIO | None = None,
         stdout: TextIO | None = None, environ: Mapping[str, str] | None = None,
         clock: Callable[[], float] = time.monotonic, terminal=None, stderr: TextIO | None = None) -> int:
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    environ = os.environ if environ is None else environ
    unknown = [a for a in argv if a not in FLAGS]
    if unknown:  # diagnostics go to stderr, never into the transcript stream
        print(f"usage error: unrecognized arguments: {' '.join(unknown)}; {USAGE}", file=stderr)
        return 2
    modes = {FLAGS[a] for a in argv}
    if "no_color" in modes:
        environ = {**environ, "NO_COLOR": "1"}
    if api_factory is None:
        print("status=control_api_unavailable: no control API client was given; nothing sent", file=stderr)
        return 1
    try:
        api = api_factory()
    except Exception as err:  # noqa: BLE001 - a client that cannot be built is the API being unavailable
        # The CLI's answer for the same failure, as a diagnostic on stderr (R2 finding 10). Only the
        # exception's type is shown: its text may hold a path or a value.
        print(f"status=control_api_unavailable: the control API client could not be built "
              f"(factory:{type(err).__name__}); nothing sent", file=stderr)
        return 1

    def is_tty(stream) -> bool:
        return bool(getattr(stream, "isatty", lambda: False)())

    if "plain" in modes or environ.get("TERM") == "dumb" or not (is_tty(stdin) and is_tty(stdout)):
        return plain(api, stdin, stdout, environ, clock)
    try:
        terminal = terminal or Tty(stdin)
    except TerminalUnsupported as exc:
        print(f"status={exc}; nothing sent; --plain works without it", file=stderr)
        return 1
    return interactive(api, stdout, environ, terminal, alt_screen="no_alt_screen" not in modes,
                       stderr=stderr)
