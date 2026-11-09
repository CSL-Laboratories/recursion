"""The full-screen frame fits the terminal and never hides a condition (PR #329 red-team rounds 1, 2).

Finding 1: more than four active conditions cut UNCERTAIN and REFUSED off the screen.
Finding 2: the size was read once and floored at 20x16, so a smaller or resized terminal scrolled the
condition rows away. Finding 4: ``--no-color`` was a usage error and diagnostics went to stdout.
Round 2 finding 1: with the API's real 36-character run ids (``run-`` + 32 hex), the per-label row was clipped at the width,
so at 80x24 only one of 12 unconfirmed Stops was named (and below ~76 columns not even one).
"""

from __future__ import annotations

import io
import re
import unittest

from recursion_core.control.api import _new_run_id
from recursion_core.control.registry import POPULATED
from recursion_core.tui import protocol as p
from recursion_core.tui import term
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.sanitize import cell_width
from recursion_core.tui.tests.fakes import Clock, FakeDriver
from recursion_core.tui.tests.test_inline import FakeTerminal, TtyOut

SIZES = [(1, 1), (2, 1), (10, 3), (12, 5), (20, 6), (40, 8), (19, 15), (60, 12), (80, 24), (100, 30), (200, 60)]
SGR = re.compile(r"\x1b\[[0-9;]*m")


def pending_stops(n: int, *, stale: bool, clock: Clock | None = None, real_ids: bool = False,
                  **size) -> TuiApp:
    """The reviewer's repro: ``n`` runs, ``s y`` on each, every Stop answered ``pending``. ``real_ids``
    uses the control API's own ``run-<32 hex>`` ids instead of the goldens' ``run-1``."""

    driver = FakeDriver(confirm_answers=[POPULATED], new_run_id=_new_run_id if real_ids else None)
    ids = [driver.start_run() for _ in range(n)]
    clock = clock or Clock()
    app = TuiApp(driver.api, clock=clock, environ={"NO_COLOR": "1"}, stale_after_s=10.0, **size)
    app.open()
    for run_id in ids:
        app.selected = run_id
        app.press("s")
        app.press("y")
    if stale:
        clock.now += 11
    app.sync_conditions()
    return app


def screen(app: TuiApp) -> list[str]:
    return [text for text, _ in app.frame()]


def block_rows(rows: list[str], label: str) -> list[str]:
    """The label's rows: its head row and the indented rows under it (indent removed)."""

    out, inside = [], False
    for r in rows:
        if r.startswith(f"{label}: "):
            inside = True
        elif inside and not r.startswith("  "):
            inside = False
        if inside:
            out.append(r[2:] if r.startswith("  ") else r)
    return out


def block(rows: list[str], label: str) -> str:
    """The label's rows joined two ways: with a space (text broken at a word) and with nothing (a run id
    wider than the terminal, broken mid-id), so either reads whole."""

    found = block_rows(rows, label)
    return " ".join(found) + "\n" + "".join(found)


