"""--no-alt-screen (CSL-799 correction, item 2): inline mode in the normal buffer.

No alternate screen is entered, finished lines are written once (never redrawn) so they stay in
scrollback, and only the small live region at the bottom is redrawn. Keys still work. A fake terminal
replaces termios; nothing here opens a real terminal.
"""

from __future__ import annotations

import collections
import contextlib
import io
import os
import re
import time
import unittest

from recursion_core.tui import term
from recursion_core.tui.app import TuiApp
from recursion_core.tui.events import speech_lines
from recursion_core.tui.tests.fakes import Clock, FakeDriver
from recursion_core.tui.view import InlinePainter

ALT_SCREEN = "\x1b[?1049h"
CURSOR_HOME = "\x1b[H"


class FakeTerminal:
    """Scripted keys, one per read, with a short pause so the loop thread handles each in turn."""

    def __init__(self, keys: list[str], columns: int = 100, lines: int = 30) -> None:
        self.keys, self.columns, self.lines = list(keys), columns, lines
        self.raw = False

    def size(self) -> os.terminal_size:
        return os.terminal_size((self.columns, self.lines))

    @contextlib.contextmanager
    def cbreak(self):
        self.raw = True
        try:
            yield
        finally:
            self.raw = False

    def read_key(self, timeout: float) -> str | None:
        time.sleep(0.03)
        return self.keys.pop(0) if self.keys else None


class TtyOut(io.StringIO):
    def isatty(self) -> bool:
        return True


def finished_lines(out: str) -> list[str]:
    """Lines ended by CRLF, with the painter's own CSI sequences taken as line starts."""

    return re.findall(r"([^\r\n\x1e]+)\r\n", re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "\x1e", out))


def typed(command: str) -> list[str]:
    return [":", *command, "enter"]


def run_tui(argv: list[str], keys: list[str], driver: FakeDriver) -> tuple[str, TuiApp]:
    out, apps = TtyOut(), []
    terminal = FakeTerminal(keys + ["q"])
    original = term.interactive

    def capture(*args, **kwargs):
        return original(*args, **kwargs, on_app=apps.append)

    term.interactive = capture
    try:
        code = term.main(argv, lambda: driver.api, stdin=TtyOut(), stdout=out, environ={"NO_COLOR": "1"},
                         terminal=terminal)
    finally:
        term.interactive = original
    assert code == 0 and not terminal.raw
    return out.getvalue(), apps[0]


class InlineModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = FakeDriver()
        self.run_id = self.driver.start_run()
        self.keys = typed("status run-1") + typed("stop nope") + typed("conditions") + ["s", "y"]

    def test_no_alt_screen_and_each_finished_line_written_once(self) -> None:
        out, app = run_tui(["--no-alt-screen"], self.keys, self.driver)
        self.assertNotIn(ALT_SCREEN, out)
        self.assertNotIn(CURSOR_HOME, out, "the full-screen painter is not used in the normal buffer")
        finished = speech_lines(app.log)
        self.assertIn("answer run.stop status=stopped run_id=run-1 state=stopped repeat=False", finished)
        self.assertTrue(any(x.startswith("error 1: REFUSED: run.stop status=run_unknown") for x in finished))
        self.assertIn("closed: no request sent; runs keep going and can be reattached", finished)
        for line, n in collections.Counter(finished).items():
            self.assertEqual(out.count(line + "\r\n"), n, f"written {out.count(line + chr(13) + chr(10))}x: {line}")
        written = finished_lines(out)
        self.assertEqual([w for w in written if w in finished], finished, "in log order, never repeated")
        self.assertEqual(self.driver.registry.get(self.run_id)["state"], "stopped", "keys still work")

    def test_live_region_is_cleared_on_exit(self) -> None:
        out, _ = run_tui(["--no-alt-screen"], typed("list"), self.driver)
        tail = out.rsplit("\x1b[J", 1)[1]
        self.assertNotIn("recursion: j/k", tail, "no live region is left behind")
        self.assertEqual(finished_lines(out)[-1], "closed: no request sent; runs keep going and can be reattached")

    def test_default_on_a_terminal_still_uses_the_alternate_screen(self) -> None:
        out, _ = run_tui([], typed("list"), self.driver)
        self.assertTrue(out.startswith(ALT_SCREEN))
        self.assertTrue(out.endswith("\x1b[?1049l"))

    def test_closing_inline_stops_nothing(self) -> None:
        run_tui(["--no-alt-screen"], typed("status run-1"), self.driver)
        self.assertEqual(self.driver.registry.get(self.run_id)["state"], "running")
        self.assertEqual(self.driver.model_calls, 1)


class InlinePainterTest(unittest.TestCase):
    def test_redraws_only_the_live_region(self) -> None:
        painter = InlinePainter(color=False)
        first = painter.paint(["one", "two"], [("status", None), ("prompt", None)])
        self.assertEqual(first, "one\r\ntwo\r\nstatus\x1b[Kprompt\x1b[K".replace("\x1b[Kp", "\x1b[K\r\np"))
        again = painter.paint([], [("status 2", None), ("prompt", None)])
        self.assertEqual(again, "\r\x1b[1A\x1b[Jstatus 2\x1b[K\r\nprompt\x1b[K")
        self.assertNotIn("one", again)
        self.assertEqual(painter.end(["three"]), "\r\x1b[1A\x1b[Jthree\r\n")

    def test_live_rows_fit_the_width_and_keep_the_error(self) -> None:
        driver = FakeDriver()
        driver.start_run()
        app = TuiApp(driver.api, clock=Clock(), environ={}, width=40)
        app.open()
        app.command("stop nope")
        for _ in range(50):
            app.command("list")
        rows = app.live_rows()
        self.assertTrue(all(len(t) < app.width for t, _ in rows), rows)
        self.assertTrue(any(t.startswith("error 1: REFUSED") for t, _ in rows), rows)


if __name__ == "__main__":
    unittest.main()
