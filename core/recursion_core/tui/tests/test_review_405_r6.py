"""Review 6 of #405 (the session reads at 71c3a006): DeepSeek V4.1 Flash's confirming read. Each finding changed here
is a test that fails on that head: F2 two threads on one transcript showed a page twice, F3 at the page budget the
marker promised a read that was not sent, F4 a rebuild whose read could not be sent was said as an answer not
taken, F5 gap lines past the 201 kept were shown again, F11 a line at seq 0 sent the next read to seq -1, and the
README's sentences, citations and table (F7, F8, F9). F1, by the Liaison's ruling: a gap line does not say where
its gap starts, so a marker row after every gap line says lines may be missing before it; a gap line that starts
late was silence there. F6 and F10 are answered without a change (report.md).
"""

from __future__ import annotations

import collections
import copy
import threading
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.tui import sessions as s
from recursion_core.tui.app import TuiApp, _Output
from recursion_core.tui.sanitize import wrap
from recursion_core.tui.tests.fake_daemon import FakeDaemon, gap_line
from recursion_core.tui.tests.fakes import Clock
from recursion_core.tui.tests.test_review_405_r5 import marks, opened
from recursion_core.tui.tests.test_sessions import SID, new_app, pane_rows, shown


def line(seq: int, text: str) -> dict:
    return {"seq": seq, "event_type": "engine.output", "text": text, "speech": None}


def page(lines: list[dict], seq: int) -> dict:
    return {"lines": lines, "more": False, "cursor": {"epoch": 1, "session_id": SID, "seq": seq}}


def reads(daemon: FakeDaemon) -> list[dict]:
    return [params for method, params in daemon.calls if method == s.READ]


class TwoCallersTest(unittest.TestCase):  # F2 (on 7239b049's successor the race shows on 3.14t, GIL off)
    def test_two_threads_applying_one_page_show_each_line_once(self) -> None:
        one = page([line(n, f"line {n}\n") for n in range(1, 21)], 20)
        doubled = 0
        for _ in range(200):
            transcript, barrier, got = s.Transcript(SID), threading.Barrier(2), []

            def run(transcript=transcript, barrier=barrier, got=got) -> None:
                barrier.wait()
                got.append(transcript.apply(one, asked=0))

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            doubled += sum(len(lines or []) for lines in got) != 20
        self.assertEqual(doubled, 0, "rounds of 200 that showed a line twice")


class BudgetTest(unittest.TestCase):  # F3
    def test_with_no_read_left_the_marker_says_the_command_reads_the_log_again(self) -> None:
        app, daemon, first = opened()
        del daemon.lines[3:]
        daemon.calls.clear()
        with mock.patch.object(s, "MAX_PAGES", 1):
            app.command(f"transcript {SID}")  # the open's one read is refused: the rebuild has no read left
        self.assertEqual(len(reads(daemon)), 1, "no read was sent")
        marker = marks(app)[0]
        self.assertIn(f"reading the log again is pending: the command transcript {SID} reads it from its start",
                      marker)  # Sol's round 2, 4: an explicit pending state
        self.assertNotIn("the log is read again from its start, below", marker)
        self.assertEqual(app.transcripts[SID].owed, "pending")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, marker, *first[:3]])
        self.assertIsNone(app.transcripts[SID].owed)


class RebuildReadTest(unittest.TestCase):  # F4
    def test_a_rebuild_whose_read_cannot_be_sent_says_so(self) -> None:
        daemon, refuse = FakeDaemon(), {"on": False}

        def dispatch(method, params, done):
            if refuse["on"] and method == s.READ and params["cursor"]["seq"] == 0:
                raise RuntimeError("the control API connection is gone")
            done(method, params, daemon.call(method, params))

        app = TuiApp(daemon, clock=Clock(), environ={"NO_COLOR": "1"}, width=220, height=120, dispatch=dispatch)
        app.open()
        app.command(f"transcript {SID}")
        first = list(shown(app))
        del daemon.lines[3:]
        refuse["on"] = True
        app.command(f"transcript {SID}")  # refused: the view is rebuilt, and its read cannot be sent
        self.assertIn("but the read of the log from its start could not be sent (RuntimeError): pending",
                      app.error_row())  # Sol's round 2, 4: a read-failed state, said as what was taken
        self.assertNotIn("it was not taken", app.error_row())
        self.assertEqual(app.in_flight[s.READ], 0)
        self.assertEqual(app.transcripts[SID].owed, "read_failed")
        refuse["on"] = False
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, *marks(app), *first[:3]])