class ConditionsNeverHiddenTest(unittest.TestCase):
    def assert_every_condition_on_screen(self, app: TuiApp) -> None:
        rows = screen(app)
        for (label, subject), (_, text) in app.shown.items():
            self.assertTrue(any(r.startswith(f"{label}: ") for r in rows), f"{label} has no row: {rows}")
            if label in (p.UNCERTAIN, p.UNRECONCILED) and subject.startswith("run="):  # each run is named whole
                run_id = subject.split("=", 1)[1]
                self.assertRegex(block(rows, label), rf"(?<![0-9a-f-]){re.escape(run_id)}(?![0-9a-f])",
                                 f"{label} {subject} not named: {rows}")

    def test_two_pending_stops_and_stale_keep_both_uncertain_rows(self) -> None:
        app = pending_stops(2, stale=True, width=100, height=30)
        labels = {label for label, _ in app.shown.values()}
        self.assertEqual(labels, {p.UNCERTAIN, p.UNRECONCILED, p.STALE})
        self.assertGreater(len(app.shown), 4, "the case the old 4-row cap cut")
        self.assert_every_condition_on_screen(app)
        self.assertFalse(any("more conditions" in r for r in screen(app)))

    def test_three_pending_stops_without_stale(self) -> None:
        app = pending_stops(3, stale=False, width=100, height=30)
        self.assertEqual(sum(1 for label, _ in app.shown if label == p.UNCERTAIN), 3)
        self.assert_every_condition_on_screen(app)

    def test_more_conditions_than_rows_fall_back_to_one_row_per_label(self) -> None:
        app = pending_stops(12, stale=True, width=200, height=24)
        rows = screen(app)
        self.assertEqual(len(rows), 24)
        self.assertGreater(len(app.shown), 24 - 5, "too many for one row each")
        self.assert_every_condition_on_screen(app)
        uncertain = next(r for r in rows if r.startswith("UNCERTAIN: "))
        self.assertIn("12 active", uncertain)

    def test_real_run_ids_at_80x24_name_every_pending_stop(self) -> None:
        for n in (1, 6, 12):
            with self.subTest(pending=n):
                app = pending_stops(n, stale=True, real_ids=True, width=80, height=24)
                ids = [run["run_id"] for run in app.runs]
                self.assertTrue(all(len(i) == 36 for i in ids), ids)
                rows = screen(app)
                self.assertEqual(len(rows), 24)
                self.assertTrue(all(len(r) <= 80 for r in rows))
                self.assert_every_condition_on_screen(app)
                for label in (p.UNCERTAIN, p.UNRECONCILED):
                    shown = block_rows(rows, label)
                    for run_id in ids:  # at 80 columns no id is split across rows
                        self.assertTrue(any(run_id in r for r in shown), (label, run_id, rows))
                    self.assertFalse(any("…" in r for r in shown[1:]), "a name row is never clipped")

    def test_one_pending_stop_keeps_its_id_on_a_narrow_terminal(self) -> None:
        for width in (20, 40, 60, 75):
            with self.subTest(width=width):
                app = pending_stops(1, stale=False, real_ids=True, width=width, height=24)
                self.assert_every_condition_on_screen(app)

    def test_more_names_than_rows_say_how_many_are_not_named(self) -> None:
        app = pending_stops(30, stale=True, real_ids=True, width=80, height=24)
        rows = screen(app)
        self.assertEqual(len(rows), 24)
        self.assertTrue(rows[-1].startswith("recursion: "), "the prompt stays")
        for label in (p.UNRECONCILED, p.STALE, p.UNCERTAIN):
            self.assertTrue(any(r.startswith(f"{label}: ") for r in rows), label)
        uncertain = block(rows, p.UNCERTAIN)
        named = [run["run_id"] for run in app.runs if run["run_id"] in uncertain]
        self.assertGreaterEqual(len(named), 12, "UNCERTAIN gets the rows first")
        if len(named) < 30:
            self.assertIn(f"… {30 - len(named)} more not named here", uncertain)

    def test_refused_and_uncertain_survive_together(self) -> None:
        app = pending_stops(3, stale=True, width=100, height=30)
        app.command("stop nope")
        self.assertIn(p.REFUSED, {label for label, _ in app.shown.values()})
        self.assert_every_condition_on_screen(app)

    def test_hiding_detector_still_fires(self) -> None:
        app = pending_stops(3, stale=True, width=100, height=30)
        real = app._current
        app._current = lambda: dict(list(real().items())[:4])  # mutant: the old 4-row cap, sorted UNCERTAIN last
        rows = screen(app)
        self.assertFalse(all(any(r.startswith("UNCERTAIN: ") and f"run-{i}" in r for r in rows) for i in (1, 2, 3)))


