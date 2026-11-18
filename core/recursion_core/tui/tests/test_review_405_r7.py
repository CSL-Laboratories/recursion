"""Review 7 of #405 (the session reads at 651f72bf): DeepSeek V4.1 Flash's confirming read, LOW and INFO only, and
GPT-6.1 Sol's round-3 read of the same head (documentation corrections 1 to 4). F1 to F3 (Sol's 1 to 3) are the
README's citations (``test_review_405_r6``'s sentence test now holds the corrected facts); Sol's 4 is the README
table's retention row; F4 and F5 are rules no test held, and so is the scrolled pager's count of a gap marker's
rows (Sol's surviving guards); F7 to F10 are the edges of what ``Transcript`` and the rebuild promise. Each test
here fails on 651f72bf except F4's, F5's and the scroll test, which hold rules already kept there. F6 and F11 are
answered in the lane's report.
"""

from __future__ import annotations

import copy
import pathlib
import unittest

from recursion_core.tui import sessions as s
from recursion_core.tui.app import TuiApp
from recursion_core.tui.tests.fake_daemon import FakeDaemon, gap_line
from recursion_core.tui.tests.fakes import Clock
from recursion_core.tui.sanitize import wrap
from recursion_core.tui.tests.test_sessions import SID, new_app, shown


def line(seq: int, text: str) -> dict:
    return {"seq": seq, "event_type": "engine.output", "text": text, "speech": None}


def page(lines: list[dict], seq: int) -> dict:
    return {"lines": lines, "more": False, "cursor": {"epoch": 1, "session_id": SID, "seq": seq}}


class RestartTest(unittest.TestCase):  # F4
    def test_a_rebuilt_view_shows_again_the_gap_line_at_the_head_of_the_log(self) -> None:
        transcript = s.Transcript(SID)
        one = page([gap_line(1, 4, 0), line(5, "five\n"), gap_line(6, 7, 0)], 7)
        self.assertEqual([x["seq"] for x in transcript.apply(one, 0)], [4, 5, 7])
        transcript.restart()
        self.assertEqual([x["seq"] for x in transcript.apply(one, 0)], [4, 5, 7], "a new view: every line again")


class CheckLimitTest(unittest.TestCase):  # F5
    def test_only_a_check_read_asks_for_one_line_more(self) -> None:
        transcript = s.Transcript(SID)
        self.assertEqual(transcript.read_params()["limit"], s.READ_LIMIT, "a first read")
        transcript.apply(page([line(1, "one\n"), gap_line(2, 3, 0)], 3), 0)
        self.assertEqual(transcript.read_params()["limit"], s.READ_LIMIT + 1, "an open's check read")
        self.assertEqual(transcript.read_params(continuing=True)["limit"], s.READ_LIMIT, "after gap lines")


class CopyTest(unittest.TestCase):  # F8
    def test_a_transcript_can_be_deep_copied_and_the_copy_has_a_lock_of_its_own(self) -> None:
        transcript = s.Transcript(SID)
        transcript.apply(page([line(1, "one\n")], 1), 0)
        copied = copy.deepcopy(transcript)
        self.assertEqual(copied.__dict__, transcript.__dict__)
        self.assertIsNot(copied._lock, transcript._lock)
        self.assertEqual(copied.apply(page([line(1, "one\n"), line(2, "two\n")], 2), 0)[0]["seq"], 2)
        self.assertEqual(transcript.last_seq, 1, "the copy's reads change only the copy")


class InterruptTest(unittest.TestCase):  # F9
    def test_an_interrupt_while_a_rebuild_read_is_sent_is_said_and_goes_on(self) -> None:
        daemon, armed = FakeDaemon(), {"on": False}

        def dispatch(method, params, done):
            if armed["on"] and method == s.READ and params["cursor"]["seq"] == 0:
                raise KeyboardInterrupt
            done(method, params, daemon.call(method, params))

        app = TuiApp(daemon, clock=Clock(), environ={"NO_COLOR": "1"}, width=220, height=120, dispatch=dispatch)
        app.open()
        app.command(f"transcript {SID}")
        del daemon.lines[3:]
        armed["on"] = True
        with self.assertRaises(KeyboardInterrupt):
            app.command(f"transcript {SID}")
        self.assertEqual(app.in_flight[s.READ], 0)
        self.assertEqual(app.transcripts[SID].owed, "read_failed")
        self.assertIn("could not be sent (KeyboardInterrupt): pending", app.error_row())


class AskedTest(unittest.TestCase):  # F10
    def test_an_asked_that_is_not_a_position_shows_nothing(self) -> None:
        transcript = s.Transcript(SID)
        self.assertEqual(transcript.apply(page([line(1, "one\n")], 1), True), [])
        self.assertIsNone(transcript.last_line)


class RaisingLineTest(unittest.TestCase):  # F7
    def test_a_line_whose_items_raise_raises_before_anything_changes_and_the_docstring_says_so(self) -> None:
        class Hostile(dict):
            def items(self):
                raise RuntimeError("hostile Mapping")

        transcript = s.Transcript(SID)
        with self.assertRaises(RuntimeError):
            transcript.apply(page([Hostile(line(1, "one\n"))], 1), 0)
        self.assertEqual((transcript.last_seq, transcript.cursor["seq"], transcript.broken), (0, 0, None))
        doc = " ".join(s.Transcript.apply.__doc__.split())
        self.assertIn("a line whose own methods raise raises to the caller there, before anything is changed", doc)


class ScrollTest(unittest.TestCase):  # Sol's round 3, a surviving guard: the gap marker's rows in a scrolled pager
    def test_a_scrolled_pager_counts_the_gap_marker_rows_it_adds(self) -> None:
        app, daemon = new_app(width=40)
        app.command(f"transcript {SID}")
        first = list(shown(app))
        for i in range(3):
            daemon.append("engine.output", f"x {i}\n")  # 8, 9, 10
        daemon.forget(8, 9)
        app.scroll = 1  # the pager is scrolled up one row; reading on keeps the view
        app.command(f"transcript {SID}")
        added = shown(app)[len(first):]
        self.assertIn(s.GAP_MARK, added)
        width = app._pane_width()
        rows = sum(len(wrap(row, width)) for text in added for row in text.split("\n"))
        self.assertEqual(app.scroll, 1 + rows, "every row added, the marker's wrapped rows included")


class SentencesTest(unittest.TestCase):  # F6
    def test_the_length_rule_is_said_for_a_read_that_names_a_limit(self) -> None:
        doc = " ".join(s.page_problem.__doc__.split())
        self.assertIn("when the read names one", doc)
        readme = " ".join((pathlib.Path(s.__file__).parent / "README.md").read_text(encoding="utf-8").split())
        self.assertNotIn("the `events` package is not in this tree yet", readme)
        self.assertIn("then its gap marker row; no rebuild marker", readme)  # Sol's round 3, 4


if __name__ == "__main__":
    unittest.main()
