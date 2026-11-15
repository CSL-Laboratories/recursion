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
no run.
"""

from __future__ import annotations

import contextlib
import os
import select
import shutil
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from typing import TextIO

from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.events import speech_lines
from recursion_core.tui.view import InlinePainter, to_ansi

_KEYS = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\x1b": "esc",
         "\x1b[A": "up", "\x1b[B": "down"}
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
    """The real terminal: cbreak mode, key reads and size. Tests pass a fake with the same methods."""

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
        import termios
        import tty

        saved = termios.tcgetattr(self.fd)
        try:
            tty.setcbreak(self.fd)
            yield
        finally:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, saved)

    def read_key(self, timeout: float) -> str | None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        data = os.read(self.fd, 16).decode("utf-8", "replace")
        return _KEYS.get(data, data[:1] if data[:1] != "\x1b" else "esc")


def interactive(api, stdout: TextIO, environ: Mapping[str, str], terminal, *, alt_screen: bool = True,
                on_app: Callable[[TuiApp], None] | None = None, stderr: TextIO | None = None) -> int:
    def size() -> tuple[int, int]:  # polled every tick: a resize gets a frame that fits (no signal handler)
        now = terminal.size()
        return now.columns, now.lines

    app = TuiApp(api, clock=time.monotonic, environ=environ, width=size()[0], height=size()[1])
    if on_app is not None:
        on_app(app)

    def write(text: str) -> None:
        if text:
            stdout.write(text)
            stdout.flush()

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
            loop.start()
            while not app.closed:
                key = terminal.read_key(0.1)
                if key:
                    loop.press(key)
    except KeyboardInterrupt:
        app.close()
    finally:
        loop.shutdown()
        if alt_screen:
            write("\x1b[?25h\x1b[?1049l")
        else:
            write(painter.end(reader.new_lines()))
    if alt_screen:
        for line in app.errors:  # the error log survives leaving the alternate screen
            print(f"error: {line}", file=stderr or sys.stderr)
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
    api = api_factory()

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
