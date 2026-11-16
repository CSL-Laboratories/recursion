"""The two Devin threads on #405 at 791004e9, answered at the Liaison's ruling on Q21 (Nov 15, 11:10 PM). Both tests
fail on 791004e9.
T1 (app.py:471-475): a transcript longer than the pane keeps lost its first lines from the full-screen pane with no
word; the pane's title row now says how many earlier rows it does not keep, and that --plain prints every line.
T2 (app.py:487-495): a page asked before the view was rebuilt was dropped by apply, but its `more` was followed and
its `epoch_advanced` said; it is now dropped whole.
"""

from __future__ import annotations

import unittest

from recursion_core.tui import sessions as s
from recursion_core.tui.tests.fake_daemon import FakeDaemon
from recursion_core.tui.tests.test_review_405_r5 import drain, marks, queued
from recursion_core.tui.tests.test_sessions import SID, new_app, shown


class PaneKeepsTest(unittest.TestCase):  # T1
    def test_the_pane_says_how_many_earlier_lines_it_does_not_keep(self) -> None:
        daemon = FakeDaemon()
        for i in range(2100):
            daemon.append("engine.output", f"line {i}\n")
        app, _ = new_app(daemon)
        app.command(f"transcript {SID}")
        self.assertEqual(len(shown(app)), 2107, "every line read and logged")
        rows_read = sum(len(text.split("\n")) for text in shown(app))  # the pane keeps display rows
        left = rows_read - app._output_lines
        self.assertEqual(app.transcript_rows[SID].left, left)
        title = [text for text, _ in app.frame() if text.startswith("-- transcript")]
        self.assertIn(f"{left} earlier rows are not kept in this pane, --plain prints every line", title[0])

    def test_a_pane_that_keeps_every_line_says_nothing_of_it(self) -> None:
        app, _ = new_app()
        app.command(f"transcript {SID}")
        self.assertFalse(any("not kept in this pane" in text for text, _ in app.frame()))


class StalePageTest(unittest.TestCase):  # T2
    def test_a_page_asked_before_a_rebuild_is_dropped_whole(self) -> None:
        daemon = FakeDaemon()
        app, pending = queued(daemon)
        app.command(f"transcript {SID}")
        drain(daemon, pending)
        for i in range(300):
            daemon.append("engine.output", f"late {i}\n")
        daemon.lines[6] = dict(daemon.lines[6], text="written again\n")  # the last line shown, seq 7
        daemon.calls.clear()
        app.command(f"transcript {SID}")
        app.command(f"transcript {SID}")  # two check reads in flight; the first answer rebuilds the view
        drain(daemon, pending)
        reads = [(p["cursor"]["seq"], p["limit"]) for m, p in daemon.calls if m == s.READ]
        self.assertEqual(reads, [(6, 201), (6, 201), (0, 200), (199, 201)], "one read chain, not two")
        self.assertEqual(len(marks(app)), 1)


if __name__ == "__main__":
    unittest.main()