class GapLinesTest(unittest.TestCase):  # F5
    def test_gap_lines_already_shown_after_the_last_line_shown_are_not_shown_again(self) -> None:
        transcript = s.Transcript(SID)
        lines = [line(1, "one\n"), *(gap_line(n, n, 0) for n in range(2, 252))]  # 250 gap lines
        self.assertEqual(len(transcript.apply(page(lines, 251), asked=0)), 251)
        self.assertEqual(transcript.apply(page(lines, 251), asked=0), [], "read again: nothing twice")
        more = [line(1, "one\n"), *(gap_line(n, n, 0) for n in range(2, 253))]
        self.assertEqual([x["seq"] for x in transcript.apply(page(more, 252), asked=0)], [252], "only the new gap")


class ConsumerSeqTest(unittest.TestCase):  # F11
    def test_a_line_at_seq_0_is_no_line(self) -> None:
        transcript = s.Transcript(SID)
        self.assertEqual(transcript.apply(page([line(0, "zero\n")], 0)), [])
        self.assertEqual(transcript.read_cursor()["seq"], 0)


class LateGap(FakeDaemon):
    """After the vector's seven lines, a gap line for 10 to 12 and a line at 13: positions 8 and 9 are in no line."""

    def call(self, method, params=None):
        answer = super().call(method, params)
        if method == s.READ and answer.get("code") == "ok" and params["cursor"]["seq"] == 6:
            answer["lines"] = [answer["lines"][0], gap_line(10, 12, 0), line(13, "after the gap\n")]
            answer["cursor"] = dict(answer["cursor"], seq=13)
        return answer


class GapStartTest(unittest.TestCase):  # F1, by the Liaison's ruling: a marker is not silence
    def test_a_gap_line_that_starts_late_is_followed_by_a_marker_row(self) -> None:
        app, daemon, first = opened(LateGap())
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, "events 10 to 12 are not available (retention)", s.GAP_MARK,
                                      "after the gap"])
        self.assertIn(s.GAP_MARK, pane_rows(app), "full screen shows it in the pane")

    def test_every_gap_line_shown_is_followed_by_the_marker_and_only_once(self) -> None:
        daemon = FakeDaemon()
        daemon.forget(2, 3)
        app, _, first = opened(daemon)
        at = first.index("events 2 to 3 are not available (retention)")
        self.assertEqual(first[at + 1], s.GAP_MARK)
        self.assertEqual(first.count(s.GAP_MARK), 1)
        daemon.append("engine.output", "one more\n")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, "one more"], "the gap is not shown again, nor its marker")


class ExtendFailureTest(unittest.TestCase):  # #404's round 3 (Astra, required 1), for the transcript's rows
    def test_a_failure_while_transcript_rows_replace_retained_lines_leaves_no_stale_count(self) -> None:
        class OnceFailingDeque(collections.deque):
            def extend(self, values):
                for i, value in enumerate(values):
                    self.append(value)
                    if i == 1:
                        raise MemoryError("simulated allocation failure after two retained lines replaced")

        out = _Output(3, SID)
        out.extend(["a", "b", "c"])
        out.counts(4)
        out.lines = OnceFailingDeque(out.lines, maxlen=3)
        with self.assertRaises(MemoryError):
            out.extend(["abcdefghijk", "lmnopqrstuv", "wxyz1234567"])
        expected = [row for text in out.source() for row in wrap(text, 4)]
        self.assertEqual(out.total_rows(4), len(expected))
        self.assertEqual(out.tail(6, 4), expected[-6:])