class FrameFitsTerminalTest(unittest.TestCase):
    def test_every_size_gets_exactly_its_rows_and_columns(self) -> None:
        for width, height in SIZES:
            for n, real in ((0, False), (1, False), (3, False), (12, False), (1, True), (12, True)):
                with self.subTest(width=width, height=height, pending=n, real_ids=real):
                    app = pending_stops(n, stale=bool(n), real_ids=real, width=width, height=height)
                    for i in range(300):
                        app.on_event({"run_id": app.runs[0]["run_id"] if app.runs else "run-1", "text": f"noise {'w' * 150} {i}\n"})
                    app.attached = app.runs[0]["run_id"] if app.runs else "run-1"
                    rows = screen(app)
                    self.assertEqual(len(rows), height)
                    for r in rows:
                        self.assertLessEqual(sum(cell_width(c) for c in r), width, r)
                    if width >= 40:  # every label stays readable as text, whatever the height
                        text = "\n".join(rows)
                        for label, _ in app.shown.values():
                            self.assertIn(label, text)
                    if height >= 3 and n:
                        self.assertTrue(any(r.startswith("recursion: ") for r in rows) or width < 12)

    def test_small_terminal_drops_runs_and_output_before_conditions(self) -> None:
        app = pending_stops(2, stale=True, width=80, height=10)
        rows = screen(app)
        self.assertEqual(len(rows), 10)
        self.assertTrue(rows[-1].startswith("recursion: "), "the prompt stays")
        self.assertTrue(any(r.startswith("UNCERTAIN: ") for r in rows))
        self.assertFalse(any(r.startswith("-- output") for r in rows))

    def test_resize_is_picked_up_every_tick(self) -> None:
        driver = FakeDriver()
        driver.start_run()
        size = [(100, 30)]
        app = TuiApp(driver.api, clock=Clock(), environ={}, width=100, height=30)
        loop = Loop(app, driver.api, size=lambda: size[0])
        app.open()
        loop.tick()
        self.assertEqual(len(loop.frame), 30)
        size[0] = (30, 8)
        loop.tick()
        self.assertEqual((app.width, app.height, len(loop.frame)), (30, 8, 8))
        self.assertTrue(all(len(r) <= 30 for r, _ in loop.frame))
        size[0] = (120, 40)
        loop.tick()
        self.assertEqual(len(loop.frame), 40)

    def test_terminal_resize_repaints_at_the_new_size(self) -> None:
        driver = FakeDriver()
        driver.start_run()

        class Shrinking(FakeTerminal):
            def read_key(self, timeout):
                key = super().read_key(timeout)
                if key == "r":
                    self.columns, self.lines = 40, 9
                return key

        out, apps = TtyOut(), []
        terminal = Shrinking(["j", "r", "j", "j", "q"])
        original = term.interactive
        term.interactive = lambda *a, **k: original(*a, **k, on_app=apps.append)
        try:
            self.assertEqual(term.main([], lambda: driver.api, stdin=TtyOut(), stdout=out, environ={},
                                       terminal=terminal), 0)
        finally:
            term.interactive = original
        text = out.getvalue()
        self.assertIn("\x1b[2J", text, "a new size clears the old layout once")
        last = text.rsplit("\x1b[H", 1)[1]
        self.assertEqual(last.count("\r\n") + 1, 9, "the last frame has the shrunk height")
        self.assertEqual((apps[0].width, apps[0].height), (40, 9))


class NoColorFlagTest(unittest.TestCase):
    def test_no_color_flag_turns_colour_off_on_screen(self) -> None:
        driver = FakeDriver()
        driver.start_run()
        apps = []
        original = term.interactive
        term.interactive = lambda *a, **k: original(*a, **k, on_app=apps.append)
        try:
            for argv in (["--no-color"], ["--no-color", "--no-alt-screen"]):
                with self.subTest(argv=argv):
                    out = TtyOut()
                    code = term.main(argv, lambda: driver.api, stdin=TtyOut(), stdout=out, environ={},
                                     terminal=FakeTerminal(["s", "y", "q"]))
                    self.assertEqual(code, 0)
                    self.assertFalse(apps[-1].color)
                    self.assertNotRegex(out.getvalue(), SGR)
        finally:
            term.interactive = original
        colour_out = TtyOut()
        term.main([], lambda: driver.api, stdin=TtyOut(), stdout=colour_out, environ={},
                  terminal=FakeTerminal(["q"]))
        self.assertRegex(colour_out.getvalue(), SGR, "without the flag a colour terminal gets colour")

    def test_no_color_flag_in_line_mode(self) -> None:
        driver = FakeDriver()
        driver.start_run()
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(term.main(["--plain", "--no-color"], lambda: driver.api, stdin=io.StringIO("list\nq\n"),
                                   stdout=out, environ={}, stderr=err), 0)
        self.assertIn("answer run.list status=ok", out.getvalue())
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
