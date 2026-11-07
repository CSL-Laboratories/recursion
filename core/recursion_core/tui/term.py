"""Terminal driver (CSL-799, candidate). The only TUI module that touches the real terminal.

``main`` takes an injected ``api_factory`` like the CLI's ``run``; the TUI owns no transport. On a
terminal it draws full-screen frames on the alternate screen. With ``--plain``, ``TERM=dumb`` or when
stdin/stdout are not a terminal it runs line mode for screen readers: it reads one CLI-grammar command
per line and only appends text lines (no redraw, no cursor movement), so no error is redrawn away.
Ctrl-C or ``q`` closes the TUI; that sends nothing and stops no run.
"""

from __future__ import annotations

import os
import select
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from typing import TextIO

from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.view import to_ansi

_KEYS = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\x1b": "esc",
         "\x1b[A": "up", "\x1b[B": "down"}


def plain(api, stdin: TextIO, stdout: TextIO, environ: Mapping[str, str],
          clock: Callable[[], float] = time.monotonic) -> int:
    """Line mode: each input line is a command; each new transcript line is printed once."""

    app = TuiApp(api, clock=clock, environ=environ)
    printed = 0

    def flush() -> None:
        nonlocal printed
        for line in app.transcript[printed:]:
            print(line, file=stdout, flush=True)
        printed = len(app.transcript)

    def conditions() -> None:  # the current conditions, as text, after every command
        for label, text in app.conditions():
            print(f"{label}: {text}", file=stdout)

    app.open()
    flush()
    conditions()
    for raw in stdin:
        line = raw.strip()
        if line in ("q", "quit", "exit"):
            break
        if line:
            app.command(line)
            flush()
            conditions()
    app.close()
    flush()
    return 0


def _read_key(fd: int) -> str:
    data = os.read(fd, 16).decode("utf-8", "replace")
    return _KEYS.get(data, data[:1] if data[:1] != "\x1b" else "esc")


def fullscreen(api, stdin: TextIO, stdout: TextIO, environ: Mapping[str, str]) -> int:
    import termios
    import tty

    size = shutil.get_terminal_size()
    app = TuiApp(api, clock=time.monotonic, environ=environ, width=size.columns, height=size.lines)
    loop = Loop(app, api, sink=lambda frame: (stdout.write(to_ansi(frame, app.color)), stdout.flush()))
    fd = stdin.fileno()
    saved = termios.tcgetattr(fd)
    stdout.write("\x1b[?1049h\x1b[?25l")
    try:
        tty.setcbreak(fd)
        loop.start()
        while not app.closed:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                loop.press(_read_key(fd))
    except KeyboardInterrupt:
        app.close()
    finally:
        loop.shutdown()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        stdout.write("\x1b[?25h\x1b[?1049l")
        stdout.flush()
    for line in app.errors:  # the error log survives leaving the alternate screen
        print(f"error: {line}", file=sys.stderr)
    return 0


def main(argv: list[str], api_factory: Callable[[], object] | None, *, stdin: TextIO | None = None,
         stdout: TextIO | None = None, environ: Mapping[str, str] | None = None,
         clock: Callable[[], float] = time.monotonic) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    environ = os.environ if environ is None else environ
    if api_factory is None:
        print("status=control_api_unavailable: no control API client was given; nothing sent", file=stdout)
        return 1
    api = api_factory()
    def is_tty(stream) -> bool:
        return bool(getattr(stream, "isatty", lambda: False)())

    if "--plain" in argv or environ.get("TERM") == "dumb" or not (is_tty(stdin) and is_tty(stdout)):
        return plain(api, stdin, stdout, environ, clock)
    return fullscreen(api, stdin, stdout, environ)