class SolRound2Test(unittest.TestCase):  # GPT-6.1 Sol's round-2 read of 71c3a006, required 2 to 4, and its F2 probe
    def test_content_after_gap_only_pages_is_reached(self) -> None:  # required 2 (its probe, READ_LIMIT 2)
        daemon = FakeDaemon()
        daemon.lines = [line(1, "line 1"), gap_line(2, 2, 2), gap_line(3, 3, 3), gap_line(4, 4, 4),
                        line(5, "reachable tail")]
        app, _ = new_app(daemon)
        with mock.patch.object(s, "READ_LIMIT", 2):
            app.command(f"transcript {SID}")
        self.assertIn("reachable tail", shown(app))
        self.assertEqual(marks(app), [], "reached by reading on, not by rebuilding the view")

    def test_more_gap_lines_than_a_page_holds_are_passed_without_asking_one_page_twice(self) -> None:  # required 2
        daemon = FakeDaemon()
        first, tail = dict(daemon.lines[0], time_rel_ms=0), dict(daemon.lines[-1], seq=203, text="reachable tail")
        daemon.lines = [first, *(gap_line(n, n, n) for n in range(2, 203)), tail]
        app, _ = new_app(daemon)
        app.command(f"transcript {SID}")
        asked = [(p["cursor"]["seq"], p["limit"]) for p in reads(daemon)]
        self.assertIn("reachable tail", shown(app))
        self.assertEqual(len(asked), len(set(asked)), asked)
        self.assertEqual(shown(app).count(s.GAP_MARK), 201, "each gap line once, each with its marker row")
        self.assertEqual(marks(app), [])

    def test_a_refused_check_after_a_consumer_moved_the_cursor_back_still_rebuilds(self) -> None:
        app, daemon = new_app()
        app.command(f"transcript {SID}")
        app.transcripts[SID].cursor = s.first_cursor(SID)  # a consumer puts the cursor back
        del daemon.lines[3:]
        app.command(f"transcript {SID}")  # the check read is refused: it is this transcript's own read
        self.assertEqual(len(marks(app)), 1)

    def test_a_refused_page_leaves_the_transcripts_state_as_it_was(self) -> None:  # Sol's R2 probe, its shape
        app, daemon = new_app()
        app.command(f"transcript {SID}")
        transcript = app.transcripts[SID]
        asked = transcript.read_params()
        answer = dict(daemon.call(s.READ, asked), projection="raw")
        before = copy.deepcopy(transcript.__dict__)
        app.on_answer(s.READ, asked, answer)
        self.assertEqual(transcript.__dict__, before)

    def test_a_page_longer_than_the_limit_asked_is_refused_before_it_is_taken(self) -> None:  # required 3
        asked = s.read_params(s.first_cursor(SID))
        over = {"code": "ok", "projection": "transcript", "lines": [line(n, f"line {n}\n") for n in range(1, 202)],
                "cursor": {"epoch": 1, "session_id": SID, "seq": 201}, "more": False, "epoch_advanced": False}
        self.assertEqual(s.page_problem(asked, over), "lines")
        self.assertIsNone(s.page_problem(dict(asked, limit=201), over), "a check read asks for one more")

    def test_a_rebuild_at_the_budget_through_the_public_callback_says_pending(self) -> None:  # required 4
        app, daemon = new_app()
        app.command(f"transcript {SID}")
        transcript = app.transcripts[SID]
        asked = transcript.read_params()
        del daemon.lines[3:]
        answer = daemon.call(s.READ, asked)
        daemon.calls.clear()
        transcript.pages = s.MAX_PAGES
        app.on_answer(s.READ, asked, answer)
        self.assertEqual(reads(daemon), [])
        self.assertTrue(any("pending" in text for text in marks(app)))

    def test_two_callers_inside_apply_at_once_show_the_line_once(self) -> None:  # Sol's deterministic F2 probe
        barrier = threading.Barrier(2)

        class PausedLine(dict):
            def items(self):
                barrier.wait(timeout=3)
                return super().items()

        transcript = s.Transcript(SID)
        answer = page([PausedLine(line(1, "line 1"))], 1)
        got: list = []
        threads = [threading.Thread(target=lambda: got.append(transcript.apply(answer, 0)), daemon=True)
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([x["seq"] for lines in got for x in lines or []], [1])

    def test_a_consumers_bool_seq_is_no_line(self) -> None:  # Sol's surviving guard E05
        transcript = s.Transcript(SID)
        self.assertEqual(transcript.apply(page([{"seq": True, "event_type": "engine.output", "text": "x"}], 1)), [])
        self.assertIsNone(transcript.last_line)


class SentencesTest(unittest.TestCase):  # F2, F3, F7, F8, F9
    def test_the_sentences_say_what_the_code_does(self) -> None:
        readme = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("line 206 there; line 204 in this tree", readme)  # F7
        self.assertIn("the `events` package is not in this tree yet", readme)
        self.assertIn("the caller asks `broken`, rebuilds the view (`restart()`) and says so, and never iterates None",
                      readme)  # F8
        self.assertIn("| Case | What the user sees | Test |", readme)  # F9
        for name in ("CursorAheadTest", "CursorBehindTest", "EpochChangedTest", "SameEpochTruncationTest",
                     "SameEpochRewriteTest", "EmptyStreamTest", "BurstTest", "RetentionTest", "GapLinesTest",
                     "BudgetTest", "RebuildReadTest", "TwoCallersTest"):
            self.assertIn(f"`{name}`", readme.replace("r6 `", "`"))
        doc = " ".join(s.Transcript.__doc__.split())
        self.assertIn("a caller must not iterate None", doc)  # F8
        self.assertIn("two threads may drive one transcript", doc)  # F2
        self.assertIn("the marker says that the command reads the log again",
                      " ".join(TuiApp._rebuild.__doc__.split()))  # F3


if __name__ == "__main__":
    unittest.main()
